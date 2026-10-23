from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from life_inbox.config import Settings
from life_inbox.db import Database
from life_inbox.web import create_web_app

RAW_DEVICE_TOKEN = "health-test-device-token"
AUTH_HEADERS = {"Authorization": f"Bearer {RAW_DEVICE_TOKEN}"}
WORKOUT_ID = hashlib.sha256(b"healthkit-workout-uuid").hexdigest()


@pytest.fixture
def health_api(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'health.db'}",
        data_dir=tmp_path,
        allowed_user_id=24680,
        rhythm_device_sync_token_sha256=hashlib.sha256(
            RAW_DEVICE_TOKEN.encode()
        ).hexdigest(),
        health_enabled=True,
    )
    database = Database(settings.database_url)
    database.init()
    app = create_web_app(settings, database=database)
    with TestClient(app) as client:
        yield client


def sleep_payload(*, revision: int = 1, asleep_minutes: int = 420):
    return {
        "timeZone": "Asia/Tokyo",
        "sleepStart": "2026-08-29T23:30:00+09:00",
        "sleepEnd": "2026-08-30T07:30:00+09:00",
        "asleepMinutes": asleep_minutes,
        "awakeMinutes": 20,
        "coreMinutes": 250,
        "deepMinutes": 70,
        "remMinutes": 100,
        "inBedMinutes": 480,
        "source": "appleWatch",
        "revision": revision,
    }


def workout_payload(*, revision: int = 1, energy: int = 210):
    return {
        "timeZone": "Asia/Tokyo",
        "activityType": "walking",
        "startedAt": "2026-08-30T18:12:00+09:00",
        "endedAt": "2026-08-30T18:54:00+09:00",
        "durationSeconds": 2_520,
        "distanceMeters": 3_800,
        "activeEnergyKcal": energy,
        "averageHeartRateBpm": 112,
        "source": "appleWatch",
        "revision": revision,
    }


def test_health_api_auth_precedes_validation_and_never_reflects_token(health_api):
    unauthorized = health_api.put(
        "/api/v1/health/sleep/not-a-date",
        content=b"{not-json",
        headers={"Content-Type": "application/json"},
    )
    invalid = health_api.get(
        "/api/v1/health/status",
        headers={"Authorization": "Bearer secret-must-not-return"},
    )

    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "unauthorized"
    assert invalid.status_code == 401
    assert "secret-must-not-return" not in invalid.text


def test_sleep_sync_is_revision_safe_idempotent_and_deletable(health_api):
    url = "/api/v1/health/sleep/2026-08-30"
    created = health_api.put(url, headers=AUTH_HEADERS, json=sleep_payload())
    repeated = health_api.put(url, headers=AUTH_HEADERS, json=sleep_payload())
    stale = health_api.put(
        url, headers=AUTH_HEADERS, json=sleep_payload(revision=0)
    )
    conflict = health_api.put(
        url, headers=AUTH_HEADERS, json=sleep_payload(asleep_minutes=421)
    )
    updated = health_api.put(
        url,
        headers=AUTH_HEADERS,
        json=sleep_payload(revision=2, asleep_minutes=421),
    )
    deleted = health_api.delete(
        f"{url}?revision=3&timeZone=Asia%2FTokyo", headers=AUTH_HEADERS
    )

    assert created.status_code == 200
    assert created.json()["created"] is True
    assert created.json()["data"]["asleepMinutes"] == 420
    assert repeated.json()["idempotent"] is True
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_revision"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "revision_conflict"
    assert updated.json()["data"]["asleepMinutes"] == 421
    assert deleted.json()["data"]["deleted"] is True


def test_sleep_rejects_naive_dates_wrong_wake_date_and_impossible_stages(health_api):
    url = "/api/v1/health/sleep/2026-08-30"
    naive = health_api.put(
        url,
        headers=AUTH_HEADERS,
        json={**sleep_payload(), "sleepEnd": "2026-08-30T07:30:00"},
    )
    wrong_date = health_api.put(
        "/api/v1/health/sleep/2026-08-29",
        headers=AUTH_HEADERS,
        json=sleep_payload(),
    )
    bad_stages = health_api.put(
        url,
        headers=AUTH_HEADERS,
        json={**sleep_payload(), "asleepMinutes": 100},
    )

    assert naive.status_code == 422
    assert wrong_date.status_code == 422
    assert wrong_date.json()["error"]["code"] == "invalid_health_summary"
    assert bad_stages.status_code == 422


def test_workout_sync_uses_hashed_id_revision_and_daily_status(health_api):
    url = f"/api/v1/health/workouts/{WORKOUT_ID}"
    created = health_api.put(url, headers=AUTH_HEADERS, json=workout_payload())
    repeated = health_api.put(url, headers=AUTH_HEADERS, json=workout_payload())
    updated = health_api.put(
        url, headers=AUTH_HEADERS, json=workout_payload(revision=2, energy=220)
    )
    daily = health_api.get("/api/v1/health/daily/2026-08-30", headers=AUTH_HEADERS)
    status = health_api.get("/api/v1/health/status", headers=AUTH_HEADERS)

    assert created.status_code == 200
    assert created.json()["data"]["workoutId"] == WORKOUT_ID
    assert repeated.json()["idempotent"] is True
    assert updated.json()["data"]["activeEnergyKcal"] == 220
    assert daily.json()["workouts"][0]["activityType"] == "walking"
    assert status.json()["workoutCount"] == 1
    assert status.json()["serverEnabled"] is True


def test_workout_rejects_raw_uuid_extra_route_and_delete_is_idempotent(health_api):
    raw_uuid_url = "/api/v1/health/workouts/9F2C7D0B-0000-0000-0000-000000000000"
    raw_uuid = health_api.put(
        raw_uuid_url, headers=AUTH_HEADERS, json=workout_payload()
    )
    route_payload = {**workout_payload(), "route": [[35.0, 139.0]]}
    route = health_api.put(
        f"/api/v1/health/workouts/{WORKOUT_ID}",
        headers=AUTH_HEADERS,
        json=route_payload,
    )
    health_api.put(
        f"/api/v1/health/workouts/{WORKOUT_ID}",
        headers=AUTH_HEADERS,
        json=workout_payload(),
    )
    deleted = health_api.delete(
        f"/api/v1/health/workouts/{WORKOUT_ID}?revision=2&timeZone=Asia%2FTokyo",
        headers=AUTH_HEADERS,
    )
    repeated = health_api.delete(
        f"/api/v1/health/workouts/{WORKOUT_ID}?revision=2&timeZone=Asia%2FTokyo",
        headers=AUTH_HEADERS,
    )

    assert raw_uuid.status_code == 422
    assert route.status_code == 422
    assert deleted.json()["data"]["deleted"] is True
    assert repeated.json()["idempotent"] is True


def test_health_api_serializes_sync_timestamps_as_utc(health_api):
    health_api.put(
        "/api/v1/health/sleep/2026-08-30",
        headers=AUTH_HEADERS,
        json=sleep_payload(),
    )
    response = health_api.get(
        "/api/v1/health/sleep/2026-08-30", headers=AUTH_HEADERS
    )
    synced = datetime.fromisoformat(response.json()["data"]["syncedAt"].replace("Z", "+00:00"))

    assert synced.tzinfo == UTC
