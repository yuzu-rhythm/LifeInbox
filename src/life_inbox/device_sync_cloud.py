"""Separately deployable public ingress for iPhone rhythm-spending sync.

This application is deliberately smaller than :mod:`life_inbox.web`.  It
does not import or mount the read-only website, does not mount the broader
iPhone API, and does not instantiate a Discord client.  It reuses only the
event-level request model/canonicalizer, accepts its PUT/GET contract, and
leaves the resulting ``pending`` delivery state for the separately running
Bot.

The production entry point requires an explicitly enabled device-sync
configuration and a non-SQLite database URL.  SQLite is available only to
tests that opt into an explicit ephemeral fixture through the app factory; no
environment variable can enable it for the public runtime.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import sys
import threading
import time
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import Message, Receive, Scope, Send

from .config import Settings
from .db import Database
from .rhythm_collectors.common import safe_message
from .rhythm_persistence import RhythmPersistence
from .rhythm_spending import (
    RhythmSpendingEventPersistence,
    SpendingDayTotals,
    SpendingEventRecord,
    SpendingEventRevisionConflictError,
    StaleSpendingEventRevisionError,
)
from .rhythm_spending_api import SpendingEventPutRequest, canonical_event_id

DEVICE_SYNC_BIND_HOST: Final = "0.0.0.0"
DEVICE_SYNC_ENABLED_ENV: Final = "DEVICE_SYNC_ENABLED"
DEVICE_SYNC_PORT_ENV: Final = "PORT"
DEVICE_SYNC_DATABASE_ENV: Final = "DATABASE_URL"
DEVICE_SYNC_TOKEN_HASH_ENV: Final = "RHYTHM_DEVICE_SYNC_TOKEN_SHA256"
DEVICE_SYNC_MAX_BODY_ENV: Final = "DEVICE_SYNC_MAX_BODY_BYTES"
DEVICE_SYNC_RATE_LIMIT_ENV: Final = "DEVICE_SYNC_RATE_LIMIT_PER_MINUTE"
DEVICE_SYNC_EVENT_PATH: Final = "/api/v1/rhythm/spending/events/"

_EVENT_PATH_PATTERN = re.compile(
    r"^/api/v1/rhythm/spending/events/[^/]+$"
)
_TRUE_VALUES = frozenset({"1", "true"})
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class DeviceSyncConfigurationError(ValueError):
    """Configuration error with no runtime value in its public message."""

    def __init__(self, variable: str, reason: str) -> None:
        self.variable = variable
        self.reason = reason
        super().__init__(f"{variable}: {reason}")


@dataclass(frozen=True, slots=True)
class DeviceSyncRuntime:
    """Validated inputs needed to start the public ingress process."""

    settings: Settings
    port: int


class DeviceSyncApiError(Exception):
    """Safe, structured error returned by the narrow public API."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class _RequestBodyTooLarge(Exception):
    pass


class _DeviceSyncRateLimiter:
    """Process-local fixed-window limiter keyed by the token hash."""

    def __init__(self, limit: int, *, window_seconds: float = 60.0) -> None:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("device-sync rate limiter values must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, token_hash: str) -> None:
        now = time.monotonic()
        with self._lock:
            entries = self._requests[token_hash]
            while entries and entries[0] <= now - self.window_seconds:
                entries.popleft()
            if len(entries) >= self.limit:
                raise DeviceSyncApiError(
                    429,
                    "rate_limited",
                    "リクエストが多すぎます。少し待って再試行してください。",
                    retryable=True,
                    retry_after_seconds=max(1, int(self.window_seconds)),
                )
            entries.append(now)


class _DeviceSyncAuthenticator:
    """Validate the dedicated bearer token without retaining the raw value."""

    def __init__(self, expected_token_hash: str, limiter: _DeviceSyncRateLimiter) -> None:
        self.expected_token_hash = expected_token_hash
        self.limiter = limiter

    def authorize(self, values: list[str]) -> None:
        if len(values) != 1:
            raise DeviceSyncApiError(401, "unauthorized", "認証が必要です。")
        authorization = values[0]
        scheme, separator, token = authorization.partition(" ")
        if (
            not separator
            or scheme.casefold() != "bearer"
            or not token
            or token != token.strip()
            or any(character.isspace() for character in token)
        ):
            raise DeviceSyncApiError(401, "unauthorized", "認証が必要です。")
        supplied_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(supplied_hash, self.expected_token_hash):
            raise DeviceSyncApiError(401, "unauthorized", "認証に失敗しました。")
        self.limiter.check(supplied_hash)


def _header_values(scope: Scope, name: str) -> list[str]:
    normalized_name = name.casefold().encode("ascii")
    values: list[str] = []
    for key, value in scope.get("headers", []):
        if key.lower() == normalized_name:
            values.append(value.decode("latin-1"))
    return values


def _security_headers(existing: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    present = {key.lower() for key, _ in existing}
    result = list(existing)
    for key, value in _SECURITY_HEADERS.items():
        encoded_key = key.lower().encode("ascii")
        if encoded_key not in present:
            result.append((encoded_key, value.encode("ascii")))
    return result


def _api_error_response(exc: DeviceSyncApiError) -> JSONResponse:
    headers: dict[str, str] = {}
    if exc.retry_after_seconds is not None:
        headers["Retry-After"] = str(exc.retry_after_seconds)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
            }
        },
        headers=headers,
    )


class _DeviceSyncIngressMiddleware:
    """Apply route allowlisting, auth, and body-size checks before FastAPI."""

    def __init__(
        self,
        app,
        *,
        authenticator: _DeviceSyncAuthenticator,
        max_body_bytes: int,
    ) -> None:
        self.app = app
        self.authenticator = authenticator
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def secure_send(message: Message) -> None:
            if message.get("type") == "http.response.start":
                message = {
                    **message,
                    "headers": _security_headers(list(message.get("headers", []))),
                }
            await send(message)

        path = str(scope.get("path", ""))
        method = str(scope.get("method", "")).upper()
        if path == "/healthz" and method == "GET":
            await self.app(scope, receive, secure_send)
            return

        if not (_EVENT_PATH_PATTERN.fullmatch(path) and method in {"GET", "PUT"}):
            await _api_error_response(
                DeviceSyncApiError(404, "not_found", "リソースが見つかりません。")
            )(scope, receive, secure_send)
            return

        try:
            self.authenticator.authorize(_header_values(scope, "authorization"))
            self._check_content_length(scope)
        except DeviceSyncApiError as exc:
            await _api_error_response(exc)(scope, receive, secure_send)
            return

        bytes_seen = 0

        async def limited_receive() -> Message:
            nonlocal bytes_seen
            message = await receive()
            if message.get("type") == "http.request":
                bytes_seen += len(message.get("body", b""))
                if bytes_seen > self.max_body_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, secure_send)
        except _RequestBodyTooLarge:
            await _api_error_response(
                DeviceSyncApiError(
                    413,
                    "payload_too_large",
                    "リクエスト本文が大きすぎます。",
                )
            )(scope, receive, secure_send)

    def _check_content_length(self, scope: Scope) -> None:
        values = _header_values(scope, "content-length")
        if len(values) > 1:
            raise DeviceSyncApiError(
                400,
                "invalid_request",
                "リクエストの形式または値が不正です。",
            )
        if not values:
            return
        try:
            length = int(values[0])
        except (TypeError, ValueError):
            raise DeviceSyncApiError(
                400,
                "invalid_request",
                "リクエストの形式または値が不正です。",
            ) from None
        if length < 0:
            raise DeviceSyncApiError(
                400,
                "invalid_request",
                "リクエストの形式または値が不正です。",
            )
        if length > self.max_body_bytes:
            raise DeviceSyncApiError(
                413,
                "payload_too_large",
                "リクエスト本文が大きすぎます。",
            )


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


def _required_environment(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None or not value.strip():
        raise DeviceSyncConfigurationError(name, "必須です")
    return value


def _require_true(environment: Mapping[str, str], name: str) -> None:
    value = environment.get(name)
    if value is None or value.strip().casefold() not in _TRUE_VALUES:
        raise DeviceSyncConfigurationError(name, "true または 1 を明示してください")


def _parse_port(environment: Mapping[str, str]) -> int:
    raw_value = _required_environment(environment, DEVICE_SYNC_PORT_ENV).strip()
    if not raw_value.isascii() or not raw_value.isdigit():
        raise DeviceSyncConfigurationError(DEVICE_SYNC_PORT_ENV, "1-65535 の整数が必要です")
    port = int(raw_value)
    if not 1 <= port <= 65_535:
        raise DeviceSyncConfigurationError(DEVICE_SYNC_PORT_ENV, "1-65535 の整数が必要です")
    return port


def _database_backend(database_url: str) -> str:
    if not database_url.strip():
        raise DeviceSyncConfigurationError(DEVICE_SYNC_DATABASE_ENV, "必須です")
    try:
        return make_url(database_url).get_backend_name()
    except Exception:
        raise DeviceSyncConfigurationError(
            DEVICE_SYNC_DATABASE_ENV,
            "有効な永続データベースURLが必要です",
        ) from None


def validate_device_sync_persistence(
    database_url: str,
    *,
    allow_ephemeral_sqlite: bool = False,
) -> None:
    """Reject SQLite for the production ingress unless a test opts in."""

    if _database_backend(database_url) == "sqlite" and not allow_ephemeral_sqlite:
        raise DeviceSyncConfigurationError(
            DEVICE_SYNC_DATABASE_ENV,
            "公開IngressではSQLiteを使用できません。永続DBを指定してください",
        )


def _validate_device_sync_settings(settings: Settings) -> None:
    if not settings.device_sync_enabled:
        raise DeviceSyncConfigurationError(
            DEVICE_SYNC_ENABLED_ENV,
            "true または 1 を明示してください",
        )
    if settings.rhythm_device_sync_token_sha256 is None:
        raise DeviceSyncConfigurationError(DEVICE_SYNC_TOKEN_HASH_ENV, "必須です")
    if settings.allowed_user_id is None:
        raise DeviceSyncConfigurationError(
            "ALLOWED_USER_ID",
            "共有DBの対象ユーザーを明示してください",
        )


def load_device_sync_runtime(
    environment: Mapping[str, str] | None = None,
) -> DeviceSyncRuntime:
    """Load production settings without reading a local ``.env`` file."""

    resolved_environment = os.environ if environment is None else environment
    _require_true(resolved_environment, DEVICE_SYNC_ENABLED_ENV)
    port = _parse_port(resolved_environment)
    database_url = _required_environment(resolved_environment, DEVICE_SYNC_DATABASE_ENV)
    validate_device_sync_persistence(database_url)
    token_hash = _required_environment(resolved_environment, DEVICE_SYNC_TOKEN_HASH_ENV)
    allowed_user_id = _required_environment(resolved_environment, "ALLOWED_USER_ID")

    settings_values: dict[str, Any] = {
        "_env_file": None,
        "device_sync_enabled": True,
        "database_url": database_url,
        "rhythm_device_sync_token_sha256": token_hash,
        "allowed_user_id": allowed_user_id,
    }
    optional_environment_fields = (
        ("RHYTHM_TIME_ZONE", "rhythm_time_zone"),
        ("RHYTHM_PLAYER_DISPLAY_NAME", "rhythm_player_display_name"),
        (DEVICE_SYNC_MAX_BODY_ENV, "device_sync_max_body_bytes"),
        (DEVICE_SYNC_RATE_LIMIT_ENV, "device_sync_rate_limit_per_minute"),
        ("LOG_LEVEL", "log_level"),
    )
    for environment_name, field_name in optional_environment_fields:
        if environment_name in resolved_environment:
            settings_values[field_name] = resolved_environment[environment_name]

    settings = Settings(**settings_values)
    _validate_device_sync_settings(settings)
    return DeviceSyncRuntime(settings=settings, port=port)


def create_device_sync_app(
    settings: Settings | None = None,
    *,
    runtime: DeviceSyncRuntime | None = None,
    database: Database | None = None,
    allow_ephemeral_sqlite: bool = False,
) -> FastAPI:
    """Create only the public device-sync routes.

    ``allow_ephemeral_sqlite`` exists for secret-free local fixture tests and
    is intentionally not wired to an environment variable or production
    entry point.
    """

    if settings is not None and runtime is not None:
        raise ValueError("provide settings or runtime, not both")
    resolved_runtime = runtime
    resolved_settings = settings
    if resolved_runtime is None and resolved_settings is None:
        resolved_runtime = load_device_sync_runtime()
    if resolved_settings is None:
        resolved_settings = resolved_runtime.settings
    _validate_device_sync_settings(resolved_settings)

    resolved_database = database or Database(resolved_settings.database_url)
    actual_backend = resolved_database.engine.url.get_backend_name()
    if actual_backend == "sqlite" and not allow_ephemeral_sqlite:
        raise DeviceSyncConfigurationError(
            DEVICE_SYNC_DATABASE_ENV,
            "公開IngressではSQLiteを使用できません。永続DBを指定してください",
        )

    rhythm_store = RhythmPersistence(resolved_database.session_factory)
    event_store = RhythmSpendingEventPersistence(resolved_database.session_factory)
    limiter = _DeviceSyncRateLimiter(resolved_settings.device_sync_rate_limit_per_minute)
    expected_hash = resolved_settings.rhythm_device_sync_token_sha256
    if expected_hash is None:  # guarded above; keeps the type boundary explicit
        raise DeviceSyncConfigurationError(DEVICE_SYNC_TOKEN_HASH_ENV, "必須です")
    authenticator = _DeviceSyncAuthenticator(
        expected_hash.get_secret_value(), limiter
    )

    app = FastAPI(
        title="Life Inbox Device Sync",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved_settings
    app.state.database = resolved_database
    app.state.device_sync_event_persistence = event_store
    app.state.device_sync_rate_limiter = limiter

    def rhythm_user_id() -> int:
        return rhythm_store.ensure_user(
            display_name=resolved_settings.rhythm_player_display_name,
            time_zone=resolved_settings.rhythm_time_zone,
            discord_user_id=resolved_settings.allowed_user_id,
        )

    def map_persistence_error(exc: Exception) -> DeviceSyncApiError:
        if isinstance(exc, StaleSpendingEventRevisionError):
            return DeviceSyncApiError(
                409,
                "stale_revision",
                "古いrevisionのためイベントを更新しませんでした。",
            )
        if isinstance(exc, SpendingEventRevisionConflictError):
            return DeviceSyncApiError(
                409,
                "revision_conflict",
                "同じeventIdとrevisionに異なる内容が指定されています。",
            )
        if isinstance(exc, SQLAlchemyError):
            return DeviceSyncApiError(
                503,
                "storage_unavailable",
                "イベントを一時的に保存できません。再送してください。",
                retryable=True,
            )
        return DeviceSyncApiError(
            422,
            "invalid_spending_event",
            "利用額イベントの値または日時が不正です。",
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.put(DEVICE_SYNC_EVENT_PATH + "{event_id}", include_in_schema=False)
    def put_event(event_id: str, payload: SpendingEventPutRequest):
        try:
            path_event_id = canonical_event_id(event_id)
        except ValueError as exc:
            raise DeviceSyncApiError(
                422,
                "invalid_event_id",
                "eventIdはUUID形式で指定してください。",
            ) from exc
        if path_event_id != payload.event_id:
            raise DeviceSyncApiError(
                409,
                "event_id_mismatch",
                "URLとJSONのeventIdが一致しません。",
            )
        try:
            resolved_user_id = rhythm_user_id()
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
            totals = event_store.daily_totals(
                rhythm_user_id=resolved_user_id,
                local_date=result.record.local_date,
                time_zone=result.record.time_zone,
            )
        except (
            StaleSpendingEventRevisionError,
            SpendingEventRevisionConflictError,
            SQLAlchemyError,
            ValueError,
        ) as exc:
            raise map_persistence_error(exc) from exc
        return {
            "accepted": True,
            "created": result.created,
            "idempotent": result.idempotent,
            "data": _event_json(result.record),
            "dailyTotals": _totals_json(totals),
            "discordDeliveryIsAsynchronous": True,
        }

    @app.get(DEVICE_SYNC_EVENT_PATH + "{event_id}", include_in_schema=False)
    def get_event(event_id: str):
        try:
            normalized = canonical_event_id(event_id)
        except ValueError as exc:
            raise DeviceSyncApiError(
                422,
                "invalid_event_id",
                "eventIdはUUID形式で指定してください。",
            ) from exc
        try:
            resolved_user_id = rhythm_user_id()
            record = event_store.get_event(
                rhythm_user_id=resolved_user_id,
                event_id=normalized,
            )
            if record is None:
                raise DeviceSyncApiError(
                    404,
                    "spending_event_not_found",
                    "利用額イベントが見つかりません。",
                )
            totals = event_store.daily_totals(
                rhythm_user_id=record.rhythm_user_id,
                local_date=record.local_date,
                time_zone=record.time_zone,
            )
        except DeviceSyncApiError:
            raise
        except SQLAlchemyError as exc:
            raise map_persistence_error(exc) from exc
        return {"data": _event_json(record), "dailyTotals": _totals_json(totals)}

    @app.exception_handler(DeviceSyncApiError)
    async def device_sync_api_error_handler(_request: Request, exc: DeviceSyncApiError):
        return _api_error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def device_sync_validation_error_handler(
        _request: Request, _exc: RequestValidationError
    ):
        return _api_error_response(
            DeviceSyncApiError(
                422,
                "invalid_request",
                "リクエストの形式または値が不正です。",
            )
        )

    @app.exception_handler(_RequestBodyTooLarge)
    async def device_sync_body_size_error_handler(
        _request: Request, _exc: _RequestBodyTooLarge
    ):
        return _api_error_response(
            DeviceSyncApiError(
                413,
                "payload_too_large",
                "リクエスト本文が大きすぎます。",
            )
        )

    @app.exception_handler(Exception)
    async def device_sync_unexpected_error_handler(_request: Request, _exc: Exception):
        return _api_error_response(
            DeviceSyncApiError(
                500,
                "internal_error",
                "サーバーで処理できませんでした。",
                retryable=True,
            )
        )

    app.add_middleware(
        _DeviceSyncIngressMiddleware,
        authenticator=authenticator,
        max_body_bytes=resolved_settings.device_sync_max_body_bytes,
    )
    return app


def run_device_sync_cloud() -> None:
    """Start the public ingress on the Cloud Run-provided port."""

    runtime = load_device_sync_runtime()
    database = Database(runtime.settings.database_url)
    database.init()
    app = create_device_sync_app(runtime=runtime, database=database)

    import uvicorn

    # Access logs are disabled so future logger changes cannot accidentally
    # add request headers or other request metadata to standard output.
    uvicorn.run(
        app,
        host=DEVICE_SYNC_BIND_HOST,
        port=runtime.port,
        access_log=False,
        log_level=runtime.settings.log_level.casefold(),
    )


def main() -> int:
    """CLI-safe process entry point; never echo settings or exception values."""

    try:
        run_device_sync_cloud()
    except DeviceSyncConfigurationError as exc:
        print(f"Device sync configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - startup output must be secret-safe
        print(f"Device sync startup error: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
