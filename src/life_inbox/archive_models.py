"""Additive SQLAlchemy tables for the local Life Archive.

The existing ``events`` and ``attachments`` tables remain the Life Inbox
schema.  These tables are a separate, provider-neutral archive projection and
can be created alongside an older database with ``Base.metadata.create_all``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from .archive_schema import (
    ARCHIVE_SCHEMA_VERSION,
    DEFAULT_VISIBILITY,
)
from .models import Base


def _utc_default() -> datetime:
    """Return the UTC-naive timestamp used by the application SQLite schema."""

    return datetime.now(UTC).replace(tzinfo=None)


class ArchiveEntry(Base):
    """Stable archive identity and the latest accepted source revision."""

    __tablename__ = "archive_entries"
    __table_args__ = (
        UniqueConstraint("archive_id", name="uq_archive_entries_archive_id"),
        UniqueConstraint(
            "source", "source_service_id", name="uq_archive_entries_source_service_id"
        ),
        CheckConstraint("schema_version >= 1", name="ck_archive_entries_schema_version"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_archive_entries_confidence",
        ),
        CheckConstraint(
            "visibility IN ('private', 'unlisted', 'public')",
            name="ck_archive_entries_visibility",
        ),
        CheckConstraint(
            "sync_status IN ('pending', 'processing', 'synced', 'retrying', 'failed', "
            "'quarantined', 'deleted', 'reconciliation_required')",
            name="ck_archive_entries_sync_status",
        ),
        Index("ix_archive_entries_timeline", "tombstone", "occurred_at", "captured_at"),
        Index("ix_archive_entries_sync_status", "sync_status"),
        Index("ix_archive_entries_source_revision", "source", "source_revision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    archive_id: Mapped[str] = mapped_column(String(36), nullable=False)
    schema_version: Mapped[int] = mapped_column(
        Integer, default=ARCHIVE_SCHEMA_VERSION, nullable=False
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_service_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(255), nullable=False)
    original_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    provenance: Mapped[str] = mapped_column(String(512), default="fixture", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    visibility: Mapped[str] = mapped_column(
        String(16), default=DEFAULT_VISIBILITY, nullable=False
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    occurred_at_estimated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tombstone: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sync_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    sync_attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_sync_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    attachments: Mapped[list[ArchiveAttachment]] = relationship(
        back_populates="entry",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    deliveries: Mapped[list[ArchiveDelivery]] = relationship(
        back_populates="entry",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    relations: Mapped[list[ArchiveRelation]] = relationship(
        back_populates="entry",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # These aliases make the requirement's wording available to adapters
    # without duplicating columns or weakening the stable source key.
    original_service_id = synonym("source_service_id")
    revision = synonym("source_revision")

    @property
    def source_event_id(self) -> str:
        """Compatibility spelling for an upstream event/message identity."""

        return self.source_service_id


class ArchiveAttachment(Base):
    """Independent archive media metadata and private storage references."""

    __tablename__ = "archive_attachments"
    __table_args__ = (
        UniqueConstraint(
            "entry_id",
            "source_attachment_id",
            name="uq_archive_attachments_entry_source_id",
        ),
        CheckConstraint("byte_size >= 0", name="ck_archive_attachments_byte_size"),
        CheckConstraint(
            "length(sha256) = 64 OR sha256 = ''", name="ck_archive_attachments_sha256"
        ),
        Index("ix_archive_attachments_entry", "entry_id"),
        Index("ix_archive_attachments_quarantine", "quarantine_reason"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("archive_entries.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_attachment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    detected_mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    byte_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    private_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    web_derivative_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    thumbnail_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metadata_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    quarantine_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metadata_removed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    quarantine_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    download_status: Mapped[str] = mapped_column(String(16), default="available", nullable=False)
    download_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    entry: Mapped[ArchiveEntry] = relationship(back_populates="attachments")


class ArchiveDelivery(Base):
    """One received delivery, retained for idempotency and retry diagnostics."""

    __tablename__ = "archive_deliveries"
    __table_args__ = (
        UniqueConstraint("source", "delivery_id", name="uq_archive_deliveries_source_id"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'synced', 'retrying', 'failed', "
            "'quarantined', 'deleted', 'reconciliation_required')",
            name="ck_archive_deliveries_status",
        ),
        Index("ix_archive_deliveries_entry", "entry_id"),
        Index("ix_archive_deliveries_received", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(255), nullable=False)
    entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("archive_entries.id", ondelete="SET NULL"), nullable=True
    )
    source_service_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    entry: Mapped[ArchiveEntry | None] = relationship(back_populates="deliveries")


class ArchiveRelation(Base):
    """Optional provider-neutral relation edge between archive identities."""

    __tablename__ = "archive_relations"
    __table_args__ = (
        UniqueConstraint(
            "entry_id", "relation_type", "target_archive_id", name="uq_archive_relations_edge"
        ),
        Index("ix_archive_relations_target", "target_archive_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("archive_entries.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_archive_id: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str] = mapped_column(String(1_000), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)

    entry: Mapped[ArchiveEntry] = relationship(back_populates="relations")


# Short aliases keep integration code readable while exposing one mapped class
# per table.
ArchiveEntryRow = ArchiveEntry
ArchiveAttachmentRow = ArchiveAttachment
ArchiveDeliveryRow = ArchiveDelivery
ArchiveRelationRow = ArchiveRelation


__all__ = [
    "ArchiveAttachment",
    "ArchiveAttachmentRow",
    "ArchiveDelivery",
    "ArchiveDeliveryRow",
    "ArchiveEntry",
    "ArchiveEntryRow",
    "ArchiveRelation",
    "ArchiveRelationRow",
]
