from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from life_inbox.config import Settings
from life_inbox.db import Database
from life_inbox.device_sync_cloud import (
    DeviceSyncConfigurationError,
    create_device_sync_app,
    load_device_sync_runtime,
)
from life_inbox.rhythm_spending_models import RhythmSpendingEventRow

RAW_DEVICE_TOKEN = "fixture-device-sync-token"
BOT_TOKEN = "fixture-discord-bot-token-that-must-not-authorize"


def _settings(
    tmp_path: Path,
    *,
    rate_limit: int = 60,
    max_body_bytes: int = 16 * 1024,
) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'device-sync.db'}",
        data_dir=tmp_path,
        allowed_user_id=24680,
        rhythm_player_display_name="fixture-player",
        rhythm_device_sync_token_sha256=hashlib.sha256(
            RAW_DEVICE_TOKEN.encode("utf-8")
        ).hexdigest(),
        device_sync_enabled=True,
        device_sync_rate_limit_per_minute=rate_limit,
        device_sync_max_body_bytes=max_body_bytes,
    )


@pytest.fixture
def device_sync_api(tmp_path: Path):
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    database.init()
    app = create_device_sync_app(
        settings,
        database=database,
        allow_ephemeral_sqlite=True,
    )
    with TestClient(app) as client:
        yield client, database


def _headers(token: str = RAW_DEVICE_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _payload(
    event_id: str,
    *,
    revision: int = 1,
    amount: int = 600,
    deleted: bool = False,
) -> dict[str, object]:
    return {
        "eventId": event_id,
        "revision": revision,
        "userId": "fixture-player",
        "game": "maimai",
        "amountYen": amount,
        "occurredAt": "2026-08-30T18:42:00+09:00",
        "timeZone": "Asia/Tokyo",
        "deleted": deleted,
        "source": "app",
    }


def test_health_is_public_but_only_allowlisted_event_routes_are_exposed(device_sync_api):
    client, _ = device_sync_api

    health = client.get("/healthz")
    root = client.get("/", headers=_headers())
    docs = client.get("/docs", headers=_headers())
    openapi = client.get("/openapi.json", headers=_headers())
    ratings = client.get("/api/v1/rhythm/ratings/latest", headers=_headers())
    post = client.post(
        "/api/v1/rhythm/spending/events/00000000-0000-0000-0000-000000000001",
        headers=_headers(),
        json={},
    )
    delete = client.delete(
        "/api/v1/rhythm/spending/events/00000000-0000-0000-0000-000000000001",
        headers=_headers(),
    )

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    for response in (root, docs, openapi, ratings, post, delete):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


def test_authentication_uses_only_dedicated_hash_and_never_reflects_secrets(device_sync_api):
    client, _ = device_sync_api
    event_id = str(uuid4())
    url = f"/api/v1/rhythm/spending/events/{event_id}"

    missing = client.put(url, content=b"{not-json")
    invalid = client.get(url, headers=_headers("wrong-device-token"))
    bot_token = client.get(url, headers=_headers(BOT_TOKEN))

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert bot_token.status_code == 401
    for response in (missing, invalid, bot_token):
        assert response.json()["error"]["code"] == "unauthorized"
        assert RAW_DEVICE_TOKEN not in response.text
        assert BOT_TOKEN not in response.text
        assert "wrong-device-token" not in response.text


def test_event_create_replay_update_delete_get_and_stale_revision_are_durable(
    device_sync_api,
):
    client, database = device_sync_api
    event_id = str(uuid4()).upper()
    url = f"/api/v1/rhythm/spending/events/{event_id}"

    created = client.put(url, headers=_headers(), json=_payload(event_id))
    replayed = client.put(url, headers=_headers(), json=_payload(event_id))
    updated = client.put(
        url,
        headers=_headers(),
        json=_payload(event_id, revision=2, amount=500),
    )
    deleted = client.put(
        url,
        headers=_headers(),
        json=_payload(event_id, revision=3, amount=500, deleted=True),
    )
    stale = client.put(
        url,
        headers=_headers(),
        json=_payload(event_id, revision=2, amount=500),
    )
    loaded = client.get(url, headers=_headers())

    assert created.status_code == 200
    assert created.json()["accepted"] is True
    assert created.json()["created"] is True
    assert created.json()["idempotent"] is False
    assert created.json()["data"]["eventId"] == event_id.casefold()
    assert created.json()["data"]["discord"]["status"] == "pending"
    assert created.json()["discordDeliveryIsAsynchronous"] is True
    assert replayed.status_code == 200
    assert replayed.json()["idempotent"] is True
    assert updated.status_code == 200
    assert updated.json()["data"]["amountYen"] == 500
    assert updated.json()["data"]["revision"] == 2
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted"] is True
    assert deleted.json()["dailyTotals"]["totalAmountYen"] == 0
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_revision"
    assert loaded.status_code == 200
    assert loaded.json()["data"]["deleted"] is True
    assert loaded.json()["data"]["revision"] == 3
    assert loaded.json()["dailyTotals"]["activeEventCount"] == 0

    with database.session() as session:
        assert session.scalar(select(func.count(RhythmSpendingEventRow.id))) == 1


def test_authentication_and_body_limit_happen_before_json_validation(tmp_path: Path):
    settings = _settings(tmp_path, max_body_bytes=1_024)
    database = Database(settings.database_url)
    database.init()
    app = create_device_sync_app(
        settings,
        database=database,
        allow_ephemeral_sqlite=True,
    )
    event_id = str(uuid4())
    url = f"/api/v1/rhythm/spending/events/{event_id}"
    with TestClient(app) as client:
        unauthenticated = client.put(url, content=b"x" * 2_000)
        oversized = client.put(url, headers=_headers(), content=b"x" * 2_000)

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "unauthorized"
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "payload_too_large"
    assert "x" * 100 not in oversized.text


def test_rate_limit_is_applied_to_authenticated_event_requests_only(tmp_path: Path):
    settings = _settings(tmp_path, rate_limit=2)
    database = Database(settings.database_url)
    database.init()
    app = create_device_sync_app(
        settings,
        database=database,
        allow_ephemeral_sqlite=True,
    )
    event_id = str(uuid4())
    url = f"/api/v1/rhythm/spending/events/{event_id}"
    with TestClient(app) as client:
        first = client.get(url, headers=_headers())
        second = client.get(url, headers=_headers())
        limited = client.get(url, headers=_headers())
        health = client.get("/healthz")
        unknown = client.get("/not-allowlisted", headers=_headers())

    assert first.status_code == 404
    assert second.status_code == 404
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert limited.headers["retry-after"] == "60"
    assert health.status_code == 200
    assert unknown.status_code == 404


def test_production_runtime_and_app_refuse_ephemeral_sqlite(tmp_path: Path):
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    database.init()

    with pytest.raises(DeviceSyncConfigurationError, match="DATABASE_URL"):
        create_device_sync_app(settings, database=database)

    environment = {
        "DEVICE_SYNC_ENABLED": "true",
        "PORT": "8080",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'runtime.db'}",
        "ALLOWED_USER_ID": "24680",
        "RHYTHM_DEVICE_SYNC_TOKEN_SHA256": (
            settings.rhythm_device_sync_token_sha256.get_secret_value()
        ),
    }
    with pytest.raises(DeviceSyncConfigurationError, match="DATABASE_URL"):
        load_device_sync_runtime(environment)


def test_production_runtime_loads_only_required_safe_values():
    token_hash = hashlib.sha256(RAW_DEVICE_TOKEN.encode("utf-8")).hexdigest()
    runtime = load_device_sync_runtime(
        {
            "DEVICE_SYNC_ENABLED": "true",
            "PORT": "8080",
            "DATABASE_URL": "postgresql+psycopg://sync-user@db.example/life_inbox",
            "ALLOWED_USER_ID": "24680",
            "RHYTHM_DEVICE_SYNC_TOKEN_SHA256": token_hash,
            "DEVICE_SYNC_MAX_BODY_BYTES": "32768",
            "DEVICE_SYNC_RATE_LIMIT_PER_MINUTE": "30",
            "RHYTHM_TIME_ZONE": "Asia/Tokyo",
        }
    )

    assert runtime.port == 8080
    assert runtime.settings.device_sync_enabled is True
    assert runtime.settings.device_sync_max_body_bytes == 32_768
    assert runtime.settings.device_sync_rate_limit_per_minute == 30
    assert runtime.settings.safe_summary()["device_sync_api_configured"] is True
    assert token_hash not in str(runtime.settings.safe_summary())
