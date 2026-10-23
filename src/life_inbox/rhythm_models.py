"""Additive SQLAlchemy tables for rhythm rating collection and iPhone sync.

The project currently uses ``Base.metadata.create_all`` rather than a versioned
migration tool.  Keeping these rows in a separate module makes the new schema
discoverable without turning the already-large legacy model module into a
single feature dump.  This module stores secret *references* and sanitized
error codes only; credentials, cookies, and bearer tokens never belong here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .models import Base


def _utc_default() -> datetime:
    """Return the UTC-naive representation used by the existing SQLite DB."""

    return datetime.now(UTC).replace(tzinfo=None)


class RhythmUserRow(Base):
    __tablename__ = "rhythm_users"
    __table_args__ = (
        UniqueConstraint("discord_user_id", name="uq_rhythm_users_discord_user_id"),
        CheckConstraint("length(trim(display_name)) > 0", name="ck_rhythm_users_display_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    time_zone: Mapped[str] = mapped_column(String(64), default="Asia/Tokyo", nullable=False)
    discord_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    provider_accounts: Mapped[list[RhythmProviderAccountRow]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )


class RhythmProviderAccountRow(Base):
    __tablename__ = "rhythm_provider_accounts"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "game", "provider", name="uq_rhythm_provider_account"
        ),
        CheckConstraint(
            "game IN ('chunithm', 'maimai', 'ongeki')",
            name="ck_rhythm_provider_accounts_game",
        ),
        Index("ix_rhythm_provider_accounts_status", "session_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("rhythm_users.id", ondelete="CASCADE"), nullable=False
    )
    game: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    external_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    encrypted_credentials_reference: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    session_status: Mapped[str] = mapped_column(
        String(64), default="unknown", nullable=False
    )
    last_authenticated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    user: Mapped[RhythmUserRow] = relationship(back_populates="provider_accounts")


class RatingSnapshotRow(Base):
    __tablename__ = "rating_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "game",
            "semantic_fingerprint",
            "collected_at",
            name="uq_rating_snapshot_observation",
        ),
        CheckConstraint(
            "game IN ('chunithm', 'maimai', 'ongeki')",
            name="ck_rating_snapshots_game",
        ),
        Index(
            "ix_rating_snapshots_user_game_collected",
            "user_id",
            "game",
            "collected_at",
        ),
        Index("ix_rating_snapshots_fingerprint", "semantic_fingerprint"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("rhythm_users.id", ondelete="CASCADE"), nullable=False
    )
    game: Mapped[str] = mapped_column(String(16), nullable=False)
    player_name: Mapped[str] = mapped_column(String(100), nullable=False)
    rating: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    previous_rating: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    rating_delta: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    semantic_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)

    entries: Mapped[list[RatingEntryRow]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan", lazy="selectin"
    )
    image_links: Mapped[list[RatingSnapshotImageRow]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan", lazy="selectin"
    )


class RatingEntryRow(Base):
    __tablename__ = "rating_entries"
    __table_args__ = (
        Index("ix_rating_entries_snapshot_frame", "snapshot_id", "frame_type", "rank"),
        CheckConstraint("rank IS NULL OR rank > 0", name="ck_rating_entries_rank"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("rating_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    frame_type: Mapped[str] = mapped_column(String(32), nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    song_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    normalized_title: Mapped[str] = mapped_column(Text, nullable=False)
    difficulty: Mapped[str | None] = mapped_column(String(32), nullable=True)
    chart_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    chart_constant: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    achievement: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    technical_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    p_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    song_rating: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    clear_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    full_combo_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    all_justice_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sync_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    full_bell_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    all_break_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    genre: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    snapshot: Mapped[RatingSnapshotRow] = relationship(back_populates="entries")


class RatingImageRow(Base):
    """A content-addressed image asset, stored only once per SHA-256."""

    __tablename__ = "rating_images"
    __table_args__ = (
        UniqueConstraint("image_sha256", name="uq_rating_images_sha256"),
        CheckConstraint("width > 0 AND height > 0", name="ck_rating_images_dimensions"),
        CheckConstraint("byte_size > 0", name="ck_rating_images_byte_size"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game: Mapped[str] = mapped_column(String(16), nullable=False)
    image_path: Mapped[str] = mapped_column(Text, nullable=False)
    image_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    renderer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)

    snapshot_links: Mapped[list[RatingSnapshotImageRow]] = relationship(
        back_populates="image", lazy="selectin"
    )


class RatingSnapshotImageRow(Base):
    __tablename__ = "rating_snapshot_images"
    __table_args__ = (
        UniqueConstraint("snapshot_id", name="uq_rating_snapshot_image_snapshot"),
        Index("ix_rating_snapshot_images_image", "image_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("rating_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    image_id: Mapped[int] = mapped_column(
        ForeignKey("rating_images.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)

    snapshot: Mapped[RatingSnapshotRow] = relationship(back_populates="image_links")
    image: Mapped[RatingImageRow] = relationship(back_populates="snapshot_links")


class DiscordDeliveryRow(Base):
    __tablename__ = "discord_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "local_date",
            "delivery_kind",
            "game_key",
            "channel_id",
            name="uq_discord_delivery_daily",
        ),
        Index("ix_discord_deliveries_status", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("rhythm_users.id", ondelete="CASCADE"), nullable=False
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    delivery_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    game_key: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    discord_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    semantic_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )


class DailyGameSpendingRow(Base):
    __tablename__ = "daily_game_spending"
    __table_args__ = (
        UniqueConstraint("user_id", "local_date", name="uq_daily_game_spending"),
        CheckConstraint(
            "chunithm_amount_yen >= 0 AND maimai_amount_yen >= 0 "
            "AND ongeki_amount_yen >= 0 AND total_amount_yen >= 0",
            name="ck_daily_game_spending_nonnegative",
        ),
        CheckConstraint(
            "total_amount_yen = chunithm_amount_yen + maimai_amount_yen "
            "+ ongeki_amount_yen",
            name="ck_daily_game_spending_total",
        ),
        CheckConstraint("revision >= 0", name="ck_daily_game_spending_revision"),
        Index("ix_daily_game_spending_date", "local_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("rhythm_users.id", ondelete="CASCADE"), nullable=False
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    time_zone: Mapped[str] = mapped_column(String(64), nullable=False)
    chunithm_amount_yen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    maimai_amount_yen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ongeki_amount_yen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_amount_yen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )


class CollectionRunRow(Base):
    __tablename__ = "collection_runs"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "game",
            "request_key_hash",
            name="uq_collection_runs_idempotency",
        ),
        CheckConstraint(
            "game IN ('chunithm', 'maimai', 'ongeki')",
            name="ck_collection_runs_game",
        ),
        Index("ix_collection_runs_state", "state", "created_at"),
        Index("ix_collection_runs_user_game_started", "user_id", "game", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("rhythm_users.id", ondelete="CASCADE"), nullable=False
    )
    game: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    request_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(64), default="queued", nullable=False)
    progress_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    manual_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manual_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmation_nonce_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("rating_snapshots.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    events: Mapped[list[CollectionRunEventRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )


class CollectionRunEventRow(Base):
    __tablename__ = "collection_run_events"
    __table_args__ = (Index("ix_collection_run_events_run_created", "run_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("collection_runs.id", ondelete="CASCADE"), nullable=False
    )
    from_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_state: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    explicit_confirmation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)

    run: Mapped[CollectionRunRow] = relationship(back_populates="events")


class CollectionLeaseRow(Base):
    """SQLite-backed lease preventing two Bot processes from running one game."""

    __tablename__ = "collection_leases"
    __table_args__ = (
        UniqueConstraint("user_id", "game", name="uq_collection_lease_user_game"),
        Index("ix_collection_leases_expires", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("rhythm_users.id", ondelete="CASCADE"), nullable=False
    )
    game: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )
