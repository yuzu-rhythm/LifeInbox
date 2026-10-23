"""CHUNIREC API v2.0 collection and normalization.

Only an injected transport is used here.  The client never creates an HTTP
session itself, and the collector's confirmation gate prevents either API
endpoint from being called until an explicit refresh event is received.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Protocol

from life_inbox.rhythm_rating import (
    CollectionReason,
    CollectionState,
    Game,
    RatingEntry,
    RatingSnapshot,
)

from .common import redact_text, safe_message, sanitize_for_output

CHUNIREC_API_BASE = "https://api.chunirec.net/2.0/records"
CHUNIREC_PROFILE_ENDPOINT = f"{CHUNIREC_API_BASE}/profile.json"
CHUNIREC_RATING_DATA_ENDPOINT = f"{CHUNIREC_API_BASE}/rating_data.json"

# The v2 rating endpoint has a deliberately small, fixed vocabulary.  Keep
# this separate from the compatibility parser below so a public API response
# cannot silently be interpreted as an arbitrary nested records document.
CHUNIREC_V2_RATING_GROUPS = (
    "best",
    "recent",
    "best_candidate",
    "best_candidate_sss",
    "best_outside",
)
_ISO_DATE_PREFIX = re.compile(
    r"\A(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})(?P<rest>[T ].*)\Z"
)


class CHUNIRECFailureReason(StrEnum):
    """Stable provider-specific classifications for one CHUNIREC attempt."""

    MISSING_TOKEN = "missing_token"
    INVALID_IDENTITY = "invalid_identity"
    AUTHENTICATION_REQUIRED = "authentication_required"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    PROVIDER_OUTAGE = "provider_outage"
    INVALID_RESPONSE = "invalid_response"
    MALFORMED_JSON = "malformed_json"
    INVALID_SCHEMA = "invalid_schema"
    TRANSPORT_ERROR = "transport_error"
    TIMEOUT = "timeout"
    MANUAL_CONFIRMATION_REQUIRED = "manual_confirmation_required"

    AUTH_REQUIRED = "authentication_required"
    NOT_FOUND_RESOURCE = "not_found"
    OUTAGE = "provider_outage"


class CHUNIRECEventType(StrEnum):
    """Events accepted by the explicit CHUNIREC refresh gate."""

    REFRESH_CONFIRMATION_REQUIRED = "refresh_confirmation_required"
    REFRESH_CONFIRMED = "refresh_confirmed"


@dataclass(frozen=True, slots=True)
class CHUNIRECEvent:
    """A small explicit event envelope; it contains no credentials."""

    event_type: CHUNIRECEventType | str
    explicit_confirmation: bool = False

    def __post_init__(self) -> None:
        try:
            event_type = (
                self.event_type
                if isinstance(self.event_type, CHUNIRECEventType)
                else CHUNIRECEventType(str(self.event_type).strip().casefold())
            )
        except ValueError as exc:
            raise ValueError("unsupported CHUNIREC event type") from exc
        if type(self.explicit_confirmation) is not bool:
            raise ValueError("explicit_confirmation must be a boolean")
        object.__setattr__(self, "event_type", event_type)

    @classmethod
    def refresh_confirmed(cls) -> CHUNIRECEvent:
        return cls(CHUNIRECEventType.REFRESH_CONFIRMED, explicit_confirmation=True)


@dataclass(frozen=True, slots=True)
class CHUNIRECConfig:
    """Explicit CHUNIREC credentials/identity input.

    The token is available to the client internally but is excluded from
    representations.  No result, error, URL, or raw-data structure contains
    this value.
    """

    token: str | None = field(default=None, repr=False)
    region: str = "jp2"
    user_id: str | None = None
    user_name: str | None = None

    def __repr__(self) -> str:
        identity = "user_id" if self.user_id and self.user_id.strip() else "user_name"
        return f"CHUNIRECConfig(region={self.region!r}, identity={identity!r})"

    def validation_failure(self) -> CHUNIRECFailureReason | None:
        if not isinstance(self.token, str) or not self.token.strip():
            return CHUNIRECFailureReason.MISSING_TOKEN
        has_user_id = isinstance(self.user_id, str) and bool(self.user_id.strip())
        has_user_name = isinstance(self.user_name, str) and bool(self.user_name.strip())
        if has_user_id == has_user_name:
            return CHUNIRECFailureReason.INVALID_IDENTITY
        if not isinstance(self.region, str) or not self.region.strip():
            return CHUNIRECFailureReason.INVALID_IDENTITY
        if self.region.strip().casefold() not in {"jp", "jp2"}:
            return CHUNIRECFailureReason.INVALID_IDENTITY
        return None

    def _request_params(self) -> dict[str, str]:
        """Build the private query envelope used only for the transport call."""

        failure = self.validation_failure()
        if failure is not None:
            raise ValueError("CHUNIREC request configuration is invalid")
        params = {"token": (self.token or "").strip(), "region": self.region.strip()}
        if self.user_id and self.user_id.strip():
            params["user_id"] = self.user_id.strip()
        else:
            params["user_name"] = (self.user_name or "").strip()
        return params


@dataclass(frozen=True, slots=True)
class CHUNIRECRateLimitStatus:
    """Safe parsed view of CHUNIREC rate-limit response headers."""

    limit: int | None = None
    remaining: int | None = None
    reset: int | None = None
    reset_at: datetime | None = None
    retry_after_seconds: float | None = None

    @classmethod
    def from_headers(
        cls,
        headers: Mapping[str, str] | None,
    ) -> CHUNIRECRateLimitStatus | None:
        if not headers:
            return None
        lowered = {str(key).casefold(): value for key, value in headers.items()}

        def header(*names: str) -> str | None:
            for name in names:
                value = lowered.get(name.casefold())
                if value is not None:
                    return str(value).strip()
            return None

        limit = _parse_int(header("x-rate-limit-limit", "x-ratelimit-limit"))
        remaining = _parse_int(
            header("x-rate-limit-remaining", "x-ratelimit-remaining")
        )
        reset_header = header("x-rate-limit-reset", "x-ratelimit-reset")
        reset = _parse_int(reset_header)
        if reset is None and reset_header is not None:
            try:
                reset_date = parsedate_to_datetime(reset_header)
                reset = int(reset_date.timestamp())
            except (TypeError, ValueError, OverflowError, OSError):
                reset = None
        retry_after = _parse_float(header("retry-after"))
        if limit is None and remaining is None and reset is None and retry_after is None:
            return None
        reset_at = None
        if reset is not None and reset >= 0:
            try:
                reset_at = datetime.fromtimestamp(reset, tz=UTC)
            except (OverflowError, OSError, ValueError):
                reset_at = None
        return cls(
            limit=limit,
            remaining=remaining,
            reset=reset,
            reset_at=reset_at,
            retry_after_seconds=retry_after,
        )

    @property
    def limited(self) -> bool:
        return self.remaining == 0

    @property
    def retry_after(self) -> float | None:
        return self.retry_after_seconds


@dataclass(frozen=True, slots=True)
class CHUNIRECResponse:
    """Minimal injected-transport response used by fixture tests."""

    status_code: int
    body: object = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        return f"CHUNIRECResponse(status_code={self.status_code!r})"

    def json_data(self) -> object:
        if isinstance(self.body, (Mapping, list, tuple)):
            return self.body
        if isinstance(self.body, bytes):
            try:
                return json.loads(self.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CHUNIRECJSONError from exc
        if isinstance(self.body, str):
            try:
                return json.loads(self.body)
            except json.JSONDecodeError as exc:
                raise CHUNIRECJSONError from exc
        raise CHUNIRECJSONError


class CHUNIRECTransport(Protocol):
    """Synchronous transport seam; implementations are supplied by callers."""

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str],
    ) -> object:
        """Return an HTTP-like response without exposing it to callers."""


class CHUNIRECJSONError(ValueError):
    """Raised only inside the parser boundary for malformed JSON."""


class CHUNIRECSchemaError(ValueError):
    """Raised only inside the parser boundary for an unsupported response."""


class CHUNIRECTransportError(RuntimeError):
    """Internal transport marker whose public text never includes the cause."""

    def __init__(self, *, timeout: bool = False) -> None:
        super().__init__()
        self.timeout = timeout


def _is_timeout_error(error: object) -> bool:
    """Classify transport/parser timeouts without exposing exception details."""

    return (
        isinstance(error, TimeoutError)
        or "timeout" in type(error).__name__.casefold()
        or "timed out" in str(error).casefold()
    )


@dataclass(frozen=True, slots=True)
class CHUNIRECProfile:
    """Normalized profile values and any profile-provided rating groups."""

    player_name: str | None
    total_rating: int | float | str | None
    updated_at: datetime | None = None
    best: tuple[RatingEntry, ...] = ()
    recent: tuple[RatingEntry, ...] = ()
    new_equivalent: tuple[RatingEntry, ...] = ()
    best_candidate: tuple[RatingEntry, ...] = ()
    best_candidate_sss: tuple[RatingEntry, ...] = ()
    best_outside: tuple[RatingEntry, ...] = ()
    raw_data: Mapping[str, object] = field(default_factory=dict, repr=False)

    @property
    def rating(self) -> int | float | str | None:
        return self.total_rating

    @property
    def new(self) -> tuple[RatingEntry, ...]:
        return self.new_equivalent


@dataclass(frozen=True, slots=True)
class CHUNIRECRatingData:
    """Normalized groups from ``rating_data.json``."""

    best: tuple[RatingEntry, ...] = ()
    recent: tuple[RatingEntry, ...] = ()
    new_equivalent: tuple[RatingEntry, ...] = ()
    best_candidate: tuple[RatingEntry, ...] = ()
    best_candidate_sss: tuple[RatingEntry, ...] = ()
    best_outside: tuple[RatingEntry, ...] = ()
    other: tuple[RatingEntry, ...] = ()
    total_rating: int | float | str | None = None
    updated_at: datetime | None = None
    best_value: int | float | str | None = None
    recent_value: int | float | str | None = None
    raw_data: Mapping[str, object] = field(default_factory=dict, repr=False)

    @property
    def new(self) -> tuple[RatingEntry, ...]:
        return self.new_equivalent

    @property
    def entries(self) -> tuple[RatingEntry, ...]:
        return tuple(
            item
            for group in (
                self.best,
                self.recent,
                self.new_equivalent,
                self.best_candidate,
                self.best_candidate_sss,
                self.best_outside,
                self.other,
            )
            for item in group
        )


@dataclass(frozen=True, slots=True)
class CHUNIRECFetchResult:
    """Secret-free result returned by the client and confirmation collector."""

    success: bool
    state: CollectionState
    failure_reason: CHUNIRECFailureReason | None = None
    snapshot: RatingSnapshot | None = field(default=None, repr=False)
    rate_limit: CHUNIRECRateLimitStatus | None = None
    message: str | None = None
    status_code: int | None = None
    profile: CHUNIRECProfile | None = field(default=None, repr=False)
    rating_data: CHUNIRECRatingData | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.success) is not bool:
            raise ValueError("success must be a boolean")
        if self.success and self.snapshot is None:
            raise ValueError("successful CHUNIREC result requires a snapshot")
        if self.success and self.failure_reason is not None:
            raise ValueError("successful CHUNIREC result cannot have a failure reason")
        if self.message is not None:
            object.__setattr__(self, "message", safe_message(self.message))

    @property
    def reason(self) -> CHUNIRECFailureReason | None:
        return self.failure_reason

    @property
    def collection_reason(self) -> CollectionReason | None:
        return (
            _collection_reason(self.failure_reason)
            if self.failure_reason is not None
            else None
        )

    @property
    def best(self) -> tuple[RatingEntry, ...]:
        return self.rating_data.best if self.rating_data is not None else ()

    @property
    def recent(self) -> tuple[RatingEntry, ...]:
        return self.rating_data.recent if self.rating_data is not None else ()

    @property
    def new_equivalent(self) -> tuple[RatingEntry, ...]:
        return self.rating_data.new_equivalent if self.rating_data is not None else ()

    @property
    def new(self) -> tuple[RatingEntry, ...]:
        return self.new_equivalent

    @property
    def best_candidate(self) -> tuple[RatingEntry, ...]:
        return self.rating_data.best_candidate if self.rating_data is not None else ()

    @property
    def best_candidate_sss(self) -> tuple[RatingEntry, ...]:
        return (
            self.rating_data.best_candidate_sss
            if self.rating_data is not None
            else ()
        )

    @property
    def best_outside(self) -> tuple[RatingEntry, ...]:
        return self.rating_data.best_outside if self.rating_data is not None else ()

    @property
    def waiting_for_manual_action(self) -> bool:
        return self.state is CollectionState.WAITING_FOR_MANUAL_ACTION

    @property
    def waitingForManualAction(self) -> bool:  # noqa: N802 - external event vocabulary
        return self.waiting_for_manual_action


CHUNIRECCollectionResult = CHUNIRECFetchResult
CHUNIRECResult = CHUNIRECFetchResult


def _normal_key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value.strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _coerce_measure(value: object, *, secrets: Sequence[str] = ()) -> int | float | str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        text = redact_text(value.strip(), secrets)
        if not text:
            return None
        numeric = text.replace(",", "")
        try:
            parsed = float(numeric)
        except ValueError:
            return text
        if not math.isfinite(parsed):
            return None
        return int(parsed) if parsed.is_integer() else parsed
    if isinstance(value, Mapping):
        nested = _first_direct(
            value,
            ("value", "score", "rating", "amount", "total"),
        )
        return _coerce_measure(nested, secrets=secrets)
    return None


def _safe_text(value: object, *, secrets: Sequence[str] = ()) -> str | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        nested = _first_direct(value, ("name", "title", "label", "value", "text"))
        if nested is None:
            return None
        return _safe_text(nested, secrets=secrets)
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = redact_text(str(value).strip(), secrets)
        return text or None
    return None


def _normalize_player_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value)
    normalized = " ".join(normalized.split())
    return normalized or None


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        # The current public v2 profile example uses an ISO-like timestamp
        # with unpadded month/day components (``2022-4-14T...+0900``).
        # Normalize only that date prefix; datetime.fromisoformat still
        # validates the calendar date, time, and timezone strictly.
        match = _ISO_DATE_PREFIX.fullmatch(text)
        if match is not None:
            text = (
                f"{match.group('year')}-{int(match.group('month')):02d}-"
                f"{int(match.group('day')):02d}{match.group('rest')}"
            )
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    return None


def _first_direct(mapping: Mapping[object, object], keys: Sequence[str]) -> object | None:
    wanted = {_normal_key(key) for key in keys}
    for raw_key, value in mapping.items():
        if _normal_key(raw_key) in wanted:
            return value
    return None


def _find_value(
    value: object,
    keys: Sequence[str],
    *,
    depth: int = 0,
) -> object | None:
    if depth > 5 or not isinstance(value, Mapping):
        return None
    direct = _first_direct(value, keys)
    if direct is not None:
        return direct
    nested_keys = (
        "profile",
        "player",
        "user",
        "result",
        "data",
        "rating_data",
        "ratingData",
        "records",
    )
    for nested_key in nested_keys:
        nested = _first_direct(value, (nested_key,))
        if isinstance(nested, Mapping):
            found = _find_value(nested, keys, depth=depth + 1)
            if found is not None:
                return found
    return None


def _row_value(
    row: Mapping[object, object],
    keys: Sequence[str],
    *,
    nested: Sequence[str] = (),
) -> object | None:
    direct = _first_direct(row, keys)
    if direct is not None:
        return direct
    for nested_key in nested:
        candidate = _first_direct(row, (nested_key,))
        if isinstance(candidate, Mapping):
            found = _first_direct(candidate, keys)
            if found is not None:
                return found
    return None


def _looks_like_row(value: Mapping[object, object]) -> bool:
    direct_keys = {
        _normal_key(key)
        for key in (
            "id",
            "song_id",
            "songId",
            "music_id",
            "title",
            "name",
            "diff",
            "difficulty",
            "score",
            "rating",
        )
    }
    if any(_normal_key(key) in direct_keys for key in value):
        return True
    return any(
        isinstance(_first_direct(value, (nested_key,)), Mapping)
        for nested_key in ("song", "music", "chart", "record")
    )


def _rows(value: object) -> list[Mapping[object, object]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        if _looks_like_row(value):
            return [value]
        for key in ("records", "entries", "songs", "items", "data", "list", "values"):
            nested = _first_direct(value, (key,))
            if nested is not None:
                return _rows(nested)
        rows: list[Mapping[object, object]] = []
        for nested in value.values():
            rows.extend(_rows(nested))
        return rows
    if isinstance(value, (list, tuple)):
        rows = []
        for item in value:
            if isinstance(item, Mapping):
                rows.extend(_rows(item))
        return rows
    return []


_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "best": ("best", "best_records", "best_list"),
    "recent": ("recent", "recent_records", "recent_list"),
    "new_equivalent": (
        "new",
        "new_equivalent",
        "new_equiv",
        "new_records",
        "recent_new",
        "recent_new_equivalent",
        "recent_or_new",
    ),
    "best_candidate": ("best_candidate", "best_candidates", "candidate"),
    "best_candidate_sss": (
        "best_candidate_sss",
        "best_candidates_sss",
        "candidate_sss",
        "sss_candidate",
        "best_candidate_for_sss",
    ),
    "best_outside": (
        "best_outside",
        "outside_best",
        "best_overflow",
        "best_35_plus",
        "outside",
    ),
}


def _category_value(payload: Mapping[object, object], aliases: Sequence[str]) -> object | None:
    return _find_value(payload, aliases)


def _status_value(
    value: object,
    *,
    label: str,
    secrets: Sequence[str] = (),
) -> str | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        nested = _first_direct(value, ("status", "name", "label", "value", "grade"))
        return _status_value(nested, label=label, secrets=secrets)
    if isinstance(value, bool):
        return label if value else None
    if isinstance(value, (int, float)):
        return label if value == 1 else None
    text = _safe_text(value, secrets=secrets)
    if text is None or text.casefold() in {"false", "no", "none", "null", "0", "未達成"}:
        return None
    return text


def _entry_from_row(
    row: Mapping[object, object],
    *,
    frame: str,
    secrets: Sequence[str] = (),
) -> RatingEntry | None:
    identifier = _safe_text(
        _row_value(
            row,
            ("id", "song_id", "songId", "music_id", "musicId", "song_identifier"),
            nested=("song", "music"),
        ),
        secrets=secrets,
    )
    title = _safe_text(
        _row_value(row, ("title", "name", "song_title", "music_name"), nested=("song", "music")),
        secrets=secrets,
    )
    if identifier is None and title is None:
        return None
    difficulty = _safe_text(
        _row_value(row, ("diff", "difficulty", "difficulty_name", "level_type"), nested=("chart",)),
        secrets=secrets,
    )
    level = _safe_text(
        _row_value(row, ("level", "level_name", "levelString"), nested=("chart",)),
        secrets=secrets,
    )
    chart_constant = _coerce_measure(
        _row_value(row, ("const", "constant", "chart_constant", "level_const"), nested=("chart",)),
        secrets=secrets,
    )
    score = _coerce_measure(
        _row_value(row, ("score", "best_score", "achievement_score")),
        secrets=secrets,
    )
    rating = _coerce_measure(
        _row_value(row, ("rating", "song_rating", "rating_value")),
        secrets=secrets,
    )
    genre = _safe_text(
        _row_value(row, ("genre", "category"), nested=("song", "music")),
        secrets=secrets,
    )
    clear_status = _status_value(
        _row_value(row, ("clear", "clear_status", "clearStatus", "is_clear")),
        label="clear",
        secrets=secrets,
    )
    fc_status = _status_value(
        _row_value(
            row,
            (
                "fc",
                "full_combo",
                "fullcombo",
                "full_combo_status",
                "fullCombo",
                "is_fullcombo",
            ),
        ),
        label="fc",
        secrets=secrets,
    )
    aj_status = _status_value(
        _row_value(
            row,
            ("aj", "all_justice", "allJustice", "all_justice_status", "is_alljustice"),
        ),
        label="aj",
        secrets=secrets,
    )
    fullchain_status = _status_value(
        _row_value(
            row,
            (
                "fullchain",
                "full_chain",
                "fullChain",
                "fullchain_status",
                "is_fullchain",
            ),
        ),
        label="fullchain",
        secrets=secrets,
    )
    statuses: list[str] = []
    for label, status in (
        ("clear", clear_status),
        ("fc", fc_status),
        ("aj", aj_status),
        ("fullchain", fullchain_status),
    ):
        if status is not None:
            statuses.append(label)
            statuses.append(status)
    source_updated_at = _parse_datetime(
        _row_value(
            row,
            ("updated_at", "updatedAt", "last_updated", "played_at"),
            nested=("record", "chart"),
        )
    )
    rank_value = _coerce_measure(_row_value(row, ("rank", "position")), secrets=secrets)
    rank = rank_value if isinstance(rank_value, int) and rank_value > 0 else None
    return RatingEntry(
        game=Game.CHUNITHM,
        song_identifier=identifier,
        title=title,
        difficulty=difficulty,
        frame_membership=frame,
        level=level,
        chart_constant=chart_constant,
        rank=rank,
        score=score,
        rating=rating,
        clear_status=clear_status,
        full_combo_status=fc_status or fullchain_status,
        all_justice_status=aj_status,
        completion_statuses=statuses,
        genre=genre,
        source_updated_at=source_updated_at,
    )


def _parse_group(
    payload: Mapping[object, object],
    group: str,
    *,
    secrets: Sequence[str] = (),
) -> tuple[RatingEntry, ...]:
    value = _category_value(payload, _CATEGORY_ALIASES[group])
    result: list[RatingEntry] = []
    for row in _rows(value):
        entry = _entry_from_row(row, frame=group, secrets=secrets)
        if entry is not None:
            result.append(entry)
    return tuple(result)


def parse_profile(
    payload: Mapping[object, object],
    *,
    secrets: Sequence[str] = (),
) -> CHUNIRECProfile:
    """Parse an official-like profile object without retaining credentials."""

    if not isinstance(payload, Mapping):
        raise CHUNIRECSchemaError
    name_value = _find_value(
        payload,
        ("player_name", "playerName", "name", "user_name", "username", "display_name"),
    )
    rating_value = _find_value(
        payload,
        ("rating", "player_rating", "total_rating", "overall_rating", "rating_total"),
    )
    updated_value = _find_value(payload, ("updated_at", "updatedAt", "last_updated"))
    recognized = any(
        _find_value(payload, keys) is not None
        for keys in (
            ("player_name", "playerName", "name", "user_name", "username", "display_name"),
            ("rating", "player_rating", "total_rating", "overall_rating", "rating_total"),
            tuple(alias for aliases in _CATEGORY_ALIASES.values() for alias in aliases),
        )
    )
    if not recognized:
        raise CHUNIRECSchemaError
    return CHUNIRECProfile(
        player_name=_normalize_player_name(_safe_text(name_value, secrets=secrets)),
        total_rating=_coerce_measure(rating_value, secrets=secrets),
        updated_at=_parse_datetime(updated_value),
        best=_parse_group(payload, "best", secrets=secrets),
        recent=_parse_group(payload, "recent", secrets=secrets),
        new_equivalent=_parse_group(payload, "new_equivalent", secrets=secrets),
        best_candidate=_parse_group(payload, "best_candidate", secrets=secrets),
        best_candidate_sss=_parse_group(payload, "best_candidate_sss", secrets=secrets),
        best_outside=_parse_group(payload, "best_outside", secrets=secrets),
        raw_data=sanitize_for_output(payload, secrets),
    )


def parse_rating_data(
    payload: Mapping[object, object],
    *,
    secrets: Sequence[str] = (),
) -> CHUNIRECRatingData:
    """Parse fixed-size rating arrays, ignoring null slots safely."""

    if not isinstance(payload, Mapping):
        raise CHUNIRECSchemaError
    groups = {
        group: _parse_group(payload, group, secrets=secrets)
        for group in _CATEGORY_ALIASES
    }
    generic_value = _find_value(payload, ("records", "entries", "songs", "items"))
    other: list[RatingEntry] = []
    if not any(groups.values()):
        for row in _rows(generic_value):
            entry = _entry_from_row(row, frame="best", secrets=secrets)
            if entry is not None:
                other.append(entry)
    recognized = any(
        _find_value(payload, aliases) is not None
        for aliases in (*_CATEGORY_ALIASES.values(), ("records", "entries", "songs", "items"))
    )
    if not recognized:
        raise CHUNIRECSchemaError
    return CHUNIRECRatingData(
        best=groups["best"],
        recent=groups["recent"],
        new_equivalent=groups["new_equivalent"],
        best_candidate=groups["best_candidate"],
        best_candidate_sss=groups["best_candidate_sss"],
        best_outside=groups["best_outside"],
        other=tuple(other),
        total_rating=_coerce_measure(
            _find_value(payload, ("rating", "player_rating", "total_rating")),
            secrets=secrets,
        ),
        updated_at=_parse_datetime(_find_value(payload, ("updated_at", "updatedAt"))),
    )


def _strict_measure(
    value: object,
    *,
    field_name: str,
) -> int | float | None:
    """Accept only finite JSON numbers at the strict v2 boundary."""

    if value is None or isinstance(value, bool):
        raise CHUNIRECSchemaError(f"{field_name} must be a number")
    if not isinstance(value, (int, float)):
        raise CHUNIRECSchemaError(f"{field_name} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise CHUNIRECSchemaError(f"{field_name} must be finite")
    if value < 0:
        raise CHUNIRECSchemaError(f"{field_name} must not be negative")
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _strict_profile_rating(value: object) -> int | float:
    """Parse the live v2 profile rating without relaxing chart-row schemas.

    The authenticated ``profile.json`` endpoint currently serializes
    ``rating`` as a decimal string, while the documented rating-data rows use
    JSON numbers.  Accept only a finite, non-negative numeric string at this
    one profile boundary; nested values and arbitrary text remain invalid.
    """

    if isinstance(value, str):
        parsed = _parse_float(value)
        if parsed is None:
            raise CHUNIRECSchemaError("profile.rating must be a number")
        return int(parsed) if parsed.is_integer() else parsed
    parsed = _strict_measure(value, field_name="profile.rating")
    if parsed is None:  # pragma: no cover - guarded by _strict_measure
        raise CHUNIRECSchemaError("profile.rating must be a number")
    return parsed


def _strict_optional_measure(
    row: Mapping[object, object],
    aliases: Sequence[str],
    *,
    field_name: str,
) -> None:
    """Validate an optional numeric row property when it is present."""

    value = _first_direct(row, aliases)
    if value is not None:
        _strict_measure(value, field_name=field_name)


def _strict_optional_boolean(
    row: Mapping[object, object],
    aliases: Sequence[str],
    *,
    field_name: str,
) -> None:
    value = _first_direct(row, aliases)
    if value is not None and type(value) is not bool:
        raise CHUNIRECSchemaError(f"{field_name} must be a boolean")


def _strict_optional_scalar_text(
    row: Mapping[object, object],
    aliases: Sequence[str],
    *,
    field_name: str,
    allow_number: bool = False,
) -> None:
    """Reject nested objects where the v2 row documents a scalar value."""

    value = _first_direct(row, aliases)
    if value is None:
        return
    valid = isinstance(value, str) and bool(value.strip())
    if allow_number:
        valid = valid or (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
        )
    if not valid:
        raise CHUNIRECSchemaError(f"{field_name} must be a scalar")


def _strict_required_text(
    value: object,
    *,
    field_name: str,
    secrets: Sequence[str] = (),
) -> str:
    """Parse one required v2 string without compatibility nesting."""

    if not isinstance(value, str) or not value.strip():
        raise CHUNIRECSchemaError(f"{field_name} must be a non-empty string")
    text = redact_text(value.strip(), secrets)
    if not text:
        raise CHUNIRECSchemaError(f"{field_name} must be a non-empty string")
    return text


def _strict_row(
    row: object,
    *,
    frame: str,
    index: int,
    secrets: Sequence[str] = (),
) -> RatingEntry:
    """Parse one official v2 row and reject silent partial normalization."""

    if not isinstance(row, Mapping):
        raise CHUNIRECSchemaError(f"{frame}[{index}] must be an object or null")
    _strict_required_text(
        _first_direct(row, ("id",)),
        field_name=f"{frame}[{index}].id",
        secrets=secrets,
    )
    _strict_required_text(
        _first_direct(row, ("diff",)),
        field_name=f"{frame}[{index}].diff",
        secrets=secrets,
    )
    _strict_optional_scalar_text(
        row,
        ("title",),
        field_name=f"{frame}[{index}].title",
    )
    _strict_optional_scalar_text(
        row,
        ("level",),
        field_name=f"{frame}[{index}].level",
        allow_number=True,
    )
    _strict_optional_scalar_text(
        row,
        ("genre",),
        field_name=f"{frame}[{index}].genre",
    )
    updated_value = _first_direct(row, ("updated_at",))
    if updated_value is not None and _parse_datetime(updated_value) is None:
        raise CHUNIRECSchemaError(f"{frame}[{index}].updated_at is invalid")
    for aliases, field_name in (
        (("const",), "const"),
        (("score",), "score"),
        (("rating",), "rating"),
        (("achievement",), "achievement"),
        (("technical_score",), "technical_score"),
        (("p_score",), "p_score"),
        (("song_rating",), "song_rating"),
    ):
        _strict_optional_measure(row, aliases, field_name=field_name)
    if not any(
        _first_direct(row, aliases) is not None
        for aliases in (
            ("const",),
            ("score",),
            ("rating",),
            ("achievement",),
            ("technical_score",),
            ("p_score",),
            ("song_rating",),
        )
    ):
        raise CHUNIRECSchemaError(f"{frame}[{index}] requires a rating measure")
    for aliases, field_name in (
        (("is_clear",), "is_clear"),
        (("is_fullcombo",), "is_fullcombo"),
        (("is_alljustice",), "is_alljustice"),
        (("is_fullchain",), "is_fullchain"),
        (("is_const_unknown",), "is_const_unknown"),
        (("is_played",), "is_played"),
    ):
        _strict_optional_boolean(row, aliases, field_name=field_name)
    entry = _entry_from_row(row, frame=frame, secrets=secrets)
    if entry is None:
        raise CHUNIRECSchemaError(f"{frame}[{index}] is not a rating row")
    return entry


def _strict_group(
    payload: Mapping[object, object],
    group: str,
    *,
    frame: str | None = None,
    expected_length: int | None = None,
    max_length: int | None = None,
    require_value: bool = False,
    secrets: Sequence[str] = (),
) -> tuple[tuple[RatingEntry, ...], int | float | None]:
    group_value = _first_direct(payload, (group,))
    if not isinstance(group_value, Mapping):
        raise CHUNIRECSchemaError(f"{group} must be an object")
    entries_value = _first_direct(group_value, ("entries",))
    if not isinstance(entries_value, list):
        raise CHUNIRECSchemaError(f"{group}.entries must be an array")
    if expected_length is not None and len(entries_value) != expected_length:
        raise CHUNIRECSchemaError(
            f"{group}.entries must contain exactly {expected_length} slots"
        )
    if max_length is not None and len(entries_value) > max_length:
        raise CHUNIRECSchemaError(
            f"{group}.entries must contain at most {max_length} slots"
        )
    value = _first_direct(group_value, ("value",))
    if require_value:
        parsed_value = _strict_measure(value, field_name=f"{group}.value")
    elif value is None:
        parsed_value = None
    else:
        parsed_value = _strict_measure(value, field_name=f"{group}.value")
    parsed_entries: list[RatingEntry] = []
    entry_frame = frame or group
    for index, row in enumerate(entries_value):
        if row is None:
            continue
        parsed_entries.append(
            _strict_row(
                row,
                frame=entry_frame,
                index=index,
                secrets=secrets,
            )
        )
    return tuple(parsed_entries), parsed_value


def parse_chunirec_v2_profile(
    payload: Mapping[object, object],
    *,
    secrets: Sequence[str] = (),
) -> CHUNIRECProfile:
    """Strictly parse the top-level object returned by ``profile.json``.

    The compatibility ``parse_profile`` function remains available for older
    fixtures and adapters.  The client uses this function so malformed or
    unexpectedly nested data cannot become a successful snapshot.
    """

    if not isinstance(payload, Mapping):
        raise CHUNIRECSchemaError("profile must be an object")
    name = _normalize_player_name(
        _strict_required_text(
            _first_direct(payload, ("player_name",)),
            field_name="profile.player_name",
            secrets=secrets,
        )
    )
    if name is None:
        raise CHUNIRECSchemaError("profile.player_name is required")
    rating_value = _first_direct(payload, ("rating",))
    rating = _strict_profile_rating(rating_value)
    updated_value = _first_direct(payload, ("updated_at",))
    updated_at = _parse_datetime(updated_value)
    if updated_value is not None and updated_at is None:
        raise CHUNIRECSchemaError("profile.updated_at is invalid")
    return CHUNIRECProfile(
        player_name=name,
        total_rating=rating,
        updated_at=updated_at,
        raw_data=sanitize_for_output(payload, secrets),
    )


def parse_chunirec_v2_rating_data(
    payload: Mapping[object, object],
    *,
    secrets: Sequence[str] = (),
) -> CHUNIRECRatingData:
    """Strictly parse CHUNIREC v2 rating envelopes and fixed slots.

    CHUNIREC documents 30 ``best`` slots and 10 ``recent`` slots.  Empty
    slots are represented by JSON ``null`` and are ignored only after the
    array shape and every non-null row have been validated.  Candidate lists
    intentionally have no fixed length but must still use the documented
    ``entries`` envelope.
    """

    if not isinstance(payload, Mapping):
        raise CHUNIRECSchemaError("rating_data must be an object")
    best, best_value = _strict_group(
        payload,
        "best",
        expected_length=30,
        require_value=True,
        secrets=secrets,
    )
    recent_value = None
    recent: tuple[RatingEntry, ...] = ()
    new_equivalent: tuple[RatingEntry, ...] = ()
    if _first_direct(payload, ("recent",)) is not None:
        recent, recent_value = _strict_group(
            payload,
            "recent",
            expected_length=10,
            require_value=True,
            secrets=secrets,
        )
    elif _first_direct(payload, ("new", "new_equivalent")) is not None:
        new_equivalent, recent_value = _strict_group(
            payload,
            "new" if _first_direct(payload, ("new",)) is not None else "new_equivalent",
            frame="new_equivalent",
            expected_length=None,
            require_value=False,
            secrets=secrets,
        )
    else:
        raise CHUNIRECSchemaError("rating_data requires recent or new")
    if (
        _first_direct(payload, ("recent",)) is not None
        and _first_direct(payload, ("new", "new_equivalent")) is not None
    ):
        new_equivalent, _ = _strict_group(
            payload,
            "new" if _first_direct(payload, ("new",)) is not None else "new_equivalent",
            frame="new_equivalent",
            expected_length=None,
            require_value=False,
            secrets=secrets,
        )
    best_candidate, _ = _strict_group(
        payload,
        "best_candidate",
        max_length=20,
        secrets=secrets,
    )
    best_candidate_sss, _ = _strict_group(
        payload,
        "best_candidate_sss",
        max_length=20,
        secrets=secrets,
    )
    best_outside, _ = _strict_group(
        payload,
        "best_outside",
        max_length=20,
        secrets=secrets,
    )
    total_value = _first_direct(payload, ("rating",))
    total_rating = (
        None
        if total_value is None
        else _strict_measure(total_value, field_name="rating_data.rating")
    )
    updated_value = _first_direct(payload, ("updated_at",))
    updated_at = _parse_datetime(updated_value)
    if updated_value is not None and updated_at is None:
        raise CHUNIRECSchemaError("rating_data.updated_at is invalid")
    return CHUNIRECRatingData(
        best=best,
        recent=recent,
        new_equivalent=new_equivalent,
        best_candidate=best_candidate,
        best_candidate_sss=best_candidate_sss,
        best_outside=best_outside,
        total_rating=total_rating,
        updated_at=updated_at,
        best_value=best_value,
        recent_value=recent_value,
        raw_data=sanitize_for_output(payload, secrets),
    )


# Explicit aliases make the strict boundary easy to discover without making
# the old, permissive fixture parser the default for callers.
parse_v2_profile = parse_chunirec_v2_profile
parse_v2_rating_data = parse_chunirec_v2_rating_data
parse_chunirec_profile_v2 = parse_chunirec_v2_profile
parse_chunirec_rating_data_v2 = parse_chunirec_v2_rating_data


def build_snapshot(
    profile_payload: Mapping[object, object],
    rating_payload: Mapping[object, object],
    *,
    source: str = "chunirec",
    player_name_fallback: str | None = None,
    collected_at: datetime | None = None,
    secrets: Sequence[str] = (),
    strict: bool = False,
) -> tuple[RatingSnapshot, CHUNIRECProfile, CHUNIRECRatingData]:
    """Build a provider-independent snapshot from both API responses."""

    profile_parser = parse_chunirec_v2_profile if strict else parse_profile
    rating_parser = parse_chunirec_v2_rating_data if strict else parse_rating_data
    profile = profile_parser(profile_payload, secrets=secrets)
    ratings = rating_parser(rating_payload, secrets=secrets)
    groups = (
        ("best", ratings.best or profile.best),
        ("recent", ratings.recent or profile.recent),
        ("new_equivalent", ratings.new_equivalent or profile.new_equivalent),
        ("best_candidate", ratings.best_candidate or profile.best_candidate),
        (
            "best_candidate_sss",
            ratings.best_candidate_sss or profile.best_candidate_sss,
        ),
        ("best_outside", ratings.best_outside or profile.best_outside),
        ("other", ratings.other),
    )
    entries = [entry for _, group in groups for entry in group]
    player_name = profile.player_name or redact_text(player_name_fallback or "", secrets) or None
    total_rating = profile.total_rating
    if total_rating is None:
        total_rating = ratings.total_rating
    source_updated_at = profile.updated_at or ratings.updated_at
    raw_data = {
        "profile": sanitize_for_output(profile_payload, secrets),
        "rating_data": sanitize_for_output(rating_payload, secrets),
    }
    moment = collected_at or datetime.now(UTC)
    snapshot = RatingSnapshot(
        game=Game.CHUNITHM,
        entries=entries,
        rating=total_rating,
        source=source,
        player_name=player_name,
        source_updated_at=source_updated_at,
        raw_data=raw_data,
        collected_at=moment,
    )
    return snapshot, profile, ratings


class CHUNIRECParser:
    """Class-style facade for callers that prefer an explicit parser object."""

    parse_profile = staticmethod(parse_profile)
    parse_rating_data = staticmethod(parse_rating_data)
    parse_v2_profile = staticmethod(parse_chunirec_v2_profile)
    parse_v2_rating_data = staticmethod(parse_chunirec_v2_rating_data)
    build_snapshot = staticmethod(build_snapshot)
    parse_snapshot = staticmethod(build_snapshot)


def _coerce_http_response(response: object) -> CHUNIRECResponse:
    if isinstance(response, CHUNIRECResponse):
        return response
    if isinstance(response, tuple) and len(response) == 3:
        status, headers, body = response
        return CHUNIRECResponse(int(status), body, dict(headers))
    status = getattr(response, "status_code", None)
    if status is None:
        raise TypeError
    headers = dict(getattr(response, "headers", {}) or {})
    if hasattr(response, "json") and callable(response.json):
        try:
            body = response.json()
        except Exception as exc:  # the public client classifies it without exposing details
            if _is_timeout_error(exc):
                raise CHUNIRECTransportError(timeout=True) from exc
            raise CHUNIRECJSONError from exc
    elif hasattr(response, "content"):
        body = response.content
    else:
        body = getattr(response, "text", None)
    return CHUNIRECResponse(int(status), body, headers)


def _collection_reason(reason: CHUNIRECFailureReason) -> CollectionReason:
    if reason is CHUNIRECFailureReason.RATE_LIMITED:
        return CollectionReason.RATE_LIMITED
    if reason is CHUNIRECFailureReason.PROVIDER_OUTAGE:
        return CollectionReason.PROVIDER_NETWORK_OUTAGE
    if reason is CHUNIRECFailureReason.TIMEOUT:
        return CollectionReason.TIMEOUT
    if reason in {
        CHUNIRECFailureReason.AUTHENTICATION_REQUIRED,
        CHUNIRECFailureReason.MISSING_TOKEN,
    }:
        return CollectionReason.AUTHENTICATION_REQUIRED
    if reason is CHUNIRECFailureReason.MANUAL_CONFIRMATION_REQUIRED:
        return CollectionReason.MANUAL_CONFIRMATION_REQUIRED
    if reason is CHUNIRECFailureReason.INVALID_IDENTITY:
        return CollectionReason.INVALID_RESPONSE
    return CollectionReason.INVALID_RESPONSE


def _http_failure(status: int) -> CHUNIRECFailureReason:
    if status == 408:
        return CHUNIRECFailureReason.TIMEOUT
    if status in {401, 403}:
        return CHUNIRECFailureReason.AUTHENTICATION_REQUIRED
    if status == 404:
        return CHUNIRECFailureReason.NOT_FOUND
    if status == 429:
        return CHUNIRECFailureReason.RATE_LIMITED
    if status == 503 or status >= 500:
        return CHUNIRECFailureReason.PROVIDER_OUTAGE
    return CHUNIRECFailureReason.INVALID_RESPONSE


def _failure_message(reason: CHUNIRECFailureReason) -> str:
    return {
        CHUNIRECFailureReason.MISSING_TOKEN: "CHUNIREC token is required",
        CHUNIRECFailureReason.INVALID_IDENTITY: "exactly one CHUNIREC user identity is required",
        CHUNIRECFailureReason.AUTHENTICATION_REQUIRED: "CHUNIREC authentication was rejected",
        CHUNIRECFailureReason.NOT_FOUND: "CHUNIREC resource was not found",
        CHUNIRECFailureReason.RATE_LIMITED: "CHUNIREC rate limit reached",
        CHUNIRECFailureReason.PROVIDER_OUTAGE: "CHUNIREC provider is unavailable",
        CHUNIRECFailureReason.MALFORMED_JSON: "CHUNIREC returned malformed JSON",
        CHUNIRECFailureReason.INVALID_SCHEMA: "CHUNIREC returned an unsupported schema",
        CHUNIRECFailureReason.TRANSPORT_ERROR: "CHUNIREC transport failed",
        CHUNIRECFailureReason.TIMEOUT: "CHUNIREC request timed out",
        CHUNIRECFailureReason.MANUAL_CONFIRMATION_REQUIRED:
        "explicit CHUNIREC refresh confirmation is required",
        CHUNIRECFailureReason.INVALID_RESPONSE: "CHUNIREC returned an invalid response",
    }[reason]


class CHUNIRECClient:
    """Synchronous CHUNIREC API client with an injected HTTP transport."""

    game = Game.CHUNITHM

    def __init__(
        self,
        transport: CHUNIRECTransport | Callable[..., object],
        config: CHUNIRECConfig,
    ) -> None:
        self.transport = transport
        self.config = config

    def _request(self, endpoint: str) -> CHUNIRECResponse:
        params = self.config._request_params()
        request = getattr(self.transport, "request", None)
        get = getattr(self.transport, "get", None)
        try:
            if callable(request):
                response = request("GET", endpoint, params=params)
            elif callable(get):
                response = get(endpoint, params=params)
            elif callable(self.transport):
                response = self.transport("GET", endpoint, params=params)
            else:
                raise TypeError
        except CHUNIRECTransportError:
            raise
        except Exception as exc:  # never copy an injected transport's error text
            raise CHUNIRECTransportError(timeout=_is_timeout_error(exc)) from exc
        try:
            return _coerce_http_response(response)
        except CHUNIRECTransportError:
            raise
        except CHUNIRECJSONError:
            raise
        except Exception as exc:
            raise CHUNIRECTransportError(timeout=_is_timeout_error(exc)) from exc

    def fetch_snapshot(
        self,
        *,
        collected_at: datetime | None = None,
    ) -> CHUNIRECFetchResult:
        """Call profile then rating data and return a normalized snapshot."""

        config_failure = self.config.validation_failure()
        if config_failure is not None:
            return CHUNIRECFetchResult(
                False,
                CollectionState.FAILED,
                config_failure,
                message=_failure_message(config_failure),
            )
        try:
            profile_response = self._request(CHUNIREC_PROFILE_ENDPOINT)
        except CHUNIRECJSONError:
            reason = CHUNIRECFailureReason.MALFORMED_JSON
            return CHUNIRECFetchResult(
                False,
                CollectionState.FAILED,
                reason,
                message=_failure_message(reason),
            )
        except CHUNIRECTransportError as exc:
            reason = (
                CHUNIRECFailureReason.TIMEOUT
                if exc.timeout
                else CHUNIRECFailureReason.TRANSPORT_ERROR
            )
            return CHUNIRECFetchResult(
                False,
                CollectionState.FAILED,
                reason,
                message=_failure_message(reason),
            )
        profile_rate_limit = CHUNIRECRateLimitStatus.from_headers(profile_response.headers)
        if profile_response.status_code != 200:
            reason = _http_failure(profile_response.status_code)
            return CHUNIRECFetchResult(
                False,
                CollectionState.FAILED,
                reason,
                rate_limit=profile_rate_limit,
                message=_failure_message(reason),
                status_code=profile_response.status_code,
            )
        try:
            profile_payload = profile_response.json_data()
        except CHUNIRECJSONError:
            reason = CHUNIRECFailureReason.MALFORMED_JSON
            return CHUNIRECFetchResult(
                False,
                CollectionState.FAILED,
                reason,
                rate_limit=profile_rate_limit,
                message=_failure_message(reason),
                status_code=profile_response.status_code,
            )
        if not isinstance(profile_payload, Mapping):
            reason = CHUNIRECFailureReason.INVALID_SCHEMA
            return CHUNIRECFetchResult(
                False,
                CollectionState.FAILED,
                reason,
                rate_limit=profile_rate_limit,
                message=_failure_message(reason),
                status_code=profile_response.status_code,
            )
        try:
            parse_chunirec_v2_profile(
                profile_payload,
                secrets=(self.config.token or "",),
            )
        except CHUNIRECSchemaError:
            reason = CHUNIRECFailureReason.INVALID_SCHEMA
            return CHUNIRECFetchResult(
                False,
                CollectionState.FAILED,
                reason,
                rate_limit=profile_rate_limit,
                message=_failure_message(reason),
                status_code=profile_response.status_code,
            )
        try:
            rating_response = self._request(CHUNIREC_RATING_DATA_ENDPOINT)
        except CHUNIRECJSONError:
            reason = CHUNIRECFailureReason.MALFORMED_JSON
            return CHUNIRECFetchResult(
                False,
                CollectionState.FAILED,
                reason,
                rate_limit=profile_rate_limit,
                message=_failure_message(reason),
            )
        except CHUNIRECTransportError as exc:
            reason = (
                CHUNIRECFailureReason.TIMEOUT
                if exc.timeout
                else CHUNIRECFailureReason.TRANSPORT_ERROR
            )
            return CHUNIRECFetchResult(
                False,
                CollectionState.FAILED,
                reason,
                rate_limit=profile_rate_limit,
                message=_failure_message(reason),
            )
        rating_rate_limit = CHUNIRECRateLimitStatus.from_headers(rating_response.headers)
        rate_limit = rating_rate_limit or profile_rate_limit
        if rating_response.status_code != 200:
            reason = _http_failure(rating_response.status_code)
            return CHUNIRECFetchResult(
                False,
                CollectionState.FAILED,
                reason,
                rate_limit=rate_limit,
                message=_failure_message(reason),
                status_code=rating_response.status_code,
            )
        try:
            rating_payload = rating_response.json_data()
        except CHUNIRECJSONError:
            reason = CHUNIRECFailureReason.MALFORMED_JSON
            return CHUNIRECFetchResult(
                False,
                CollectionState.FAILED,
                reason,
                rate_limit=rate_limit,
                message=_failure_message(reason),
                status_code=rating_response.status_code,
            )
        if not isinstance(rating_payload, Mapping):
            reason = CHUNIRECFailureReason.INVALID_SCHEMA
            return CHUNIRECFetchResult(
                False,
                CollectionState.FAILED,
                reason,
                rate_limit=rate_limit,
                message=_failure_message(reason),
                status_code=rating_response.status_code,
            )
        try:
            snapshot, profile, rating_data = build_snapshot(
                profile_payload,
                rating_payload,
                player_name_fallback=self.config.user_name,
                collected_at=collected_at,
                secrets=(self.config.token or "",),
                strict=True,
            )
        except CHUNIRECSchemaError:
            reason = CHUNIRECFailureReason.INVALID_SCHEMA
            return CHUNIRECFetchResult(
                False,
                CollectionState.FAILED,
                reason,
                rate_limit=rate_limit,
                message=_failure_message(reason),
                status_code=rating_response.status_code,
            )
        except (TypeError, ValueError):
            reason = CHUNIRECFailureReason.INVALID_RESPONSE
            return CHUNIRECFetchResult(
                False,
                CollectionState.FAILED,
                reason,
                rate_limit=rate_limit,
                message=_failure_message(reason),
                status_code=rating_response.status_code,
            )
        return CHUNIRECFetchResult(
            True,
            CollectionState.COLLECTING_DATA,
            snapshot=snapshot,
            rate_limit=rate_limit,
            profile=profile,
            rating_data=rating_data,
            status_code=rating_response.status_code,
        )

    collect = fetch_snapshot


class CHUNIRECCollector:
    """Confirmation-gated CHUNIREC collector facade."""

    game = Game.CHUNITHM

    def __init__(
        self,
        client: CHUNIRECClient,
        *,
        require_refresh_confirmation: bool = True,
    ) -> None:
        self.client = client
        self.require_refresh_confirmation = require_refresh_confirmation
        self._refresh_confirmed = not require_refresh_confirmation

    @property
    def refresh_confirmed(self) -> bool:
        return self._refresh_confirmed

    def handle_event(self, event: CHUNIRECEvent) -> bool:
        if (
            event.event_type is CHUNIRECEventType.REFRESH_CONFIRMED
            and event.explicit_confirmation
        ):
            self._refresh_confirmed = True
            return True
        return False

    feed_event = handle_event

    def confirm_refresh(self) -> CHUNIRECEvent:
        event = CHUNIRECEvent.refresh_confirmed()
        self.handle_event(event)
        return event

    def collect(
        self,
        *,
        event: CHUNIRECEvent | None = None,
        collected_at: datetime | None = None,
    ) -> CHUNIRECFetchResult:
        if event is not None:
            self.handle_event(event)
        if not self._refresh_confirmed:
            reason = CHUNIRECFailureReason.MANUAL_CONFIRMATION_REQUIRED
            return CHUNIRECFetchResult(
                False,
                CollectionState.WAITING_FOR_MANUAL_ACTION,
                reason,
                message=_failure_message(reason),
            )
        return self.client.fetch_snapshot(collected_at=collected_at)


ChunirecClient = CHUNIRECClient
ChunirecCollector = CHUNIRECCollector
ChunirecConfig = CHUNIRECConfig
ChunirecParser = CHUNIRECParser
CHUNIRECAPIClient = CHUNIRECClient


__all__ = [
    "CHUNIREC_API_BASE",
    "CHUNIREC_PROFILE_ENDPOINT",
    "CHUNIREC_RATING_DATA_ENDPOINT",
    "CHUNIRECClient",
    "CHUNIRECAPIClient",
    "CHUNIRECCollectionResult",
    "CHUNIRECResult",
    "CHUNIRECCollector",
    "CHUNIRECConfig",
    "CHUNIRECEvent",
    "CHUNIRECEventType",
    "CHUNIRECFailureReason",
    "CHUNIRECFetchResult",
    "CHUNIRECJSONError",
    "CHUNIRECProfile",
    "CHUNIRECParser",
    "CHUNIRECRateLimitStatus",
    "CHUNIRECRatingData",
    "CHUNIRECResponse",
    "CHUNIRECSchemaError",
    "CHUNIRECTransport",
    "CHUNIRECTransportError",
    "CHUNIREC_V2_RATING_GROUPS",
    "ChunirecClient",
    "ChunirecCollector",
    "ChunirecConfig",
    "ChunirecParser",
    "build_snapshot",
    "parse_chunirec_profile_v2",
    "parse_chunirec_rating_data_v2",
    "parse_chunirec_v2_profile",
    "parse_chunirec_v2_rating_data",
    "parse_profile",
    "parse_rating_data",
    "parse_v2_profile",
    "parse_v2_rating_data",
]
