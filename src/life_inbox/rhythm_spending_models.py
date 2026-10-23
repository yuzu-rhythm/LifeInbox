"""Durable, revisioned rhythm-spending events received from Apple clients.

The iPhone stores a record locally first and may resend it many times.  This
table is therefore the source of truth for event-level idempotency and keeps
Discord delivery state beside the event it represents.  The Discord Bot token
and any device bearer token are intentionally absent from this schema.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
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
    return datetime.now(UTC).replace(tzinfo=None)


class RhythmSpendingEventRow(Base):
    __tablename__ = "rhythm_spending_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_rhythm_spending_events_event_id"),
        CheckConstraint(
            "game IN ('chunithm', 'maimai', 'ongeki')",
            name="ck_rhythm_spending_events_game",
        ),
        CheckConstraint(
            "source IN ('app', 'widget', 'shortcut', 'siri', 'calendar', 'import')",
            name="ck_rhythm_spending_events_source",
        ),
        CheckConstraint("amount_yen >= 0", name="ck_rhythm_spending_events_amount"),
        CheckConstraint("revision >= 1", name="ck_rhythm_spending_events_revision"),
        CheckConstraint(
            "discord_status IN ("
            "'pending', 'sending', 'posted', 'editing', 'deleting', "
            "'retryWaiting', 'failed'"
            ")",
            name="ck_rhythm_spending_events_discord_status",
        ),
        Index(
            "ix_rhythm_spending_events_day",
            "rhythm_user_id",
            "local_date",
            "occurred_at",
        ),
        Index(
            "ix_rhythm_spending_events_delivery",
            "discord_status",
            "discord_next_retry_at",
            "updated_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    rhythm_user_id: Mapped[int] = mapped_column(
        ForeignKey("rhythm_users.id", ondelete="CASCADE"), nullable=False
    )
    # The client-supplied identifier is metadata, not an authorization key.
    source_user_id: Mapped[str] = mapped_column("user_id", String(100), nullable=False)
    game: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_yen: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    time_zone: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    discord_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discord_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discord_status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )
    discord_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    discord_attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discord_next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    discord_claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    event_link: Mapped[RhythmSpendingEventLink | None] = relationship(
        back_populates="spending_event",
        cascade="all, delete-orphan",
        lazy="joined",
        single_parent=True,
        uselist=False,
    )


class RhythmSpendingEventLink(Base):
    """One local Life Archive Event corresponding to one spending event.

    This additive mapping lets the legacy app importer and Discord capture
    share the existing revision-safe spending table without adding columns to
    an already-created SQLite table.
    """

    __tablename__ = "rhythm_spending_event_links"
    __table_args__ = (
        UniqueConstraint(
            "spending_event_id",
            name="uq_rhythm_spending_event_links_spending_event_id",
        ),
    )

    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    spending_event_id: Mapped[int] = mapped_column(
        ForeignKey("rhythm_spending_events.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, nullable=False
    )

    event: Mapped[Event] = relationship(back_populates="rhythm_spending_link")
    spending_event: Mapped[RhythmSpendingEventRow] = relationship(
        back_populates="event_link"
    )
