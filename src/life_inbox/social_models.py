"""Durable local state for social-network draft choices.

The row in this module is deliberately an outbox *choice*, not a publisher
queue.  It contains no OAuth material, access token, cookie, or remote post
payload.  The canonical text and media remain on the linked local Event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .models import Base, Event


def _utc_default() -> datetime:
    """Return the UTC-naive timestamp used by the SQLite schema."""

    return datetime.now(UTC).replace(tzinfo=None)


class SocialDraftTarget(StrEnum):
    """The only local destination choices exposed by the Discord View."""

    PENDING = "pending"
    X = "x"
    INSTAGRAM = "instagram"
    BOTH = "both"
    HOLD = "hold"


class SocialDraftState(StrEnum):
    """Local readiness states; none of these means an external post occurred."""

    PENDING = "pending"
    READY = "ready"
    BLOCKED = "blocked"
    HELD = "held"
    POSTED = "posted"
    ERROR = "error"


class SocialDraft(Base):
    """One durable social-draft choice for one local Event."""

    __tablename__ = "social_drafts"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_social_drafts_event_id"),
        Index(
            "ix_social_drafts_state_updated_at",
            "state",
            "updated_at",
        ),
        CheckConstraint(
            "target IN ('pending', 'x', 'instagram', 'both', 'hold')",
            name="ck_social_drafts_target",
        ),
        CheckConstraint(
            "state IN ('pending', 'ready', 'blocked', 'held', 'posted', 'error')",
            name="ck_social_drafts_state",
        ),
        CheckConstraint("revision > 0", name="ck_social_drafts_revision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    target: Mapped[str] = mapped_column(
        String(16), default=SocialDraftTarget.PENDING, nullable=False
    )
    state: Mapped[str] = mapped_column(
        String(16), default=SocialDraftState.PENDING, nullable=False
    )
    has_media: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    selected_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discord_action_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discord_action_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    selected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    event: Mapped[Event] = relationship("Event", lazy="joined")

    # Readability aliases keep the storage names explicit while making the
    # public row convenient for callers that use the shorter domain terms.
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


__all__ = [
    "SocialDraft",
    "SocialDraftState",
    "SocialDraftTarget",
]
