"""Explicit, event-driven collection state machine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from .enums import (
    CollectionEventType,
    CollectionReason,
    CollectionState,
    Game,
    ManualAction,
)
from .models import (
    CollectionEvent,
    CollectionResult,
    RatingSnapshot,
    RenderedImageResult,
)
from .normalization import ensure_aware_datetime, normalize_optional_text, utc_now

ALLOWED_TRANSITIONS: Mapping[CollectionState, frozenset[CollectionState]] = {
    CollectionState.QUEUED: frozenset(
        {CollectionState.OPENING_PROVIDER, CollectionState.FAILED, CollectionState.CANCELLED}
    ),
    CollectionState.OPENING_PROVIDER: frozenset(
        {
            CollectionState.AUTHENTICATION_REQUIRED,
            CollectionState.WAITING_FOR_MANUAL_ACTION,
            CollectionState.REFRESHING_SOURCE,
            CollectionState.FAILED,
            CollectionState.CANCELLED,
        }
    ),
    CollectionState.AUTHENTICATION_REQUIRED: frozenset(
        {
            CollectionState.WAITING_FOR_MANUAL_ACTION,
            CollectionState.FAILED,
            CollectionState.CANCELLED,
        }
    ),
    CollectionState.WAITING_FOR_MANUAL_ACTION: frozenset(
        {CollectionState.REFRESHING_SOURCE, CollectionState.FAILED, CollectionState.CANCELLED}
    ),
    CollectionState.REFRESHING_SOURCE: frozenset(
        {
            CollectionState.AUTHENTICATION_REQUIRED,
            CollectionState.WAITING_FOR_MANUAL_ACTION,
            CollectionState.COLLECTING_DATA,
            CollectionState.FAILED,
            CollectionState.CANCELLED,
        }
    ),
    CollectionState.COLLECTING_DATA: frozenset(
        {
            CollectionState.AUTHENTICATION_REQUIRED,
            CollectionState.WAITING_FOR_MANUAL_ACTION,
            CollectionState.RENDERING_IMAGE,
            CollectionState.FAILED,
            CollectionState.CANCELLED,
        }
    ),
    CollectionState.RENDERING_IMAGE: frozenset(
        {CollectionState.COMPARING_SNAPSHOT, CollectionState.FAILED, CollectionState.CANCELLED}
    ),
    CollectionState.COMPARING_SNAPSHOT: frozenset(
        {
            CollectionState.READY_TO_POST,
            CollectionState.UNCHANGED,
            CollectionState.FAILED,
            CollectionState.CANCELLED,
        }
    ),
    CollectionState.READY_TO_POST: frozenset(
        {
            CollectionState.POSTING,
            CollectionState.UNCHANGED,
            CollectionState.FAILED,
            CollectionState.CANCELLED,
        }
    ),
    CollectionState.POSTING: frozenset(
        {CollectionState.POSTED, CollectionState.FAILED, CollectionState.CANCELLED}
    ),
    CollectionState.POSTED: frozenset(),
    CollectionState.UNCHANGED: frozenset(),
    CollectionState.FAILED: frozenset(),
    CollectionState.CANCELLED: frozenset(),
}

TERMINAL_STATES = frozenset(
    {
        CollectionState.POSTED,
        CollectionState.UNCHANGED,
        CollectionState.FAILED,
        CollectionState.CANCELLED,
    }
)


class InvalidCollectionTransition(ValueError):
    """Raised when a collection event attempts an invalid state change."""


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


def _state(value: CollectionState | str) -> CollectionState:
    if isinstance(value, CollectionState):
        return value
    for candidate in CollectionState:
        if candidate.value.casefold() == str(value).strip().casefold():
            return candidate
    raise ValueError("unsupported collection state")


def _event_type(value: CollectionEventType | str) -> CollectionEventType:
    if isinstance(value, CollectionEventType):
        return value
    try:
        return CollectionEventType(str(value).strip().casefold())
    except ValueError as exc:
        raise ValueError("unsupported collection event type") from exc


def _default_event_type(state: CollectionState) -> CollectionEventType:
    return {
        CollectionState.AUTHENTICATION_REQUIRED: CollectionEventType.AUTHENTICATION_REQUIRED,
        CollectionState.WAITING_FOR_MANUAL_ACTION: CollectionEventType.MANUAL_ACTION_REQUIRED,
        CollectionState.REFRESHING_SOURCE: CollectionEventType.SOURCE_REFRESHED,
        CollectionState.COLLECTING_DATA: CollectionEventType.DATA_COLLECTED,
        CollectionState.RENDERING_IMAGE: CollectionEventType.IMAGE_RENDERED,
        CollectionState.COMPARING_SNAPSHOT: CollectionEventType.SNAPSHOT_COMPARED,
        CollectionState.READY_TO_POST: CollectionEventType.POST_READY,
        CollectionState.POSTING: CollectionEventType.STATE_TRANSITION,
        CollectionState.POSTED: CollectionEventType.POSTED,
        CollectionState.UNCHANGED: CollectionEventType.UNCHANGED,
        CollectionState.FAILED: CollectionEventType.FAILURE,
        CollectionState.CANCELLED: CollectionEventType.CANCELLED,
    }.get(state, CollectionEventType.STATE_TRANSITION)


@dataclass(frozen=True, slots=True)
class CollectionTrigger:
    """Domain input that explains why a state transition is being made."""

    event_type: CollectionEventType | str = CollectionEventType.STATE_TRANSITION
    reason: CollectionReason | str | None = None
    manual_action: ManualAction | str | None = None
    explicit_confirmation: bool = False
    occurred_at: datetime = field(default_factory=utc_now)
    message: str | None = None

    def __post_init__(self) -> None:
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


class CollectionStateMachine:
    """Mutable orchestration cursor whose history is immutable to callers.

    The machine has no timer or delay-based success path.  Every move is
    caused by a ``CollectionTrigger`` and recorded as a ``CollectionEvent``.
    In particular, leaving ``WAITING_FOR_MANUAL_ACTION`` requires an explicit
    ``MANUAL_ACTION_CONFIRMED`` trigger.
    """

    def __init__(
        self,
        *,
        source: str = "unknown",
        game: Game | str = Game.MAIMAI,
        initial_state: CollectionState | str = CollectionState.QUEUED,
    ) -> None:
        from .normalization import normalize_game, normalize_text

        self.source = normalize_text(source, field_name="source")
        self.game = normalize_game(game)
        self._state = _state(initial_state)
        self._events: list[CollectionEvent] = []

    @property
    def state(self) -> CollectionState:
        return self._state

    @property
    def events(self) -> tuple[CollectionEvent, ...]:
        return tuple(self._events)

    @property
    def is_terminal(self) -> bool:
        return self._state in TERMINAL_STATES

    def allowed_next_states(self) -> frozenset[CollectionState]:
        return ALLOWED_TRANSITIONS[self._state]

    def can_transition(
        self,
        next_state: CollectionState | str,
        *,
        trigger: CollectionTrigger | None = None,
        explicit_confirmation: bool = False,
    ) -> bool:
        try:
            target = _state(next_state)
            candidate = trigger
            if candidate is None and explicit_confirmation:
                candidate = CollectionTrigger(
                    event_type=CollectionEventType.MANUAL_ACTION_CONFIRMED,
                    explicit_confirmation=True,
                )
            self._validate_transition(target, candidate)
        except (InvalidCollectionTransition, ValueError):
            return False
        return True

    def _validate_transition(
        self,
        target: CollectionState,
        trigger: CollectionTrigger | None,
    ) -> None:
        if target not in ALLOWED_TRANSITIONS[self._state]:
            raise InvalidCollectionTransition(
                f"cannot transition from {self._state.value} to {target.value}"
            )
        if (
            self._state is CollectionState.WAITING_FOR_MANUAL_ACTION
            and target is CollectionState.REFRESHING_SOURCE
        ):
            if (
                trigger is None
                or not trigger.explicit_confirmation
                or trigger.event_type is not CollectionEventType.MANUAL_ACTION_CONFIRMED
            ):
                raise InvalidCollectionTransition(
                    "manual action must be explicitly confirmed before refreshing"
                )
        if self._state in {
            CollectionState.AUTHENTICATION_REQUIRED,
            CollectionState.WAITING_FOR_MANUAL_ACTION,
        } and target in {CollectionState.POSTED, CollectionState.UNCHANGED}:
            raise InvalidCollectionTransition(
                "manual or authentication states cannot transition to success"
            )
        if target in {CollectionState.POSTED, CollectionState.UNCHANGED} and trigger is not None:
            if trigger.reason is not None:
                raise InvalidCollectionTransition(
                    "a failure reason cannot transition to a successful state"
                )

    def transition(
        self,
        next_state: CollectionState | str,
        *,
        trigger: CollectionTrigger | None = None,
        reason: CollectionReason | str | None = None,
        manual_action: ManualAction | str | None = None,
        explicit_confirmation: bool = False,
        occurred_at: datetime | str | None = None,
        message: str | None = None,
    ) -> CollectionEvent:
        """Apply and record one event-driven transition.

        ``explicit_confirmation`` is accepted as a convenience for adapters,
        but it is converted into a recorded manual-confirmation trigger.  A
        bare call from a waiting state therefore remains invalid.
        """

        target = _state(next_state)
        supplied_details = any(
            value is not None
            for value in (reason, manual_action, occurred_at, message)
        )
        if trigger is not None and (supplied_details or explicit_confirmation):
            raise ValueError("use trigger or transition details, not both")
        if trigger is None:
            trigger = CollectionTrigger(
                event_type=(
                    CollectionEventType.MANUAL_ACTION_CONFIRMED
                    if explicit_confirmation
                    else _default_event_type(target)
                ),
                reason=reason,
                manual_action=manual_action,
                explicit_confirmation=explicit_confirmation,
                occurred_at=occurred_at or utc_now(),
                message=message,
            )
        self._validate_transition(target, trigger)
        event = CollectionEvent(
            source=self.source,
            game=self.game,
            previous_state=self._state,
            state=target,
            event_type=trigger.event_type,
            occurred_at=trigger.occurred_at,
            reason=trigger.reason,
            manual_action=trigger.manual_action,
            explicit_confirmation=trigger.explicit_confirmation,
            message=trigger.message,
        )
        self._state = target
        self._events.append(event)
        return event

    def confirm_manual_action(
        self,
        action: ManualAction | str,
        *,
        next_state: CollectionState | str = CollectionState.REFRESHING_SOURCE,
        occurred_at: datetime | str | None = None,
        message: str | None = None,
    ) -> CollectionEvent:
        """Record the explicit confirmation required to resume collection."""

        return self.transition(
            next_state,
            trigger=CollectionTrigger(
                event_type=CollectionEventType.MANUAL_ACTION_CONFIRMED,
                manual_action=action,
                explicit_confirmation=True,
                occurred_at=occurred_at or utc_now(),
                message=message,
            ),
        )

    def fail(
        self,
        reason: CollectionReason | str = CollectionReason.UNKNOWN,
        *,
        message: str | None = None,
        occurred_at: datetime | str | None = None,
    ) -> CollectionEvent:
        """Enter terminal failure with an explicit reason."""

        return self.transition(
            CollectionState.FAILED,
            reason=reason,
            occurred_at=occurred_at,
            message=message,
        )

    def timeout(self, *, message: str | None = None) -> CollectionEvent:
        """Record timeout as failure; it can never be converted to success."""

        return self.fail(CollectionReason.TIMEOUT, message=message)

    def cancel(
        self,
        *,
        reason: CollectionReason | str = CollectionReason.UNKNOWN,
        message: str | None = None,
        occurred_at: datetime | str | None = None,
    ) -> CollectionEvent:
        """Enter terminal cancellation with a recorded reason."""

        return self.transition(
            CollectionState.CANCELLED,
            reason=reason,
            occurred_at=occurred_at,
            message=message,
        )

    def result(
        self,
        *,
        snapshot: RatingSnapshot | None = None,
        rendered_image: RenderedImageResult | None = None,
    ) -> CollectionResult:
        """Build a detached result once the cursor reaches a terminal state."""

        if not self.is_terminal:
            raise ValueError("collection result requires a terminal state")
        successful_state = self._state in {
            CollectionState.POSTED,
            CollectionState.UNCHANGED,
        }
        if successful_state:
            if snapshot is None:
                raise ValueError("successful collection result requires a snapshot")
            metadata = rendered_image.metadata if rendered_image is not None else None
            sha256 = metadata.image_sha256 if metadata is not None else None
            if (
                rendered_image is None
                or not rendered_image.success
                or metadata is None
                or sha256 is None
                or len(sha256) != 64
                or any(character not in "0123456789abcdef" for character in sha256)
                or metadata.mime_type not in {"image/png", "image/jpeg"}
                or metadata.width is None
                or metadata.width < 1
                or metadata.height is None
                or metadata.height < 1
                or metadata.byte_size is None
                or metadata.byte_size < 1
            ):
                raise ValueError(
                    "successful collection result requires verified rendered image evidence"
                )
        failure_reason = self._events[-1].reason if self._state is CollectionState.FAILED else None
        return CollectionResult(
            source=self.source,
            game=self.game,
            final_state=self._state,
            success=successful_state,
            events=self.events,
            snapshot=snapshot,
            rendered_image=rendered_image,
            failure_reason=failure_reason,
        )
