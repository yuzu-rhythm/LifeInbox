"""Additive persistence model for privacy-processed Kiseki Moments.

Only the identity, revision state, privacy-safe content fingerprint, and the
local ``Event`` mapping cross this boundary.  Raw route data, coordinates,
privacy-zone identifiers, and other location details deliberately have no
place in this table.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .models import Base, Event


def _utc_default() -> datetime:
    """Return the UTC-naive timestamp used by the existing SQLite schema."""

    return datetime.now(UTC).replace(tzinfo=None)


class KisekiMomentSyncRecord(Base):
    """The durable revision/tombstone mapping for one selected Kiseki Moment."""

    __tablename__ = "kiseki_moment_sync_records"
    __table_args__ = (
        UniqueConstraint("moment_id", name="uq_kiseki_moment_sync_records_moment_id"),
        UniqueConstraint("event_id", name="uq_kiseki_moment_sync_records_event_id"),
        CheckConstraint("revision >= 1", name="ck_kiseki_moment_sync_records_revision"),
        CheckConstraint(
            "length(semantic_fingerprint) = 64",
            name="ck_kiseki_moment_sync_records_fingerprint",
        ),
        Index("ix_kiseki_moment_sync_records_revision", "revision"),
        Index("ix_kiseki_moment_sync_records_deleted_at", "deleted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    moment_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    semantic_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    event: Mapped[Event] = relationship("Event", lazy="joined")

    @property
    def content_sha256(self) -> str:
        """Compatibility alias for callers that describe the hash explicitly."""

        return self.semantic_fingerprint


# Keep the short names discoverable for integration code without creating a
# second mapped class or table.
KisekiMomentRow = KisekiMomentSyncRecord
KisekiMomentSyncRow = KisekiMomentSyncRecord
KisekiSyncRow = KisekiMomentSyncRecord


__all__ = [
    "KisekiMomentRow",
    "KisekiMomentSyncRecord",
    "KisekiMomentSyncRow",
    "KisekiSyncRow",
]
