"""Additive SQLAlchemy models for privacy-bounded Apple Health summaries."""

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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


def _utc_default() -> datetime:
    """Follow the project's UTC-naive SQLite convention."""

    return datetime.now(UTC).replace(tzinfo=None)


class HealthSleepSummaryRow(Base):
    """One revisioned, aggregate-only sleep record for a local calendar day."""

    __tablename__ = "health_sleep_summaries"
    __table_args__ = (
        UniqueConstraint("user_id", "local_date", name="uq_health_sleep_user_date"),
        CheckConstraint("revision >= 0", name="ck_health_sleep_revision"),
        CheckConstraint(
            "asleep_minutes >= 0 AND awake_minutes >= 0 "
            "AND core_minutes >= 0 AND deep_minutes >= 0 AND rem_minutes >= 0",
            name="ck_health_sleep_nonnegative",
        ),
        CheckConstraint(
            "in_bed_minutes IS NULL OR in_bed_minutes >= 0",
            name="ck_health_sleep_in_bed_nonnegative",
        ),
        Index("ix_health_sleep_local_date", "local_date"),
        Index("ix_health_sleep_synced_at", "synced_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("rhythm_users.id", ondelete="CASCADE"), nullable=False
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    time_zone: Mapped[str] = mapped_column(String(64), nullable=False)
    sleep_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sleep_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    asleep_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    awake_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    core_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deep_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rem_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    in_bed_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="appleWatch", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    semantic_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )


class HealthWorkoutRow(Base):
    """One revisioned HealthKit workout summary without route or sample data."""

    __tablename__ = "health_workouts"
    __table_args__ = (
        UniqueConstraint("user_id", "workout_key", name="uq_health_workout_user_key"),
        CheckConstraint("revision >= 0", name="ck_health_workout_revision"),
        CheckConstraint("duration_seconds >= 0", name="ck_health_workout_duration"),
        CheckConstraint(
            "distance_meters IS NULL OR distance_meters >= 0",
            name="ck_health_workout_distance",
        ),
        CheckConstraint(
            "active_energy_kcal IS NULL OR active_energy_kcal >= 0",
            name="ck_health_workout_energy",
        ),
        CheckConstraint(
            "average_heart_rate_bpm IS NULL OR "
            "(average_heart_rate_bpm > 0 AND average_heart_rate_bpm <= 300)",
            name="ck_health_workout_heart_rate",
        ),
        Index("ix_health_workout_local_date", "local_date"),
        Index("ix_health_workout_synced_at", "synced_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("rhythm_users.id", ondelete="CASCADE"), nullable=False
    )
    # The iPhone hashes HealthKit's UUID before crossing the API boundary.
    workout_key: Mapped[str] = mapped_column(String(64), nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    time_zone: Mapped[str] = mapped_column(String(64), nullable=False)
    activity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    distance_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_energy_kcal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    average_heart_rate_bpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="appleWatch", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    semantic_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )
