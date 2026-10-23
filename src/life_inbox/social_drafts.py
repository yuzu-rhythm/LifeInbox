"""Local-first social-draft registration and target selection.

This service only writes SQLite state.  It intentionally has no transport,
OAuth, credential, or external-network boundary.  Selecting X or Instagram
records a local outbox choice and never publishes anything by itself.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .attachments import SUPPORTED_IMAGE_TYPES
from .models import Event
from .social_models import SocialDraft, SocialDraftState, SocialDraftTarget

SOCIAL_TARGETS = frozenset(target.value for target in SocialDraftTarget)
# AttachmentStore currently persists only image MIME types.  Keep the social
# gate aligned with that actual local storage boundary until video capture is
# implemented separately and safely.
SOCIAL_MEDIA_PREFIXES = tuple(SUPPORTED_IMAGE_TYPES)

SOCIAL_TARGET_LABELS = {
    SocialDraftTarget.PENDING.value: "未選択",
    SocialDraftTarget.X.value: "X",
    SocialDraftTarget.INSTAGRAM.value: "Instagram",
    SocialDraftTarget.BOTH.value: "両方",
    SocialDraftTarget.HOLD.value: "保留",
}

SOCIAL_STATE_LABELS = {
    SocialDraftState.PENDING.value: "対象未選択",
    SocialDraftState.READY.value: "ローカル出力候補",
    SocialDraftState.BLOCKED.value: "メディア不足で停止",
    SocialDraftState.HELD.value: "保留",
    SocialDraftState.POSTED.value: "外部投稿記録あり",
    SocialDraftState.ERROR.value: "エラー・再確認待ち",
}


def _utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _normalize_target(target: str | SocialDraftTarget) -> str:
    value = getattr(target, "value", target)
    normalized = str(value).strip().casefold()
    if normalized not in SOCIAL_TARGETS:
        raise ValueError("unsupported social draft target")
    return normalized


def _normalize_user_id(user_id: str | int) -> str:
    if isinstance(user_id, bool):
        raise ValueError("selected_by_user_id must be a non-empty identifier")
    value = str(user_id).strip()
    if not value or len(value) > 64:
        raise ValueError("selected_by_user_id must be a non-empty identifier")
    return value


def _normalize_media_type(value: str | None) -> str:
    return (value or "").lower().split(";", 1)[0].strip()


def event_has_media(event: Event) -> bool:
    """Return whether the Event has a saved image Attachment row."""

    return any(
        _normalize_media_type(getattr(attachment, "content_type", None))
        in SOCIAL_MEDIA_PREFIXES
        for attachment in (getattr(event, "attachments", None) or [])
    )


def event_content_fingerprint(event: Event) -> str:
    """Hash the local Event text and saved attachment metadata only."""

    digest = hashlib.sha256()

    def add(value: Any) -> None:
        encoded = str(value or "").encode("utf-8", errors="replace")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)

    add(event.raw_text or "")
    attachments = sorted(
        getattr(event, "attachments", None) or [],
        key=lambda item: (str(getattr(item, "discord_attachment_id", "")), getattr(item, "id", 0)),
    )
    for attachment in attachments:
        add(getattr(attachment, "discord_attachment_id", ""))
        add(_normalize_media_type(getattr(attachment, "content_type", None)))
        add(getattr(attachment, "original_filename", ""))
        add(getattr(attachment, "sha256", ""))
    return digest.hexdigest()


def state_for_target(target: str | SocialDraftTarget, *, has_media: bool) -> str:
    """Map a local target choice to a safe, non-publication state."""

    normalized = _normalize_target(target)
    if normalized == SocialDraftTarget.PENDING.value:
        return SocialDraftState.PENDING.value
    if normalized == SocialDraftTarget.HOLD.value:
        return SocialDraftState.HELD.value
    if normalized in {
        SocialDraftTarget.INSTAGRAM.value,
        SocialDraftTarget.BOTH.value,
    } and not has_media:
        return SocialDraftState.BLOCKED.value
    return SocialDraftState.READY.value


@dataclass(frozen=True, slots=True)
class SocialDraftResult:
    """Detached, secret-free view of one durable social draft row."""

    id: int
    event_id: int
    target: str
    state: str
    has_media: bool
    content_fingerprint: str
    selected_by_user_id: str | None
    discord_action_channel_id: str | None
    discord_action_message_id: str | None
    revision: int
    selected_at: datetime | None
    published_at: datetime | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    created: bool = False
    changed: bool = False

    @property
    def selected_by(self) -> str | None:
        return self.selected_by_user_id

    @property
    def action_channel_id(self) -> str | None:
        return self.discord_action_channel_id

    @property
    def action_message_id(self) -> str | None:
        return self.discord_action_message_id

    @property
    def last_error(self) -> str | None:
        return self.error

    @property
    def target_label(self) -> str:
        return SOCIAL_TARGET_LABELS.get(self.target, self.target)

    @property
    def state_label(self) -> str:
        return SOCIAL_STATE_LABELS.get(self.state, self.state)


def _result(row: SocialDraft, *, created: bool = False, changed: bool = False) -> SocialDraftResult:
    return SocialDraftResult(
        id=row.id,
        event_id=row.event_id,
        target=str(row.target),
        state=str(row.state),
        has_media=bool(row.has_media),
        content_fingerprint=row.content_fingerprint,
        selected_by_user_id=row.selected_by_user_id,
        discord_action_channel_id=row.discord_action_channel_id,
        discord_action_message_id=row.discord_action_message_id,
        revision=row.revision,
        selected_at=row.selected_at,
        published_at=row.published_at,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created=created,
        changed=changed,
    )


class SocialDraftService:
    """Persist one idempotent social-draft row per Event."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.now = now or _utc_now_naive

    @staticmethod
    def _event(session: Session, event_id: int) -> Event:
        event = session.scalar(
            select(Event)
            .where(Event.id == event_id)
            .options(selectinload(Event.attachments))
        )
        if event is None:
            raise ValueError(f"event not found: {event_id}")
        return event

    def _new_row(self, event: Event, fingerprint: str, has_media: bool) -> SocialDraft:
        return SocialDraft(
            event_id=event.id,
            target=SocialDraftTarget.PENDING.value,
            state=SocialDraftState.PENDING.value,
            has_media=has_media,
            content_fingerprint=fingerprint,
            revision=1,
        )

    def _reset_for_content_change(
        self,
        row: SocialDraft,
        *,
        fingerprint: str,
        has_media: bool,
    ) -> None:
        row.target = SocialDraftTarget.PENDING.value
        row.state = SocialDraftState.PENDING.value
        row.has_media = has_media
        row.content_fingerprint = fingerprint
        row.selected_by_user_id = None
        row.selected_at = None
        row.published_at = None
        row.error = None
        # The old action message contains the old content.  Clearing its
        # mapping makes a subsequent route send a fresh, auditable action card.
        row.discord_action_channel_id = None
        row.discord_action_message_id = None
        row.revision += 1
        row.updated_at = self.now()

    def register_event(self, event_id: int) -> SocialDraftResult:
        """Create or refresh the local row without changing an identical choice."""

        session = self.session_factory()
        try:
            event = self._event(session, event_id)
            fingerprint = event_content_fingerprint(event)
            has_media = event_has_media(event)
            row = session.scalar(
                select(SocialDraft).where(SocialDraft.event_id == event_id)
            )
            created = row is None
            changed = False
            if row is None:
                row = self._new_row(event, fingerprint, has_media)
                session.add(row)
                session.flush()
            elif row.content_fingerprint != fingerprint:
                self._reset_for_content_change(
                    row,
                    fingerprint=fingerprint,
                    has_media=has_media,
                )
                changed = True
            session.commit()
            return _result(row, created=created, changed=changed)
        except IntegrityError:
            session.rollback()
            # Another local worker may have registered the same Event between
            # the SELECT and INSERT.  Re-reading converges without duplicating
            # the row or changing its revision.
            session.close()
            return self.register_event(event_id)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def ensure(self, event_id: int) -> SocialDraftResult:
        """Compatibility/readability alias for ``register_event``."""

        return self.register_event(event_id)

    register = ensure

    def get(self, event_id: int) -> SocialDraftResult | None:
        session = self.session_factory()
        try:
            row = session.scalar(
                select(SocialDraft).where(SocialDraft.event_id == event_id)
            )
            return _result(row) if row is not None else None
        finally:
            session.close()

    get_by_event_id = get

    def list_active(self) -> list[SocialDraftResult]:
        """Return all local rows that may still have a persistent action View."""

        session = self.session_factory()
        try:
            rows = session.scalars(
                select(SocialDraft)
                .where(SocialDraft.state != SocialDraftState.POSTED.value)
                .order_by(SocialDraft.updated_at.desc(), SocialDraft.id.desc())
            ).all()
            return [_result(row) for row in rows]
        finally:
            session.close()

    def select_target(
        self,
        event_id: int,
        target: str | SocialDraftTarget,
        *,
        selected_by_user_id: str | int,
    ) -> SocialDraftResult:
        """Persist a target choice and report blocked media choices honestly."""

        normalized_target = _normalize_target(target)
        selected_by = _normalize_user_id(selected_by_user_id)
        session = self.session_factory()
        try:
            event = self._event(session, event_id)
            fingerprint = event_content_fingerprint(event)
            has_media = event_has_media(event)
            row = session.scalar(
                select(SocialDraft).where(SocialDraft.event_id == event_id)
            )
            created = row is None
            content_changed = False
            if row is None:
                row = self._new_row(event, fingerprint, has_media)
                session.add(row)
                session.flush()
            elif row.content_fingerprint != fingerprint:
                self._reset_for_content_change(
                    row,
                    fingerprint=fingerprint,
                    has_media=has_media,
                )
                content_changed = True

            desired_state = state_for_target(normalized_target, has_media=has_media)
            choice_changed = row.target != normalized_target
            actor_changed = row.selected_by_user_id != selected_by
            state_changed = row.state != desired_state
            changed = content_changed or choice_changed or actor_changed or state_changed
            if choice_changed:
                row.revision += 1
            if choice_changed or actor_changed or state_changed or content_changed:
                row.target = normalized_target
                row.state = desired_state
                row.has_media = has_media
                row.selected_by_user_id = selected_by
                row.selected_at = self.now()
                row.published_at = None
                row.error = None
                row.updated_at = self.now()
            session.commit()
            return _result(row, created=created, changed=changed)
        except IntegrityError:
            session.rollback()
            session.close()
            return self.select_target(
                event_id,
                normalized_target,
                selected_by_user_id=selected_by,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    select = select_target

    def record_action_message(
        self,
        event_id: int,
        *,
        channel_id: str | int | None,
        message_id: str | int | None,
    ) -> SocialDraftResult:
        """Persist the Discord action-card mapping after a successful send."""

        row_result = self.register_event(event_id)
        session = self.session_factory()
        try:
            row = session.scalar(
                select(SocialDraft).where(SocialDraft.event_id == event_id)
            )
            if row is None:
                raise ValueError(f"social draft not found: {event_id}")
            normalized_channel = str(channel_id).strip() if channel_id is not None else None
            normalized_message = str(message_id).strip() if message_id is not None else None
            changed = (
                row.discord_action_channel_id != normalized_channel
                or row.discord_action_message_id != normalized_message
            )
            if changed:
                row.discord_action_channel_id = normalized_channel
                row.discord_action_message_id = normalized_message
                row.updated_at = self.now()
                session.commit()
            return _result(row, created=row_result.created, changed=changed)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    mark_action_message = record_action_message


def social_target_label(target: str) -> str:
    return SOCIAL_TARGET_LABELS.get(str(target), str(target))


def social_state_label(state: str) -> str:
    return SOCIAL_STATE_LABELS.get(str(state), str(state))


__all__ = [
    "SOCIAL_MEDIA_PREFIXES",
    "SOCIAL_STATE_LABELS",
    "SOCIAL_TARGET_LABELS",
    "SOCIAL_TARGETS",
    "SocialDraftResult",
    "SocialDraftService",
    "event_content_fingerprint",
    "event_has_media",
    "social_state_label",
    "social_target_label",
    "state_for_target",
]
