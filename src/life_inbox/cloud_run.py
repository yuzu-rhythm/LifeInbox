"""Cloud Run entry point for the web-only Life Inbox process.

The regular ``life_inbox web`` command intentionally binds to a loopback
address.  Cloud Run needs the process to listen on the platform-provided
``PORT`` on all interfaces, so this module keeps the application's validated
loopback setting separate from Uvicorn's container bind address.

This entry point does not read ``.env`` files.  Cloud Run configuration must
be supplied explicitly by the deployment environment, and a SQLite deployment
must explicitly acknowledge that its writable storage is ephemeral.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .config import Settings
from .db import Database
from .finance import FinanceService
from .rhythm import RhythmService
from .web import create_web_app

CLOUD_RUN_BIND_HOST: Final = "0.0.0.0"
CLOUD_RUN_MODE_ENV: Final = "LIFE_INBOX_CLOUD_RUN"
CLOUD_RUN_READ_ONLY_ENV: Final = "LIFE_INBOX_CLOUD_RUN_READ_ONLY"
CLOUD_RUN_SQLITE_ACK_ENV: Final = "LIFE_INBOX_CLOUD_RUN_ALLOW_EPHEMERAL_SQLITE"
CLOUD_RUN_PORT_ENV: Final = "PORT"
CLOUD_RUN_DATABASE_ENV: Final = "DATABASE_URL"
CLOUD_RUN_DATA_DIR_ENV: Final = "DATA_DIR"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_TRUE_VALUES = frozenset({"1", "true"})
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_READ_ONLY_HEAD_PATHS = frozenset({"/", "/healthz"})

_CLOUD_RESPONSE_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'none'"
    ),
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": "noindex, nofollow",
}


class CloudRunConfigurationError(ValueError):
    """Configuration error whose public message contains no runtime values."""

    def __init__(self, variable: str, reason: str) -> None:
        self.variable = variable
        self.reason = reason
        super().__init__(f"{variable}: {reason}")


@dataclass(frozen=True, slots=True)
class CloudRunRuntime:
    """Validated startup inputs for the Cloud Run web process."""

    settings: Settings
    port: int


def _required_environment(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None or not value.strip():
        raise CloudRunConfigurationError(name, "必須です")
    return value


def _require_true(environment: Mapping[str, str], name: str) -> None:
    value = environment.get(name)
    if value is None or value.strip().casefold() not in _TRUE_VALUES:
        raise CloudRunConfigurationError(name, "true または 1 を明示してください")


def _parse_port(environment: Mapping[str, str]) -> int:
    raw_value = _required_environment(environment, CLOUD_RUN_PORT_ENV).strip()
    if not raw_value.isascii() or not raw_value.isdigit():
        raise CloudRunConfigurationError(CLOUD_RUN_PORT_ENV, "1-65535 の整数が必要です")
    port = int(raw_value)
    if not 1 <= port <= 65_535:
        raise CloudRunConfigurationError(CLOUD_RUN_PORT_ENV, "1-65535 の整数が必要です")
    return port


def _validate_web_host(environment: Mapping[str, str]) -> None:
    configured_host = environment.get("WEB_HOST")
    if configured_host is not None and configured_host.strip().casefold() not in _LOOPBACK_HOSTS:
        raise CloudRunConfigurationError("WEB_HOST", "アプリ設定は loopback のままにしてください")


def load_cloud_run_runtime(
    environment: Mapping[str, str] | None = None,
) -> CloudRunRuntime:
    """Load an explicit, secret-safe Cloud Run configuration.

    ``WEB_HOST`` is validated as a local-only application setting and then
    overridden with the same loopback value accepted by ``Settings``.  The
    actual container listener is controlled independently by
    ``CLOUD_RUN_BIND_HOST`` in :func:`main`.
    """

    resolved_environment = os.environ if environment is None else environment
    _require_true(resolved_environment, CLOUD_RUN_MODE_ENV)
    _require_true(resolved_environment, CLOUD_RUN_READ_ONLY_ENV)
    _validate_web_host(resolved_environment)
    port = _parse_port(resolved_environment)
    database_url = _required_environment(resolved_environment, CLOUD_RUN_DATABASE_ENV)
    data_dir = _required_environment(resolved_environment, CLOUD_RUN_DATA_DIR_ENV)

    if database_url.strip().casefold().startswith("sqlite"):
        _require_true(resolved_environment, CLOUD_RUN_SQLITE_ACK_ENV)

    settings = Settings(
        _env_file=None,
        database_url=database_url,
        data_dir=Path(data_dir),
        # Keep Settings' application-level local-only invariant intact.  Only
        # Uvicorn's container listener below uses 0.0.0.0.
        web_host="127.0.0.1",
        web_port=port,
    )
    return CloudRunRuntime(settings=settings, port=port)


def create_cloud_run_app(runtime: CloudRunRuntime | None = None) -> FastAPI:
    """Initialize the existing web services and return the FastAPI app."""

    resolved_runtime = runtime or load_cloud_run_runtime()
    database = Database(resolved_runtime.settings.database_url)
    database.init()
    finance = FinanceService(database.session_factory)
    finance.ensure_default_accounts()
    rhythm = RhythmService(database.session_factory)
    app = create_web_app(
        resolved_runtime.settings,
        database=database,
        finance=finance,
        rhythm=rhythm,
    )
    install_cloud_read_only_middleware(app)
    return app


def install_cloud_read_only_middleware(app: FastAPI) -> None:
    """Reject all state-changing HTTP methods for the cloud snapshot."""

    @app.middleware("http")
    async def reject_cloud_state_changes(request: Request, call_next):
        method = request.method.upper()
        if method in _STATE_CHANGING_METHODS:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "cloud_run_read_only",
                    "message": "This Cloud Run revision is a read-only snapshot.",
                },
                headers=_CLOUD_RESPONSE_SECURITY_HEADERS,
            )
        if method == "HEAD" and request.url.path in _READ_ONLY_HEAD_PATHS:
            return Response(status_code=200, headers=_CLOUD_RESPONSE_SECURITY_HEADERS)
        return await call_next(request)


def main() -> int:
    """Start the existing web app on the Cloud Run listener."""

    try:
        runtime = load_cloud_run_runtime()
        app = create_cloud_run_app(runtime)
    except CloudRunConfigurationError as exc:
        # ``exc`` contains only the name of a missing/invalid setting and a
        # fixed reason; no environment value is included in this message.
        print(f"Cloud Run configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - startup must not echo config values
        print(
            f"Cloud Run startup error: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1

    import uvicorn

    uvicorn.run(app, host=CLOUD_RUN_BIND_HOST, port=runtime.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
