from __future__ import annotations

import base64
import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from life_inbox.config import Settings
from life_inbox.db import Database
from life_inbox.models import Attachment, Event, Memo
from life_inbox.rhythm_models import RatingImageRow
from life_inbox.web import create_web_app

RAW_DEVICE_TOKEN = "iphone-test-device-token"
AUTH_HEADERS = {"Authorization": f"Bearer {RAW_DEVICE_TOKEN}"}


@pytest.fixture
def api_environment(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'api.db'}",
        data_dir=tmp_path,
        allowed_user_id=123456,
        rhythm_enabled=True,
        rhythm_device_sync_token_sha256=hashlib.sha256(
            RAW_DEVICE_TOKEN.encode()
        ).hexdigest(),
        rhythm_image_dir=tmp_path / "rhythm-images",
        rhythm_api_rate_limit_per_minute=60,
    )
    database = Database(settings.database_url)
    database.init()
    app = create_web_app(settings, database=database)
    persistence = app.state.rhythm_persistence
    with TestClient(app) as client:
        yield settings, database, persistence, client


def _spending_payload(*, revision: int = 1, total: int = 1_200):
    return {
        "timeZone": "Asia/Tokyo",
        "chunithmAmountYen": 500,
        "maimaiAmountYen": 300,
        "ongekiAmountYen": 400,
        "totalAmountYen": total,
        "revision": revision,
    }


KISEKI_MOMENT_ID = "550e8400-e29b-41d4-a716-446655440000"


def _kiseki_payload(*, revision: int = 1, **changes):
    payload = {
        "schemaVersion": 1,
        "revision": revision,
        "occurredAt": "2026-08-30T18:42:00+09:00",
        "title": "今日のMoment",
        "note": "生活アーカイブへ明示的に保存する本文",
        "mood": 4,
        "favorite": True,
        "origin": "iphone",
        "privacyProcessed": True,
        "deleted": False,
    }
    payload.update(changes)
    return payload


def _kiseki_headers(*, revision: int = 1):
    return {
        **AUTH_HEADERS,
        "Idempotency-Key": f"kiseki-moment:{KISEKI_MOMENT_ID}:{revision}",
    }


def test_api_requires_dedicated_device_token_and_never_reflects_it(api_environment):
    _, _, _, client = api_environment

    missing = client.get("/api/v1/rhythm/ratings/latest")
    invalid = client.get(
        "/api/v1/rhythm/ratings/latest",
        headers={"Authorization": "Bearer a-secret-that-must-not-return"},
    )

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"
    assert invalid.status_code == 401
    assert "a-secret-that-must-not-return" not in invalid.text
    assert RAW_DEVICE_TOKEN not in invalid.text


def test_authentication_precedes_path_and_body_validation(api_environment):
    _, _, _, client = api_environment

    malformed_path = client.put(
        "/api/v1/rhythm/spending/not-a-date",
        content=b"{not-json",
        headers={"Content-Type": "application/json"},
    )
    malformed_body = client.post(
        "/api/v1/rhythm/collection/run",
        content=b"{not-json",
        headers={"Content-Type": "application/json"},
    )

    assert malformed_path.status_code == 401
    assert malformed_path.json()["error"]["code"] == "unauthorized"
    assert malformed_body.status_code == 401
    assert malformed_body.json()["error"]["code"] == "unauthorized"


def test_kiseki_moment_api_requires_auth_before_path_and_body_validation(
    api_environment,
):
    _, _, _, client = api_environment

    response = client.put(
        "/api/v1/archive/kiseki/moments/not-a-uuid",
        content=b"{not-json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_kiseki_moment_api_create_retry_update_delete_restore_and_get(
    api_environment,
):
    _, database, _, client = api_environment
    url = f"/api/v1/archive/kiseki/moments/{KISEKI_MOMENT_ID}"

    created = client.put(
        url,
        headers=_kiseki_headers(revision=1),
        json=_kiseki_payload(revision=1),
    )
    repeated = client.put(
        url,
        headers=_kiseki_headers(revision=1),
        json=_kiseki_payload(revision=1),
    )
    updated = client.put(
        url,
        headers=_kiseki_headers(revision=2),
        json=_kiseki_payload(revision=2, title="更新後のMoment", favorite=False),
    )
    loaded = client.get(url, headers=AUTH_HEADERS)

    assert created.status_code == 200
    assert created.json()["accepted"] is True
    assert created.json()["created"] is True
    assert created.json()["data"]["status"] == "confirmed"
    assert created.json()["discordDeliveryIsAsynchronous"] is True
    assert created.json()["notionDeliveryIsExplicit"] is True
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] is True
    assert repeated.json()["data"]["eventId"] == created.json()["data"]["eventId"]
    assert updated.status_code == 200
    assert updated.json()["data"]["eventId"] == created.json()["data"]["eventId"]
    assert updated.json()["data"]["revision"] == 2
    assert loaded.status_code == 200
    assert loaded.json()["data"]["revision"] == 2

    deleted = client.put(
        url,
        headers=_kiseki_headers(revision=3),
        json={
            "schemaVersion": 1,
            "revision": 3,
            "privacyProcessed": True,
            "deleted": True,
        },
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted"] is True
    assert deleted.json()["data"]["status"] == "ignored"

    with database.session() as session:
        event = session.get(Event, created.json()["data"]["eventId"])
        assert event is not None
        assert event.raw_text == ""
        assert event.memo is None

    restored = client.put(
        url,
        headers=_kiseki_headers(revision=4),
        json=_kiseki_payload(revision=4, title="復元したMoment", note="復元本文"),
    )
    assert restored.status_code == 200
    assert restored.json()["data"]["deleted"] is False
    assert restored.json()["data"]["eventId"] == created.json()["data"]["eventId"]

    with database.session() as session:
        event = session.get(Event, created.json()["data"]["eventId"])
        assert event is not None
        assert event.raw_text == "復元したMoment\n復元本文"
        assert event.memo is not None


def test_kiseki_moment_api_rejects_stale_conflict_location_and_bad_key(
    api_environment,
):
    _, _, _, client = api_environment
    url = f"/api/v1/archive/kiseki/moments/{KISEKI_MOMENT_ID}"
    assert client.put(
        url,
        headers=_kiseki_headers(revision=2),
        json=_kiseki_payload(revision=2),
    ).status_code == 200

    stale = client.put(
        url,
        headers=_kiseki_headers(revision=1),
        json=_kiseki_payload(revision=1),
    )
    conflict = client.put(
        url,
        headers=_kiseki_headers(revision=2),
        json=_kiseki_payload(revision=2, title="同revisionの別内容"),
    )
    location = client.put(
        url,
        headers=_kiseki_headers(revision=3),
        json=_kiseki_payload(revision=3, latitude=35.0, longitude=139.0),
    )
    mismatched_key = client.put(
        url,
        headers={**AUTH_HEADERS, "Idempotency-Key": "kiseki-moment:wrong:3"},
        json=_kiseki_payload(revision=3),
    )
    unsafe_delete = client.put(
        url,
        headers=_kiseki_headers(revision=3),
        json={
            "schemaVersion": 1,
            "revision": 3,
            "privacyProcessed": True,
            "deleted": True,
            "title": "削除リクエストに本文を混ぜない",
        },
    )

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_revision"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "revision_conflict"
    assert location.status_code == 422
    assert location.json()["error"]["code"] == "invalid_request"
    assert mismatched_key.status_code == 409
    assert mismatched_key.json()["error"]["code"] == "idempotency_key_conflict"
    assert unsafe_delete.status_code == 422


def test_kiseki_moment_api_rejects_missing_privacy_attestation_and_naive_time(
    api_environment,
):
    _, _, _, client = api_environment
    url = f"/api/v1/archive/kiseki/moments/{KISEKI_MOMENT_ID}"
    missing_privacy = _kiseki_payload()
    del missing_privacy["privacyProcessed"]

    missing = client.put(
        url,
        headers=_kiseki_headers(),
        json=missing_privacy,
    )
    false = client.put(
        url,
        headers=_kiseki_headers(),
        json=_kiseki_payload(privacyProcessed=False),
    )
    naive = client.put(
        url,
        headers=_kiseki_headers(),
        json=_kiseki_payload(occurredAt="2026-08-30T18:42:00"),
    )

    assert missing.status_code == 422
    assert false.status_code == 422
    assert naive.status_code == 422
    assert naive.json()["error"]["code"] == "invalid_kiseki_moment"


def test_spending_put_is_yen_only_revision_safe_and_gettable(api_environment):
    _, _, _, client = api_environment
    url = "/api/v1/rhythm/spending/2026-08-29"

    created = client.put(url, headers=AUTH_HEADERS, json=_spending_payload())
    repeated = client.put(url, headers=AUTH_HEADERS, json=_spending_payload())
    stale = client.put(
        url,
        headers=AUTH_HEADERS,
        json={**_spending_payload(), "revision": 0},
    )
    loaded = client.get(url, headers=AUTH_HEADERS)

    assert created.status_code == 200
    assert created.json()["data"]["totalAmountYen"] == 1_200
    assert created.json()["created"] is True
    assert repeated.json()["idempotent"] is True
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_revision"
    assert loaded.json()["data"]["chunithmAmountYen"] == 500
    assert "cr" not in loaded.text.casefold()


def test_spending_rejects_negative_total_mismatch_vending_and_bad_date(api_environment):
    _, _, _, client = api_environment
    url = "/api/v1/rhythm/spending/2026-08-29"

    negative = client.put(
        url,
        headers=AUTH_HEADERS,
        json={**_spending_payload(), "maimaiAmountYen": -100},
    )
    mismatch = client.put(
        url,
        headers=AUTH_HEADERS,
        json=_spending_payload(total=999),
    )
    vending = client.put(
        url,
        headers=AUTH_HEADERS,
        json={**_spending_payload(), "vendingAmountYen": 100},
    )
    bad_date = client.put(
        "/api/v1/rhythm/spending/2026-02-30",
        headers=AUTH_HEADERS,
        json=_spending_payload(),
    )

    assert negative.status_code == 422
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "invalid_spending"
    assert vending.status_code == 422
    assert bad_date.status_code == 422
    assert bad_date.json()["error"]["code"] == "invalid_request"


def test_spending_delete_is_revisioned_and_idempotent(api_environment):
    _, _, _, client = api_environment
    url = "/api/v1/rhythm/spending/2026-08-29"
    client.put(url, headers=AUTH_HEADERS, json=_spending_payload())

    deleted = client.delete(
        f"{url}?revision=2&timeZone=Asia%2FTokyo", headers=AUTH_HEADERS
    )
    repeated = client.delete(
        f"{url}?revision=2&timeZone=Asia%2FTokyo", headers=AUTH_HEADERS
    )

    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted"] is True
    assert deleted.json()["data"]["totalAmountYen"] == 0
    assert repeated.json()["idempotent"] is True


def _seed_snapshot(api_environment, *, game: str, minute: int, rating: str) -> int:
    settings, _, persistence, _ = api_environment
    user_id = persistence.ensure_user(
        display_name=settings.rhythm_player_display_name,
        time_zone=settings.rhythm_time_zone,
        discord_user_id=settings.allowed_user_id,
    )
    collected = datetime(2026, 8, 29, 12, minute, tzinfo=UTC)
    return persistence.save_snapshot(
        user_id=user_id,
        game=game,
        player_name="APIテスト",
        rating=Decimal(rating),
        previous_rating=Decimal("16.70") if game != "maimai" else Decimal("15420"),
        semantic_fingerprint=hashlib.sha256(
            f"{game}:{minute}:{rating}".encode()
        ).hexdigest(),
        source_updated_at=collected,
        collected_at=collected,
        raw_data={"fixture": True},
        entries=[],
    )


def test_latest_history_and_authenticated_image(api_environment):
    settings, _, persistence, client = api_environment
    snapshot_id = _seed_snapshot(
        api_environment, game="chunithm", minute=1, rating="16.73"
    )
    _seed_snapshot(api_environment, game="maimai", minute=2, rating="15432")
    image_path = settings.rhythm_image_dir / "chunithm-fixture.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    persistence.attach_image(
        snapshot_id=snapshot_id,
        game="chunithm",
        image_path=str(image_path),
        image_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
        width=1_200,
        height=675,
        byte_size=image_path.stat().st_size,
        generated_at=datetime.now(UTC),
        renderer_version="api-test",
        image_root=str(settings.rhythm_image_dir),
    )

    latest = client.get("/api/v1/rhythm/ratings/latest", headers=AUTH_HEADERS)
    history = client.get(
        "/api/v1/rhythm/ratings/history?game=chunithm&page=1&pageSize=1",
        headers=AUTH_HEADERS,
    )
    unauthorized_image = client.get(f"/api/v1/rhythm/ratings/{snapshot_id}/image")
    image = client.get(
        f"/api/v1/rhythm/ratings/{snapshot_id}/image", headers=AUTH_HEADERS
    )

    assert latest.status_code == 200
    assert latest.json()["chunithm"]["rating"] == 16.73
    assert latest.json()["maimai"]["status"] == "not_collected"
    assert latest.json()["ongeki"]["status"] == "not_collected"
    assert history.json()["total"] == 1
    assert history.json()["items"][0]["game"] == "chunithm"
    assert unauthorized_image.status_code == 401
    assert image.status_code == 200
    assert image.content.startswith(b"\x89PNG")
    assert image.headers["cache-control"] == "no-store"


def test_image_endpoint_does_not_trust_a_database_path_outside_image_root(
    api_environment,
):
    settings, database, persistence, client = api_environment
    snapshot_id = _seed_snapshot(
        api_environment, game="chunithm", minute=4, rating="16.74"
    )
    valid = settings.rhythm_image_dir / "valid.png"
    valid.parent.mkdir(parents=True, exist_ok=True)
    valid.write_bytes(b"\x89PNG\r\n\x1a\nvalid")
    persistence.attach_image(
        snapshot_id=snapshot_id,
        game="chunithm",
        image_path=str(valid),
        image_sha256=hashlib.sha256(valid.read_bytes()).hexdigest(),
        width=1,
        height=1,
        byte_size=valid.stat().st_size,
        generated_at=datetime.now(UTC),
        renderer_version="api-test",
        image_root=str(settings.rhythm_image_dir),
    )
    outside = settings.data_dir / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\noutside")
    with database.session() as session:
        row = session.query(RatingImageRow).one()
        row.image_path = str(outside)
        session.commit()

    response = client.get(
        f"/api/v1/rhythm/ratings/{snapshot_id}/image",
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "image_not_found"
    assert outside.read_bytes() not in response.content


def test_history_validates_game_date_range_and_pagination(api_environment):
    _, _, _, client = api_environment

    invalid_game = client.get(
        "/api/v1/rhythm/ratings/history?game=sdvx", headers=AUTH_HEADERS
    )
    invalid_range = client.get(
        "/api/v1/rhythm/ratings/history?from=2026-08-30&to=2026-08-29",
        headers=AUTH_HEADERS,
    )
    invalid_page = client.get(
        "/api/v1/rhythm/ratings/history?page=0", headers=AUTH_HEADERS
    )

    assert invalid_game.json()["error"]["code"] == "invalid_game"
    assert invalid_range.json()["error"]["code"] == "invalid_date_range"
    assert invalid_page.json()["error"]["code"] == "invalid_request"


def test_collection_run_request_is_idempotent_and_status_is_explicit(api_environment):
    _, _, persistence, client = api_environment
    headers = {**AUTH_HEADERS, "Idempotency-Key": "action-button-run-001"}
    payload = {"games": ["chunithm", "maimai"]}

    first = client.post("/api/v1/rhythm/collection/run", headers=headers, json=payload)
    repeated = client.post("/api/v1/rhythm/collection/run", headers=headers, json=payload)
    status = client.get("/api/v1/rhythm/collection/status", headers=AUTH_HEADERS)

    assert first.status_code == 200
    assert [item["runId"] for item in first.json()["runs"]] == [
        item["runId"] for item in repeated.json()["runs"]
    ]
    assert status.json()["games"]["chunithm"]["state"] == "queued"
    assert status.json()["games"]["ongeki"]["state"] == "not_started"
    assert status.json()["games"]["chunithm"]["lastFailureAt"] is None
    assert status.json()["games"]["chunithm"]["lastDiscordPostAt"] is None
    assert status.json()["games"]["chunithm"]["unpostedCount"] == 0
    assert status.json()["games"]["chunithm"]["providerSessionStatus"] == (
        "not_configured"
    )
    assert status.json()["dryRun"] is True
    assert status.json()["nextRunAt"].endswith("Z")

    run_id = first.json()["runs"][0]["runId"]
    opening = persistence.transition_run(
        run_id=run_id,
        to_state="openingProvider",
        event_type="providerOpening",
        message="CHUNIRECを開きます",
    )
    persistence.transition_run(
        run_id=opening.id,
        to_state="waitingForManualAction",
        event_type="manualActionRequired",
        message="更新完了を確認してください",
        manual_action="confirmChunirecRefresh",
    )
    confirmed = client.post(
        f"/api/v1/rhythm/collection/{run_id}/confirm", headers=AUTH_HEADERS
    )
    repeated_confirm = client.post(
        f"/api/v1/rhythm/collection/{run_id}/confirm", headers=AUTH_HEADERS
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["run"]["state"] == "waitingForManualAction"
    assert confirmed.json()["run"]["manualActionRequired"] is False
    assert repeated_confirm.status_code == 200


def test_collection_run_rejects_invalid_game_or_missing_idempotency(api_environment):
    _, _, _, client = api_environment

    invalid = client.post(
        "/api/v1/rhythm/collection/run",
        headers={**AUTH_HEADERS, "Idempotency-Key": "request-001"},
        json={"games": ["sdvx"]},
    )
    missing = client.post(
        "/api/v1/rhythm/collection/run",
        headers=AUTH_HEADERS,
        json={"games": ["maimai"]},
    )

    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "idempotency_key_required"


def test_collection_idempotency_key_rejects_a_different_game_set(api_environment):
    _, _, _, client = api_environment
    headers = {**AUTH_HEADERS, "Idempotency-Key": "collection-conflict-001"}

    first = client.post(
        "/api/v1/rhythm/collection/run",
        headers=headers,
        json={"games": ["chunithm"]},
    )
    conflicting = client.post(
        "/api/v1/rhythm/collection/run",
        headers=headers,
        json={"games": ["maimai"]},
    )

    assert first.status_code == 200
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "idempotency_key_conflict"


def test_action_button_shortcut_memo_is_idempotent(api_environment):
    _, database, _, client = api_environment
    headers = {**AUTH_HEADERS, "Idempotency-Key": "memo-shortcut-20260829-001"}
    payload = {
        "body": "研究室へ充電器を持っていく",
        "occurredAt": "2026-08-29T18:00:00+09:00",
    }

    first = client.post("/api/v1/shortcuts/memo", headers=headers, json=payload)
    repeated = client.post("/api/v1/shortcuts/memo", headers=headers, json=payload)

    assert first.status_code == 200
    assert repeated.json()["eventId"] == first.json()["eventId"]
    assert repeated.json()["idempotent"] is True
    with database.session() as session:
        assert session.query(Event).count() == 1
        assert session.query(Memo).one().body == payload["body"]


def test_action_button_memo_idempotency_key_is_bound_to_payload(api_environment):
    _, database, _, client = api_environment
    headers = {**AUTH_HEADERS, "Idempotency-Key": "memo-conflict-20260829"}
    first = client.post(
        "/api/v1/shortcuts/memo",
        headers=headers,
        json={"body": "充電器を持つ"},
    )
    conflicting = client.post(
        "/api/v1/shortcuts/memo",
        headers=headers,
        json={"body": "傘を持つ"},
    )

    assert first.status_code == 200
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "idempotency_key_conflict"
    with database.session() as session:
        assert session.query(Event).count() == 1


def test_action_button_anything_inbox_is_raw_first_and_idempotent(api_environment):
    _, database, _, client = api_environment
    headers = {**AUTH_HEADERS, "Idempotency-Key": "anything-20260830-001"}
    payload = {
        "body": "帰りに牛乳を買う\r\n予算は300円",
        "captureType": "shopping",
        "source": "actionButton",
        "occurredAt": "2026-08-30T18:42:00+09:00",
    }

    first = client.post("/api/v1/shortcuts/inbox", headers=headers, json=payload)
    repeated = client.post("/api/v1/shortcuts/inbox", headers=headers, json=payload)

    assert first.status_code == 200
    assert first.json() == {
        "eventId": first.json()["eventId"],
        "idempotent": False,
        "status": "pending",
        "captureType": "shopping",
        "source": "actionButton",
        "classificationDeferred": True,
        "locationShared": False,
        "photoStored": False,
        "discordDeliveryIsAsynchronous": True,
    }
    assert repeated.status_code == 200
    assert repeated.json()["eventId"] == first.json()["eventId"]
    assert repeated.json()["idempotent"] is True
    with database.session() as session:
        event = session.query(Event).one()
        assert len(event.discord_message_id) <= 64
        assert event.discord_channel_id == "shortcut-inbox:shopping"
        assert event.source == "iphone_action_button"
        assert event.raw_text == "帰りに牛乳を買う\n予算は300円"
        assert event.status == "pending"
        assert event.event_type == "unknown"
        assert event.event_kinds == []
        assert session.query(Memo).count() == 0


def test_action_button_anything_inbox_persists_explicit_one_shot_location(
    api_environment,
):
    _, database, _, client = api_environment
    headers = {**AUTH_HEADERS, "Idempotency-Key": "anything-location-20260901"}
    payload = {
        "body": "昼ご飯を記録",
        "captureType": "meal",
        "source": "actionButton",
        "occurredAt": "2026-09-01T12:30:00+09:00",
        "location": {
            "latitude": 34.7692,
            "longitude": 137.3915,
            "horizontalAccuracyMeters": 18.5,
            "label": "豊橋駅付近",
            "sharedAt": "2026-09-01T12:30:01+09:00",
        },
    }

    created = client.post("/api/v1/shortcuts/inbox", headers=headers, json=payload)
    repeated = client.post("/api/v1/shortcuts/inbox", headers=headers, json=payload)

    assert created.status_code == 200
    assert created.json()["locationShared"] is True
    assert created.json()["discordDeliveryIsAsynchronous"] is True
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] is True
    with database.session() as session:
        event = session.query(Event).one()
        assert event.capture_context is not None
        assert event.capture_context.latitude == pytest.approx(34.7692)
        assert event.capture_context.longitude == pytest.approx(137.3915)
        assert event.capture_context.location_label == "豊橋駅付近"
        assert event.shortcut_delivery is not None
        assert event.shortcut_delivery.status == "pending"

    conflicting = client.post(
        "/api/v1/shortcuts/inbox",
        headers=headers,
        json={
            **payload,
            "location": {**payload["location"], "latitude": 35.0},
        },
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "idempotency_key_conflict"


def test_action_button_anything_inbox_rejects_invalid_location(api_environment):
    _, database, _, client = api_environment
    response = client.post(
        "/api/v1/shortcuts/inbox",
        headers={**AUTH_HEADERS, "Idempotency-Key": "anything-bad-location"},
        json={
            "body": "位置付きメモ",
            "location": {"latitude": 91, "longitude": 137.3},
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    with database.session() as session:
        assert session.query(Event).count() == 0


def test_action_button_photo_is_stored_and_bound_to_idempotency_key(api_environment):
    settings, database, _, client = api_environment
    headers = {**AUTH_HEADERS, "Idempotency-Key": "anything-photo-20260901"}
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    photo = {
        "filename": "daily-note.png",
        "contentType": "image/png",
        "dataBase64": base64.b64encode(png).decode(),
        "sha256": hashlib.sha256(png).hexdigest(),
    }
    payload = {
        "body": "",
        "captureType": "normal",
        "source": "actionButton",
        "occurredAt": "2026-09-01T21:00:00+09:00",
        "photo": photo,
    }

    created = client.post("/api/v1/shortcuts/inbox", headers=headers, json=payload)
    repeated = client.post("/api/v1/shortcuts/inbox", headers=headers, json=payload)

    assert created.status_code == 200
    assert created.json()["photoStored"] is True
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] is True
    with database.session() as session:
        event = session.query(Event).one()
        attachment = session.query(Attachment).one()
        assert event.raw_text == ""
        assert attachment.event_id == event.id
        assert attachment.sha256 == hashlib.sha256(png).hexdigest()
        stored_path = Path(attachment.local_path).resolve()
        stored_path.relative_to((settings.data_dir / "attachments").resolve())
        assert stored_path.read_bytes() == png

    changed = {**photo, "dataBase64": base64.b64encode(png + b"changed").decode()}
    changed.pop("sha256")
    conflict = client.post(
        "/api/v1/shortcuts/inbox",
        headers=headers,
        json={**payload, "photo": changed},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_key_conflict"


def test_action_button_photo_rejects_bad_signature_and_hash(api_environment):
    _, database, _, client = api_environment
    bad_signature = client.post(
        "/api/v1/shortcuts/inbox",
        headers={**AUTH_HEADERS, "Idempotency-Key": "anything-photo-bad-signature"},
        json={
            "photo": {
                "filename": "not-image.png",
                "contentType": "image/png",
                "dataBase64": base64.b64encode(b"not an image").decode(),
            }
        },
    )
    png = b"\x89PNG\r\n\x1a\nminimal"
    bad_hash = client.post(
        "/api/v1/shortcuts/inbox",
        headers={**AUTH_HEADERS, "Idempotency-Key": "anything-photo-bad-hash"},
        json={
            "photo": {
                "filename": "image.png",
                "contentType": "image/png",
                "dataBase64": base64.b64encode(png).decode(),
                "sha256": "0" * 64,
            }
        },
    )
    empty = client.post(
        "/api/v1/shortcuts/inbox",
        headers={**AUTH_HEADERS, "Idempotency-Key": "anything-photo-empty"},
        json={"body": ""},
    )

    assert bad_signature.status_code == 422
    assert bad_signature.json()["error"]["code"] == "invalid_photo"
    assert bad_hash.status_code == 422
    assert bad_hash.json()["error"]["code"] == "photo_hash_mismatch"
    assert empty.status_code == 422
    with database.session() as session:
        assert session.query(Event).count() == 0


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("body", "別の内容"),
        ("captureType", "memo"),
        ("source", "widget"),
        ("occurredAt", "2026-08-30T18:43:00+09:00"),
    ],
)
def test_anything_inbox_idempotency_key_is_bound_to_entire_payload(
    api_environment,
    changed_field: str,
    changed_value: str,
):
    _, database, _, client = api_environment
    headers = {**AUTH_HEADERS, "Idempotency-Key": "anything-conflict-20260830"}
    payload = {
        "body": "今日の独り言",
        "captureType": "monologue",
        "source": "siri",
        "occurredAt": "2026-08-30T20:00:00+09:00",
    }

    assert client.post(
        "/api/v1/shortcuts/inbox", headers=headers, json=payload
    ).status_code == 200
    conflicting = client.post(
        "/api/v1/shortcuts/inbox",
        headers=headers,
        json={**payload, changed_field: changed_value},
    )

    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "idempotency_key_conflict"
    with database.session() as session:
        assert session.query(Event).count() == 1


def test_anything_inbox_requires_auth_and_rejects_unsupported_input(api_environment):
    _, database, _, client = api_environment

    unauthorized = client.post(
        "/api/v1/shortcuts/inbox",
        content=b"{not-json",
        headers={"Content-Type": "application/json"},
    )
    invalid = client.post(
        "/api/v1/shortcuts/inbox",
        headers={**AUTH_HEADERS, "Idempotency-Key": "anything-invalid-001"},
        json={
            "body": "unsupported source",
            "captureType": "normal",
            "source": "unknown-device",
        },
    )

    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "unauthorized"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"
    with database.session() as session:
        assert session.query(Event).count() == 0


def test_shortcut_capabilities_are_authenticated_and_machine_readable(api_environment):
    _, _, _, client = api_environment

    unauthorized = client.get("/api/v1/shortcuts/capabilities")
    response = client.get(
        "/api/v1/shortcuts/capabilities",
        headers=AUTH_HEADERS,
    )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    payload = response.json()
    assert payload["apiVersion"] == 1
    assert payload["authentication"] == "bearer-device-token"
    assert payload["anythingInbox"]["path"] == "/api/v1/shortcuts/inbox"
    assert payload["anythingInbox"]["idempotencyKeyRequired"] is True
    assert payload["anythingInbox"]["classificationDeferred"] is True
    assert payload["anythingInbox"]["explicitLocationSupported"] is True
    assert payload["anythingInbox"]["continuousTrackingSupported"] is False
    assert payload["anythingInbox"]["discordDeliveryIsAsynchronous"] is True
    assert payload["anythingInbox"]["photoSupported"] is True
    assert payload["anythingInbox"]["maxPhotoCount"] == 1
    assert payload["anythingInbox"]["maxPhotoBytes"] > 0
    assert "image/png" in payload["anythingInbox"]["photoContentTypes"]
    assert "monologue" in payload["anythingInbox"]["captureTypes"]
    assert "shopping" in payload["anythingInbox"]["captureTypes"]
    assert "actionButton" in payload["anythingInbox"]["sources"]
    assert payload["health"]["statusPath"] == "/api/v1/health/status"
    assert payload["health"]["workoutPathTemplate"].endswith("/{sha256WorkoutId}")
    assert payload["health"]["serverEnabled"] is False
    assert RAW_DEVICE_TOKEN not in response.text


def test_action_button_today_summary_is_compact_and_authenticated(api_environment):
    _, _, _, client = api_environment

    unauthorized = client.get("/api/v1/shortcuts/today")
    response = client.get("/api/v1/shortcuts/today", headers=AUTH_HEADERS)

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    payload = response.json()
    assert payload["timeZone"] == "Asia/Tokyo"
    assert payload["spending"] is None
    assert set(payload["ratings"]) == {"chunithm", "maimai", "ongeki"}
    assert set(payload["collection"]) == {"chunithm", "maimai", "ongeki"}
    assert payload["collection"]["maimai"]["state"] == "not_started"
    assert payload["dryRun"] is True


def test_api_rate_limit_is_machine_readable_and_retryable(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'limited.db'}",
        data_dir=tmp_path,
        rhythm_device_sync_token_sha256=hashlib.sha256(
            RAW_DEVICE_TOKEN.encode()
        ).hexdigest(),
        rhythm_api_rate_limit_per_minute=1,
    )
    database = Database(settings.database_url)
    database.init()
    app = create_web_app(settings, database=database)

    with TestClient(app) as client:
        assert client.get(
            "/api/v1/rhythm/ratings/latest", headers=AUTH_HEADERS
        ).status_code == 200
        limited = client.get("/api/v1/rhythm/ratings/latest", headers=AUTH_HEADERS)

    assert limited.status_code == 429
    assert limited.json()["error"] == {
        "code": "rate_limited",
        "message": "リクエストが多すぎます。少し待って再試行してください。",
        "retryable": True,
    }


def test_history_to_date_is_inclusive_in_configured_timezone(api_environment):
    _, _, persistence, client = api_environment
    snapshot_id = _seed_snapshot(
        api_environment, game="ongeki", minute=3, rating="16.45"
    )
    response = client.get(
        "/api/v1/rhythm/ratings/history?game=ongeki&from=2026-08-29&to=2026-08-29",
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["snapshotId"] == snapshot_id

    next_day = date(2026, 8, 30)
    assert next_day == date(2026, 8, 29) + timedelta(days=1)
