"""Event-driven ONGEKI NET / ScoreLog / image-generator foundation."""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from life_inbox.rhythm_rating import Game, RatingSnapshot

from ..rhythm_images import RatingImageVerificationError, png_dimensions
from .common import BrowserEvidence, EvidenceKind, load_runtime_source, safe_browser_url
from .maimai import MaimaiDataSchemaError, parse_maimai_data

_ONGEKI_TIME_ZONE = ZoneInfo("Asia/Tokyo")


def _normalize_ongeki_updated_at(value: object) -> object:
    """Interpret ScoreLog's timezone-less chart dates as Japan local time."""

    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_ONGEKI_TIME_ZONE)
    return parsed.astimezone(UTC).isoformat()


class OngekiState(StrEnum):
    """Observable ONGEKI collection states and failure outcomes."""

    INITIAL = "initial"
    OFFICIAL_NET = "official_net"
    SCORE_LOG_REFRESH_REQUIRED = "score_log_refresh_required"
    SCORE_LOG_REFRESHED = "score_log_refreshed"
    ADMIN_CONFIRMATION_REQUIRED = "admin_confirmation_required"
    GENERATOR_READY = "generator_ready"
    GENERATOR = "generator"
    USER_ID_REQUIRED = "user_id_required"
    IMAGE_WAITING = "image_waiting"
    IMAGE_SUCCESS = "image_success"
    JACKET_IMAGES_NOT_READY = "jacket_images_not_ready"
    USER_NOT_FOUND = "user_not_found"
    PRIVATE_MODE = "private_mode"
    PREMIUM_MISSING = "premium_missing"
    PROVIDER_NETWORK_OUTAGE = "provider_network_outage"
    IMAGE_MISSING = "image_missing"
    DOM_MISMATCH = "dom_mismatch"
    BOOKMARKLET_LOAD_FAILURE = "bookmarklet_load_failure"
    TIMEOUT = "timeout"
    SCORE_LOG_DATA_MISSING = "score_log_data_missing"
    DATA_MISSING = "score_log_data_missing"


ONGEKICollectionState = OngekiState


_BLOCKING_STATES = frozenset(
    {
        OngekiState.USER_NOT_FOUND,
        OngekiState.PRIVATE_MODE,
        OngekiState.PREMIUM_MISSING,
        OngekiState.PROVIDER_NETWORK_OUTAGE,
        OngekiState.DOM_MISMATCH,
        OngekiState.BOOKMARKLET_LOAD_FAILURE,
        OngekiState.TIMEOUT,
        OngekiState.SCORE_LOG_DATA_MISSING,
        OngekiState.JACKET_IMAGES_NOT_READY,
    }
)


class OngekiFailureReason(StrEnum):
    """Stable ONGEKI failure classifications."""

    USER_NOT_FOUND = "user_not_found"
    PRIVATE_MODE = "private_mode"
    PREMIUM_MISSING = "premium_missing"
    PROVIDER_NETWORK_OUTAGE = "provider_network_outage"
    IMAGE_MISSING = "image_missing"
    DOM_MISMATCH = "dom_mismatch"
    BOOKMARKLET_LOAD_FAILURE = "bookmarklet_load_failure"
    MANUAL_CONFIRMATION_REQUIRED = "manual_confirmation_required"
    USER_ID_REQUIRED = "user_id_required"
    IMAGE_WAITING = "image_waiting"
    JACKET_IMAGES_NOT_READY = "jacket_images_not_ready"
    TIMEOUT = "timeout"
    STALE_IMAGE = "stale_image"
    PLAYWRIGHT_UNAVAILABLE = "playwright_unavailable"
    SCORE_LOG_DATA_MISSING = "score_log_data_missing"
    DATA_MISSING = "score_log_data_missing"


class OngekiEventType(StrEnum):
    """Explicit sequence and browser evidence events."""

    OFFICIAL_NET_OPENED = "official_net_opened"
    URL_OBSERVED = "url_observed"
    SCORE_LOG_REFRESH_STARTED = "score_log_refresh_started"
    SCORE_LOG_REFRESH_COMPLETED = "score_log_refresh_completed"
    MANUAL_SCORE_LOG_REFRESH_CONFIRMED = "manual_score_log_refresh_confirmed"
    SCORE_LOG_DATA_AVAILABLE = "score_log_data_available"
    SCORE_LOG_DATA_FAILED = "score_log_data_failed"
    ADMIN_CONFIRMATION = "admin_confirmation"
    GENERATOR_OPENED = "generator_opened"
    USER_MODE_SELECTED = "user_mode_selected"
    USER_NOT_FOUND = "user_not_found"
    PRIVATE_MODE = "private_mode"
    PREMIUM_MISSING = "premium_missing"
    PROVIDER_NETWORK_OUTAGE = "provider_network_outage"
    BOOKMARKLET_LOADED = "bookmarklet_loaded"
    BOOKMARKLET_LOAD_FAILED = "bookmarklet_load_failed"
    IMAGE_GENERATED = "image_generated"
    IMAGE_SAVED = "image_saved"
    IMAGE_WAITING = "image_waiting"
    IMAGE_MISSING = "image_missing"
    JACKET_IMAGES_NOT_READY = "jacket_images_not_ready"
    DOM_MISMATCH = "dom_mismatch"
    TIMEOUT = "timeout"

    SCORELOG_REFRESHED = "score_log_refresh_completed"
    CONFIRMED = "admin_confirmation"
    DATA_AVAILABLE = "score_log_data_available"
    DATA_MISSING = "score_log_data_failed"


@dataclass(frozen=True, slots=True)
class OngekiConfig:
    """Caller-owned ONGEKI URLs, session path, and optional safe selectors."""

    persistent_session_dir: str | Path
    bookmarklet_runtime_path: str | Path
    official_url: str = "https://ongeki-net.com/ongeki-mobile/home/"
    score_log_url: str = "https://ongeki-net.com/ongeki-mobile/home/"
    generator_url: str = ""
    user_id_selector: str | None = None
    mode_selector: str | None = None
    premium_required: bool = True

    def __post_init__(self) -> None:
        session = str(self.persistent_session_dir).strip()
        runtime = str(self.bookmarklet_runtime_path).strip()
        official = self.official_url.strip() if isinstance(self.official_url, str) else ""
        score_log = self.score_log_url.strip() if isinstance(self.score_log_url, str) else ""
        generator = self.generator_url.strip() if isinstance(self.generator_url, str) else ""
        if not session or not runtime or not official or not score_log:
            raise ValueError("ongeki paths and provider URLs are required")
        for field_name in ("user_id_selector", "mode_selector"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, value.strip() or None)
        if type(self.premium_required) is not bool:
            raise ValueError("premium_required must be a boolean")
        object.__setattr__(self, "persistent_session_dir", session)
        object.__setattr__(self, "bookmarklet_runtime_path", runtime)
        object.__setattr__(self, "official_url", official)
        object.__setattr__(self, "score_log_url", score_log)
        object.__setattr__(self, "generator_url", generator)

    @property
    def session_directory(self) -> str:
        return str(self.persistent_session_dir)

    @property
    def runtime_file(self) -> str:
        return str(self.bookmarklet_runtime_path)


class OngekiDataSchemaError(ValueError):
    """Raised when ScoreLog data cannot be normalized safely."""


def _ongeki_key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _ongeki_direct(mapping: object, aliases: tuple[str, ...]) -> object | None:
    if not isinstance(mapping, Mapping):
        return None
    wanted = {_ongeki_key(alias) for alias in aliases}
    for key, value in mapping.items():
        if _ongeki_key(key) in wanted:
            return value
    return None


def _is_timeout_error(error: object) -> bool:
    """Classify browser timeout exceptions without exposing their text."""

    return (
        isinstance(error, TimeoutError)
        or "timeout" in type(error).__name__.casefold()
        or "timed out" in str(error).casefold()
    )


def _jpeg_dimensions(payload: bytes) -> tuple[int, int]:
    """Validate the JPEG envelope needed by the live image runner."""

    if len(payload) < 4 or payload[:2] != b"\xff\xd8" or payload[-2:] != b"\xff\xd9":
        raise RatingImageVerificationError("rating image is not a complete JPEG")
    offset = 2
    dimensions: tuple[int, int] | None = None
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while offset < len(payload) - 2:
        if payload[offset] != 0xFF:
            raise RatingImageVerificationError("rating image has an invalid JPEG marker")
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            raise RatingImageVerificationError("rating image has a truncated JPEG marker")
        marker = payload[offset]
        offset += 1
        if marker == 0xD9:
            break
        if marker == 0xDA:
            if dimensions is None:
                raise RatingImageVerificationError("rating image has no JPEG dimensions")
            return dimensions
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(payload):
            raise RatingImageVerificationError("rating image has a truncated JPEG segment")
        length = struct.unpack(">H", payload[offset : offset + 2])[0]
        if length < 2 or offset + length > len(payload):
            raise RatingImageVerificationError("rating image has an invalid JPEG segment")
        if marker in sof_markers:
            if length < 7:
                raise RatingImageVerificationError("rating image has an invalid JPEG frame")
            height, width = struct.unpack(">HH", payload[offset + 3 : offset + 7])
            if width < 1 or height < 1:
                raise RatingImageVerificationError("rating image dimensions are invalid")
            dimensions = (width, height)
        offset += length
    if dimensions is None:
        raise RatingImageVerificationError("rating image has no JPEG dimensions")
    return dimensions


def _image_dimensions(payload: bytes) -> tuple[int, int] | None:
    """Accept the complete PNG/JPEG formats emitted by live image runners."""

    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return png_dimensions(payload, require_complete=True)
    if payload.startswith(b"\xff\xd8"):
        return _jpeg_dimensions(payload)
    raise RatingImageVerificationError("rating image format is unsupported")


def _observed_page_url(page: object, requested_url: str) -> str:
    """Return the browser's final URL, falling back to the requested URL."""

    try:
        observed_url = getattr(page, "url", None)
    except Exception:
        observed_url = None
    return (
        observed_url.strip()
        if isinstance(observed_url, str) and observed_url.strip()
        else requested_url
    )


def _optional_url_matches(observed_url: str | None, expected_url: str) -> bool:
    """Validate an optional navigation URL without requiring synthetic events to provide one."""

    if observed_url is None:
        return True
    observed = safe_browser_url(observed_url)
    expected = safe_browser_url(expected_url)
    return (
        observed is not None
        and expected is not None
        and observed.rstrip("/") == expected.rstrip("/")
    )


def parse_ongeki_data(
    payload: object,
    *,
    source: str = "ongeki_scorelog",
    collected_at: datetime | None = None,
    secrets: tuple[str, ...] = (),
) -> RatingSnapshot:
    """Normalize a ScoreLog payload into provider-independent ONGEKI rows.

    ScoreLog is a browser-authenticated surface, so this function is pure and
    accepts only a payload already emitted by an adapter.  Its row schema is
    intentionally shared with the maimai normalizer because both browser
    surfaces expose the same rating/chart metrics, while the resulting game
    identity is always ONGEKI.
    """

    if isinstance(payload, RatingSnapshot):
        if payload.game is not Game.ONGEKI:
            raise OngekiDataSchemaError("snapshot game is not ongeki")
        return payload
    source_payload = payload
    if isinstance(payload, Mapping):
        profile = _ongeki_direct(payload, ("profile",))
        rating_frames = _ongeki_direct(payload, ("rating", "rating_frames"))
        if isinstance(profile, Mapping) and isinstance(rating_frames, Mapping):
            normalized_frames: dict[str, list[dict[str, object]]] = {}
            for frame_name in ("best", "new", "pscore"):
                rows = _ongeki_direct(rating_frames, (frame_name,))
                if not isinstance(rows, list):
                    continue
                normalized_rows: list[dict[str, object]] = []
                for row in rows:
                    if not isinstance(row, Mapping):
                        continue
                    lamps = _ongeki_direct(row, ("lamps",))
                    full_combo = (
                        bool(_ongeki_direct(lamps, ("is_fullcombo", "full_combo")))
                        if isinstance(lamps, Mapping)
                        else False
                    )
                    all_break = (
                        bool(_ongeki_direct(lamps, ("is_allbreak", "all_break")))
                        if isinstance(lamps, Mapping)
                        else False
                    )
                    full_bell = (
                        bool(_ongeki_direct(lamps, ("is_fullbell", "full_bell")))
                        if isinstance(lamps, Mapping)
                        else False
                    )
                    score = _ongeki_direct(row, ("score", "technical_score"))
                    song_rating = _ongeki_direct(
                        row,
                        ("p_rating",) if frame_name == "pscore" else ("rating",),
                    )
                    normalized_rows.append(
                        {
                            "song_id": _ongeki_direct(row, ("id", "song_id")),
                            "title": _ongeki_direct(row, ("title",)),
                            "difficulty": _ongeki_direct(row, ("diff", "difficulty")),
                            "chart_type": "standard",
                            "level": _ongeki_direct(row, ("level",)),
                            "chart_constant": _ongeki_direct(row, ("const", "constant")),
                            "score": score,
                            "technical_score": score,
                            "p_score": _ongeki_direct(row, ("p_score", "pscore")),
                            "song_rating": song_rating,
                            "full_combo": "full_combo" if full_combo else None,
                            "all_break": "all_break" if all_break else None,
                            "full_bell": "full_bell" if full_bell else None,
                            "clear": _ongeki_direct(row, ("rank",)),
                            "genre": _ongeki_direct(row, ("genre",)),
                            "updated_at": _normalize_ongeki_updated_at(
                                _ongeki_direct(row, ("update", "updated_at"))
                            ),
                        }
                    )
                if normalized_rows:
                    normalized_frames[frame_name] = normalized_rows
            source_payload = {
                "player_name": _ongeki_direct(profile, ("name", "player_name")),
                "rating": _ongeki_direct(profile, ("rating", "player_rating")),
                "frames": normalized_frames,
            }
        score_log = _ongeki_direct(payload, ("score_log", "scoreLog"))
        if score_log is not None and source_payload is payload:
            if isinstance(score_log, dict):
                inner = dict(score_log)
                for aliases in (
                    ("player_name", "playerName", "display_name", "username"),
                    ("rating", "overall_rating", "total_rating", "player_rating"),
                ):
                    if _ongeki_direct(inner, aliases) is None:
                        value = _ongeki_direct(payload, aliases)
                        if value is not None:
                            inner[aliases[0]] = value
                source_payload = inner
            elif isinstance(score_log, list):
                source_payload = {
                    "player_name": _ongeki_direct(
                        payload, ("player_name", "playerName", "display_name", "username")
                    ),
                    "rating": _ongeki_direct(
                        payload, ("rating", "overall_rating", "total_rating", "player_rating")
                    ),
                    "entries": score_log,
                }
    try:
        maimai_snapshot = parse_maimai_data(
            source_payload,
            source=source,
            collected_at=collected_at,
            secrets=secrets,
        )
    except (MaimaiDataSchemaError, TypeError, ValueError) as exc:
        raise OngekiDataSchemaError("ScoreLog data has an unsupported schema") from exc
    frame_ranks: dict[str, int] = {}
    entries = []
    for entry in maimai_snapshot.entries:
        frame = entry.frame_membership or "other"
        frame_ranks[frame] = frame_ranks.get(frame, 0) + 1
        entries.append(
            replace(
                entry,
                game=Game.ONGEKI,
                rank=frame_ranks[frame],
            )
        )
    return RatingSnapshot(
        game=Game.ONGEKI,
        entries=tuple(entries),
        rating=maimai_snapshot.rating,
        source=source,
        player_name=maimai_snapshot.player_name,
        source_updated_at=maimai_snapshot.source_updated_at,
        raw_data=maimai_snapshot.raw_data,
        collected_at=maimai_snapshot.collected_at,
    )


parse_ongeki_score_log = parse_ongeki_data
parse_ongeki_snapshot = parse_ongeki_data


class OngekiParser:
    """Class-style facade for the pure ScoreLog normalizer."""

    parse = staticmethod(parse_ongeki_data)
    parse_snapshot = staticmethod(parse_ongeki_data)
    parse_score_log = staticmethod(parse_ongeki_data)


@dataclass(frozen=True, slots=True)
class OngekiBrowserEvent:
    """One detached event from ONGEKI NET or its generator."""

    event_type: OngekiEventType | str
    completed: bool = False
    url: str | None = None
    path: str | Path | None = None
    generation_id: str | None = None
    user_id: str | None = None
    mode: str | None = None
    explicit_confirmation: bool = False
    payload: object | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            event_type = (
                self.event_type
                if isinstance(self.event_type, OngekiEventType)
                else OngekiEventType(str(self.event_type).strip().casefold())
            )
        except ValueError as exc:
            raise ValueError("unsupported ONGEKI browser event") from exc
        if type(self.completed) is not bool or type(self.explicit_confirmation) is not bool:
            raise ValueError("event completion and confirmation must be booleans")
        object.__setattr__(self, "event_type", event_type)
        if self.url is not None:
            object.__setattr__(self, "url", safe_browser_url(self.url))
        if self.path is not None:
            object.__setattr__(self, "path", str(self.path))

    @classmethod
    def official_net_opened(cls, url: str) -> OngekiBrowserEvent:
        return cls(OngekiEventType.OFFICIAL_NET_OPENED, completed=True, url=url)

    @classmethod
    def score_log_refresh_completed(
        cls,
        *,
        explicit_confirmation: bool = False,
        payload: object | None = None,
        url: str | None = None,
    ) -> OngekiBrowserEvent:
        return cls(
            OngekiEventType.SCORE_LOG_REFRESH_COMPLETED,
            completed=True,
            url=url,
            explicit_confirmation=explicit_confirmation,
            payload=payload,
        )

    @classmethod
    def score_log_data(cls, payload: object, *, completed: bool = True) -> OngekiBrowserEvent:
        return cls(
            OngekiEventType.SCORE_LOG_DATA_AVAILABLE,
            completed=completed,
            payload=payload,
        )

    data_available = score_log_data

    @classmethod
    def manual_score_log_refresh_confirmed(cls) -> OngekiBrowserEvent:
        """Record a human-confirmed refresh performed in an external browser.

        This event intentionally does not claim that the collector observed an
        authenticated official-NET page.  It is the explicit handoff used
        when a user ran the official ScoreLog bookmarklet themselves.
        """

        return cls(
            OngekiEventType.MANUAL_SCORE_LOG_REFRESH_CONFIRMED,
            completed=True,
            explicit_confirmation=True,
        )

    @classmethod
    def admin_confirmation(cls) -> OngekiBrowserEvent:
        return cls(
            OngekiEventType.ADMIN_CONFIRMATION,
            completed=True,
            explicit_confirmation=True,
        )

    @classmethod
    def generator_opened(cls, url: str | None = None) -> OngekiBrowserEvent:
        return cls(OngekiEventType.GENERATOR_OPENED, completed=True, url=url)

    @classmethod
    def user_mode_selected(cls, user_id: str, mode: str) -> OngekiBrowserEvent:
        return cls(
            OngekiEventType.USER_MODE_SELECTED,
            completed=True,
            user_id=user_id,
            mode=mode,
        )

    @classmethod
    def image_generated(cls, *, generation_id: str | None = None) -> OngekiBrowserEvent:
        return cls(
            OngekiEventType.IMAGE_GENERATED,
            completed=True,
            generation_id=generation_id,
        )

    @classmethod
    def image_saved(
        cls,
        path: str | Path | None = None,
        *,
        generation_id: str | None = None,
        payload: object | None = None,
    ) -> OngekiBrowserEvent:
        return cls(
            OngekiEventType.IMAGE_SAVED,
            completed=True,
            path=path,
            generation_id=generation_id,
            payload=payload,
        )

    @classmethod
    def jacket_images_not_ready(cls) -> OngekiBrowserEvent:
        """Report that required non-copyright jacket images did not load."""

        return cls(OngekiEventType.JACKET_IMAGES_NOT_READY, completed=True)


class OngekiBrowserAdapter(Protocol):
    """Optional adapter seam; each operation returns an explicit event."""

    def open_official_net(self, url: str) -> OngekiBrowserEvent:
        """Open official ONGEKI NET."""

    def refresh_score_log(self, url: str) -> OngekiBrowserEvent:
        """Trigger ScoreLog refresh; completion remains a separate event."""

    def capture_score_log_data(self) -> OngekiBrowserEvent:
        """Return an explicit ScoreLog data event after refresh completion."""

    def open_generator(self, url: str) -> OngekiBrowserEvent:
        """Open the generator only after collector sequence gates pass."""

    def select_user_mode(self, user_id: str, mode: str) -> OngekiBrowserEvent:
        """Select explicitly supplied user ID and mode."""

    def execute_bookmarklet(self, source: str) -> OngekiBrowserEvent:
        """Execute caller-supplied runtime source and report load status."""


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """A no-sleep retry classification and its proposed delay."""

    retry: bool
    transient: bool
    attempt: int
    delay_seconds: float
    reason: str


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Pure retry schedule; callers decide whether/when to execute it."""

    max_attempts: int = 3
    initial_delay_seconds: float = 1.0
    multiplier: float = 2.0
    max_delay_seconds: float = 30.0

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if (
            not isinstance(self.initial_delay_seconds, (int, float))
            or not math.isfinite(self.initial_delay_seconds)
            or self.initial_delay_seconds < 0
        ):
            raise ValueError("initial delay must be finite and non-negative")
        if (
            not isinstance(self.multiplier, (int, float))
            or not math.isfinite(self.multiplier)
            or self.multiplier < 1
        ):
            raise ValueError("multiplier must be finite and at least one")
        if (
            not isinstance(self.max_delay_seconds, (int, float))
            or not math.isfinite(self.max_delay_seconds)
            or self.max_delay_seconds < 0
        ):
            raise ValueError("max delay must be finite and non-negative")

    def schedule(self) -> tuple[float, ...]:
        return tuple(
            min(
                float(self.max_delay_seconds),
                float(self.initial_delay_seconds) * float(self.multiplier) ** index,
            )
            for index in range(max(0, self.max_attempts - 1))
        )

    def classify(self, error: object, *, attempt: int = 1) -> RetryDecision:
        try:
            text = str(error).casefold()
        except Exception:
            text = ""
        if not text:
            text = type(error).__name__.casefold()
        transient = any(
            marker in text
            for marker in (
                "timeout",
                "timed out",
                "connection",
                "temporar",
                "network",
                "429",
                "502",
                "503",
                "504",
            )
        )
        valid_attempt = max(1, int(attempt))
        retry = transient and valid_attempt < self.max_attempts
        delays = self.schedule()
        delay = delays[valid_attempt - 1] if retry and valid_attempt - 1 < len(delays) else 0.0
        return RetryDecision(
            retry,
            transient,
            valid_attempt,
            delay,
            "transient" if transient else "permanent",
        )


@dataclass(frozen=True, slots=True)
class OngekiCollectionResult:
    """Detached result with a separate last-successful-image reference."""

    state: OngekiState
    success: bool
    failure_reason: OngekiFailureReason | None = None
    events: tuple[OngekiBrowserEvent, ...] = ()
    evidence: tuple[BrowserEvidence, ...] = ()
    image_path: str | None = None
    image_sha256: str | None = None
    image_bytes: bytes | None = field(default=None, repr=False)
    snapshot: RatingSnapshot | None = field(default=None, repr=False)
    score_log_data_confirmed: bool = False
    data_failure_reason: OngekiFailureReason | None = None
    last_successful_image_path: str | None = None
    last_successful_image_sha256: str | None = None
    stale_image_rejected: bool = False
    message: str | None = None

    @property
    def reason(self) -> OngekiFailureReason | None:
        return self.failure_reason

    @property
    def waiting_for_manual_action(self) -> bool:
        return (
            self.failure_reason is OngekiFailureReason.MANUAL_CONFIRMATION_REQUIRED
            or self.state
            in {
            OngekiState.SCORE_LOG_REFRESH_REQUIRED,
            OngekiState.ADMIN_CONFIRMATION_REQUIRED,
            OngekiState.USER_ID_REQUIRED,
            OngekiState.SCORE_LOG_DATA_MISSING,
            }
        )

    @property
    def waitingForManualAction(self) -> bool:  # noqa: N802 - external event vocabulary
        return self.waiting_for_manual_action

    @property
    def complete(self) -> bool:
        """Whether the confirmed ScoreLog and generator image are both new."""

        return (
            self.success
            and self.snapshot is not None
            and self.score_log_data_confirmed
            and self.image_sha256 is not None
            and (self.image_path is not None or self.image_bytes is not None)
            and self.data_failure_reason is None
        )

    @property
    def ready_for_persistence(self) -> bool:
        return self.complete and self.image_path is not None


def _state_reason(state: OngekiState) -> OngekiFailureReason | None:
    return {
        OngekiState.USER_NOT_FOUND: OngekiFailureReason.USER_NOT_FOUND,
        OngekiState.PRIVATE_MODE: OngekiFailureReason.PRIVATE_MODE,
        OngekiState.PREMIUM_MISSING: OngekiFailureReason.PREMIUM_MISSING,
        OngekiState.PROVIDER_NETWORK_OUTAGE: OngekiFailureReason.PROVIDER_NETWORK_OUTAGE,
        OngekiState.IMAGE_MISSING: OngekiFailureReason.IMAGE_MISSING,
        OngekiState.DOM_MISMATCH: OngekiFailureReason.DOM_MISMATCH,
        OngekiState.BOOKMARKLET_LOAD_FAILURE: OngekiFailureReason.BOOKMARKLET_LOAD_FAILURE,
        OngekiState.USER_ID_REQUIRED: OngekiFailureReason.USER_ID_REQUIRED,
        OngekiState.IMAGE_WAITING: OngekiFailureReason.IMAGE_WAITING,
        OngekiState.JACKET_IMAGES_NOT_READY: OngekiFailureReason.JACKET_IMAGES_NOT_READY,
        OngekiState.TIMEOUT: OngekiFailureReason.TIMEOUT,
        OngekiState.SCORE_LOG_DATA_MISSING: OngekiFailureReason.SCORE_LOG_DATA_MISSING,
    }.get(state)


class OngekiCollector:
    """Explicit ONGEKI sequence reducer with stale-image protection."""

    game = Game.ONGEKI

    def __init__(
        self,
        config: OngekiConfig,
        *,
        adapter: OngekiBrowserAdapter | None = None,
        last_successful_image: str | Path | None = None,
        last_successful_image_path: str | Path | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        if last_successful_image is not None and last_successful_image_path is not None:
            raise ValueError("provide only one last successful image path")
        if last_successful_image is None:
            last_successful_image = last_successful_image_path
        self.config = config
        self.adapter = adapter
        self.retry_policy = retry_policy or RetryPolicy()
        self.state = OngekiState.INITIAL
        self._events: list[OngekiBrowserEvent] = []
        self._evidence: list[BrowserEvidence] = []
        self._score_log_refreshed = False
        self._manual_score_log_refresh_confirmed = False
        self._score_log_data: RatingSnapshot | None = None
        self._data_failure_reason: OngekiFailureReason | None = None
        self._official_opened = False
        self._admin_confirmed = False
        self._generator_opened = False
        self._user_mode_selected = False
        self._image_generation_id: str | None = None
        self._image_generated = False
        self._image_path: str | None = None
        self._image_sha256: str | None = None
        self._image_bytes: bytes | None = None
        self._stale_image_rejected = False
        self._failure_reason_override: OngekiFailureReason | None = None
        self._last_successful_image_path = (
            str(last_successful_image) if last_successful_image is not None else None
        )
        self._last_successful_image_sha256 = self._hash_existing(last_successful_image)
        self._runtime_source: str | None = None
        self._blocked = False

    @staticmethod
    def _hash_existing(path: str | Path | None) -> str | None:
        if path is None:
            return None
        try:
            candidate = Path(path)
            if candidate.is_file():
                return hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            return None
        return None

    @property
    def events(self) -> tuple[OngekiBrowserEvent, ...]:
        return tuple(self._events)

    @property
    def evidence(self) -> tuple[BrowserEvidence, ...]:
        return tuple(self._evidence)

    @property
    def score_log_refresh_completed(self) -> bool:
        return self._score_log_refreshed

    @property
    def generator_allowed(self) -> bool:
        source_confirmed = (
            self._official_opened or self._manual_score_log_refresh_confirmed
        )
        return source_confirmed and self._score_log_refreshed and self._admin_confirmed

    def _record_evidence(self, event: OngekiBrowserEvent) -> None:
        kind_map = {
            OngekiEventType.URL_OBSERVED: EvidenceKind.URL,
            OngekiEventType.OFFICIAL_NET_OPENED: EvidenceKind.URL,
            OngekiEventType.SCORE_LOG_REFRESH_COMPLETED: EvidenceKind.DATA,
            OngekiEventType.MANUAL_SCORE_LOG_REFRESH_CONFIRMED: EvidenceKind.DATA,
            OngekiEventType.SCORE_LOG_DATA_AVAILABLE: EvidenceKind.DATA,
            OngekiEventType.SCORE_LOG_DATA_FAILED: EvidenceKind.DATA,
            OngekiEventType.IMAGE_GENERATED: EvidenceKind.IMAGE,
            OngekiEventType.IMAGE_SAVED: EvidenceKind.IMAGE,
            OngekiEventType.JACKET_IMAGES_NOT_READY: EvidenceKind.IMAGE,
        }
        kind = kind_map.get(event.event_type)
        if kind is not None:
            self._evidence.append(
                BrowserEvidence(
                    kind=kind,
                    completed=event.completed,
                    url=event.url,
                    path=event.path,
                    generation_id=event.generation_id,
                )
            )

    def _failure(self, state: OngekiState) -> None:
        if self.state is OngekiState.IMAGE_SUCCESS:
            return
        self.state = state
        if state in _BLOCKING_STATES:
            self._blocked = True

    def _score_log_failure(self) -> None:
        """Invalidate the current candidate without replacing prior success."""

        self._data_failure_reason = OngekiFailureReason.SCORE_LOG_DATA_MISSING
        if self.state is OngekiState.IMAGE_SUCCESS:
            self.state = OngekiState.SCORE_LOG_DATA_MISSING
            self._blocked = True
        else:
            self._failure(OngekiState.SCORE_LOG_DATA_MISSING)

    def _record_successful_image_baseline(self) -> None:
        """Promote a candidate only after data and a durable path both exist."""

        if (
            self._score_log_data is None
            or self._data_failure_reason is not None
            or self.state is not OngekiState.IMAGE_SUCCESS
            or self._image_path is None
            or self._image_sha256 is None
        ):
            return
        self._last_successful_image_path = self._image_path
        self._last_successful_image_sha256 = self._image_sha256

    def _invalidate_generation(self) -> None:
        self._image_generated = False
        self._image_generation_id = None

    def _mark_score_log_data(self, event: OngekiBrowserEvent) -> None:
        source_confirmed = (
            self._official_opened or self._manual_score_log_refresh_confirmed
        )
        if not event.completed or not source_confirmed or not self._score_log_refreshed:
            self._score_log_failure()
            return
        try:
            self._score_log_data = parse_ongeki_data(event.payload)
        except (OngekiDataSchemaError, TypeError, ValueError):
            self._score_log_failure()
            return
        self._data_failure_reason = None
        self._record_successful_image_baseline()

    def _save_image_event(self, event: OngekiBrowserEvent) -> None:
        if self._blocked:
            return
        if (
            not event.completed
            or not self._image_generated
            or not event.generation_id
            or event.generation_id != self._image_generation_id
            or (
                event.path is None
                and not isinstance(event.payload, (bytes, bytearray, memoryview))
            )
        ):
            self._invalidate_generation()
            self._failure(OngekiState.IMAGE_MISSING)
            return
        path = Path(event.path) if event.path is not None else None
        if path is not None:
            try:
                if not path.is_file():
                    self._invalidate_generation()
                    self._failure(OngekiState.IMAGE_MISSING)
                    return
                payload = path.read_bytes()
            except OSError:
                self._invalidate_generation()
                self._failure(OngekiState.IMAGE_MISSING)
                return
        else:
            payload = bytes(event.payload)
        try:
            _image_dimensions(payload)
        except RatingImageVerificationError:
            self._invalidate_generation()
            self._failure(OngekiState.IMAGE_MISSING)
            return
        image_sha256 = hashlib.sha256(payload).hexdigest()
        if (
            self._last_successful_image_path is not None
            and str(path) == self._last_successful_image_path
            and self._last_successful_image_sha256 is not None
            and image_sha256 == self._last_successful_image_sha256
        ) or (
            self._last_successful_image_sha256 is not None
            and image_sha256 == self._last_successful_image_sha256
        ):
            self._stale_image_rejected = True
            self._failure_reason_override = OngekiFailureReason.STALE_IMAGE
            self._invalidate_generation()
            self._failure(OngekiState.IMAGE_MISSING)
            return
        self._image_path = str(path) if path is not None else None
        self._image_sha256 = image_sha256
        self._image_bytes = payload
        self._failure_reason_override = None
        self.state = OngekiState.IMAGE_SUCCESS
        self._record_successful_image_baseline()

    def observe(self, event: OngekiBrowserEvent) -> OngekiCollectionResult:
        """Apply one event; no event means no progress toward success."""

        self._events.append(event)
        self._record_evidence(event)
        if self._blocked:
            return self.result()
        event_type = event.event_type
        if event_type in {OngekiEventType.OFFICIAL_NET_OPENED, OngekiEventType.URL_OBSERVED}:
            if not event.completed or event.url is None:
                self._failure(OngekiState.DOM_MISMATCH)
            elif (
                safe_browser_url(event.url) is None
                or safe_browser_url(self.config.official_url) is None
                or safe_browser_url(event.url).rstrip("/")
                != safe_browser_url(self.config.official_url).rstrip("/")
            ):
                self._failure(OngekiState.DOM_MISMATCH)
            else:
                self._official_opened = True
                self.state = OngekiState.SCORE_LOG_REFRESH_REQUIRED
        elif event_type is OngekiEventType.SCORE_LOG_REFRESH_STARTED:
            if not self._official_opened:
                self.state = OngekiState.SCORE_LOG_REFRESH_REQUIRED
            elif not _optional_url_matches(event.url, self.config.score_log_url):
                self._failure(OngekiState.DOM_MISMATCH)
            elif self.state is OngekiState.INITIAL:
                self.state = OngekiState.SCORE_LOG_REFRESH_REQUIRED
            elif not self._score_log_refreshed:
                self.state = OngekiState.SCORE_LOG_REFRESH_REQUIRED
        elif event_type is OngekiEventType.SCORE_LOG_REFRESH_COMPLETED:
            if not self._official_opened or not event.completed:
                self.state = OngekiState.SCORE_LOG_REFRESH_REQUIRED
            elif not _optional_url_matches(event.url, self.config.score_log_url):
                self._failure(OngekiState.DOM_MISMATCH)
            else:
                self._score_log_refreshed = True
                self.state = (
                    OngekiState.GENERATOR_READY
                    if event.explicit_confirmation
                    else OngekiState.ADMIN_CONFIRMATION_REQUIRED
                )
                if event.explicit_confirmation:
                    self._admin_confirmed = True
                if event.payload is not None:
                    self._mark_score_log_data(
                        OngekiBrowserEvent.score_log_data(event.payload)
                    )
        elif event_type is OngekiEventType.MANUAL_SCORE_LOG_REFRESH_CONFIRMED:
            if event.completed and event.explicit_confirmation:
                self._manual_score_log_refresh_confirmed = True
                self._score_log_refreshed = True
                self._admin_confirmed = True
                self.state = OngekiState.GENERATOR_READY
            else:
                self.state = OngekiState.ADMIN_CONFIRMATION_REQUIRED
        elif event_type is OngekiEventType.SCORE_LOG_DATA_AVAILABLE:
            self._mark_score_log_data(event)
        elif event_type is OngekiEventType.SCORE_LOG_DATA_FAILED:
            self._score_log_failure()
        elif event_type is OngekiEventType.ADMIN_CONFIRMATION:
            if (
                event.completed
                and event.explicit_confirmation
                and self._official_opened
                and self._score_log_refreshed
            ):
                self._admin_confirmed = True
                self.state = OngekiState.GENERATOR_READY
            else:
                self.state = OngekiState.ADMIN_CONFIRMATION_REQUIRED
        elif event_type is OngekiEventType.GENERATOR_OPENED:
            if not self.generator_allowed or not event.completed:
                self.state = OngekiState.ADMIN_CONFIRMATION_REQUIRED
            elif not _optional_url_matches(event.url, self.config.generator_url):
                self._failure(OngekiState.DOM_MISMATCH)
            else:
                self._generator_opened = True
                self.state = OngekiState.GENERATOR
        elif event_type is OngekiEventType.USER_MODE_SELECTED:
            if (
                not self._generator_opened
                or not event.completed
                or not event.user_id
                or not event.mode
            ):
                self._failure(OngekiState.USER_ID_REQUIRED)
            else:
                self._user_mode_selected = True
                self.state = OngekiState.IMAGE_WAITING
        elif event_type is OngekiEventType.USER_NOT_FOUND:
            self._failure(OngekiState.USER_NOT_FOUND)
        elif event_type is OngekiEventType.PRIVATE_MODE:
            self._failure(OngekiState.PRIVATE_MODE)
        elif event_type is OngekiEventType.PREMIUM_MISSING:
            self._failure(OngekiState.PREMIUM_MISSING)
        elif event_type is OngekiEventType.PROVIDER_NETWORK_OUTAGE:
            self._failure(OngekiState.PROVIDER_NETWORK_OUTAGE)
        elif event_type is OngekiEventType.BOOKMARKLET_LOAD_FAILED:
            self._failure(OngekiState.BOOKMARKLET_LOAD_FAILURE)
        elif event_type is OngekiEventType.BOOKMARKLET_LOADED:
            if not event.completed:
                self._failure(OngekiState.BOOKMARKLET_LOAD_FAILURE)
        elif event_type is OngekiEventType.IMAGE_GENERATED:
            if (
                not event.completed
                or not self._user_mode_selected
                or not event.generation_id
            ):
                self._invalidate_generation()
                self._failure(OngekiState.IMAGE_MISSING)
            else:
                self._image_generated = True
                self._image_generation_id = event.generation_id
                self._failure_reason_override = None
                self.state = OngekiState.IMAGE_WAITING
        elif event_type is OngekiEventType.IMAGE_SAVED:
            self._save_image_event(event)
        elif event_type is OngekiEventType.JACKET_IMAGES_NOT_READY:
            self._failure(OngekiState.JACKET_IMAGES_NOT_READY)
        elif event_type is OngekiEventType.IMAGE_WAITING:
            self.state = OngekiState.IMAGE_WAITING
        elif event_type is OngekiEventType.IMAGE_MISSING:
            self._failure(OngekiState.IMAGE_MISSING)
        elif event_type is OngekiEventType.DOM_MISMATCH:
            self._failure(OngekiState.DOM_MISMATCH)
        elif event_type is OngekiEventType.TIMEOUT:
            self._failure(OngekiState.TIMEOUT)
        return self.result()

    handle_event = observe
    feed_event = observe

    def load_bookmarklet(self) -> OngekiCollectionResult:
        loaded = load_runtime_source(self.config.bookmarklet_runtime_path)
        if not loaded.success or loaded.source is None:
            self._events.append(OngekiBrowserEvent(OngekiEventType.BOOKMARKLET_LOAD_FAILED))
            self._failure(OngekiState.BOOKMARKLET_LOAD_FAILURE)
            return self.result()
        self._runtime_source = loaded.source
        if self.adapter is None:
            return self.result()
        try:
            method = getattr(self.adapter, "execute_bookmarklet")
            event = method(self._runtime_source)
        except Exception:
            event = OngekiBrowserEvent(OngekiEventType.BOOKMARKLET_LOAD_FAILED)
        return self.observe(event)

    def open_official_net(self) -> OngekiCollectionResult:
        if self.adapter is None:
            return self.observe(OngekiBrowserEvent.official_net_opened(self.config.official_url))
        try:
            event = self.adapter.open_official_net(self.config.official_url)
        except Exception:
            event = OngekiBrowserEvent(OngekiEventType.PROVIDER_NETWORK_OUTAGE)
        return self.observe(event)

    def refresh_score_log(self) -> OngekiCollectionResult:
        if self.state not in {OngekiState.SCORE_LOG_REFRESH_REQUIRED, OngekiState.OFFICIAL_NET}:
            return self.result()
        if self.adapter is None:
            return self.observe(
                OngekiBrowserEvent(
                    OngekiEventType.SCORE_LOG_REFRESH_STARTED,
                    completed=True,
                )
            )
        try:
            event = self.adapter.refresh_score_log(self.config.score_log_url)
        except Exception:
            event = OngekiBrowserEvent(OngekiEventType.PROVIDER_NETWORK_OUTAGE)
        return self.observe(event)

    def capture_score_log_data(self) -> OngekiCollectionResult:
        """Request data only through an adapter's explicit capture seam."""

        method = getattr(self.adapter, "capture_score_log_data", None)
        if not callable(method):
            return self.observe(
                OngekiBrowserEvent(OngekiEventType.SCORE_LOG_DATA_FAILED, completed=False)
            )
        try:
            event = method()
        except Exception:
            event = OngekiBrowserEvent(OngekiEventType.SCORE_LOG_DATA_FAILED)
        return self.observe(event)

    def confirm_administrator(self) -> OngekiCollectionResult:
        return self.observe(OngekiBrowserEvent.admin_confirmation())

    def open_generator(self) -> OngekiCollectionResult:
        if not self.generator_allowed:
            return self.result(failure_reason=OngekiFailureReason.MANUAL_CONFIRMATION_REQUIRED)
        if not self.config.generator_url:
            return self.result(failure_reason=OngekiFailureReason.DOM_MISMATCH)
        if self.adapter is None:
            return self.observe(OngekiBrowserEvent.generator_opened())
        try:
            event = self.adapter.open_generator(self.config.generator_url)
        except Exception:
            event = OngekiBrowserEvent(OngekiEventType.PROVIDER_NETWORK_OUTAGE)
        return self.observe(event)

    def select_user_mode(self, user_id: str, mode: str) -> OngekiCollectionResult:
        if not self._generator_opened:
            return self.result(failure_reason=OngekiFailureReason.USER_ID_REQUIRED)
        if self.adapter is None:
            return self.observe(OngekiBrowserEvent.user_mode_selected(user_id, mode))
        try:
            event = self.adapter.select_user_mode(user_id, mode)
        except Exception:
            event = OngekiBrowserEvent(OngekiEventType.DOM_MISMATCH)
        return self.observe(event)

    def process(self, events: Iterable[OngekiBrowserEvent]) -> OngekiCollectionResult:
        for event in events:
            self.observe(event)
        return self.result()

    def collect(
        self,
        events: Iterable[OngekiBrowserEvent] = (),
    ) -> OngekiCollectionResult:
        return self.process(events)

    def result(
        self,
        *,
        failure_reason: OngekiFailureReason | None = None,
    ) -> OngekiCollectionResult:
        reason = (
            failure_reason
            or self._failure_reason_override
            or _state_reason(self.state)
            or self._data_failure_reason
        )
        return OngekiCollectionResult(
            state=self.state,
            success=self.state is OngekiState.IMAGE_SUCCESS,
            failure_reason=reason,
            events=tuple(self._events),
            evidence=tuple(self._evidence),
            image_path=self._image_path,
            image_sha256=self._image_sha256,
            image_bytes=self._image_bytes,
            snapshot=self._score_log_data,
            score_log_data_confirmed=(
                self._score_log_data is not None
                and self._data_failure_reason is None
            ),
            data_failure_reason=self._data_failure_reason,
            last_successful_image_path=self._last_successful_image_path,
            last_successful_image_sha256=self._last_successful_image_sha256,
            stale_image_rejected=self._stale_image_rejected,
            message=f"ongeki:{reason.value}" if reason is not None else None,
        )


class PlaywrightOngekiAdapter:
    """Optional lazy adapter around a caller-owned Playwright page."""

    def __init__(
        self,
        page: object | None = None,
        *,
        timeout_ms: int = 10_000,
        data_capture: Callable[[], object] | None = None,
    ) -> None:
        self.page = page
        self.timeout_ms = timeout_ms
        self.data_capture = data_capture

    def open_official_net(self, url: str) -> OngekiBrowserEvent:
        if self.page is None:
            return OngekiBrowserEvent(OngekiEventType.PROVIDER_NETWORK_OUTAGE)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception as exc:
            return OngekiBrowserEvent(
                OngekiEventType.TIMEOUT
                if _is_timeout_error(exc)
                else OngekiEventType.PROVIDER_NETWORK_OUTAGE
            )
        return OngekiBrowserEvent.official_net_opened(_observed_page_url(self.page, url))

    def refresh_score_log(self, url: str) -> OngekiBrowserEvent:
        if self.page is None:
            return OngekiBrowserEvent(OngekiEventType.SCORE_LOG_REFRESH_STARTED, completed=True)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception as exc:
            return OngekiBrowserEvent(
                OngekiEventType.TIMEOUT
                if _is_timeout_error(exc)
                else OngekiEventType.PROVIDER_NETWORK_OUTAGE
            )
        # DOM/administrator confirmation must emit the completion event later.
        return OngekiBrowserEvent(
            OngekiEventType.SCORE_LOG_REFRESH_STARTED,
            completed=True,
            url=_observed_page_url(self.page, url),
        )

    def capture_score_log_data(self) -> OngekiBrowserEvent:
        """Require a caller-provided extraction hook; never guess selectors."""

        if callable(self.data_capture):
            try:
                payload = self.data_capture()
            except Exception:
                return OngekiBrowserEvent(
                    OngekiEventType.SCORE_LOG_DATA_FAILED,
                    completed=False,
                )
            if isinstance(payload, OngekiBrowserEvent):
                return payload
            if payload is not None:
                return OngekiBrowserEvent.score_log_data(payload)
        return OngekiBrowserEvent(OngekiEventType.SCORE_LOG_DATA_FAILED, completed=False)

    def open_generator(self, url: str) -> OngekiBrowserEvent:
        if self.page is None:
            return OngekiBrowserEvent(OngekiEventType.PROVIDER_NETWORK_OUTAGE)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception as exc:
            return OngekiBrowserEvent(
                OngekiEventType.TIMEOUT
                if _is_timeout_error(exc)
                else OngekiEventType.PROVIDER_NETWORK_OUTAGE
            )
        return OngekiBrowserEvent.generator_opened(_observed_page_url(self.page, url))

    def select_user_mode(self, user_id: str, mode: str) -> OngekiBrowserEvent:
        if self.page is None:
            return OngekiBrowserEvent(OngekiEventType.DOM_MISMATCH)
        # Selectors and credentials are intentionally caller-owned.  This
        # adapter only reports the explicit values the caller supplied.
        if not user_id.strip() or not mode.strip():
            return OngekiBrowserEvent(OngekiEventType.DOM_MISMATCH)
        return OngekiBrowserEvent.user_mode_selected(user_id, mode)

    def execute_bookmarklet(self, source: str) -> OngekiBrowserEvent:
        if self.page is None:
            return OngekiBrowserEvent(OngekiEventType.BOOKMARKLET_LOAD_FAILED)
        try:
            self.page.evaluate(source)
        except Exception:
            return OngekiBrowserEvent(OngekiEventType.BOOKMARKLET_LOAD_FAILED)
        return OngekiBrowserEvent(OngekiEventType.BOOKMARKLET_LOADED, completed=True)

    def image_generation_event(
        self,
        *,
        completed: bool,
        generation_id: str | None = None,
    ) -> OngekiBrowserEvent:
        return OngekiBrowserEvent(
            OngekiEventType.IMAGE_GENERATED,
            completed=completed,
            generation_id=generation_id,
        )

    @staticmethod
    def score_log_data_event(payload: object, *, completed: bool = True) -> OngekiBrowserEvent:
        return OngekiBrowserEvent.score_log_data(payload, completed=completed)


OngekiBrowserCollector = OngekiCollector
OngekiCollectorConfig = OngekiConfig


__all__ = [
    "ONGEKICollectionState",
    "OngekiBrowserAdapter",
    "OngekiBrowserCollector",
    "OngekiBrowserEvent",
    "OngekiCollectionResult",
    "OngekiConfig",
    "OngekiCollectorConfig",
    "OngekiCollector",
    "OngekiDataSchemaError",
    "OngekiEventType",
    "OngekiFailureReason",
    "OngekiParser",
    "OngekiState",
    "PlaywrightOngekiAdapter",
    "RetryDecision",
    "RetryPolicy",
    "parse_ongeki_data",
    "parse_ongeki_score_log",
    "parse_ongeki_snapshot",
]
