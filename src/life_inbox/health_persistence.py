"""Revision-safe persistence for Apple Health sleep and workout summaries."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from .health_models import HealthSleepSummaryRow, HealthWorkoutRow
from .rhythm_persistence import RhythmPersistence, utc_naive

WORKOUT_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ACTIVITY_TYPE_PATTERN = re.compile(r"^[a-z][A-Za-z0-9_]{0,63}$")
HEALTH_SOURCES = frozenset({"appleWatch", "appleHealth"})


class HealthRevisionError(ValueError):
    """Base class for rejected device revisions."""


class StaleHealthRevisionError(HealthRevisionError):
    pass


class HealthRevisionConflictError(HealthRevisionError):
    pass


@dataclass(frozen=True, slots=True)
class SleepSummaryRecord:
    id: int
    user_id: int
    local_date: date
    time_zone: str
    sleep_start: datetime
    sleep_end: datetime
    asleep_minutes: int
    awake_minutes: int
    core_minutes: int
    deep_minutes: int
    rem_minutes: int
    in_bed_minutes: int | None
    source: str
    revision: int
    semantic_fingerprint: str
    synced_at: datetime
    deleted: bool


@dataclass(frozen=True, slots=True)
class WorkoutSummaryRecord:
    id: int
    user_id: int
    workout_key: str
    local_date: date
    time_zone: str
    activity_type: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: int
    distance_meters: int | None
    active_energy_kcal: int | None
    average_heart_rate_bpm: int | None
    source: str
    revision: int
    semantic_fingerprint: str
    synced_at: datetime
    deleted: bool


@dataclass(frozen=True, slots=True)
class HealthSyncResult:
    record: SleepSummaryRecord | WorkoutSummaryRecord
    created: bool
    idempotent: bool


def _aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")
    return value


def _timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timeZone must be a valid IANA timezone") from exc


def _source(value: str) -> str:
    if value not in HEALTH_SOURCES:
        raise ValueError("source must be appleWatch or appleHealth")
    return value


def _revision(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("revision must be a non-negative integer")
    return value


def _nonnegative(value: int | None, *, field: str, maximum: int) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{field} is outside its allowed range")
    return value


def _fingerprint(kind: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"kind": kind, **payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _sleep_record(row: HealthSleepSummaryRow) -> SleepSummaryRecord:
    return SleepSummaryRecord(
        id=row.id,
        user_id=row.user_id,
        local_date=row.local_date,
        time_zone=row.time_zone,
        sleep_start=row.sleep_start,
        sleep_end=row.sleep_end,
        asleep_minutes=row.asleep_minutes,
        awake_minutes=row.awake_minutes,
        core_minutes=row.core_minutes,
        deep_minutes=row.deep_minutes,
        rem_minutes=row.rem_minutes,
        in_bed_minutes=row.in_bed_minutes,
        source=row.source,
        revision=row.revision,
        semantic_fingerprint=row.semantic_fingerprint,
        synced_at=row.synced_at,
        deleted=row.deleted_at is not None,
    )


def _workout_record(row: HealthWorkoutRow) -> WorkoutSummaryRecord:
    return WorkoutSummaryRecord(
        id=row.id,
        user_id=row.user_id,
        workout_key=row.workout_key,
        local_date=row.local_date,
        time_zone=row.time_zone,
        activity_type=row.activity_type,
        started_at=row.started_at,
        ended_at=row.ended_at,
        duration_seconds=row.duration_seconds,
        distance_meters=row.distance_meters,
        active_energy_kcal=row.active_energy_kcal,
        average_heart_rate_bpm=row.average_heart_rate_bpm,
        source=row.source,
        revision=row.revision,
        semantic_fingerprint=row.semantic_fingerprint,
        synced_at=row.synced_at,
        deleted=row.deleted_at is not None,
    )


class HealthPersistence:
    """Unit-of-work API shared by the iPhone API and Discord scheduler."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory
        self._users = RhythmPersistence(session_factory)

    def ensure_user(
        self, *, display_name: str, time_zone: str, discord_user_id: str | int | None
    ) -> int:
        return self._users.ensure_user(
            display_name=display_name,
            time_zone=time_zone,
            discord_user_id=discord_user_id,
        )

    def sync_sleep(
        self,
        *,
        user_id: int,
        local_date: date,
        time_zone: str,
        sleep_start: datetime,
        sleep_end: datetime,
        asleep_minutes: int,
        awake_minutes: int,
        core_minutes: int,
        deep_minutes: int,
        rem_minutes: int,
        in_bed_minutes: int | None,
        source: str,
        revision: int,
        synced_at: datetime | None = None,
        _retry_count: int = 0,
    ) -> HealthSyncResult:
        zone = _timezone(time_zone)
        start = _aware(sleep_start, field="sleepStart")
        end = _aware(sleep_end, field="sleepEnd")
        if end <= start:
            raise ValueError("sleepEnd must be after sleepStart")
        if end.astimezone(zone).date() != local_date:
            raise ValueError("localDate must match sleepEnd in timeZone")
        values = {
            "asleep_minutes": _nonnegative(asleep_minutes, field="asleepMinutes", maximum=1_440),
            "awake_minutes": _nonnegative(awake_minutes, field="awakeMinutes", maximum=1_440),
            "core_minutes": _nonnegative(core_minutes, field="coreMinutes", maximum=1_440),
            "deep_minutes": _nonnegative(deep_minutes, field="deepMinutes", maximum=1_440),
            "rem_minutes": _nonnegative(rem_minutes, field="remMinutes", maximum=1_440),
            "in_bed_minutes": _nonnegative(in_bed_minutes, field="inBedMinutes", maximum=1_440),
        }
        known_stage_minutes = (
            values["core_minutes"] + values["deep_minutes"] + values["rem_minutes"]
        )
        if known_stage_minutes > values["asleep_minutes"]:
            raise ValueError("known sleep stages cannot exceed asleepMinutes")
        normalized_source = _source(source)
        normalized_revision = _revision(revision)
        normalized_start = utc_naive(start)
        normalized_end = utc_naive(end)
        normalized_sync = utc_naive(synced_at)
        payload = {
            "localDate": local_date.isoformat(),
            "timeZone": time_zone,
            "sleepStart": normalized_start.isoformat(),
            "sleepEnd": normalized_end.isoformat(),
            **values,
            "source": normalized_source,
            "deleted": False,
        }
        fingerprint = _fingerprint("sleep", payload)
        session = self.session_factory()
        try:
            row = session.scalar(
                select(HealthSleepSummaryRow).where(
                    HealthSleepSummaryRow.user_id == user_id,
                    HealthSleepSummaryRow.local_date == local_date,
                )
            )
            if row is None:
                row = HealthSleepSummaryRow(
                    user_id=user_id,
                    local_date=local_date,
                    time_zone=time_zone,
                    sleep_start=normalized_start,
                    sleep_end=normalized_end,
                    source=normalized_source,
                    revision=normalized_revision,
                    semantic_fingerprint=fingerprint,
                    synced_at=normalized_sync,
                    **values,
                )
                session.add(row)
                session.commit()
                return HealthSyncResult(_sleep_record(row), True, False)
            if normalized_revision < row.revision:
                raise StaleHealthRevisionError("older sleep revision rejected")
            if normalized_revision == row.revision:
                if row.semantic_fingerprint != fingerprint:
                    raise HealthRevisionConflictError("same sleep revision has different content")
                return HealthSyncResult(_sleep_record(row), False, True)
            expected = row.revision
            result = session.execute(
                update(HealthSleepSummaryRow)
                .where(
                    HealthSleepSummaryRow.id == row.id,
                    HealthSleepSummaryRow.revision == expected,
                )
                .values(
                    time_zone=time_zone,
                    sleep_start=normalized_start,
                    sleep_end=normalized_end,
                    source=normalized_source,
                    revision=normalized_revision,
                    semantic_fingerprint=fingerprint,
                    synced_at=normalized_sync,
                    deleted_at=None,
                    updated_at=normalized_sync,
                    **values,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                if _retry_count >= 4:
                    raise HealthRevisionConflictError("sleep changed concurrently")
                return self.sync_sleep(
                    user_id=user_id,
                    local_date=local_date,
                    time_zone=time_zone,
                    sleep_start=start,
                    sleep_end=end,
                    asleep_minutes=asleep_minutes,
                    awake_minutes=awake_minutes,
                    core_minutes=core_minutes,
                    deep_minutes=deep_minutes,
                    rem_minutes=rem_minutes,
                    in_bed_minutes=in_bed_minutes,
                    source=source,
                    revision=revision,
                    synced_at=normalized_sync,
                    _retry_count=_retry_count + 1,
                )
            session.commit()
            saved = session.get(HealthSleepSummaryRow, row.id)
            if saved is None:
                raise RuntimeError("sleep row disappeared after update")
            return HealthSyncResult(_sleep_record(saved), False, False)
        except IntegrityError:
            session.rollback()
            if _retry_count >= 4:
                raise
            return self.sync_sleep(
                user_id=user_id,
                local_date=local_date,
                time_zone=time_zone,
                sleep_start=start,
                sleep_end=end,
                asleep_minutes=asleep_minutes,
                awake_minutes=awake_minutes,
                core_minutes=core_minutes,
                deep_minutes=deep_minutes,
                rem_minutes=rem_minutes,
                in_bed_minutes=in_bed_minutes,
                source=source,
                revision=revision,
                synced_at=normalized_sync,
                _retry_count=_retry_count + 1,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_sleep(
        self,
        *,
        user_id: int,
        local_date: date,
        time_zone: str,
        revision: int,
        synced_at: datetime | None = None,
    ) -> HealthSyncResult:
        zone = _timezone(time_zone)
        normalized_revision = _revision(revision)
        changed_at = utc_naive(synced_at)
        midnight = utc_naive(datetime.combine(local_date, time.min, tzinfo=zone))
        fingerprint = _fingerprint(
            "sleep", {"localDate": local_date.isoformat(), "deleted": True}
        )
        session = self.session_factory()
        try:
            row = session.scalar(
                select(HealthSleepSummaryRow).where(
                    HealthSleepSummaryRow.user_id == user_id,
                    HealthSleepSummaryRow.local_date == local_date,
                )
            )
            if row is None:
                row = HealthSleepSummaryRow(
                    user_id=user_id,
                    local_date=local_date,
                    time_zone=time_zone,
                    sleep_start=midnight,
                    sleep_end=midnight,
                    asleep_minutes=0,
                    awake_minutes=0,
                    core_minutes=0,
                    deep_minutes=0,
                    rem_minutes=0,
                    in_bed_minutes=None,
                    source="appleHealth",
                    revision=normalized_revision,
                    semantic_fingerprint=fingerprint,
                    synced_at=changed_at,
                    deleted_at=changed_at,
                )
                session.add(row)
                session.commit()
                return HealthSyncResult(_sleep_record(row), True, False)
            if normalized_revision < row.revision:
                raise StaleHealthRevisionError("older sleep deletion rejected")
            if normalized_revision == row.revision:
                if row.deleted_at is None:
                    raise HealthRevisionConflictError("same revision contains active sleep")
                return HealthSyncResult(_sleep_record(row), False, True)
            row.revision = normalized_revision
            row.semantic_fingerprint = fingerprint
            row.synced_at = changed_at
            row.deleted_at = changed_at
            row.updated_at = changed_at
            session.commit()
            return HealthSyncResult(_sleep_record(row), False, False)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def sync_workout(
        self,
        *,
        user_id: int,
        workout_key: str,
        time_zone: str,
        activity_type: str,
        started_at: datetime,
        ended_at: datetime,
        duration_seconds: int,
        distance_meters: int | None,
        active_energy_kcal: int | None,
        average_heart_rate_bpm: int | None,
        source: str,
        revision: int,
        synced_at: datetime | None = None,
        _retry_count: int = 0,
    ) -> HealthSyncResult:
        normalized_key = workout_key.strip().lower()
        if not WORKOUT_KEY_PATTERN.fullmatch(normalized_key):
            raise ValueError("workoutId must be a 64-character SHA-256 hex value")
        zone = _timezone(time_zone)
        start = _aware(started_at, field="startedAt")
        end = _aware(ended_at, field="endedAt")
        if end <= start:
            raise ValueError("endedAt must be after startedAt")
        normalized_activity = activity_type.strip()
        if not ACTIVITY_TYPE_PATTERN.fullmatch(normalized_activity):
            raise ValueError("activityType is invalid")
        duration = _nonnegative(duration_seconds, field="durationSeconds", maximum=172_800)
        distance = _nonnegative(distance_meters, field="distanceMeters", maximum=2_000_000)
        energy = _nonnegative(active_energy_kcal, field="activeEnergyKcal", maximum=100_000)
        heart_rate = _nonnegative(
            average_heart_rate_bpm, field="averageHeartRateBpm", maximum=300
        )
        if heart_rate == 0:
            raise ValueError("averageHeartRateBpm must be positive when present")
        normalized_source = _source(source)
        normalized_revision = _revision(revision)
        normalized_start = utc_naive(start)
        normalized_end = utc_naive(end)
        normalized_sync = utc_naive(synced_at)
        local_date = start.astimezone(zone).date()
        payload = {
            "workoutId": normalized_key,
            "timeZone": time_zone,
            "activityType": normalized_activity,
            "startedAt": normalized_start.isoformat(),
            "endedAt": normalized_end.isoformat(),
            "durationSeconds": duration,
            "distanceMeters": distance,
            "activeEnergyKcal": energy,
            "averageHeartRateBpm": heart_rate,
            "source": normalized_source,
            "deleted": False,
        }
        fingerprint = _fingerprint("workout", payload)
        session = self.session_factory()
        try:
            row = session.scalar(
                select(HealthWorkoutRow).where(
                    HealthWorkoutRow.user_id == user_id,
                    HealthWorkoutRow.workout_key == normalized_key,
                )
            )
            if row is None:
                row = HealthWorkoutRow(
                    user_id=user_id,
                    workout_key=normalized_key,
                    local_date=local_date,
                    time_zone=time_zone,
                    activity_type=normalized_activity,
                    started_at=normalized_start,
                    ended_at=normalized_end,
                    duration_seconds=duration,
                    distance_meters=distance,
                    active_energy_kcal=energy,
                    average_heart_rate_bpm=heart_rate,
                    source=normalized_source,
                    revision=normalized_revision,
                    semantic_fingerprint=fingerprint,
                    synced_at=normalized_sync,
                )
                session.add(row)
                session.commit()
                return HealthSyncResult(_workout_record(row), True, False)
            if normalized_revision < row.revision:
                raise StaleHealthRevisionError("older workout revision rejected")
            if normalized_revision == row.revision:
                if row.semantic_fingerprint != fingerprint:
                    raise HealthRevisionConflictError("same workout revision has different content")
                return HealthSyncResult(_workout_record(row), False, True)
            expected = row.revision
            result = session.execute(
                update(HealthWorkoutRow)
                .where(HealthWorkoutRow.id == row.id, HealthWorkoutRow.revision == expected)
                .values(
                    local_date=local_date,
                    time_zone=time_zone,
                    activity_type=normalized_activity,
                    started_at=normalized_start,
                    ended_at=normalized_end,
                    duration_seconds=duration,
                    distance_meters=distance,
                    active_energy_kcal=energy,
                    average_heart_rate_bpm=heart_rate,
                    source=normalized_source,
                    revision=normalized_revision,
                    semantic_fingerprint=fingerprint,
                    synced_at=normalized_sync,
                    deleted_at=None,
                    updated_at=normalized_sync,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                if _retry_count >= 4:
                    raise HealthRevisionConflictError("workout changed concurrently")
                return self.sync_workout(
                    user_id=user_id,
                    workout_key=normalized_key,
                    time_zone=time_zone,
                    activity_type=normalized_activity,
                    started_at=start,
                    ended_at=end,
                    duration_seconds=duration,
                    distance_meters=distance,
                    active_energy_kcal=energy,
                    average_heart_rate_bpm=heart_rate,
                    source=source,
                    revision=revision,
                    synced_at=normalized_sync,
                    _retry_count=_retry_count + 1,
                )
            session.commit()
            saved = session.get(HealthWorkoutRow, row.id)
            if saved is None:
                raise RuntimeError("workout row disappeared after update")
            return HealthSyncResult(_workout_record(saved), False, False)
        except IntegrityError:
            session.rollback()
            if _retry_count >= 4:
                raise
            return self.sync_workout(
                user_id=user_id,
                workout_key=normalized_key,
                time_zone=time_zone,
                activity_type=normalized_activity,
                started_at=start,
                ended_at=end,
                duration_seconds=duration,
                distance_meters=distance,
                active_energy_kcal=energy,
                average_heart_rate_bpm=heart_rate,
                source=source,
                revision=revision,
                synced_at=normalized_sync,
                _retry_count=_retry_count + 1,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_workout(
        self,
        *,
        user_id: int,
        workout_key: str,
        time_zone: str,
        revision: int,
        synced_at: datetime | None = None,
    ) -> HealthSyncResult:
        normalized_key = workout_key.strip().lower()
        if not WORKOUT_KEY_PATTERN.fullmatch(normalized_key):
            raise ValueError("workoutId must be a 64-character SHA-256 hex value")
        zone = _timezone(time_zone)
        normalized_revision = _revision(revision)
        changed_at = utc_naive(synced_at)
        local_date = datetime.now(zone).date()
        midnight = utc_naive(datetime.combine(local_date, time.min, tzinfo=zone))
        fingerprint = _fingerprint(
            "workout", {"workoutId": normalized_key, "deleted": True}
        )
        session = self.session_factory()
        try:
            row = session.scalar(
                select(HealthWorkoutRow).where(
                    HealthWorkoutRow.user_id == user_id,
                    HealthWorkoutRow.workout_key == normalized_key,
                )
            )
            if row is None:
                row = HealthWorkoutRow(
                    user_id=user_id,
                    workout_key=normalized_key,
                    local_date=local_date,
                    time_zone=time_zone,
                    activity_type="deleted",
                    started_at=midnight,
                    ended_at=midnight,
                    duration_seconds=0,
                    source="appleHealth",
                    revision=normalized_revision,
                    semantic_fingerprint=fingerprint,
                    synced_at=changed_at,
                    deleted_at=changed_at,
                )
                session.add(row)
                session.commit()
                return HealthSyncResult(_workout_record(row), True, False)
            if normalized_revision < row.revision:
                raise StaleHealthRevisionError("older workout deletion rejected")
            if normalized_revision == row.revision:
                if row.deleted_at is None:
                    raise HealthRevisionConflictError("same revision contains active workout")
                return HealthSyncResult(_workout_record(row), False, True)
            row.revision = normalized_revision
            row.semantic_fingerprint = fingerprint
            row.synced_at = changed_at
            row.deleted_at = changed_at
            row.updated_at = changed_at
            session.commit()
            return HealthSyncResult(_workout_record(row), False, False)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_sleep(self, *, user_id: int, local_date: date) -> SleepSummaryRecord | None:
        session = self.session_factory()
        try:
            row = session.scalar(
                select(HealthSleepSummaryRow).where(
                    HealthSleepSummaryRow.user_id == user_id,
                    HealthSleepSummaryRow.local_date == local_date,
                )
            )
            return _sleep_record(row) if row is not None else None
        finally:
            session.close()

    def get_workout(self, *, user_id: int, workout_key: str) -> WorkoutSummaryRecord | None:
        session = self.session_factory()
        try:
            row = session.scalar(
                select(HealthWorkoutRow).where(
                    HealthWorkoutRow.user_id == user_id,
                    HealthWorkoutRow.workout_key == workout_key.lower(),
                )
            )
            return _workout_record(row) if row is not None else None
        finally:
            session.close()

    def records_since(
        self, *, user_id: int, date_from: date
    ) -> tuple[list[SleepSummaryRecord], list[WorkoutSummaryRecord]]:
        session = self.session_factory()
        try:
            sleeps = session.scalars(
                select(HealthSleepSummaryRow)
                .where(
                    HealthSleepSummaryRow.user_id == user_id,
                    HealthSleepSummaryRow.local_date >= date_from,
                )
                .order_by(HealthSleepSummaryRow.local_date)
            ).all()
            workouts = session.scalars(
                select(HealthWorkoutRow)
                .where(
                    HealthWorkoutRow.user_id == user_id,
                    HealthWorkoutRow.local_date >= date_from,
                )
                .order_by(HealthWorkoutRow.started_at)
            ).all()
            return (
                [_sleep_record(row) for row in sleeps],
                [_workout_record(row) for row in workouts],
            )
        finally:
            session.close()

    def status(self, *, user_id: int) -> dict[str, Any]:
        session = self.session_factory()
        try:
            last_sleep = session.scalar(
                select(func.max(HealthSleepSummaryRow.synced_at)).where(
                    HealthSleepSummaryRow.user_id == user_id
                )
            )
            last_workout = session.scalar(
                select(func.max(HealthWorkoutRow.synced_at)).where(
                    HealthWorkoutRow.user_id == user_id
                )
            )
            sleep_count = session.scalar(
                select(func.count(HealthSleepSummaryRow.id)).where(
                    HealthSleepSummaryRow.user_id == user_id,
                    HealthSleepSummaryRow.deleted_at.is_(None),
                )
            )
            workout_count = session.scalar(
                select(func.count(HealthWorkoutRow.id)).where(
                    HealthWorkoutRow.user_id == user_id,
                    HealthWorkoutRow.deleted_at.is_(None),
                )
            )
            return {
                "lastSleepSyncAt": last_sleep,
                "lastWorkoutSyncAt": last_workout,
                "sleepSummaryCount": int(sleep_count or 0),
                "workoutCount": int(workout_count or 0),
            }
        finally:
            session.close()


def public_record_dict(record: SleepSummaryRecord | WorkoutSummaryRecord) -> dict[str, Any]:
    """Testing/documentation helper that never includes credentials or raw HealthKit samples."""

    return asdict(record)
