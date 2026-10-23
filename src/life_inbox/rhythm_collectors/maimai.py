"""Event-driven maimai browser collection foundations.

This module does not launch a browser by default.  A caller supplies a
per-game persistent-session directory, a runtime bookmarklet file, and
explicit browser events.  No timer or elapsed-duration heuristic can produce
an image-success result.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from life_inbox.rhythm_rating import Game, RatingEntry, RatingSnapshot

from ..rhythm_images import RatingImageVerificationError, png_dimensions
from .common import (
    BrowserEvidence,
    EvidenceKind,
    load_runtime_source,
    redact_text,
    safe_browser_url,
    sanitize_for_output,
)


class MaimaiState(StrEnum):
    """Observable maimai browser outcomes/states."""

    INITIAL = "initial"
    NOT_LOGGED_IN = "not_logged_in"
    AUTH_REQUIRED = "not_logged_in"
    AUTH_WAITING = "auth_waiting"
    CAPTCHA = "captcha"
    TERMS = "terms"
    STANDARD_COURSE_MISSING = "standard_course_missing"
    SUBSCRIPTION_MISSING = "standard_course_missing"
    WRONG_PAGE = "wrong_page"
    BOOKMARKLET_LOAD_FAILURE = "bookmarklet_load_failure"
    FULL_DATA_SELECTION_FAILURE = "full_data_selection_failure"
    IMAGE_WAITING = "image_waiting"
    IMAGE_MISSING = "image_missing"
    IMAGE_SUCCESS = "image_success"
    RATING_DATA_MISSING = "rating_data_missing"
    DATA_MISSING = "rating_data_missing"
    DOM_MISMATCH = "dom_mismatch"
    TIMEOUT = "timeout"


MaimaiCollectionState = MaimaiState


_BLOCKING_STATES = frozenset(
    {
        MaimaiState.NOT_LOGGED_IN,
        MaimaiState.AUTH_WAITING,
        MaimaiState.CAPTCHA,
        MaimaiState.TERMS,
        MaimaiState.STANDARD_COURSE_MISSING,
        MaimaiState.WRONG_PAGE,
        MaimaiState.BOOKMARKLET_LOAD_FAILURE,
        MaimaiState.FULL_DATA_SELECTION_FAILURE,
        MaimaiState.DOM_MISMATCH,
        MaimaiState.TIMEOUT,
        MaimaiState.RATING_DATA_MISSING,
    }
)


class MaimaiFailureReason(StrEnum):
    """Stable reasons attached to maimai state transitions."""

    NOT_LOGGED_IN = "not_logged_in"
    AUTH_REQUIRED = "not_logged_in"
    AUTH_WAITING = "auth_waiting"
    CAPTCHA = "captcha"
    TERMS = "terms"
    STANDARD_COURSE_MISSING = "standard_course_missing"
    SUBSCRIPTION_MISSING = "standard_course_missing"
    WRONG_PAGE = "wrong_page"
    BOOKMARKLET_LOAD_FAILURE = "bookmarklet_load_failure"
    FULL_DATA_SELECTION_FAILURE = "full_data_selection_failure"
    MANUAL_ACTION_REQUIRED = "manual_action_required"
    IMAGE_WAITING = "image_waiting"
    IMAGE_MISSING = "image_missing"
    DOM_MISMATCH = "dom_mismatch"
    TIMEOUT = "timeout"
    STALE_IMAGE = "stale_image"
    PLAYWRIGHT_UNAVAILABLE = "playwright_unavailable"
    RATING_DATA_MISSING = "rating_data_missing"
    DATA_MISSING = "rating_data_missing"


class MaimaiEventType(StrEnum):
    """Explicit URL/DOM/window/artifact events accepted by the collector."""

    URL_OBSERVED = "url_observed"
    DOM_OBSERVED = "dom_observed"
    NOT_LOGGED_IN = "not_logged_in"
    AUTH_WAITING = "auth_waiting"
    CAPTCHA = "captcha"
    TERMS = "terms"
    STANDARD_COURSE_MISSING = "standard_course_missing"
    WRONG_PAGE = "wrong_page"
    BOOKMARKLET_LOADED = "bookmarklet_loaded"
    BOOKMARKLET_LOAD_FAILED = "bookmarklet_load_failed"
    FULL_DATA_SELECTED = "full_data_selected"
    FULL_DATA_SELECTION_FAILED = "full_data_selection_failed"
    POPUP_OPENED = "popup_opened"
    WINDOW_OPENED = "window_opened"
    DOWNLOAD_STARTED = "download_started"
    DOWNLOAD_COMPLETED = "download_completed"
    BLOB_AVAILABLE = "blob_available"
    CANVAS_CAPTURED = "canvas_captured"
    IMAGE_AVAILABLE = "image_available"
    IMAGE_SUCCESS = "image_success"
    IMAGE_WAITING = "image_waiting"
    IMAGE_MISSING = "image_missing"
    DOM_MISMATCH = "dom_mismatch"
    TIMEOUT = "timeout"
    RATING_DATA_AVAILABLE = "rating_data_available"
    RATING_DATA_FAILED = "rating_data_failed"

    URL = "url_observed"
    DOM = "dom_observed"
    AUTH_REQUIRED = "not_logged_in"
    CAPTCHA_DETECTED = "captcha"
    TERMS_DETECTED = "terms"
    SUBSCRIPTION_MISSING = "standard_course_missing"
    WINDOW_OPEN = "window_opened"
    DATA_AVAILABLE = "rating_data_available"
    DATA_MISSING = "rating_data_failed"


@dataclass(frozen=True, slots=True)
class MaimaiConfig:
    """Caller-owned maimai browser paths and safe selector configuration."""

    persistent_session_dir: str | Path
    bookmarklet_runtime_path: str | Path
    expected_url: str = "https://maimaidx.jp/maimai-mobile/"
    full_data_selector: str | None = None
    standard_course_required: bool = True

    def __post_init__(self) -> None:
        session = str(self.persistent_session_dir).strip()
        runtime = str(self.bookmarklet_runtime_path).strip()
        expected = self.expected_url.strip() if isinstance(self.expected_url, str) else ""
        if not session or not runtime or not expected:
            raise ValueError("maimai paths and expected_url are required")
        if self.full_data_selector is not None:
            selector = self.full_data_selector.strip()
            object.__setattr__(self, "full_data_selector", selector or None)
        if type(self.standard_course_required) is not bool:
            raise ValueError("standard_course_required must be a boolean")
        object.__setattr__(self, "persistent_session_dir", session)
        object.__setattr__(self, "bookmarklet_runtime_path", runtime)
        object.__setattr__(self, "expected_url", expected)

    @property
    def session_directory(self) -> str:
        return str(self.persistent_session_dir)

    @property
    def runtime_file(self) -> str:
        return str(self.bookmarklet_runtime_path)


class MaimaiDataSchemaError(ValueError):
    """Raised when a maimai NET/bookmarklet data event is incomplete."""


def _maimai_key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _maimai_direct(mapping: Mapping[object, object], aliases: Sequence[str]) -> object | None:
    wanted = {_maimai_key(alias) for alias in aliases}
    for key, value in mapping.items():
        if _maimai_key(key) in wanted:
            return value
    return None


def _maimai_text(
    value: object,
    *,
    field_name: str,
    secrets: Sequence[str] = (),
) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Mapping):
        value = _maimai_direct(value, ("name", "title", "label", "value", "text"))
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    text = redact_text(str(value).strip(), secrets)
    return text or None


def _maimai_measure(
    value: object,
    *,
    field_name: str,
    required: bool = False,
) -> int | float | None:
    if value is None or isinstance(value, bool):
        if required:
            raise MaimaiDataSchemaError(f"{field_name} is required")
        return None
    if isinstance(value, int):
        if value < 0:
            raise MaimaiDataSchemaError(f"{field_name} must not be negative")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MaimaiDataSchemaError(f"{field_name} must be finite")
        if value < 0:
            raise MaimaiDataSchemaError(f"{field_name} must not be negative")
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        text = value.strip().replace(",", "").removesuffix("%")
        if not text:
            if required:
                raise MaimaiDataSchemaError(f"{field_name} is required")
            return None
        try:
            parsed = float(text)
        except ValueError as exc:
            raise MaimaiDataSchemaError(f"{field_name} must be numeric") from exc
        if not math.isfinite(parsed):
            raise MaimaiDataSchemaError(f"{field_name} must be finite")
        if parsed < 0:
            raise MaimaiDataSchemaError(f"{field_name} must not be negative")
        return int(parsed) if parsed.is_integer() else parsed
    if isinstance(value, Mapping):
        nested = _maimai_direct(value, ("value", "score", "rating", "amount"))
        if nested is None:
            raise MaimaiDataSchemaError(f"{field_name} must be numeric")
        return _maimai_measure(nested, field_name=field_name, required=required)
    raise MaimaiDataSchemaError(f"{field_name} must be numeric")


def _maimai_datetime(value: object, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise MaimaiDataSchemaError(f"{field_name} must include a timezone")
        return value.astimezone(UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise MaimaiDataSchemaError(f"{field_name} is invalid") from exc
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise MaimaiDataSchemaError(f"{field_name} is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise MaimaiDataSchemaError(f"{field_name} must include a timezone")
        return parsed.astimezone(UTC)
    raise MaimaiDataSchemaError(f"{field_name} is invalid")


def _maimai_status(value: object, label: str, *, secrets: Sequence[str] = ()) -> str | None:
    if value is None or value is False:
        return None
    if value is True:
        return label
    text = _maimai_text(value, field_name=label, secrets=secrets)
    if text is None or text.casefold() in {"false", "none", "null", "0", "no"}:
        return None
    return text


def _maimai_frame_rows(
    value: object,
    *,
    frame: str,
) -> list[Mapping[object, object]]:
    if isinstance(value, list):
        rows = value
    elif isinstance(value, Mapping):
        nested = _maimai_direct(value, ("entries", "records", "items", "songs"))
        if not isinstance(nested, list):
            if _maimai_direct(value, ("id", "song_id", "music_id", "title", "name")) is not None:
                nested = [value]
            else:
                raise MaimaiDataSchemaError(f"{frame} must contain an entries array")
        rows = nested
    else:
        raise MaimaiDataSchemaError(f"{frame} must be an array or object")
    result: list[Mapping[object, object]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise MaimaiDataSchemaError(f"{frame}[{index}] must be an object")
        result.append(row)
    return result


def _maimai_row(
    row: Mapping[object, object],
    *,
    frame: str,
    index: int,
    secrets: Sequence[str] = (),
) -> RatingEntry:
    identifier = _maimai_text(
        _maimai_direct(row, ("song_id", "songId", "music_id", "musicId", "id")),
        field_name="song_identifier",
        secrets=secrets,
    )
    title = _maimai_text(
        _maimai_direct(row, ("title", "song_title", "music_name", "name")),
        field_name="title",
        secrets=secrets,
    )
    if identifier is None and title is None:
        raise MaimaiDataSchemaError(f"{frame}[{index}] requires song identifier or title")
    source_updated_at = _maimai_datetime(
        _maimai_direct(row, ("updated_at", "updatedAt", "played_at", "last_played")),
        field_name=f"{frame}[{index}].updated_at",
    )
    clear_status = _maimai_status(
        _maimai_direct(row, ("clear", "clear_status", "clearStatus")),
        "clear",
        secrets=secrets,
    )
    fc_status = _maimai_status(
        _maimai_direct(row, ("fc", "full_combo", "fullCombo", "fullcombo")),
        "fc",
        secrets=secrets,
    )
    sync_status = _maimai_status(
        _maimai_direct(row, ("sync", "sync_status", "syncStatus")),
        "sync",
        secrets=secrets,
    )
    full_bell_status = _maimai_status(
        _maimai_direct(row, ("full_bell", "fullBell", "bell")),
        "full_bell",
        secrets=secrets,
    )
    all_break_status = _maimai_status(
        _maimai_direct(row, ("all_break", "allBreak")),
        "all_break",
        secrets=secrets,
    )
    statuses = [
        item
        for item in (
            clear_status,
            fc_status,
            sync_status,
            full_bell_status,
            all_break_status,
        )
        if item is not None
    ]
    chart_constant = _maimai_measure(
        _maimai_direct(row, ("chart_constant", "chartConstant", "const", "constant")),
        field_name=f"{frame}[{index}].chart_constant",
    )
    score = _maimai_measure(
        _maimai_direct(row, ("score", "best_score")),
        field_name=f"{frame}[{index}].score",
    )
    achievement = _maimai_measure(
        _maimai_direct(row, ("achievement", "achievement_rate", "percent")),
        field_name=f"{frame}[{index}].achievement",
    )
    technical_score = _maimai_measure(
        _maimai_direct(row, ("technical_score", "technical", "dx_score", "dxScore")),
        field_name=f"{frame}[{index}].technical_score",
    )
    p_score = _maimai_measure(
        _maimai_direct(row, ("p_score", "pScore", "pscore")),
        field_name=f"{frame}[{index}].p_score",
    )
    rating = _maimai_measure(
        _maimai_direct(row, ("rating", "song_rating", "rating_value")),
        field_name=f"{frame}[{index}].rating",
    )
    song_rating = _maimai_measure(
        _maimai_direct(row, ("song_rating", "rating", "rating_value")),
        field_name=f"{frame}[{index}].song_rating",
    )
    if all(
        value is None
        for value in (
            chart_constant,
            score,
            achievement,
            technical_score,
            p_score,
            rating,
            song_rating,
        )
    ):
        raise MaimaiDataSchemaError(
            f"{frame}[{index}] requires at least one chart or rating measure"
        )
    return RatingEntry(
        game=Game.MAIMAI,
        song_identifier=identifier,
        title=title,
        difficulty=_maimai_text(
            _maimai_direct(row, ("difficulty", "diff", "difficulty_name")),
            field_name="difficulty",
            secrets=secrets,
        ),
        chart_type=_maimai_text(
            _maimai_direct(row, ("chart_type", "chartType", "type")),
            field_name="chart_type",
            secrets=secrets,
        ),
        frame_membership=frame,
        level=_maimai_text(
            _maimai_direct(row, ("level", "level_name", "levelString")),
            field_name="level",
            secrets=secrets,
        ),
        chart_constant=chart_constant,
        score=score,
        achievement=achievement,
        technical_score=technical_score,
        p_score=p_score,
        rating=rating,
        song_rating=song_rating,
        completion_statuses=statuses,
        clear_status=clear_status,
        full_combo_status=fc_status,
        sync_status=sync_status,
        full_bell_status=full_bell_status,
        all_break_status=all_break_status,
        genre=_maimai_text(
            _maimai_direct(row, ("genre", "category")),
            field_name="genre",
            secrets=secrets,
        ),
        source_updated_at=source_updated_at,
    )


def parse_maimai_data(
    payload: object,
    *,
    source: str = "maimai_net",
    collected_at: datetime | None = None,
    secrets: Sequence[str] = (),
) -> RatingSnapshot:
    """Normalize one explicit maimai NET/bookmarklet data event.

    The parser accepts JSON bytes/text or a browser-produced mapping, but only
    one optional ``data`` wrapper and named frame containers.  It never walks
    arbitrary nested objects and never turns a missing or malformed frame
    into a successful snapshot.
    """

    if isinstance(payload, RatingSnapshot):
        if payload.game is not Game.MAIMAI:
            raise MaimaiDataSchemaError("snapshot game is not maimai")
        return payload
    if isinstance(payload, (bytes, bytearray, memoryview)):
        try:
            payload = json.loads(bytes(payload).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MaimaiDataSchemaError("rating data is not valid JSON") from exc
    elif isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MaimaiDataSchemaError("rating data is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise MaimaiDataSchemaError("rating data must be an object")
    root = payload
    wrapped = _maimai_direct(payload, ("data",))
    if isinstance(wrapped, Mapping):
        root = wrapped

    player = _maimai_direct(root, ("player", "user"))
    player_value = _maimai_direct(
        root, ("player_name", "playerName", "display_name", "username")
    )
    if player_value is None:
        player_value = (
            player
            if not isinstance(player, Mapping)
            else _maimai_direct(player, ("name", "player_name", "playerName"))
        )
    player_name = _maimai_text(
        player_value,
        field_name="player_name",
        secrets=secrets,
    )
    if player_name is None:
        raise MaimaiDataSchemaError("player_name is required")
    rating_value = _maimai_direct(
        root, ("rating", "overall_rating", "total_rating", "player_rating")
    )
    if rating_value is None and isinstance(player, Mapping):
        rating_value = _maimai_direct(player, ("rating", "overall_rating", "total_rating"))
    rating = _maimai_measure(rating_value, field_name="rating", required=True)

    frame_values: list[tuple[str, object]] = []
    seen_frame_names: set[str] = set()
    canonical_frame_names = {
        "bestratings": "best",
        "bestrecords": "best",
        "recentratings": "recent",
        "recentrecords": "recent",
        "newequivalent": "new",
        "newrecords": "new",
    }

    def add_frame(name: object, value: object) -> None:
        normalized_name = _maimai_key(name)
        canonical_name = canonical_frame_names.get(normalized_name, normalized_name)
        if canonical_name and canonical_name not in seen_frame_names:
            seen_frame_names.add(canonical_name)
            frame_values.append((canonical_name, value))

    frames = _maimai_direct(root, ("frames", "rating_frames"))
    if isinstance(frames, Mapping):
        for name, value in frames.items():
            add_frame(name, value)
    elif frames is not None:
        raise MaimaiDataSchemaError("frames must be an object")
    frame_aliases = (
        (("best", "best_ratings", "best_records"), "best"),
        (("recent", "recent_ratings", "recent_records"), "recent"),
        (("new", "new_equivalent", "new_records"), "new"),
        (("all", "records", "entries", "items"), "all"),
    )
    for aliases, frame_name in frame_aliases:
        value = _maimai_direct(root, aliases)
        if value is not None:
            add_frame(frame_name, value)
    if not frame_values:
        raise MaimaiDataSchemaError("at least one rating frame is required")

    entries: list[RatingEntry] = []
    for frame, value in frame_values:
        normalized_frame = _maimai_text(frame, field_name="frame")
        if normalized_frame is None:
            raise MaimaiDataSchemaError("rating frame name must not be blank")
        for index, row in enumerate(_maimai_frame_rows(value, frame=normalized_frame)):
            entries.append(
                _maimai_row(
                    row,
                    frame=normalized_frame,
                    index=index,
                    secrets=secrets,
                )
            )
    source_updated_at = _maimai_datetime(
        _maimai_direct(root, ("updated_at", "updatedAt", "last_updated")),
        field_name="updated_at",
    )
    return RatingSnapshot(
        game=Game.MAIMAI,
        entries=entries,
        rating=rating,
        source=source,
        player_name=player_name,
        source_updated_at=source_updated_at,
        raw_data=sanitize_for_output(payload, secrets),
        collected_at=collected_at or datetime.now(UTC),
    )


parse_maimai_snapshot = parse_maimai_data
parse_rating_snapshot = parse_maimai_data


class MaimaiParser:
    """Class-style facade for the pure maimai data normalizer."""

    parse = staticmethod(parse_maimai_data)
    parse_snapshot = staticmethod(parse_maimai_data)
    parse_rating_snapshot = staticmethod(parse_maimai_data)


@dataclass(frozen=True, slots=True)
class MaimaiBrowserEvent:
    """One detached event from a browser adapter.

    ``payload`` is intentionally excluded from repr and result evidence.  It
    may be bytes or a browser object held only by the adapter while it emits
    an explicit completion signal.
    """

    event_type: MaimaiEventType | str
    completed: bool = False
    url: str | None = None
    selector: str | None = None
    path: str | Path | None = None
    generation_id: str | None = None
    label: str | None = None
    explicit_confirmation: bool = False
    payload: object | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            event_type = (
                self.event_type
                if isinstance(self.event_type, MaimaiEventType)
                else MaimaiEventType(str(self.event_type).strip().casefold())
            )
        except ValueError as exc:
            raise ValueError("unsupported maimai browser event") from exc
        if type(self.completed) is not bool or type(self.explicit_confirmation) is not bool:
            raise ValueError("event completion and confirmation must be booleans")
        object.__setattr__(self, "event_type", event_type)
        if self.url is not None:
            object.__setattr__(self, "url", safe_browser_url(self.url))
        if self.path is not None:
            object.__setattr__(self, "path", str(self.path))

    @classmethod
    def url_observed(cls, url: str) -> MaimaiBrowserEvent:
        return cls(MaimaiEventType.URL_OBSERVED, completed=True, url=url)

    url_event = url_observed

    @classmethod
    def dom(cls, label: str, *, completed: bool = True) -> MaimaiBrowserEvent:
        return cls(MaimaiEventType.DOM_OBSERVED, completed=completed, label=label)

    dom_observed = dom

    @classmethod
    def popup_opened(cls, *, completed: bool = True) -> MaimaiBrowserEvent:
        return cls(MaimaiEventType.POPUP_OPENED, completed=completed)

    @classmethod
    def window_opened(cls, *, completed: bool = True) -> MaimaiBrowserEvent:
        return cls(MaimaiEventType.WINDOW_OPENED, completed=completed)

    @classmethod
    def download_started(cls) -> MaimaiBrowserEvent:
        return cls(MaimaiEventType.DOWNLOAD_STARTED, completed=True)

    @classmethod
    def download_completed(cls, path: str | Path) -> MaimaiBrowserEvent:
        return cls(MaimaiEventType.DOWNLOAD_COMPLETED, completed=True, path=path)

    @classmethod
    def blob_available(
        cls,
        payload: object,
        *,
        path: str | Path | None = None,
    ) -> MaimaiBrowserEvent:
        return cls(MaimaiEventType.BLOB_AVAILABLE, completed=True, payload=payload, path=path)

    @classmethod
    def canvas_captured(
        cls,
        payload: object,
        *,
        path: str | Path | None = None,
    ) -> MaimaiBrowserEvent:
        return cls(MaimaiEventType.CANVAS_CAPTURED, completed=True, payload=payload, path=path)

    @classmethod
    def image_available(cls, path: str | Path) -> MaimaiBrowserEvent:
        return cls(MaimaiEventType.IMAGE_AVAILABLE, completed=True, path=path)

    image_success = image_available

    @classmethod
    def rating_data(cls, payload: object, *, completed: bool = True) -> MaimaiBrowserEvent:
        return cls(
            MaimaiEventType.RATING_DATA_AVAILABLE,
            completed=completed,
            payload=payload,
        )

    data_available = rating_data


class MaimaiBrowserAdapter(Protocol):
    """Optional adapter interface; every operation returns explicit events."""

    def open_url(self, url: str) -> MaimaiBrowserEvent:
        """Open a URL and return a URL/DOM observation event."""

    def execute_bookmarklet(self, source: str) -> MaimaiBrowserEvent:
        """Execute caller-supplied runtime source and report load status."""

    def select_full_data(self, selector: str) -> MaimaiBrowserEvent:
        """Use only an explicitly configured selector."""

    def capture_rating_data(self) -> MaimaiBrowserEvent:
        """Return an explicit DOM/network-derived normalized-data event."""


@dataclass(frozen=True, slots=True)
class MaimaiCollectionResult:
    """Detached maimai outcome; success is possible only with image evidence."""

    state: MaimaiState
    success: bool
    failure_reason: MaimaiFailureReason | None = None
    events: tuple[MaimaiBrowserEvent, ...] = ()
    evidence: tuple[BrowserEvidence, ...] = ()
    image_path: str | None = None
    image_sha256: str | None = None
    image_bytes: bytes | None = field(default=None, repr=False)
    snapshot: RatingSnapshot | None = field(default=None, repr=False)
    data_observed: bool = False
    data_failure_reason: MaimaiFailureReason | None = None
    last_successful_image_path: str | None = None
    last_successful_image_sha256: str | None = None
    stale_image_rejected: bool = False
    manual_action_required: bool = False
    message: str | None = None

    @property
    def reason(self) -> MaimaiFailureReason | None:
        return self.failure_reason

    @property
    def image_success(self) -> bool:
        return self.state is MaimaiState.IMAGE_SUCCESS and self.success

    @property
    def complete(self) -> bool:
        """Whether normalized data and usable image evidence are both present."""

        return (
            self.success
            and self.snapshot is not None
            and self.image_sha256 is not None
            and (self.image_path is not None or self.image_bytes is not None)
            and self.data_failure_reason is None
        )

    @property
    def ready_for_persistence(self) -> bool:
        return self.complete and self.image_path is not None

    @property
    def waiting_for_manual_action(self) -> bool:
        return self.manual_action_required

    @property
    def waitingForManualAction(self) -> bool:  # noqa: N802 - external event vocabulary
        return self.waiting_for_manual_action


def _failure_reason(state: MaimaiState) -> MaimaiFailureReason | None:
    return {
        MaimaiState.NOT_LOGGED_IN: MaimaiFailureReason.NOT_LOGGED_IN,
        MaimaiState.AUTH_WAITING: MaimaiFailureReason.AUTH_WAITING,
        MaimaiState.CAPTCHA: MaimaiFailureReason.CAPTCHA,
        MaimaiState.TERMS: MaimaiFailureReason.TERMS,
        MaimaiState.STANDARD_COURSE_MISSING: MaimaiFailureReason.STANDARD_COURSE_MISSING,
        MaimaiState.WRONG_PAGE: MaimaiFailureReason.WRONG_PAGE,
        MaimaiState.BOOKMARKLET_LOAD_FAILURE: MaimaiFailureReason.BOOKMARKLET_LOAD_FAILURE,
        MaimaiState.FULL_DATA_SELECTION_FAILURE: MaimaiFailureReason.FULL_DATA_SELECTION_FAILURE,
        MaimaiState.IMAGE_WAITING: MaimaiFailureReason.IMAGE_WAITING,
        MaimaiState.IMAGE_MISSING: MaimaiFailureReason.IMAGE_MISSING,
        MaimaiState.DOM_MISMATCH: MaimaiFailureReason.DOM_MISMATCH,
        MaimaiState.TIMEOUT: MaimaiFailureReason.TIMEOUT,
        MaimaiState.RATING_DATA_MISSING: MaimaiFailureReason.RATING_DATA_MISSING,
    }.get(state)


def _result_message(reason: MaimaiFailureReason | None) -> str | None:
    if reason is None:
        return None
    return f"maimai:{reason.value}"


class MaimaiCollector:
    """Stateful event reducer with no browser or timer side effects."""

    game = Game.MAIMAI

    def __init__(
        self,
        config: MaimaiConfig,
        *,
        adapter: MaimaiBrowserAdapter | None = None,
        last_successful_image: str | Path | None = None,
        last_successful_image_path: str | Path | None = None,
    ) -> None:
        if last_successful_image is not None and last_successful_image_path is not None:
            raise ValueError("provide only one last successful image path")
        if last_successful_image is None:
            last_successful_image = last_successful_image_path
        self.config = config
        self.adapter = adapter
        self.state = MaimaiState.INITIAL
        self._events: list[MaimaiBrowserEvent] = []
        self._evidence: list[BrowserEvidence] = []
        self._image_path: str | None = None
        self._image_sha256: str | None = None
        self._image_bytes: bytes | None = None
        self._snapshot: RatingSnapshot | None = None
        self._data_failure_reason: MaimaiFailureReason | None = None
        # ``result()`` is valid before the first event, so initialize every
        # result field before any reducer or failure path can be observed.
        self._stale_image_rejected: bool = False
        self._failure_reason_override: MaimaiFailureReason | None = None
        self._runtime_source: str | None = None
        self._full_data_selected = False
        self._blocked = False
        self._last_successful_image_path = (
            str(last_successful_image) if last_successful_image is not None else None
        )
        self._last_successful_image_sha256 = self._hash_existing(last_successful_image)

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
    def events(self) -> tuple[MaimaiBrowserEvent, ...]:
        return tuple(self._events)

    @property
    def evidence(self) -> tuple[BrowserEvidence, ...]:
        return tuple(self._evidence)

    def _set_failure(self, state: MaimaiState) -> None:
        if self.state is MaimaiState.IMAGE_SUCCESS:
            return
        self.state = state
        if state in _BLOCKING_STATES:
            self._blocked = True

    def _rating_data_failure(self) -> None:
        """Invalidate the current candidate without replacing prior success."""

        self._data_failure_reason = MaimaiFailureReason.RATING_DATA_MISSING
        if self.state is MaimaiState.IMAGE_SUCCESS:
            self.state = MaimaiState.RATING_DATA_MISSING
            self._blocked = True
        else:
            self._set_failure(MaimaiState.RATING_DATA_MISSING)

    def _record_successful_image_baseline(self) -> None:
        """Promote a candidate only after data and a durable path both exist."""

        if (
            self._snapshot is None
            or self._data_failure_reason is not None
            or self.state is not MaimaiState.IMAGE_SUCCESS
            or self._image_path is None
            or self._image_sha256 is None
        ):
            return
        self._last_successful_image_path = self._image_path
        self._last_successful_image_sha256 = self._image_sha256

    def _reject_stale_image(self) -> None:
        self._stale_image_rejected = True
        self._failure_reason_override = MaimaiFailureReason.STALE_IMAGE
        self._image_path = None
        self._image_sha256 = None
        self._image_bytes = None
        self.state = MaimaiState.IMAGE_MISSING

    def _record_evidence(self, event: MaimaiBrowserEvent) -> None:
        kind_map = {
            MaimaiEventType.URL_OBSERVED: EvidenceKind.URL,
            MaimaiEventType.DOM_OBSERVED: EvidenceKind.DOM,
            MaimaiEventType.POPUP_OPENED: EvidenceKind.POPUP,
            MaimaiEventType.WINDOW_OPENED: EvidenceKind.POPUP,
            MaimaiEventType.DOWNLOAD_STARTED: EvidenceKind.DOWNLOAD,
            MaimaiEventType.DOWNLOAD_COMPLETED: EvidenceKind.DOWNLOAD,
            MaimaiEventType.BLOB_AVAILABLE: EvidenceKind.BLOB,
            MaimaiEventType.CANVAS_CAPTURED: EvidenceKind.CANVAS,
            MaimaiEventType.IMAGE_AVAILABLE: EvidenceKind.IMAGE,
            MaimaiEventType.IMAGE_SUCCESS: EvidenceKind.IMAGE,
            MaimaiEventType.RATING_DATA_AVAILABLE: EvidenceKind.DATA,
            MaimaiEventType.RATING_DATA_FAILED: EvidenceKind.DATA,
        }
        kind = kind_map.get(event.event_type)
        if kind is not None:
            self._evidence.append(
                BrowserEvidence(
                    kind=kind,
                    completed=event.completed,
                    url=event.url,
                    selector=event.selector,
                    label=event.label,
                    path=event.path,
                    generation_id=event.generation_id,
                )
            )

    def _mark_data(self, event: MaimaiBrowserEvent) -> None:
        if not event.completed:
            self._rating_data_failure()
            return
        try:
            self._snapshot = parse_maimai_data(
                event.payload,
                secrets=(),
            )
        except (MaimaiDataSchemaError, TypeError, ValueError):
            self._rating_data_failure()
            return
        self._data_failure_reason = None
        self._record_successful_image_baseline()
        if self.state is MaimaiState.INITIAL:
            self.state = MaimaiState.IMAGE_WAITING

    def _mark_image(self, event: MaimaiBrowserEvent) -> None:
        if self._blocked:
            return
        if not self._full_data_selected:
            self.state = MaimaiState.IMAGE_MISSING
            return
        if not event.completed:
            self.state = MaimaiState.IMAGE_WAITING
            return
        path: Path | None = Path(event.path) if event.path is not None else None
        payload = event.payload
        if path is None and not isinstance(payload, (bytes, bytearray, memoryview)):
            self.state = MaimaiState.IMAGE_MISSING
            return
        if path is not None:
            try:
                if not path.is_file():
                    self.state = MaimaiState.IMAGE_MISSING
                    return
                content = path.read_bytes()
            except OSError:
                self.state = MaimaiState.IMAGE_MISSING
                return
            self._image_path = str(path)
        else:
            content = bytes(payload)
        try:
            png_dimensions(content, require_complete=True)
        except RatingImageVerificationError:
            self.state = MaimaiState.IMAGE_MISSING
            return
        image_sha256 = hashlib.sha256(content).hexdigest()
        if (
            self._last_successful_image_sha256 is not None
            and image_sha256 == self._last_successful_image_sha256
        ):
            self._reject_stale_image()
            return
        self._failure_reason_override = None
        self._image_sha256 = image_sha256
        self._image_bytes = content
        self.state = MaimaiState.IMAGE_SUCCESS
        self._record_successful_image_baseline()

    def observe(self, event: MaimaiBrowserEvent) -> MaimaiCollectionResult:
        """Reduce one explicit browser event and return the current outcome."""

        self._events.append(event)
        self._record_evidence(event)
        if self._blocked:
            return self.result()
        event_type = event.event_type
        if event_type is MaimaiEventType.URL_OBSERVED:
            if not event.completed or event.url is None:
                self._set_failure(MaimaiState.WRONG_PAGE)
            elif (
                safe_browser_url(event.url) is None
                or safe_browser_url(self.config.expected_url) is None
                or safe_browser_url(event.url).rstrip("/")
                != safe_browser_url(self.config.expected_url).rstrip("/")
            ):
                self._set_failure(MaimaiState.WRONG_PAGE)
        elif event_type is MaimaiEventType.DOM_OBSERVED:
            label = (event.label or "").casefold()
            if label in {"not_logged_in", "login", "unauthenticated"}:
                self._set_failure(MaimaiState.NOT_LOGGED_IN)
            elif label in {"captcha", "recaptcha"}:
                self._set_failure(MaimaiState.CAPTCHA)
            elif label in {"terms", "terms_confirmation"}:
                self._set_failure(MaimaiState.TERMS)
            elif label in {"standard_course_missing", "subscription_missing"}:
                self._set_failure(MaimaiState.STANDARD_COURSE_MISSING)
            elif label in {"wrong_page"}:
                self._set_failure(MaimaiState.WRONG_PAGE)
            elif label in {"dom_mismatch", "unexpected_dom"}:
                self._set_failure(MaimaiState.DOM_MISMATCH)
        elif event_type is MaimaiEventType.NOT_LOGGED_IN:
            self._set_failure(MaimaiState.NOT_LOGGED_IN)
        elif event_type is MaimaiEventType.AUTH_WAITING:
            self._set_failure(MaimaiState.AUTH_WAITING)
        elif event_type is MaimaiEventType.CAPTCHA:
            self._set_failure(MaimaiState.CAPTCHA)
        elif event_type is MaimaiEventType.TERMS:
            self._set_failure(MaimaiState.TERMS)
        elif event_type is MaimaiEventType.STANDARD_COURSE_MISSING:
            self._set_failure(MaimaiState.STANDARD_COURSE_MISSING)
        elif event_type is MaimaiEventType.WRONG_PAGE:
            self._set_failure(MaimaiState.WRONG_PAGE)
        elif event_type is MaimaiEventType.BOOKMARKLET_LOADED:
            if not event.completed:
                self._set_failure(MaimaiState.BOOKMARKLET_LOAD_FAILURE)
        elif event_type is MaimaiEventType.BOOKMARKLET_LOAD_FAILED:
            self._set_failure(MaimaiState.BOOKMARKLET_LOAD_FAILURE)
        elif event_type is MaimaiEventType.FULL_DATA_SELECTED:
            if self.config.full_data_selector is None:
                self._set_failure(MaimaiState.FULL_DATA_SELECTION_FAILURE)
            elif (
                not event.completed
                or event.selector != self.config.full_data_selector
            ):
                self._set_failure(MaimaiState.FULL_DATA_SELECTION_FAILURE)
            else:
                self._full_data_selected = True
                self.state = MaimaiState.IMAGE_WAITING
        elif event_type is MaimaiEventType.FULL_DATA_SELECTION_FAILED:
            self._set_failure(MaimaiState.FULL_DATA_SELECTION_FAILURE)
        elif event_type in {
            MaimaiEventType.POPUP_OPENED,
            MaimaiEventType.WINDOW_OPENED,
            MaimaiEventType.DOWNLOAD_STARTED,
        }:
            if self._full_data_selected:
                self.state = MaimaiState.IMAGE_WAITING
            else:
                self.state = MaimaiState.IMAGE_MISSING
        elif event_type in {
            MaimaiEventType.DOWNLOAD_COMPLETED,
            MaimaiEventType.BLOB_AVAILABLE,
            MaimaiEventType.CANVAS_CAPTURED,
            MaimaiEventType.IMAGE_AVAILABLE,
            MaimaiEventType.IMAGE_SUCCESS,
        }:
            self._mark_image(event)
        elif event_type is MaimaiEventType.RATING_DATA_AVAILABLE:
            self._mark_data(event)
        elif event_type is MaimaiEventType.RATING_DATA_FAILED:
            self._rating_data_failure()
        elif event_type is MaimaiEventType.IMAGE_WAITING:
            self.state = MaimaiState.IMAGE_WAITING
        elif event_type is MaimaiEventType.IMAGE_MISSING:
            self._set_failure(MaimaiState.IMAGE_MISSING)
        elif event_type is MaimaiEventType.DOM_MISMATCH:
            self._set_failure(MaimaiState.DOM_MISMATCH)
        elif event_type is MaimaiEventType.TIMEOUT:
            self._set_failure(MaimaiState.TIMEOUT)
        return self.result()

    handle_event = observe
    feed_event = observe

    def load_bookmarklet(self) -> MaimaiCollectionResult:
        """Read a caller-owned runtime file and optionally hand it to an adapter."""

        loaded = load_runtime_source(self.config.bookmarklet_runtime_path)
        if not loaded.success or loaded.source is None:
            self.observe(
                MaimaiBrowserEvent(
                    MaimaiEventType.BOOKMARKLET_LOAD_FAILED,
                    completed=False,
                )
            )
            return self.result()
        self._runtime_source = loaded.source
        if self.adapter is None:
            return self.result()
        try:
            event = self.adapter.execute_bookmarklet(self._runtime_source)
        except Exception:
            event = MaimaiBrowserEvent(MaimaiEventType.BOOKMARKLET_LOAD_FAILED)
        return self.observe(event)

    def select_full_data(self) -> MaimaiCollectionResult:
        """Select only a configured safe selector; otherwise require manual action."""

        if self.config.full_data_selector is None:
            self._events.append(
                MaimaiBrowserEvent(MaimaiEventType.FULL_DATA_SELECTION_FAILED)
            )
            self._set_failure(MaimaiState.FULL_DATA_SELECTION_FAILURE)
            return self.result(manual_action_required=True)
        if self.adapter is None:
            self._set_failure(MaimaiState.FULL_DATA_SELECTION_FAILURE)
            return self.result(manual_action_required=True)
        try:
            event = self.adapter.select_full_data(self.config.full_data_selector)
        except Exception:
            event = MaimaiBrowserEvent(MaimaiEventType.FULL_DATA_SELECTION_FAILED)
        return self.observe(event)

    def process(self, events: Iterable[MaimaiBrowserEvent]) -> MaimaiCollectionResult:
        for event in events:
            self.observe(event)
        return self.result()

    def capture_rating_data(self) -> MaimaiCollectionResult:
        """Ask an adapter for data only when it exposes an explicit capture API."""

        method = getattr(self.adapter, "capture_rating_data", None)
        if not callable(method):
            return self.observe(
                MaimaiBrowserEvent(MaimaiEventType.RATING_DATA_FAILED, completed=False)
            )
        try:
            event = method()
        except Exception:
            event = MaimaiBrowserEvent(MaimaiEventType.RATING_DATA_FAILED)
        return self.observe(event)

    def collect(
        self,
        events: Iterable[MaimaiBrowserEvent] = (),
    ) -> MaimaiCollectionResult:
        return self.process(events)

    def result(self, *, manual_action_required: bool | None = None) -> MaimaiCollectionResult:
        reason = (
            self._failure_reason_override
            or _failure_reason(self.state)
            or self._data_failure_reason
        )
        manual = (
            manual_action_required
            if manual_action_required is not None
            else self.state
            in {
                MaimaiState.NOT_LOGGED_IN,
                MaimaiState.AUTH_WAITING,
                MaimaiState.CAPTCHA,
                MaimaiState.TERMS,
                MaimaiState.STANDARD_COURSE_MISSING,
                MaimaiState.FULL_DATA_SELECTION_FAILURE,
                MaimaiState.RATING_DATA_MISSING,
            }
        )
        if self.state is MaimaiState.FULL_DATA_SELECTION_FAILURE and manual:
            reason = MaimaiFailureReason.MANUAL_ACTION_REQUIRED
        return MaimaiCollectionResult(
            state=self.state,
            success=self.state is MaimaiState.IMAGE_SUCCESS,
            failure_reason=reason,
            events=tuple(self._events),
            evidence=tuple(self._evidence),
            image_path=self._image_path,
            image_sha256=self._image_sha256,
            image_bytes=self._image_bytes,
            snapshot=self._snapshot,
            data_observed=(
                self._snapshot is not None
                and self._data_failure_reason is None
            ),
            data_failure_reason=self._data_failure_reason,
            last_successful_image_path=self._last_successful_image_path,
            last_successful_image_sha256=self._last_successful_image_sha256,
            stale_image_rejected=self._stale_image_rejected,
            manual_action_required=manual,
            message=_result_message(reason),
        )


class PlaywrightMaimaiAdapter:
    """Optional lazy adapter for an already-created Playwright page/context.

    It intentionally has no launch or credential-loading method.  The caller
    owns the browser context and persistent-session directory.  Methods use
    Playwright's popup/download event managers when a page is provided.
    """

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

    def capture_rating_data(self) -> MaimaiBrowserEvent:
        """Use only an explicit caller-owned analyzer/data extraction hook."""

        if not callable(self.data_capture):
            return MaimaiBrowserEvent(MaimaiEventType.RATING_DATA_FAILED, completed=False)
        try:
            payload = self.data_capture()
        except Exception:
            return MaimaiBrowserEvent(MaimaiEventType.RATING_DATA_FAILED, completed=False)
        if isinstance(payload, MaimaiBrowserEvent):
            return payload
        if payload is None:
            return MaimaiBrowserEvent(MaimaiEventType.RATING_DATA_FAILED, completed=False)
        return MaimaiBrowserEvent.rating_data(payload)

    def open_url(self, url: str) -> MaimaiBrowserEvent:
        if self.page is None:
            return MaimaiBrowserEvent(MaimaiEventType.BOOKMARKLET_LOAD_FAILED)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception:
            return MaimaiBrowserEvent(MaimaiEventType.TIMEOUT)
        try:
            observed_url = getattr(self.page, "url", None)
        except Exception:
            observed_url = None
        if not isinstance(observed_url, str) or not observed_url.strip():
            observed_url = url
        return MaimaiBrowserEvent.url_observed(observed_url)

    def execute_bookmarklet(self, source: str) -> MaimaiBrowserEvent:
        if self.page is None:
            return MaimaiBrowserEvent(MaimaiEventType.BOOKMARKLET_LOAD_FAILED)
        try:
            self.page.evaluate(source)
        except Exception:
            return MaimaiBrowserEvent(MaimaiEventType.BOOKMARKLET_LOAD_FAILED)
        return MaimaiBrowserEvent(MaimaiEventType.BOOKMARKLET_LOADED, completed=True)

    def select_full_data(self, selector: str) -> MaimaiBrowserEvent:
        if self.page is None or not selector.strip():
            return MaimaiBrowserEvent(MaimaiEventType.FULL_DATA_SELECTION_FAILED)
        try:
            locator = self.page.locator(selector)
            if locator.count() != 1:
                return MaimaiBrowserEvent(MaimaiEventType.FULL_DATA_SELECTION_FAILED)
            locator.click(timeout=self.timeout_ms)
        except Exception:
            return MaimaiBrowserEvent(MaimaiEventType.FULL_DATA_SELECTION_FAILED)
        return MaimaiBrowserEvent(
            MaimaiEventType.FULL_DATA_SELECTED,
            completed=True,
            selector=selector,
        )

    def popup_event(self, trigger: object) -> MaimaiBrowserEvent:
        """Capture popup evidence around a caller-owned trigger callback."""

        if self.page is None:
            return MaimaiBrowserEvent(MaimaiEventType.POPUP_OPENED)
        try:
            with self.page.expect_popup(timeout=self.timeout_ms) as popup_info:
                trigger()
            popup_info.value
        except Exception:
            return MaimaiBrowserEvent(MaimaiEventType.TIMEOUT)
        return MaimaiBrowserEvent.popup_opened()

    def window_open_event(self, trigger: object) -> MaimaiBrowserEvent:
        """Capture a caller-owned ``window.open``/popup completion signal."""

        if self.page is None:
            return MaimaiBrowserEvent(MaimaiEventType.TIMEOUT)
        try:
            with self.page.expect_popup(timeout=self.timeout_ms) as popup_info:
                trigger()
            popup_info.value
        except Exception:
            return MaimaiBrowserEvent(MaimaiEventType.TIMEOUT)
        return MaimaiBrowserEvent.window_opened()

    def download_event(self, trigger: object, output_path: str | Path) -> MaimaiBrowserEvent:
        """Capture a Playwright download and save it to the caller path."""

        if self.page is None:
            return MaimaiBrowserEvent(MaimaiEventType.DOWNLOAD_COMPLETED, path=output_path)
        try:
            with self.page.expect_download(timeout=self.timeout_ms) as download_info:
                trigger()
            download = download_info.value
            download.save_as(str(output_path))
        except Exception:
            return MaimaiBrowserEvent(MaimaiEventType.TIMEOUT)
        return MaimaiBrowserEvent.download_completed(output_path)

    @staticmethod
    def data_event(payload: object, *, completed: bool = True) -> MaimaiBrowserEvent:
        return MaimaiBrowserEvent.rating_data(payload, completed=completed)

    @staticmethod
    def blob_event(*, completed: bool, payload: object | None = None) -> MaimaiBrowserEvent:
        return MaimaiBrowserEvent(
            MaimaiEventType.BLOB_AVAILABLE,
            completed=completed,
            payload=payload,
        )

    @staticmethod
    def canvas_event(*, completed: bool, payload: object | None = None) -> MaimaiBrowserEvent:
        return MaimaiBrowserEvent(
            MaimaiEventType.CANVAS_CAPTURED,
            completed=completed,
            payload=payload,
        )

    @staticmethod
    def image_event(path: str | Path, *, completed: bool = True) -> MaimaiBrowserEvent:
        return MaimaiBrowserEvent(MaimaiEventType.IMAGE_AVAILABLE, completed=completed, path=path)


MaimaiBrowserCollector = MaimaiCollector
MaimaiCollectorConfig = MaimaiConfig


__all__ = [
    "MaimaiBrowserAdapter",
    "MaimaiBrowserCollector",
    "MaimaiBrowserEvent",
    "MaimaiCollectionResult",
    "MaimaiDataSchemaError",
    "MaimaiCollectionState",
    "MaimaiConfig",
    "MaimaiCollectorConfig",
    "MaimaiEventType",
    "MaimaiFailureReason",
    "MaimaiParser",
    "MaimaiState",
    "PlaywrightMaimaiAdapter",
    "parse_maimai_data",
    "parse_maimai_snapshot",
    "parse_rating_snapshot",
]
