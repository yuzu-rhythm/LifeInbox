"""Immutable-ish domain records for rhythm-rating collection and publication."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .enums import (
    CollectionEventType,
    CollectionReason,
    CollectionState,
    Game,
    ManualAction,
)
from .normalization import (
    canonical_json,
    canonical_measure,
    ensure_aware_datetime,
    normalize_game,
    normalize_measure,
    normalize_optional_text,
    normalize_text,
    utc_now,
)

type RatingValue = int | float | str | None


def _reason(value: CollectionReason | str | None) -> CollectionReason | None:
    if value is None or isinstance(value, CollectionReason):
        return value
    try:
        return CollectionReason(str(value).strip().casefold())
    except ValueError as exc:
        raise ValueError("unsupported collection reason") from exc


def _manual_action(value: ManualAction | str | None) -> ManualAction | None:
    if value is None or isinstance(value, ManualAction):
        return value
    try:
        return ManualAction(str(value).strip().casefold())
    except ValueError as exc:
        raise ValueError("unsupported manual action") from exc


def _event_type(value: CollectionEventType | str) -> CollectionEventType:
    if isinstance(value, CollectionEventType):
        return value
    try:
        return CollectionEventType(str(value).strip().casefold())
    except ValueError as exc:
        raise ValueError("unsupported collection event type") from exc


def _state(value: CollectionState | str) -> CollectionState:
    if isinstance(value, CollectionState):
        return value
    try:
        return CollectionState(str(value).strip())
    except ValueError:
        normalized = str(value).strip().casefold()
        for candidate in CollectionState:
            if candidate.value.casefold() == normalized:
                return candidate
        raise ValueError("unsupported collection state") from None


@dataclass(frozen=True, slots=True)
class RatingEntry:
    """One normalized chart/rating row from a provider snapshot.

    The raw provider page is intentionally not represented here.  Only
    presentation-safe values that participate in semantic comparison cross
    this boundary.
    """

    game: Game | str
    song_identifier: str | None = None
    title: str | None = None
    normalized_title: str | None = None
    rating: RatingValue = None
    frame_membership: str | None = None
    difficulty: str | None = None
    chart_type: str | None = None
    level: str | None = None
    chart_constant: RatingValue = None
    rank: int | None = None
    score: RatingValue = None
    achievement: RatingValue = None
    technical_score: RatingValue = None
    p_score: RatingValue = None
    song_rating: RatingValue = None
    completion_status: str | None = None
    completion_statuses: Iterable[str] = ()
    clear_status: str | None = None
    full_combo_status: str | None = None
    all_justice_status: str | None = None
    sync_status: str | None = None
    full_bell_status: str | None = None
    all_break_status: str | None = None
    genre: str | None = None
    source_updated_at: datetime | str | None = None

    def __post_init__(self) -> None:
        normalized_game = normalize_game(self.game)
        identifier = normalize_optional_text(
            self.song_identifier,
            field_name="song_identifier",
            casefold=True,
        )
        title = normalize_optional_text(self.title, field_name="title")
        normalized_title = normalize_optional_text(
            self.normalized_title,
            field_name="normalized_title",
            casefold=True,
        )
        if normalized_title is None and title is not None:
            normalized_title = normalize_text(
                title,
                field_name="title",
                casefold=True,
            )
        if identifier is None and normalized_title is None:
            raise ValueError("rating entry requires song_identifier or normalized_title")

        statuses: list[str] = []
        normalized_completion_status: str | None = None
        supplied_statuses = self.completion_statuses
        if supplied_statuses is not None:
            if isinstance(supplied_statuses, str):
                supplied_statuses = (supplied_statuses,)
            try:
                for status in supplied_statuses:
                    statuses.append(
                        normalize_text(
                            status,
                            field_name="completion_status",
                            casefold=True,
                        )
                    )
            except TypeError as exc:
                raise ValueError("completion_statuses must be iterable") from exc
        if self.completion_status is not None:
            normalized_completion_status = normalize_text(
                self.completion_status,
                field_name="completion_status",
                casefold=True,
            )
            statuses.append(normalized_completion_status)

        object.__setattr__(self, "game", normalized_game)
        object.__setattr__(self, "song_identifier", identifier)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "normalized_title", normalized_title)
        object.__setattr__(
            self,
            "rating",
            normalize_measure(self.rating, field_name="rating"),
        )
        object.__setattr__(
            self,
            "frame_membership",
            normalize_optional_text(
                self.frame_membership,
                field_name="frame_membership",
                casefold=True,
            ),
        )
        object.__setattr__(
            self,
            "difficulty",
            normalize_optional_text(
                self.difficulty,
                field_name="difficulty",
                casefold=True,
            ),
        )
        object.__setattr__(
            self,
            "chart_type",
            normalize_optional_text(
                self.chart_type,
                field_name="chart_type",
                casefold=True,
            ),
        )
        object.__setattr__(
            self,
            "level",
            normalize_optional_text(self.level, field_name="level"),
        )
        object.__setattr__(
            self,
            "chart_constant",
            normalize_measure(self.chart_constant, field_name="chart_constant"),
        )
        if self.rank is not None and (type(self.rank) is not int or self.rank < 1):
            raise ValueError("rank must be a positive integer")
        for field_name in (
            "score",
            "achievement",
            "technical_score",
            "p_score",
            "song_rating",
        ):
            object.__setattr__(
                self,
                field_name,
                normalize_measure(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(self, "completion_status", normalized_completion_status)
        for field_name in (
            "clear_status",
            "full_combo_status",
            "all_justice_status",
            "sync_status",
            "full_bell_status",
            "all_break_status",
        ):
            value = normalize_optional_text(
                getattr(self, field_name), field_name=field_name, casefold=True
            )
            object.__setattr__(self, field_name, value)
            if value is not None:
                statuses.append(f"{field_name}:{value}")
        object.__setattr__(
            self,
            "genre",
            normalize_optional_text(self.genre, field_name="genre"),
        )
        if self.source_updated_at is not None:
            object.__setattr__(
                self,
                "source_updated_at",
                ensure_aware_datetime(
                    self.source_updated_at, field_name="source_updated_at"
                ),
            )
        object.__setattr__(self, "completion_statuses", tuple(sorted(set(statuses))))

    @property
    def song_title(self) -> str | None:
        """Display-title alias useful to provider adapters."""

        return self.title

    @property
    def frame(self) -> str | None:
        """Short alias for the source frame/list membership."""

        return self.frame_membership

    @property
    def technical(self) -> RatingValue:
        return self.technical_score

    @property
    def pscore(self) -> RatingValue:
        return self.p_score

    def semantic_payload(self) -> dict[str, object]:
        """Return only fields whose meaning affects snapshot identity."""

        song: dict[str, str]
        if self.song_identifier is not None:
            # Keep the stable provider identifier and the normalized title.
            # The title is material display/data content: a provider changing
            # metadata for the same ID must not silently look unchanged.
            song = {
                "song_identifier": self.song_identifier,
                "normalized_title": self.normalized_title or "",
            }
        else:
            song = {"normalized_title": self.normalized_title or ""}
        return {
            "game": self.game.value,
            "rating": canonical_measure(self.rating),
            "frame_membership": self.frame_membership,
            "song": song,
            "difficulty": self.difficulty,
            "chart_type": self.chart_type,
            "level": self.level,
            "chart_constant": canonical_measure(self.chart_constant),
            "score": canonical_measure(self.score),
            "achievement": canonical_measure(self.achievement),
            "technical_score": canonical_measure(self.technical_score),
            "p_score": canonical_measure(self.p_score),
            "song_rating": canonical_measure(self.song_rating),
            "completion_statuses": list(self.completion_statuses),
            "genre": self.genre,
        }

    def semantic_sort_key(self) -> str:
        return canonical_json(self.semantic_payload())


@dataclass(frozen=True, slots=True)
class RatingSnapshot:
    """A provider-independent collection of normalized rating entries."""

    game: Game | str
    entries: Iterable[RatingEntry | Mapping[str, object]] = ()
    rating: RatingValue = None
    source: str = "unknown"
    player_name: str | None = None
    source_updated_at: datetime | str | None = None
    raw_data: Mapping[str, object] | None = None
    collected_at: datetime = field(default_factory=utc_now)
    generated_at: datetime | str | None = None
    snapshot_id: str | None = None

    def __post_init__(self) -> None:
        normalized_game = normalize_game(self.game)
        normalized_source = normalize_text(self.source, field_name="source")
        normalized_entries: list[RatingEntry] = []
        try:
            source_entries = self.entries
            if source_entries is None:
                source_entries = ()
            for entry in source_entries:
                if isinstance(entry, RatingEntry):
                    normalized_entries.append(entry)
                elif isinstance(entry, Mapping):
                    normalized_entries.append(RatingEntry(**entry))
                else:
                    raise ValueError("entries must contain RatingEntry values")
        except TypeError as exc:
            raise ValueError("entries must be iterable") from exc
        if any(entry.game != normalized_game for entry in normalized_entries):
            raise ValueError("all rating entries must use the snapshot game")

        object.__setattr__(self, "game", normalized_game)
        object.__setattr__(self, "source", normalized_source)
        object.__setattr__(
            self,
            "player_name",
            normalize_optional_text(self.player_name, field_name="player_name"),
        )
        object.__setattr__(
            self,
            "entries",
            tuple(sorted(normalized_entries, key=RatingEntry.semantic_sort_key)),
        )
        object.__setattr__(
            self,
            "rating",
            normalize_measure(self.rating, field_name="rating"),
        )
        object.__setattr__(
            self,
            "collected_at",
            ensure_aware_datetime(self.collected_at, field_name="collected_at"),
        )
        if self.generated_at is not None:
            object.__setattr__(
                self,
                "generated_at",
                ensure_aware_datetime(self.generated_at, field_name="generated_at"),
            )
        if self.source_updated_at is not None:
            object.__setattr__(
                self,
                "source_updated_at",
                ensure_aware_datetime(
                    self.source_updated_at, field_name="source_updated_at"
                ),
            )
        if self.raw_data is not None:
            if not isinstance(self.raw_data, Mapping):
                raise ValueError("raw_data must be a mapping")
            object.__setattr__(self, "raw_data", dict(self.raw_data))
        object.__setattr__(
            self,
            "snapshot_id",
            normalize_optional_text(self.snapshot_id, field_name="snapshot_id"),
        )

    @property
    def overall_rating(self) -> RatingValue:
        return self.rating

    @property
    def semantic_fingerprint(self) -> str:
        from .fingerprint import semantic_fingerprint

        return semantic_fingerprint(self)

    @property
    def fingerprint(self) -> str:
        return self.semantic_fingerprint

    def semantic_payload(self) -> dict[str, object]:
        """Return a canonical payload without timestamps or generated IDs."""

        return {
            "game": self.game.value,
            "rating": canonical_measure(self.rating),
            "entries": [entry.semantic_payload() for entry in self.entries],
        }


@dataclass(frozen=True, slots=True)
class RenderedImageMetadata:
    """Presentation metadata for a rendered snapshot image."""

    path: str | Path
    sha256: str | None = None
    mime_type: str = "image/png"
    width: int | None = None
    height: int | None = None
    byte_size: int | None = None
    compression: str | None = None
    rendered_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        path = normalize_text(str(self.path), field_name="image_path")
        sha256 = normalize_optional_text(
            self.sha256,
            field_name="image_sha256",
            casefold=True,
        )
        mime_type = normalize_text(self.mime_type, field_name="mime_type", casefold=True)
        for field_name in ("width", "height", "byte_size"):
            value = getattr(self, field_name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{field_name} must be a non-negative integer")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "sha256", sha256)
        object.__setattr__(self, "mime_type", mime_type)
        object.__setattr__(
            self,
            "rendered_at",
            ensure_aware_datetime(self.rendered_at, field_name="rendered_at"),
        )

    @property
    def image_path(self) -> str:
        return self.path

    @property
    def image_sha256(self) -> str | None:
        return self.sha256


@dataclass(frozen=True, slots=True)
class RenderedImageResult:
    """Success/failure result from an image renderer."""

    success: bool
    metadata: RenderedImageMetadata | None = None
    failure_reason: CollectionReason | str | None = None
    message: str | None = None
    rendered_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if type(self.success) is not bool:
            raise ValueError("success must be a boolean")
        if self.success and self.metadata is None:
            raise ValueError("a successful image result requires metadata")
        normalized_reason = _reason(self.failure_reason)
        if self.success and normalized_reason is not None:
            raise ValueError("a successful image result cannot have a failure reason")
        object.__setattr__(self, "failure_reason", normalized_reason)
        object.__setattr__(
            self,
            "message",
            normalize_optional_text(self.message, field_name="message"),
        )
        object.__setattr__(
            self,
            "rendered_at",
            ensure_aware_datetime(self.rendered_at, field_name="rendered_at"),
        )

    @property
    def image_path(self) -> str | None:
        return self.metadata.image_path if self.metadata is not None else None

    @property
    def image_sha256(self) -> str | None:
        return self.metadata.image_sha256 if self.metadata is not None else None


@dataclass(frozen=True, slots=True)
class CollectionRequest:
    """Safe input envelope passed from a scheduler to a game collector."""

    source: str
    game: Game | str
    requested_at: datetime = field(default_factory=utc_now)
    dry_run: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", normalize_text(self.source, field_name="source"))
        object.__setattr__(self, "game", normalize_game(self.game))
        object.__setattr__(
            self,
            "requested_at",
            ensure_aware_datetime(self.requested_at, field_name="requested_at"),
        )
        if type(self.dry_run) is not bool:
            raise ValueError("dry_run must be a boolean")


@dataclass(frozen=True, slots=True)
class CollectionEvent:
    """Recorded state-machine event with an explicit reason/action."""

    source: str
    game: Game | str
    previous_state: CollectionState | str
    state: CollectionState | str
    event_type: CollectionEventType | str = CollectionEventType.STATE_TRANSITION
    occurred_at: datetime = field(default_factory=utc_now)
    reason: CollectionReason | str | None = None
    manual_action: ManualAction | str | None = None
    explicit_confirmation: bool = False
    message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", normalize_text(self.source, field_name="source"))
        object.__setattr__(self, "game", normalize_game(self.game))
        object.__setattr__(self, "previous_state", _state(self.previous_state))
        object.__setattr__(self, "state", _state(self.state))
        object.__setattr__(self, "event_type", _event_type(self.event_type))
        object.__setattr__(self, "reason", _reason(self.reason))
        object.__setattr__(self, "manual_action", _manual_action(self.manual_action))
        if type(self.explicit_confirmation) is not bool:
            raise ValueError("explicit_confirmation must be a boolean")
        object.__setattr__(
            self,
            "occurred_at",
            ensure_aware_datetime(self.occurred_at, field_name="occurred_at"),
        )
        object.__setattr__(
            self,
            "message",
            normalize_optional_text(self.message, field_name="message"),
        )

    @property
    def from_state(self) -> CollectionState:
        return self.previous_state

    @property
    def to_state(self) -> CollectionState:
        return self.state


RatingRecord = RatingEntry
Snapshot = RatingSnapshot
ImageMetadata = RenderedImageMetadata
ImageResult = RenderedImageResult


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """Terminal or intermediate result detached from provider internals."""

    source: str
    game: Game | str
    final_state: CollectionState | str
    success: bool
    events: Iterable[CollectionEvent] = ()
    snapshot: RatingSnapshot | None = None
    rendered_image: RenderedImageResult | None = None
    failure_reason: CollectionReason | str | None = None

    def __post_init__(self) -> None:
        normalized_state = _state(self.final_state)
        normalized_reason = _reason(self.failure_reason)
        if type(self.success) is not bool:
            raise ValueError("success must be a boolean")
        success_states = {CollectionState.POSTED, CollectionState.UNCHANGED}
        if self.success != (normalized_state in success_states):
            raise ValueError("success must match the collection final state")
        if normalized_state is CollectionState.FAILED and normalized_reason is None:
            normalized_reason = CollectionReason.UNKNOWN
        try:
            normalized_events = tuple(self.events)
        except TypeError as exc:
            raise ValueError("events must be iterable") from exc
        object.__setattr__(self, "source", normalize_text(self.source, field_name="source"))
        object.__setattr__(self, "game", normalize_game(self.game))
        object.__setattr__(self, "final_state", normalized_state)
        object.__setattr__(self, "events", normalized_events)
        object.__setattr__(self, "failure_reason", normalized_reason)

    @property
    def is_terminal(self) -> bool:
        return self.final_state in {
            CollectionState.POSTED,
            CollectionState.UNCHANGED,
            CollectionState.FAILED,
            CollectionState.CANCELLED,
        }
