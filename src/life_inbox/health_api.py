"""Authenticated iPhone API for aggregate Apple Health data."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .db import Database
from .health_persistence import (
    HealthPersistence,
    HealthRevisionConflictError,
    SleepSummaryRecord,
    StaleHealthRevisionError,
    WorkoutSummaryRecord,
)


class HealthApiError(Exception):
    def __init__(
        self, status_code: int, code: str, message: str, *, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


class SleepPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    time_zone: str = Field(alias="timeZone", min_length=1, max_length=64)
    sleep_start: datetime = Field(alias="sleepStart")
    sleep_end: datetime = Field(alias="sleepEnd")
    asleep_minutes: int = Field(alias="asleepMinutes", ge=0, le=1_440)
    awake_minutes: int = Field(default=0, alias="awakeMinutes", ge=0, le=1_440)
    core_minutes: int = Field(default=0, alias="coreMinutes", ge=0, le=1_440)
    deep_minutes: int = Field(default=0, alias="deepMinutes", ge=0, le=1_440)
    rem_minutes: int = Field(default=0, alias="remMinutes", ge=0, le=1_440)
    in_bed_minutes: int | None = Field(default=None, alias="inBedMinutes", ge=0, le=1_440)
    source: Literal["appleWatch", "appleHealth"] = "appleWatch"
    revision: int = Field(ge=0)


class WorkoutPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    time_zone: str = Field(alias="timeZone", min_length=1, max_length=64)
    activity_type: str = Field(alias="activityType", min_length=1, max_length=64)
    started_at: datetime = Field(alias="startedAt")
    ended_at: datetime = Field(alias="endedAt")
    duration_seconds: int = Field(alias="durationSeconds", ge=0, le=172_800)
    distance_meters: int | None = Field(
        default=None, alias="distanceMeters", ge=0, le=2_000_000
    )
    active_energy_kcal: int | None = Field(
        default=None, alias="activeEnergyKcal", ge=0, le=100_000
    )
    average_heart_rate_bpm: int | None = Field(
        default=None, alias="averageHeartRateBpm", gt=0, le=300
    )
    source: Literal["appleWatch", "appleHealth"] = "appleWatch"
    revision: int = Field(ge=0)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _sleep_json(record: SleepSummaryRecord) -> dict[str, Any]:
    return {
        "localDate": record.local_date.isoformat(),
        "timeZone": record.time_zone,
        "sleepStart": _iso(record.sleep_start),
        "sleepEnd": _iso(record.sleep_end),
        "asleepMinutes": record.asleep_minutes,
        "awakeMinutes": record.awake_minutes,
        "coreMinutes": record.core_minutes,
        "deepMinutes": record.deep_minutes,
        "remMinutes": record.rem_minutes,
        "inBedMinutes": record.in_bed_minutes,
        "source": record.source,
        "revision": record.revision,
        "syncedAt": _iso(record.synced_at),
        "deleted": record.deleted,
    }


def _workout_json(record: WorkoutSummaryRecord) -> dict[str, Any]:
    return {
        "workoutId": record.workout_key,
        "localDate": record.local_date.isoformat(),
        "timeZone": record.time_zone,
        "activityType": record.activity_type,
        "startedAt": _iso(record.started_at),
        "endedAt": _iso(record.ended_at),
        "durationSeconds": record.duration_seconds,
        "distanceMeters": record.distance_meters,
        "activeEnergyKcal": record.active_energy_kcal,
        "averageHeartRateBpm": record.average_heart_rate_bpm,
        "source": record.source,
        "revision": record.revision,
        "syncedAt": _iso(record.synced_at),
        "deleted": record.deleted,
    }


def install_health_api(
    app: FastAPI,
    *,
    settings: Settings,
    database: Database,
) -> None:
    """Install Health routes protected by the shared device-token middleware."""

    persistence = HealthPersistence(database.session_factory)
    router = APIRouter(prefix="/api/v1/health", tags=["health"])

    def user_id() -> int:
        return persistence.ensure_user(
            display_name=settings.rhythm_player_display_name,
            time_zone=settings.timezone,
            discord_user_id=settings.allowed_user_id,
        )

    def map_persistence_error(exc: Exception) -> HealthApiError:
        if isinstance(exc, StaleHealthRevisionError):
            return HealthApiError(409, "stale_revision", "古いrevisionのため更新しませんでした。")
        if isinstance(exc, HealthRevisionConflictError):
            return HealthApiError(
                409,
                "revision_conflict",
                "同じrevisionに異なる内容が指定されています。",
            )
        return HealthApiError(422, "invalid_health_summary", "健康記録の内容が不正です。")

    @router.put("/sleep/{local_date}")
    def put_sleep(local_date: date, payload: SleepPutRequest):
        try:
            result = persistence.sync_sleep(
                user_id=user_id(),
                local_date=local_date,
                time_zone=payload.time_zone,
                sleep_start=payload.sleep_start,
                sleep_end=payload.sleep_end,
                asleep_minutes=payload.asleep_minutes,
                awake_minutes=payload.awake_minutes,
                core_minutes=payload.core_minutes,
                deep_minutes=payload.deep_minutes,
                rem_minutes=payload.rem_minutes,
                in_bed_minutes=payload.in_bed_minutes,
                source=payload.source,
                revision=payload.revision,
            )
        except (ValueError, HealthRevisionConflictError, StaleHealthRevisionError) as exc:
            raise map_persistence_error(exc) from exc
        return {
            "data": _sleep_json(result.record),
            "created": result.created,
            "idempotent": result.idempotent,
        }

    @router.get("/sleep/{local_date}")
    def get_sleep(local_date: date):
        record = persistence.get_sleep(user_id=user_id(), local_date=local_date)
        if record is None:
            raise HealthApiError(404, "sleep_not_found", "睡眠記録はまだありません。")
        return {"data": _sleep_json(record)}

    @router.delete("/sleep/{local_date}")
    def delete_sleep(
        local_date: date,
        revision: Annotated[int, Query(ge=0)],
        time_zone: Annotated[str, Query(alias="timeZone", max_length=64)] = "Asia/Tokyo",
    ):
        try:
            result = persistence.delete_sleep(
                user_id=user_id(),
                local_date=local_date,
                time_zone=time_zone,
                revision=revision,
            )
        except (ValueError, HealthRevisionConflictError, StaleHealthRevisionError) as exc:
            raise map_persistence_error(exc) from exc
        return {
            "data": _sleep_json(result.record),
            "created": result.created,
            "idempotent": result.idempotent,
        }

    @router.put("/workouts/{workout_id}")
    def put_workout(workout_id: str, payload: WorkoutPutRequest):
        try:
            result = persistence.sync_workout(
                user_id=user_id(),
                workout_key=workout_id,
                time_zone=payload.time_zone,
                activity_type=payload.activity_type,
                started_at=payload.started_at,
                ended_at=payload.ended_at,
                duration_seconds=payload.duration_seconds,
                distance_meters=payload.distance_meters,
                active_energy_kcal=payload.active_energy_kcal,
                average_heart_rate_bpm=payload.average_heart_rate_bpm,
                source=payload.source,
                revision=payload.revision,
            )
        except (ValueError, HealthRevisionConflictError, StaleHealthRevisionError) as exc:
            raise map_persistence_error(exc) from exc
        return {
            "data": _workout_json(result.record),
            "created": result.created,
            "idempotent": result.idempotent,
        }

    @router.get("/workouts/{workout_id}")
    def get_workout(workout_id: str):
        record = persistence.get_workout(user_id=user_id(), workout_key=workout_id)
        if record is None:
            raise HealthApiError(404, "workout_not_found", "ワークアウトはまだありません。")
        return {"data": _workout_json(record)}

    @router.delete("/workouts/{workout_id}")
    def delete_workout(
        workout_id: str,
        revision: Annotated[int, Query(ge=0)],
        time_zone: Annotated[str, Query(alias="timeZone", max_length=64)] = "Asia/Tokyo",
    ):
        try:
            result = persistence.delete_workout(
                user_id=user_id(),
                workout_key=workout_id,
                time_zone=time_zone,
                revision=revision,
            )
        except (ValueError, HealthRevisionConflictError, StaleHealthRevisionError) as exc:
            raise map_persistence_error(exc) from exc
        return {
            "data": _workout_json(result.record),
            "created": result.created,
            "idempotent": result.idempotent,
        }

    @router.get("/daily/{local_date}")
    def daily(local_date: date):
        resolved_user_id = user_id()
        sleep = persistence.get_sleep(user_id=resolved_user_id, local_date=local_date)
        _, workouts = persistence.records_since(
            user_id=resolved_user_id, date_from=local_date
        )
        daily_workouts = [row for row in workouts if row.local_date == local_date]
        return {
            "localDate": local_date.isoformat(),
            "sleep": _sleep_json(sleep) if sleep is not None else None,
            "workouts": [_workout_json(row) for row in daily_workouts],
        }

    @router.get("/status")
    def status():
        data = persistence.status(user_id=user_id())
        serialized = {
            key: _iso(value) if isinstance(value, datetime) else value
            for key, value in data.items()
        }
        return {
            **serialized,
            "serverEnabled": settings.health_enabled,
            "schedulerEnabled": settings.health_scheduler_enabled,
            "discordDryRun": settings.health_discord_dry_run,
            "discordChannelConfigured": settings.discord_health_channel_id is not None,
        }

    @app.exception_handler(HealthApiError)
    async def health_api_error_handler(_request, exc: HealthApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                }
            },
        )

    app.state.health_persistence = persistence
    app.include_router(router)
