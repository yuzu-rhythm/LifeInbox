"""Authenticated event-level iPhone synchronization for rhythm spending."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError

from .config import Settings
from .db import Database
from .rhythm_api import RhythmApiError
from .rhythm_collectors.common import safe_message
from .rhythm_persistence import RhythmPersistence
from .rhythm_spending import (
    SPENDING_GAMES,
    SPENDING_SOURCES,
    RhythmSpendingEventPersistence,
    SpendingDayTotals,
    SpendingEventRecord,
    SpendingEventRevisionConflictError,
    StaleSpendingEventRevisionError,
)


def canonical_event_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("eventId must be a UUID") from exc


class SpendingEventPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    event_id: str = Field(alias="eventId")
    revision: int = Field(ge=1)
    user_id: str = Field(alias="userId", min_length=1, max_length=100)
    game: str
    amount_yen: int = Field(alias="amountYen", ge=0, le=10_000_000)
    occurred_at: datetime = Field(alias="occurredAt")
    time_zone: str = Field(alias="timeZone", max_length=64)
    deleted: bool = False
    source: str

    @field_validator("event_id")
    @classmethod
    def normalize_event_id(cls, value: str) -> str:
        return canonical_event_id(value)

    @field_validator("user_id")
    @classmethod
    def normalize_user_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("userId must not be blank")
        return normalized

    @field_validator("game")
    @classmethod
    def validate_game(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in SPENDING_GAMES:
            raise ValueError("game is unsupported")
        return normalized

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in SPENDING_SOURCES:
            raise ValueError("source is unsupported")
        return normalized

    @field_validator("occurred_at")
    @classmethod
    def require_occurred_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurredAt must include a timezone")
        return value

    @field_validator("time_zone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timeZone must be a valid IANA timezone") from exc
        return value


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


def _event_json(record: SpendingEventRecord) -> dict[str, Any]:
    return {
        "eventId": record.event_id,
        "revision": record.revision,
        "userId": record.source_user_id,
        "game": record.game,
        "amountYen": record.amount_yen,
        "occurredAt": _iso(record.occurred_at),
        "localDate": record.local_date.isoformat(),
        "timeZone": record.time_zone,
        "deleted": record.deleted,
        "deletedAt": _iso(record.deleted_at),
        "source": record.source,
        "discord": {
            "channelId": record.discord_channel_id,
            "messageId": record.discord_message_id,
            "status": record.discord_status,
            "lastError": safe_message(record.discord_last_error),
            "attemptCount": record.discord_attempt_count,
            "nextRetryAt": _iso(record.discord_next_retry_at),
        },
        "syncedAt": _iso(record.synced_at),
        "createdAt": _iso(record.created_at),
        "updatedAt": _iso(record.updated_at),
    }


def _totals_json(totals: SpendingDayTotals) -> dict[str, Any]:
    return {
        "localDate": totals.local_date.isoformat(),
        "timeZone": totals.time_zone,
        "chunithmAmountYen": totals.chunithm_amount_yen,
        "maimaiAmountYen": totals.maimai_amount_yen,
        "ongekiAmountYen": totals.ongeki_amount_yen,
        "totalAmountYen": totals.total_amount_yen,
        "activeEventCount": totals.active_event_count,
    }


def install_rhythm_spending_event_api(
    app: FastAPI, *, settings: Settings, database: Database
) -> None:
    rhythm_store = RhythmPersistence(database.session_factory)
    event_store = RhythmSpendingEventPersistence(database.session_factory)
    router = APIRouter(prefix="/api/v1/rhythm/spending/events", tags=["rhythm-spending"])

    def rhythm_user_id() -> int:
        return rhythm_store.ensure_user(
            display_name=settings.rhythm_player_display_name,
            time_zone=settings.rhythm_time_zone,
            discord_user_id=settings.allowed_user_id,
        )

    @router.put("/{event_id}")
    def put_event(event_id: str, payload: SpendingEventPutRequest):
        try:
            path_event_id = canonical_event_id(event_id)
        except ValueError as exc:
            raise RhythmApiError(
                422, "invalid_event_id", "eventIdはUUID形式で指定してください。"
            ) from exc
        if path_event_id != payload.event_id:
            raise RhythmApiError(
                409,
                "event_id_mismatch",
                "URLとJSONのeventIdが一致しません。",
            )
        resolved_user_id = rhythm_user_id()
        try:
            result = event_store.sync_event(
                rhythm_user_id=resolved_user_id,
                event_id=path_event_id,
                source_user_id=payload.user_id,
                game=payload.game,
                amount_yen=payload.amount_yen,
                occurred_at=payload.occurred_at,
                time_zone=payload.time_zone,
                revision=payload.revision,
                source=payload.source,
                deleted=payload.deleted,
            )
        except StaleSpendingEventRevisionError as exc:
            raise RhythmApiError(
                409,
                "stale_revision",
                "古いrevisionのためイベントを更新しませんでした。",
            ) from exc
        except SpendingEventRevisionConflictError as exc:
            raise RhythmApiError(
                409,
                "revision_conflict",
                "同じeventIdとrevisionに異なる内容が指定されています。",
            ) from exc
        except ValueError as exc:
            raise RhythmApiError(
                422,
                "invalid_spending_event",
                "利用額イベントの値または日時が不正です。",
            ) from exc
        except SQLAlchemyError as exc:
            raise RhythmApiError(
                503,
                "storage_unavailable",
                "イベントを一時的に保存できません。再送してください。",
                retryable=True,
            ) from exc
        totals = event_store.daily_totals(
            rhythm_user_id=resolved_user_id,
            local_date=result.record.local_date,
            time_zone=result.record.time_zone,
        )
        return {
            "accepted": True,
            "created": result.created,
            "idempotent": result.idempotent,
            "data": _event_json(result.record),
            "dailyTotals": _totals_json(totals),
            "discordDeliveryIsAsynchronous": True,
        }

    @router.get("/{event_id}")
    def get_event(event_id: str):
        try:
            normalized = canonical_event_id(event_id)
        except ValueError as exc:
            raise RhythmApiError(
                422, "invalid_event_id", "eventIdはUUID形式で指定してください。"
            ) from exc
        record = event_store.get_event(
            rhythm_user_id=rhythm_user_id(), event_id=normalized
        )
        if record is None:
            raise RhythmApiError(
                404, "spending_event_not_found", "利用額イベントが見つかりません。"
            )
        totals = event_store.daily_totals(
            rhythm_user_id=record.rhythm_user_id,
            local_date=record.local_date,
            time_zone=record.time_zone,
        )
        return {"data": _event_json(record), "dailyTotals": _totals_json(totals)}

    app.include_router(router)
    app.state.rhythm_spending_event_persistence = event_store
