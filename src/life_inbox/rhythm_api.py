"""Authenticated iPhone/Shortcuts API for rhythm ratings and spending.

The API deliberately authenticates with a dedicated device token hash rather
than the Discord Bot token.  The raw bearer value is never persisted, logged,
or reflected in an error response.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .attachments import SUPPORTED_IMAGE_TYPES, AttachmentError, AttachmentStore
from .config import Settings
from .constants import EventKind, EventStatus
from .db import Database
from .kiseki_sync import (
    KisekiMomentDeletePayload,
    KisekiMomentPayload,
    KisekiMomentRevisionConflictError,
    KisekiSyncService,
    StaleKisekiMomentRevisionError,
)
from .models import Attachment, Event, EventCaptureContext, Memo, ShortcutInboxDelivery
from .rhythm_collectors.common import safe_message
from .rhythm_images import RatingImageVerificationError, verify_rating_image
from .rhythm_persistence import (
    RHYTHM_GAMES,
    CollectionRunConflictError,
    RhythmPersistence,
    SpendingRecord,
    SpendingRevisionConflictError,
    StaleSpendingRevisionError,
    utc_naive,
)


class RhythmApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


class _RateLimiter:
    """Small process-local request limiter; secrets are represented by hashes."""

    def __init__(self, limit: int, *, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key_hash: str) -> None:
        now = time.monotonic()
        with self._lock:
            entries = self._requests[key_hash]
            while entries and entries[0] <= now - self.window_seconds:
                entries.popleft()
            if len(entries) >= self.limit:
                raise RhythmApiError(
                    429,
                    "rate_limited",
                    "リクエストが多すぎます。少し待って再試行してください。",
                    retryable=True,
                )
            entries.append(now)


class SpendingPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    time_zone: str = Field(alias="timeZone", max_length=64)
    chunithm_amount_yen: int = Field(alias="chunithmAmountYen", ge=0, le=10_000_000)
    maimai_amount_yen: int = Field(alias="maimaiAmountYen", ge=0, le=10_000_000)
    ongeki_amount_yen: int = Field(alias="ongekiAmountYen", ge=0, le=10_000_000)
    total_amount_yen: int = Field(alias="totalAmountYen", ge=0, le=30_000_000)
    revision: int = Field(ge=0)

    @field_validator("time_zone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timeZone must be a valid IANA timezone") from exc
        return value


class CollectionRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    games: list[str] = Field(default_factory=lambda: sorted(RHYTHM_GAMES), min_length=1)

    @field_validator("games")
    @classmethod
    def validate_games(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(game.strip().casefold() for game in value))
        if not normalized or any(game not in RHYTHM_GAMES for game in normalized):
            raise ValueError("games contains an unsupported game")
        return normalized


class ShortcutMemoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    body: str = Field(min_length=1, max_length=1_000)
    occurred_at: datetime | None = Field(default=None, alias="occurredAt")

    @field_validator("body")
    @classmethod
    def normalize_body(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("body must not be blank")
        return normalized

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("occurredAt must include a timezone")
        return value


ShortcutCaptureType = Literal[
    "normal",
    "memo",
    "task",
    "meal",
    "expense",
    "rhythm",
    "career",
    "monologue",
    "shopping",
    "link",
    "memory",
]
ShortcutCaptureSource = Literal[
    "actionButton",
    "shortcut",
    "widget",
    "siri",
    "shareSheet",
    "mac",
]

SHORTCUT_CAPTURE_TYPES: tuple[str, ...] = (
    "normal",
    "memo",
    "task",
    "meal",
    "expense",
    "rhythm",
    "career",
    "monologue",
    "shopping",
    "link",
    "memory",
)
SHORTCUT_CAPTURE_SOURCES: tuple[str, ...] = (
    "actionButton",
    "shortcut",
    "widget",
    "siri",
    "shareSheet",
    "mac",
)


class ShortcutPhotoRequest(BaseModel):
    """One bounded image explicitly selected by an Apple Shortcut."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    filename: str = Field(min_length=1, max_length=255)
    content_type: Literal[
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
    ] = Field(alias="contentType")
    data_base64: str = Field(alias="dataBase64", min_length=4, max_length=14_000_000)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")

    @field_validator("filename")
    @classmethod
    def normalize_filename(cls, value: str) -> str:
        normalized = Path(value).name.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("photo filename is invalid")
        return normalized


class ShortcutInboxRequest(BaseModel):
    """Raw-first capture payload shared by iPhone, iPad, and Mac shortcuts."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    body: str = Field(default="", max_length=4_000)
    capture_type: ShortcutCaptureType = Field(default="normal", alias="captureType")
    source: ShortcutCaptureSource = "shortcut"
    occurred_at: datetime | None = Field(default=None, alias="occurredAt")
    location: ShortcutLocationRequest | None = None
    photo: ShortcutPhotoRequest | None = None

    @field_validator("body")
    @classmethod
    def normalize_body(cls, value: str) -> str:
        normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if any(ord(character) < 32 and character not in "\n\t" for character in normalized):
            raise ValueError("body contains unsupported control characters")
        return normalized

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("occurredAt must include a timezone")
        return value

    @model_validator(mode="after")
    def require_body_or_photo(self) -> ShortcutInboxRequest:
        if not self.body and self.photo is None:
            raise ValueError("body or photo is required")
        return self


class ShortcutLocationRequest(BaseModel):
    """One explicitly shared current location for one Shortcut capture."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    horizontal_accuracy_meters: float | None = Field(
        default=None,
        alias="horizontalAccuracyMeters",
        ge=0,
        le=100_000,
    )
    label: str | None = Field(default=None, max_length=255)
    shared_at: datetime | None = Field(default=None, alias="sharedAt")

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None

    @field_validator("shared_at")
    @classmethod
    def require_shared_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("location.sharedAt must include a timezone")
        return value


ShortcutInboxRequest.model_rebuild()


@dataclass(frozen=True, slots=True)
class _DecodedShortcutPhoto:
    filename: str
    content_type: str
    data: bytes = field(repr=False)
    sha256: str


def _image_signature_matches(content_type: str, data: bytes) -> bool:
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False


def _decode_shortcut_photo(
    photo: ShortcutPhotoRequest | None,
    *,
    max_bytes: int,
) -> _DecodedShortcutPhoto | None:
    if photo is None:
        return None
    try:
        data = base64.b64decode(photo.data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RhythmApiError(
            422,
            "invalid_photo",
            "写真データを読み取れませんでした。",
        ) from exc
    if not data or len(data) > max_bytes:
        raise RhythmApiError(
            413,
            "photo_too_large",
            "写真が空か、許可されたサイズを超えています。",
        )
    if photo.content_type not in SUPPORTED_IMAGE_TYPES or not _image_signature_matches(
        photo.content_type,
        data,
    ):
        raise RhythmApiError(
            422,
            "invalid_photo",
            "写真形式とファイル内容が一致しません。",
        )
    digest = hashlib.sha256(data).hexdigest()
    if photo.sha256 is not None and not hmac.compare_digest(
        digest,
        photo.sha256.casefold(),
    ):
        raise RhythmApiError(
            422,
            "photo_hash_mismatch",
            "写真の整合性を確認できませんでした。",
        )
    return _DecodedShortcutPhoto(
        filename=photo.filename,
        content_type=photo.content_type,
        data=data,
        sha256=digest,
    )


def _discard_shortcut_photo(path: Path | None) -> None:
    """Best-effort rollback cleanup without masking the original API error."""

    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


class KisekiMomentActiveRequest(BaseModel):
    """Allowlisted wire shape for one explicitly selected Kiseki Moment.

    Coordinates, location accuracy, addresses, routes, privacy-zone metadata,
    and full Kiseki archives are intentionally absent.  ``extra='forbid'``
    makes those fields invalid rather than silently discarding them.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal[1] = Field(alias="schemaVersion")
    revision: StrictInt = Field(ge=1)
    occurred_at: datetime = Field(alias="occurredAt")
    title: StrictStr = Field(min_length=1, max_length=200)
    note: StrictStr = Field(default="", max_length=10_000)
    mood: StrictInt | None = Field(default=None, ge=1, le=5)
    favorite: StrictBool = False
    origin: StrictStr = Field(default="iphone", min_length=1, max_length=32)
    privacy_processed: Literal[True] = Field(alias="privacyProcessed")
    deleted: Literal[False] = False


class KisekiMomentDeleteRequest(BaseModel):
    """Content-free tombstone wire shape for a promoted Kiseki Moment."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal[1] = Field(alias="schemaVersion")
    revision: StrictInt = Field(ge=1)
    privacy_processed: Literal[True] = Field(alias="privacyProcessed")
    deleted: Literal[True]


_SHORTCUT_SOURCE_NAMES: dict[str, str] = {
    "actionButton": "iphone_action_button",
    "shortcut": "iphone_shortcut",
    "widget": "iphone_widget",
    "siri": "iphone_siri",
    "shareSheet": "iphone_share_sheet",
    "mac": "mac_shortcut",
}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


def _number(value: Any) -> int | float | None:
    if value is None:
        return None
    numeric = float(value)
    return int(numeric) if numeric.is_integer() else numeric


def _spending_json(record: SpendingRecord) -> dict[str, Any]:
    return {
        "localDate": record.local_date.isoformat(),
        "timeZone": record.time_zone,
        "chunithmAmountYen": record.chunithm_amount_yen,
        "maimaiAmountYen": record.maimai_amount_yen,
        "ongekiAmountYen": record.ongeki_amount_yen,
        "totalAmountYen": record.total_amount_yen,
        "revision": record.revision,
        "syncedAt": _iso(record.synced_at),
        "deleted": record.deleted,
    }


def _image_url(snapshot: Any) -> str | None:
    if not snapshot.image_links:
        return None
    return f"/api/v1/rhythm/ratings/{snapshot.id}/image"


def _snapshot_json(snapshot: Any, *, include_game: bool = False) -> dict[str, Any]:
    result = {
        "snapshotId": snapshot.id,
        "rating": _number(snapshot.rating),
        "previousRating": _number(snapshot.previous_rating),
        "delta": _number(snapshot.rating_delta),
        "collectedAt": _iso(snapshot.collected_at),
        "sourceUpdatedAt": _iso(snapshot.source_updated_at),
        "imageUrl": _image_url(snapshot),
        "semanticFingerprint": snapshot.semantic_fingerprint,
        "status": "success" if snapshot.image_links else "collected_without_image",
    }
    if include_game:
        result["game"] = snapshot.game
    return result


def _run_json(run: Any) -> dict[str, Any]:
    return {
        "runId": run.id,
        "game": run.game,
        "state": run.state,
        "progressMessage": safe_message(run.progress_message),
        "authenticationRequired": run.state == "authenticationRequired",
        "manualActionRequired": (
            run.state == "waitingForManualAction" and run.manual_action is not None
        ),
        "manualAction": safe_message(run.manual_action),
        "lastAttemptAt": _iso(run.started_at or run.created_at),
        "lastErrorCode": safe_message(run.last_error_code),
        "lastErrorMessage": safe_message(run.last_error_message),
        "retryCount": run.retry_count,
    }


def _next_daily_run(settings: Settings, *, now: datetime | None = None) -> datetime:
    timezone = ZoneInfo(settings.rhythm_time_zone)
    current = (now or datetime.now(UTC)).astimezone(timezone)
    minute, hour, *_ = settings.rhythm_daily_post_cron.split()
    candidate = datetime.combine(
        current.date(),
        datetime_time(hour=int(hour), minute=int(minute)),
        tzinfo=timezone,
    )
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def install_rhythm_api(
    app: FastAPI,
    *,
    settings: Settings,
    database: Database,
) -> None:
    """Install authenticated routes without mutating the DB at app creation."""

    persistence = RhythmPersistence(database.session_factory)
    kiseki_sync = KisekiSyncService(database.session_factory)
    shortcut_attachment_store = AttachmentStore(
        settings.data_dir,
        max_bytes=settings.attachment_max_bytes,
    )
    limiter = _RateLimiter(settings.rhythm_api_rate_limit_per_minute)
    router = APIRouter(prefix="/api/v1", tags=["rhythm"])

    def authorize(authorization: str | None) -> None:
        expected_secret = settings.rhythm_device_sync_token_sha256
        if expected_secret is None:
            raise RhythmApiError(
                503,
                "device_auth_not_configured",
                "iPhone連携用の認証が設定されていません。",
            )
        if authorization is None or not authorization.startswith("Bearer "):
            raise RhythmApiError(401, "unauthorized", "認証が必要です。")
        token = authorization.removeprefix("Bearer ")
        supplied_hash = hashlib.sha256(token.encode()).hexdigest()
        expected_hash = expected_secret.get_secret_value()
        if not hmac.compare_digest(supplied_hash, expected_hash):
            raise RhythmApiError(401, "unauthorized", "認証に失敗しました。")
        limiter.check(supplied_hash)

    def error_response(exc: RhythmApiError) -> JSONResponse:
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

    @app.middleware("http")
    async def rhythm_authentication_middleware(request: Request, call_next):
        """Authenticate protected paths before route/body validation.

        FastAPI validates path and JSON parameters before entering endpoint
        functions. A middleware gate prevents malformed unauthenticated bodies
        from bypassing the documented 401/503 authentication contract and
        avoids parsing secrets-bearing payloads before rate limiting.
        """

        if request.url.path.startswith(
            (
                "/api/v1/rhythm/",
                "/api/v1/shortcuts/",
                "/api/v1/health/",
                "/api/v1/archive/",
            )
        ):
            try:
                authorize(request.headers.get("authorization"))
            except RhythmApiError as exc:
                return error_response(exc)
        return await call_next(request)

    def user_id() -> int:
        return persistence.ensure_user(
            display_name=settings.rhythm_player_display_name,
            time_zone=settings.rhythm_time_zone,
            discord_user_id=settings.allowed_user_id,
        )

    def _kiseki_record_json(record) -> dict[str, Any]:
        return {
            "momentId": record.moment_id,
            "revision": record.revision,
            "eventId": record.event_id,
            "deleted": record.deleted,
            "status": (
                EventStatus.IGNORED.value
                if record.deleted
                else EventStatus.CONFIRMED.value
            ),
            "semanticFingerprint": record.semantic_fingerprint,
            "syncedAt": _iso(record.synced_at),
            "deletedAt": _iso(record.deleted_at),
        }

    @router.get("/shortcuts/capabilities")
    def shortcut_capabilities():
        """Return a non-secret, machine-readable client integration contract."""

        return {
            "apiVersion": 1,
            "authentication": "bearer-device-token",
            "timeZone": settings.rhythm_time_zone,
            "anythingInbox": {
                "path": "/api/v1/shortcuts/inbox",
                "method": "POST",
                "maxBodyCharacters": 4_000,
                "captureTypes": list(SHORTCUT_CAPTURE_TYPES),
                "sources": list(SHORTCUT_CAPTURE_SOURCES),
                "idempotencyKeyRequired": True,
                "classificationDeferred": True,
                "discordDeliveryIsAsynchronous": True,
                "explicitLocationSupported": True,
                "continuousTrackingSupported": False,
                "photoSupported": True,
                "maxPhotoCount": 1,
                "maxPhotoBytes": settings.attachment_max_bytes,
                "photoContentTypes": sorted(SUPPORTED_IMAGE_TYPES),
            },
            "memo": {
                "path": "/api/v1/shortcuts/memo",
                "method": "POST",
                "maxBodyCharacters": 1_000,
                "idempotencyKeyRequired": True,
            },
            "kisekiMoments": {
                "pathTemplate": "/api/v1/archive/kiseki/moments/{momentId}",
                "methods": ["GET", "PUT"],
                "schemaVersion": 1,
                "idempotencyKeyTemplate": "kiseki-moment:{momentId}:{revision}",
                "explicitSelectionRequired": True,
                "privacyProcessedRequired": True,
                "rawLocationAccepted": False,
                "discordDeliveryIsAsynchronous": True,
                "notionDeliveryIsExplicit": True,
            },
            "today": {
                "path": "/api/v1/shortcuts/today",
                "method": "GET",
            },
            "health": {
                "statusPath": "/api/v1/health/status",
                "dailyPathTemplate": "/api/v1/health/daily/{localDate}",
                "sleepPathTemplate": "/api/v1/health/sleep/{localDate}",
                "workoutPathTemplate": "/api/v1/health/workouts/{sha256WorkoutId}",
                "serverEnabled": settings.health_enabled,
                "schedulerEnabled": settings.health_scheduler_enabled,
                "discordDryRun": settings.health_discord_dry_run,
            },
        }

    @router.put("/rhythm/spending/{local_date}", dependencies=[])
    def put_spending(
        local_date: date,
        payload: SpendingPutRequest,
    ):
        try:
            result = persistence.sync_spending(
                user_id=user_id(),
                local_date=local_date,
                time_zone=payload.time_zone,
                chunithm_amount_yen=payload.chunithm_amount_yen,
                maimai_amount_yen=payload.maimai_amount_yen,
                ongeki_amount_yen=payload.ongeki_amount_yen,
                total_amount_yen=payload.total_amount_yen,
                revision=payload.revision,
            )
        except StaleSpendingRevisionError as exc:
            raise RhythmApiError(
                409, "stale_revision", "古いrevisionのため更新しませんでした。"
            ) from exc
        except SpendingRevisionConflictError as exc:
            raise RhythmApiError(
                409,
                "revision_conflict",
                "同じrevisionに異なる内容が指定されています。",
            ) from exc
        except ValueError as exc:
            raise RhythmApiError(
                422,
                "invalid_spending",
                "利用額、合計、revisionの組み合わせが不正です。",
            ) from exc
        return {
            "data": _spending_json(result.record),
            "created": result.created,
            "idempotent": result.idempotent,
        }

    @router.get("/rhythm/spending/{local_date}")
    def get_spending(
        local_date: date,
    ):
        record = persistence.get_spending(user_id=user_id(), local_date=local_date)
        if record is None:
            raise RhythmApiError(404, "spending_not_found", "利用額はまだありません。")
        return {"data": _spending_json(record)}

    @router.delete("/rhythm/spending/{local_date}")
    def delete_spending(
        local_date: date,
        revision: Annotated[int, Query(ge=0)],
        time_zone: Annotated[str, Query(alias="timeZone", max_length=64)] = "Asia/Tokyo",
    ):
        try:
            ZoneInfo(time_zone)
            result = persistence.delete_spending(
                user_id=user_id(),
                local_date=local_date,
                time_zone=time_zone,
                revision=revision,
            )
        except ZoneInfoNotFoundError as exc:
            raise RhythmApiError(422, "invalid_timezone", "timeZoneが不正です。") from exc
        except StaleSpendingRevisionError as exc:
            raise RhythmApiError(409, "stale_revision", "古いrevisionです。") from exc
        except SpendingRevisionConflictError as exc:
            raise RhythmApiError(409, "revision_conflict", "revisionが競合しました。") from exc
        return {
            "data": _spending_json(result.record),
            "created": result.created,
            "idempotent": result.idempotent,
        }

    @router.get("/rhythm/ratings/latest")
    def latest_ratings():
        resolved_user_id = user_id()
        data: dict[str, Any] = {}
        for game in ("chunithm", "maimai", "ongeki"):
            snapshot = persistence.latest_rendered_snapshot(
                user_id=resolved_user_id,
                game=game,
            )
            if snapshot is None:
                data[game] = {
                    "rating": None,
                    "previousRating": None,
                    "delta": None,
                    "collectedAt": None,
                    "imageUrl": None,
                    "status": "not_collected",
                }
            else:
                data[game] = _snapshot_json(snapshot)
        return data

    @router.get("/rhythm/ratings/history")
    def rating_history(
        game: Annotated[str | None, Query()] = None,
        date_from: Annotated[date | None, Query(alias="from")] = None,
        date_to: Annotated[date | None, Query(alias="to")] = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 50,
    ):
        if game is not None and game not in RHYTHM_GAMES:
            raise RhythmApiError(422, "invalid_game", "gameが不正です。")
        if date_from is not None and date_to is not None and date_from > date_to:
            raise RhythmApiError(422, "invalid_date_range", "fromはto以前にしてください。")
        timezone = ZoneInfo(settings.rhythm_time_zone)
        collected_from = (
            datetime.combine(date_from, datetime_time.min, tzinfo=timezone)
            if date_from is not None
            else None
        )
        collected_to = (
            datetime.combine(date_to + timedelta(days=1), datetime_time.min, tzinfo=timezone)
            if date_to is not None
            else None
        )
        rows, total = persistence.list_snapshots(
            user_id=user_id(),
            game=game,
            collected_from=collected_from,
            collected_to=collected_to,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        return {
            "items": [_snapshot_json(row, include_game=True) for row in rows],
            "page": page,
            "pageSize": page_size,
            "total": total,
            "hasNext": page * page_size < total,
        }

    @router.get("/rhythm/ratings/{snapshot_id}/image")
    def rating_image(
        snapshot_id: int,
    ):
        snapshot = persistence.get_snapshot(user_id=user_id(), snapshot_id=snapshot_id)
        if snapshot is None or not snapshot.image_links:
            raise RhythmApiError(404, "image_not_found", "画像が見つかりません。")
        image = snapshot.image_links[0].image
        try:
            path = verify_rating_image(
                image_root=settings.rhythm_image_dir,
                image_path=image.image_path,
                expected_sha256=image.image_sha256,
                expected_byte_size=image.byte_size,
            )
        except RatingImageVerificationError:
            path = None
        if path is None:
            raise RhythmApiError(404, "image_not_found", "画像が見つかりません。")
        media_type = "image/jpeg" if path.suffix.casefold() in {".jpg", ".jpeg"} else "image/png"
        response = FileResponse(path, media_type=media_type)
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/rhythm/collection/status")
    def collection_status():
        resolved_user_id = user_id()
        latest_runs = persistence.latest_runs(user_id=resolved_user_id)
        failed_runs = persistence.latest_failed_runs(user_id=resolved_user_id)
        delivery_statuses = persistence.delivery_statuses(user_id=resolved_user_id)
        provider_statuses = persistence.provider_statuses(user_id=resolved_user_id)
        data: dict[str, Any] = {}
        for game in ("chunithm", "maimai", "ongeki"):
            run = latest_runs.get(game)
            failed_run = failed_runs.get(game)
            snapshot = persistence.latest_rendered_snapshot(
                user_id=resolved_user_id,
                game=game,
            )
            session_path = settings.rhythm_browser_session_dir / game
            delivery = delivery_statuses[game]
            provider = provider_statuses.get(game)
            item = (
                _run_json(run)
                if run is not None
                else {
                    "runId": None,
                    "game": game,
                    "state": "not_started",
                    "progressMessage": "まだ取得していません",
                    "authenticationRequired": False,
                    "manualActionRequired": False,
                    "manualAction": None,
                    "lastAttemptAt": None,
                    "lastErrorCode": None,
                    "lastErrorMessage": None,
                    "retryCount": 0,
                }
            )
            item["lastSuccessAt"] = _iso(snapshot.collected_at) if snapshot else None
            item["lastFailureAt"] = (
                _iso(failed_run.finished_at) if failed_run is not None else None
            )
            item["lastDiscordPostAt"] = _iso(delivery.last_posted_at)
            item["unpostedCount"] = delivery.pending_or_failed_count
            item["providerSessionStatus"] = (
                provider.session_status if provider is not None else "not_configured"
            )
            item["browserSessionStatus"] = (
                "present_unverified" if session_path.exists() else "not_present"
            )
            data[game] = item
        return {
            "games": data,
            "nextRunAt": _iso(_next_daily_run(settings)),
            "schedulerEnabled": settings.rhythm_scheduler_enabled,
            "dryRun": settings.rhythm_discord_dry_run,
        }

    @router.post("/rhythm/collection/run")
    def run_collection(
        payload: CollectionRunRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        if not settings.rhythm_enabled:
            raise RhythmApiError(503, "rhythm_disabled", "音ゲー収集は無効です。")
        if idempotency_key is None or not 8 <= len(idempotency_key) <= 200:
            raise RhythmApiError(
                400,
                "idempotency_key_required",
                "8〜200文字のIdempotency-Keyが必要です。",
            )
        try:
            runs = persistence.queue_collection_runs(
                user_id=user_id(),
                games=payload.games,
                trigger="iphone",
                request_key=idempotency_key,
            )
        except CollectionRunConflictError as exc:
            raise RhythmApiError(
                409,
                "idempotency_key_conflict",
                "同じIdempotency-Keyが異なる取得対象に使われています。",
            ) from exc
        return {"runs": [_run_json(run) for run in runs], "accepted": True}

    @router.post("/rhythm/collection/{run_id}/confirm")
    def confirm_collection(
        run_id: str,
    ):
        resolved_user_id = user_id()
        run = persistence.get_run(run_id)
        if run is None or run.user_id != resolved_user_id:
            raise RhythmApiError(404, "run_not_found", "取得実行が見つかりません。")
        try:
            confirmed = persistence.confirm_manual_action(run_id=run_id)
        except CollectionRunConflictError as exc:
            raise RhythmApiError(
                409,
                "manual_action_not_pending",
                "この実行は手動確認待ちではありません。",
            ) from exc
        return {"run": _run_json(confirmed), "confirmed": True}

    @router.get("/archive/kiseki/moments/{moment_id}")
    def get_kiseki_moment(moment_id: UUID):
        """Return sync metadata only; selected Moment text stays in the vault."""

        record = kiseki_sync.get_moment(moment_id)
        if record is None:
            raise RhythmApiError(404, "kiseki_moment_not_found", "Momentが見つかりません。")
        return {
            "data": _kiseki_record_json(record),
            "discordDeliveryIsAsynchronous": True,
            "notionDeliveryIsExplicit": True,
        }

    @router.put("/archive/kiseki/moments/{moment_id}")
    def put_kiseki_moment(
        moment_id: UUID,
        payload: KisekiMomentActiveRequest | KisekiMomentDeleteRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        """Store one explicitly selected, privacy-processed Moment revision."""

        expected_key = f"kiseki-moment:{moment_id}:{payload.revision}"
        if idempotency_key is None:
            raise RhythmApiError(
                400,
                "idempotency_key_required",
                "Kiseki Moment用のIdempotency-Keyが必要です。",
            )
        if idempotency_key != expected_key:
            raise RhythmApiError(
                409,
                "idempotency_key_conflict",
                "Idempotency-KeyがMoment IDまたはrevisionと一致しません。",
            )
        try:
            if isinstance(payload, KisekiMomentDeleteRequest):
                result = kiseki_sync.delete(
                    KisekiMomentDeletePayload.model_validate(
                        {
                            "momentId": str(moment_id),
                            "revision": payload.revision,
                            "privacyProcessed": payload.privacy_processed,
                        }
                    )
                )
            else:
                result = kiseki_sync.upsert_moment(
                    KisekiMomentPayload.model_validate(
                        {
                            "momentId": str(moment_id),
                            "revision": payload.revision,
                            "occurredAt": payload.occurred_at,
                            "title": payload.title,
                            "note": payload.note,
                            "mood": payload.mood,
                            "favorite": payload.favorite,
                            "origin": payload.origin,
                            "privacyProcessed": payload.privacy_processed,
                        }
                    )
                )
        except StaleKisekiMomentRevisionError as exc:
            raise RhythmApiError(
                409,
                "stale_revision",
                "古いKiseki Moment revisionのため更新しませんでした。",
            ) from exc
        except KisekiMomentRevisionConflictError as exc:
            raise RhythmApiError(
                409,
                "revision_conflict",
                "同じrevisionに異なるKiseki Moment内容が指定されています。",
            ) from exc
        except ValueError as exc:
            raise RhythmApiError(
                422,
                "invalid_kiseki_moment",
                "Kiseki Momentの形式または値が不正です。",
            ) from exc
        return {
            "accepted": True,
            "created": result.created,
            "idempotent": result.idempotent,
            "data": _kiseki_record_json(result.record),
            "discordDeliveryIsAsynchronous": True,
            "notionDeliveryIsExplicit": True,
        }

    @router.post("/shortcuts/inbox")
    def shortcut_inbox(
        payload: ShortcutInboxRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        """Durably accept one raw capture without invoking AI or Discord.

        The caller owns the idempotency key and must reuse it when an offline
        queue retries the same capture.  The capture type is only a routing
        hint: the Event intentionally remains pending until the normal Inbox
        workflow classifies or confirms it.
        """

        if idempotency_key is None or not 8 <= len(idempotency_key) <= 200:
            raise RhythmApiError(
                400,
                "idempotency_key_required",
                "8〜200文字のIdempotency-Keyが必要です。",
            )
        message_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()[:48]
        message_id = f"shortcut-inbox:{message_hash}"
        channel_id = f"shortcut-inbox:{payload.capture_type}"
        source = _SHORTCUT_SOURCE_NAMES[payload.source]
        captured = payload.occurred_at or datetime.now(UTC)
        normalized_time = utc_naive(captured)
        decoded_photo = _decode_shortcut_photo(
            payload.photo,
            max_bytes=settings.attachment_max_bytes,
        )

        def same_photo(existing: Event) -> bool:
            attachments = tuple(existing.attachments or ())
            if decoded_photo is None:
                return not attachments
            if len(attachments) != 1:
                return False
            attachment = attachments[0]
            return (
                attachment.sha256 == decoded_photo.sha256
                and attachment.content_type == decoded_photo.content_type
                and attachment.original_filename == decoded_photo.filename
            )

        def matches(existing: Event) -> bool:
            same_time = (
                payload.occurred_at is None
                or existing.occurred_at == normalized_time
            )
            context = existing.capture_context
            location = payload.location
            same_location = (
                context is not None
                and context.capture_type == payload.capture_type
                and context.source_label == payload.source
                and (
                    (location is None and context.latitude is None and context.longitude is None)
                    or (
                        location is not None
                        and context.latitude == location.latitude
                        and context.longitude == location.longitude
                        and context.horizontal_accuracy_meters
                        == location.horizontal_accuracy_meters
                        and context.location_label == location.label
                        and (
                            location.shared_at is None
                            or context.location_shared_at == utc_naive(location.shared_at)
                        )
                    )
                )
            )
            return (
                existing.raw_text == payload.body
                and existing.discord_channel_id == channel_id
                and existing.source == source
                and same_time
                and same_location
                and same_photo(existing)
            )

        def result(existing: Event, *, idempotent: bool) -> dict[str, Any]:
            return {
                "eventId": existing.id,
                "idempotent": idempotent,
                "status": existing.status,
                "captureType": payload.capture_type,
                "source": payload.source,
                "classificationDeferred": True,
                "locationShared": payload.location is not None,
                "photoStored": decoded_photo is not None,
                "discordDeliveryIsAsynchronous": True,
            }

        session = database.session_factory()
        saved_photo_path: Path | None = None
        try:
            existing = session.scalar(
                select(Event).where(Event.discord_message_id == message_id)
            )
            if existing is not None:
                if not matches(existing):
                    raise RhythmApiError(
                        409,
                        "idempotency_key_conflict",
                        "同じIdempotency-Keyが異なる入力内容に使われています。",
                    )
                return result(existing, idempotent=True)
            event = Event(
                discord_message_id=message_id,
                discord_channel_id=channel_id,
                discord_user_id=str(settings.allowed_user_id or user_id()),
                captured_at=normalized_time,
                occurred_at=normalized_time,
                raw_text=payload.body,
                source=source,
                status=EventStatus.PENDING.value,
                event_type="unknown",
                event_kinds=[],
            )
            location = payload.location
            event.capture_context = EventCaptureContext(
                capture_type=payload.capture_type,
                source_label=payload.source,
                latitude=location.latitude if location is not None else None,
                longitude=location.longitude if location is not None else None,
                horizontal_accuracy_meters=(
                    location.horizontal_accuracy_meters if location is not None else None
                ),
                location_label=location.label if location is not None else None,
                location_shared_at=(
                    utc_naive(location.shared_at or captured)
                    if location is not None
                    else None
                ),
            )
            event.shortcut_delivery = ShortcutInboxDelivery(status="pending")
            session.add(event)
            session.flush()
            if decoded_photo is not None:
                local_path, digest = shortcut_attachment_store.save(
                    event_id=event.id,
                    original_filename=decoded_photo.filename,
                    content_type=decoded_photo.content_type,
                    data=decoded_photo.data,
                )
                saved_photo_path = Path(local_path)
                session.add(
                    Attachment(
                        event_id=event.id,
                        discord_attachment_id=f"shortcut-photo:{message_hash}",
                        original_filename=decoded_photo.filename,
                        content_type=decoded_photo.content_type,
                        local_path=local_path,
                        sha256=digest,
                    )
                )
            session.commit()
            return result(event, idempotent=False)
        except (AttachmentError, OSError) as exc:
            session.rollback()
            _discard_shortcut_photo(saved_photo_path)
            raise RhythmApiError(
                503,
                "photo_storage_unavailable",
                "写真を安全に保存できませんでした。再送してください。",
                retryable=True,
            ) from exc
        except IntegrityError:
            session.rollback()
            _discard_shortcut_photo(saved_photo_path)
            retry = database.session_factory()
            try:
                existing = retry.scalar(
                    select(Event).where(Event.discord_message_id == message_id)
                )
                if existing is None:
                    raise RuntimeError(
                        "concurrent shortcut inbox insert could not be resolved"
                    )
                if not matches(existing):
                    raise RhythmApiError(
                        409,
                        "idempotency_key_conflict",
                        "同じIdempotency-Keyが異なる入力内容に使われています。",
                    )
                return result(existing, idempotent=True)
            finally:
                retry.close()
        except Exception:
            session.rollback()
            _discard_shortcut_photo(saved_photo_path)
            raise
        finally:
            session.close()

    @router.post("/shortcuts/memo")
    def shortcut_memo(
        payload: ShortcutMemoRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        if idempotency_key is None or not 8 <= len(idempotency_key) <= 200:
            raise RhythmApiError(
                400,
                "idempotency_key_required",
                "8〜200文字のIdempotency-Keyが必要です。",
            )
        message_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        message_id = f"shortcut:{message_hash}"
        occurred = payload.occurred_at or datetime.now(UTC)
        normalized_time = utc_naive(occurred)
        session = database.session_factory()
        try:
            existing = session.scalar(
                select(Event).where(Event.discord_message_id == message_id)
            )
            if existing is not None:
                same_time = (
                    payload.occurred_at is None
                    or existing.occurred_at == normalized_time
                )
                if existing.raw_text != payload.body or not same_time:
                    raise RhythmApiError(
                        409,
                        "idempotency_key_conflict",
                        "同じIdempotency-Keyが異なるメモ内容に使われています。",
                    )
                return {"eventId": existing.id, "idempotent": True}
            event = Event(
                discord_message_id=message_id,
                discord_channel_id="iphone-shortcut",
                discord_user_id=str(settings.allowed_user_id or user_id()),
                captured_at=normalized_time,
                occurred_at=normalized_time,
                raw_text=payload.body,
                source="iphone_shortcut",
                status=EventStatus.CONFIRMED.value,
                event_type=EventKind.MEMO.value,
                event_kinds=[EventKind.MEMO.value],
            )
            event.memo = Memo(body=payload.body)
            session.add(event)
            session.commit()
            return {"eventId": event.id, "idempotent": False}
        except IntegrityError:
            session.rollback()
            retry = database.session_factory()
            try:
                existing = retry.scalar(
                    select(Event).where(Event.discord_message_id == message_id)
                )
                same_time = (
                    existing is not None
                    and (
                        payload.occurred_at is None
                        or existing.occurred_at == normalized_time
                    )
                )
                if existing is None:
                    raise RuntimeError(
                        "concurrent shortcut memo insert could not be resolved"
                    )
                if existing.raw_text != payload.body or not same_time:
                    raise RhythmApiError(
                        409,
                        "idempotency_key_conflict",
                        "同じIdempotency-Keyが異なるメモ内容に使われています。",
                    )
                return {"eventId": existing.id, "idempotent": True}
            finally:
                retry.close()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @router.get("/shortcuts/today")
    def shortcut_today():
        """Return one compact payload suitable for an Action Button shortcut."""

        resolved_user_id = user_id()
        local_date = datetime.now(ZoneInfo(settings.rhythm_time_zone)).date()
        spending = persistence.get_spending(
            user_id=resolved_user_id,
            local_date=local_date,
        )
        latest_runs = persistence.latest_runs(user_id=resolved_user_id)
        ratings: dict[str, Any] = {}
        collection: dict[str, Any] = {}
        for game in ("chunithm", "maimai", "ongeki"):
            snapshot = persistence.latest_rendered_snapshot(
                user_id=resolved_user_id,
                game=game,
            )
            ratings[game] = (
                _snapshot_json(snapshot)
                if snapshot is not None
                else {
                    "rating": None,
                    "previousRating": None,
                    "delta": None,
                    "collectedAt": None,
                    "imageUrl": None,
                    "status": "not_collected",
                }
            )
            run = latest_runs.get(game)
            collection[game] = {
                "state": run.state if run is not None else "not_started",
                "progressMessage": (
                    run.progress_message if run is not None else "まだ取得していません"
                ),
                "authenticationRequired": (
                    run is not None and run.state == "authenticationRequired"
                ),
                "manualActionRequired": (
                    run is not None
                    and run.state == "waitingForManualAction"
                    and run.manual_action is not None
                ),
            }
        return {
            "localDate": local_date.isoformat(),
            "timeZone": settings.rhythm_time_zone,
            "spending": _spending_json(spending) if spending is not None else None,
            "ratings": ratings,
            "collection": collection,
            "dryRun": settings.rhythm_discord_dry_run,
        }

    @app.exception_handler(RhythmApiError)
    async def rhythm_api_error_handler(_request: Request, exc: RhythmApiError):
        return error_response(exc)

    previous_validation_handler = app.exception_handlers.get(RequestValidationError)

    @app.exception_handler(RequestValidationError)
    async def rhythm_validation_error_handler(request: Request, exc: RequestValidationError):
        if request.url.path.startswith(
            ("/api/v1/rhythm/", "/api/v1/shortcuts/", "/api/v1/archive/")
        ):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_request",
                        "message": "リクエストの形式または値が不正です。",
                        "retryable": False,
                    }
                },
            )
        if previous_validation_handler is not None:
            return await previous_validation_handler(request, exc)
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    app.include_router(router)
    app.state.rhythm_persistence = persistence
    app.state.kiseki_sync = kiseki_sync
