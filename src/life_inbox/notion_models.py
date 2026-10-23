"""Additive SQLAlchemy rows for the opt-in Notion secondary archive.

The local Event and its attachments remain canonical.  This table only keeps
the delivery state, remote page identity, and fingerprint-to-file-ID mappings
needed to retry or update one secondary archive page.  They deliberately
contain no token, URL containing a token, or serialized page payload.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .models import Base, Event


def _utc_default() -> datetime:
    """Return the UTC-naive timestamp used by the existing SQLite schema."""

    return datetime.now(UTC).replace(tzinfo=None)


class NotionSyncStatus(StrEnum):
    """Durable local states for one explicitly promoted event."""

    PENDING = "pending"
    SYNCING = "syncing"
    SYNCED = "synced"
    RETRY_WAITING = "retry_waiting"
    FAILED = "failed"
    SKIPPED = "skipped"


class NotionSyncRecord(Base):
    """One durable Notion mapping and outbox state for one local Event."""

    __tablename__ = "notion_sync_records"
    __table_args__ = (
        Index(
            "ix_notion_sync_records_delivery",
            "status",
            "next_attempt_at",
            "updated_at",
        ),
        UniqueConstraint("event_id", name="uq_notion_sync_records_event_id"),
        CheckConstraint(
            "status IN ('pending', 'syncing', 'synced', 'retry_waiting', 'failed', 'skipped')",
            name="ck_notion_sync_records_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_notion_sync_records_attempts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notion_page_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    event: Mapped[Event] = relationship("Event", lazy="joined")


class _NotionAttachmentBase(DeclarativeBase):
    """Opt-in metadata so legacy ``Database.init()`` table sets stay stable."""


_notion_sync_record_reference = Table(
    "notion_sync_records",
    _NotionAttachmentBase.metadata,
    Column("id", Integer, primary_key=True),
)


class NotionAttachmentRecord(_NotionAttachmentBase):
    """Persist one completed Notion file upload for a local content hash.

    File Upload IDs are not regenerated when the page text changes. Keeping
    this mapping in a separate additive table avoids putting a serialized
    provider payload into ``NotionSyncRecord``. It is created lazily when the
    sync service first processes an explicitly promoted row, keeping legacy
    ``Database.init()`` table sets stable.
    """

    __tablename__ = "notion_sync_attachments"
    __table_args__ = (
        Index(
            "ix_notion_sync_attachments_lookup",
            "notion_sync_record_id",
            "content_fingerprint",
        ),
        UniqueConstraint(
            "notion_sync_record_id",
            "content_fingerprint",
            name="uq_notion_sync_attachments_fingerprint",
        ),
        CheckConstraint(
            "length(content_fingerprint) = 64",
            name="ck_notion_sync_attachments_fingerprint",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notion_sync_record_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(_notion_sync_record_reference.c.id, ondelete="CASCADE"),
        nullable=False,
    )
    attachment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    notion_file_id: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )


def ensure_notion_attachment_schema(bind: object) -> None:
    """Create the opt-in attachment mapping table when Notion delivery runs."""

    NotionAttachmentRecord.__table__.create(bind=bind, checkfirst=True)
