from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from life_inbox.rhythm_persistence import (
    CollectionRunConflictError,
    DeliveryClaimConflictError,
    RhythmPersistence,
    SpendingRevisionConflictError,
    StaleSpendingRevisionError,
)


@pytest.fixture
def persistence(db):
    store = RhythmPersistence(db.session_factory)
    user_id = store.ensure_user(
        display_name="テストプレイヤー",
        time_zone="Asia/Tokyo",
        discord_user_id="rhythm-persistence-user",
    )
    return store, user_id


def test_spending_sync_uses_yen_and_is_revision_idempotent(persistence):
    store, user_id = persistence
    target = date(2026, 8, 29)

    created = store.sync_spending(
        user_id=user_id,
        local_date=target,
        time_zone="Asia/Tokyo",
        chunithm_amount_yen=100,
        maimai_amount_yen=200,
        ongeki_amount_yen=300,
        total_amount_yen=600,
        revision=12,
    )
    repeated = store.sync_spending(
        user_id=user_id,
        local_date=target,
        time_zone="Asia/Tokyo",
        chunithm_amount_yen=100,
        maimai_amount_yen=200,
        ongeki_amount_yen=300,
        total_amount_yen=600,
        revision=12,
    )

    assert created.created is True
    assert created.record.chunithm_amount_yen == 100
    assert created.record.total_amount_yen == 600
    assert repeated.idempotent is True
    assert repeated.record.id == created.record.id


def test_spending_sync_rejects_old_or_conflicting_revision(persistence):
    store, user_id = persistence
    target = date(2026, 8, 29)
    store.sync_spending(
        user_id=user_id,
        local_date=target,
        time_zone="Asia/Tokyo",
        chunithm_amount_yen=500,
        maimai_amount_yen=0,
        ongeki_amount_yen=0,
        total_amount_yen=500,
        revision=2,
    )

    with pytest.raises(StaleSpendingRevisionError):
        store.sync_spending(
            user_id=user_id,
            local_date=target,
            time_zone="Asia/Tokyo",
            chunithm_amount_yen=100,
            maimai_amount_yen=0,
            ongeki_amount_yen=0,
            total_amount_yen=100,
            revision=1,
        )

    with pytest.raises(SpendingRevisionConflictError):
        store.sync_spending(
            user_id=user_id,
            local_date=target,
            time_zone="Asia/Tokyo",
            chunithm_amount_yen=600,
            maimai_amount_yen=0,
            ongeki_amount_yen=0,
            total_amount_yen=600,
            revision=2,
        )


def test_spending_delete_is_tombstoned_and_can_be_resynced(persistence):
    store, user_id = persistence
    target = date(2026, 8, 29)
    store.sync_spending(
        user_id=user_id,
        local_date=target,
        time_zone="Asia/Tokyo",
        chunithm_amount_yen=100,
        maimai_amount_yen=0,
        ongeki_amount_yen=0,
        total_amount_yen=100,
        revision=1,
    )

    deleted = store.delete_spending(
        user_id=user_id,
        local_date=target,
        time_zone="Asia/Tokyo",
        revision=2,
    )
    restored = store.sync_spending(
        user_id=user_id,
        local_date=target,
        time_zone="Asia/Tokyo",
        chunithm_amount_yen=0,
        maimai_amount_yen=100,
        ongeki_amount_yen=0,
        total_amount_yen=100,
        revision=3,
    )

    assert deleted.record.deleted is True
    assert deleted.record.total_amount_yen == 0
    assert restored.record.deleted is False
    assert restored.record.maimai_amount_yen == 100


def test_manual_confirmation_records_event_but_never_marks_success(persistence):
    store, user_id = persistence
    run = store.queue_collection_runs(
        user_id=user_id, games=["chunithm"], trigger="manual"
    )[0]
    opening = store.transition_run(
        run_id=run.id,
        to_state="openingProvider",
        event_type="providerOpening",
        message="CHUNIRECを開きます",
    )
    waiting = store.transition_run(
        run_id=opening.id,
        to_state="waitingForManualAction",
        event_type="manualActionRequired",
        message="CHUNIREC更新完了を確認してください",
        manual_action="confirmChunirecRefresh",
    )

    confirmed = store.confirm_manual_action(run_id=waiting.id)

    assert confirmed.state == "waitingForManualAction"
    assert confirmed.manual_action is None
    assert confirmed.manual_confirmed_at is not None
    assert confirmed.state not in {"posted", "unchanged"}

    repeated = store.confirm_manual_action(run_id=waiting.id)
    assert repeated.id == confirmed.id
    assert repeated.manual_confirmed_at == confirmed.manual_confirmed_at


def test_collection_run_idempotency_key_returns_same_runs(persistence):
    store, user_id = persistence

    first = store.queue_collection_runs(
        user_id=user_id,
        games=["chunithm", "maimai"],
        trigger="iphone",
        request_key="shortcut-request-1",
    )
    repeated = store.queue_collection_runs(
        user_id=user_id,
        games=["chunithm", "maimai"],
        trigger="iphone",
        request_key="shortcut-request-1",
    )

    assert [run.id for run in repeated] == [run.id for run in first]


def test_collection_run_idempotency_key_is_bound_to_exact_game_set(persistence):
    store, user_id = persistence
    store.queue_collection_runs(
        user_id=user_id,
        games=["chunithm", "maimai"],
        trigger="iphone",
        request_key="shortcut-request-bound",
    )

    with pytest.raises(CollectionRunConflictError):
        store.queue_collection_runs(
            user_id=user_id,
            games=["chunithm", "ongeki"],
            trigger="iphone",
            request_key="shortcut-request-bound",
        )


def test_manual_wait_cannot_be_bypassed_without_recorded_explicit_confirmation(
    persistence,
):
    store, user_id = persistence
    run = store.queue_collection_runs(
        user_id=user_id, games=["chunithm"], trigger="manual"
    )[0]
    opening = store.transition_run(
        run_id=run.id,
        to_state="openingProvider",
        event_type="providerOpening",
        message="取得元を開きます",
    )
    waiting = store.transition_run(
        run_id=opening.id,
        to_state="waitingForManualAction",
        event_type="manualActionRequired",
        message="確認待ちです",
        manual_action="confirmChunirecRefresh",
    )

    with pytest.raises(CollectionRunConflictError):
        store.transition_run(
            run_id=waiting.id,
            to_state="refreshingSource",
            event_type="unsafeTimerElapsed",
            message="時間が経過しました",
        )

    store.confirm_manual_action(run_id=waiting.id)
    with pytest.raises(CollectionRunConflictError):
        store.transition_run(
            run_id=waiting.id,
            to_state="refreshingSource",
            event_type="confirmationFlagMissing",
            message="確認済みです",
        )

    resumed = store.transition_run(
        run_id=waiting.id,
        to_state="refreshingSource",
        event_type="manualConfirmationConsumed",
        message="明示確認を受けて再開します",
        explicit_confirmation=True,
    )
    assert resumed.state == "refreshingSource"


def test_process_restart_fails_inflight_but_keeps_manual_wait_resumable(persistence):
    store, user_id = persistence
    collecting, waiting = store.queue_collection_runs(
        user_id=user_id,
        games=["maimai", "ongeki"],
        trigger="daily",
    )
    current = store.transition_run(
        run_id=collecting.id,
        to_state="openingProvider",
        event_type="providerOpening",
        message="開いています",
    )
    current = store.transition_run(
        run_id=current.id,
        to_state="refreshingSource",
        event_type="sourceRefreshing",
        message="更新中",
    )
    store.transition_run(
        run_id=current.id,
        to_state="collectingData",
        event_type="providerDataReady",
        message="構造化中",
    )
    opening = store.transition_run(
        run_id=waiting.id,
        to_state="openingProvider",
        event_type="providerOpening",
        message="開いています",
    )
    auth = store.transition_run(
        run_id=opening.id,
        to_state="authenticationRequired",
        event_type="authenticationRequired",
        message="ログインが必要です",
    )
    store.transition_run(
        run_id=auth.id,
        to_state="waitingForManualAction",
        event_type="manualActionRequired",
        message="ログインが必要です",
        manual_action="login",
    )

    recovered = store.recover_interrupted_runs()

    assert collecting.id in recovered
    assert store.get_run(collecting.id).state == "failed"
    assert store.get_run(collecting.id).last_error_code == "process_restarted"
    assert store.get_run(waiting.id).state == "waitingForManualAction"


def test_sqlite_lease_prevents_two_bot_instances_and_can_expire(persistence):
    store, user_id = persistence
    now = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)

    assert store.acquire_lease(
        user_id=user_id, game="maimai", owner_id="bot-a", now=now, lease_seconds=30
    )
    assert not store.acquire_lease(
        user_id=user_id, game="maimai", owner_id="bot-b", now=now, lease_seconds=30
    )
    assert store.acquire_lease(
        user_id=user_id,
        game="maimai",
        owner_id="bot-b",
        now=now + timedelta(seconds=31),
        lease_seconds=30,
    )


def test_lease_renewal_keeps_owner_and_restart_recovery_respects_it(persistence):
    store, user_id = persistence
    now = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
    run = store.queue_collection_runs(
        user_id=user_id,
        games=["ongeki"],
        trigger="daily",
        now=now,
    )[0]
    opening = store.transition_run(
        run_id=run.id,
        to_state="openingProvider",
        event_type="providerOpening",
        message="取得元を開いています",
        now=now,
    )
    collecting = store.transition_run(
        run_id=opening.id,
        to_state="refreshingSource",
        event_type="sourceRefreshing",
        message="更新しています",
        now=now,
    )
    store.transition_run(
        run_id=collecting.id,
        to_state="collectingData",
        event_type="dataCollecting",
        message="収集中です",
        now=now,
    )
    assert store.acquire_lease(
        user_id=user_id,
        game="ongeki",
        owner_id="bot-a",
        now=now,
        lease_seconds=30,
    )
    assert store.renew_lease(
        user_id=user_id,
        game="ongeki",
        owner_id="bot-a",
        now=now + timedelta(seconds=20),
        lease_seconds=30,
    )
    assert not store.renew_lease(
        user_id=user_id,
        game="ongeki",
        owner_id="bot-b",
        now=now + timedelta(seconds=20),
        lease_seconds=30,
    )

    assert store.recover_interrupted_runs(now=now + timedelta(seconds=40)) == []
    assert store.get_run(run.id).state == "collectingData"
    assert store.recover_interrupted_runs(now=now + timedelta(seconds=51)) == [run.id]
    assert store.get_run(run.id).state == "failed"


def test_stale_discord_claim_cannot_finalize_newer_attempt(persistence):
    store, user_id = persistence
    now = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    kwargs = {
        "user_id": user_id,
        "local_date": date(2026, 8, 29),
        "delivery_kind": "rating",
        "game": "maimai",
        "channel_id": 777,
        "semantic_fingerprint": "a" * 64,
        "image_sha256": "b" * 64,
    }
    first = store.claim_delivery(**kwargs, now=now, claim_seconds=30)
    second = store.claim_delivery(
        **kwargs,
        now=now + timedelta(seconds=31),
        claim_seconds=30,
        force=True,
    )

    with pytest.raises(DeliveryClaimConflictError):
        store.mark_delivery_succeeded(
            delivery_id=first.delivery_id,
            claim_attempt_count=first.attempt_count,
            discord_message_id="stale-message",
            posted_at=now,
        )

    store.mark_delivery_succeeded(
        delivery_id=second.delivery_id,
        claim_attempt_count=second.attempt_count,
        discord_message_id="current-message",
        posted_at=now + timedelta(seconds=32),
    )
    assert store.delivery_statuses(user_id=user_id)["maimai"].pending_or_failed_count == 0


def test_identical_rating_image_sha_is_stored_once_and_linked_twice(persistence, tmp_path):
    store, user_id = persistence
    collected = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    image_root = tmp_path / "rhythm-images"
    image_path = image_root / "fixture.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture-rating-image")
    image_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
    first_snapshot = store.save_snapshot(
        user_id=user_id,
        game="chunithm",
        player_name="テスト",
        rating=Decimal("16.72"),
        previous_rating=None,
        semantic_fingerprint="a" * 64,
        source_updated_at=collected,
        collected_at=collected,
        raw_data={"source": "fixture"},
        entries=[
            {
                "frame_type": "best",
                "rank": 1,
                "song_id": "song-1",
                "normalized_title": "Fixture Song",
                "score": 1_009_000,
            }
        ],
    )
    second_snapshot = store.save_snapshot(
        user_id=user_id,
        game="chunithm",
        player_name="テスト",
        rating=Decimal("16.73"),
        previous_rating=Decimal("16.72"),
        semantic_fingerprint="b" * 64,
        source_updated_at=collected,
        collected_at=collected + timedelta(minutes=1),
        raw_data={"source": "fixture-2"},
        entries=[],
    )

    first_image_id, first_created = store.attach_image(
        snapshot_id=first_snapshot,
        game="chunithm",
        image_path=str(image_path),
        image_sha256=image_sha256,
        width=1_200,
        height=675,
        byte_size=image_path.stat().st_size,
        generated_at=collected,
        renderer_version="fixture-v1",
        image_root=str(image_root),
    )
    second_image_id, second_created = store.attach_image(
        snapshot_id=second_snapshot,
        game="chunithm",
        image_path=str(image_path),
        image_sha256=image_sha256,
        width=1_200,
        height=675,
        byte_size=image_path.stat().st_size,
        generated_at=collected,
        renderer_version="fixture-v1",
        image_root=str(image_root),
    )

    assert first_created is True
    assert second_created is False
    assert second_image_id == first_image_id


def test_admin_status_reports_latest_failure_and_delivery_state(persistence):
    store, user_id = persistence
    occurred = datetime(2026, 8, 29, 14, 0, tzinfo=UTC)
    run = store.queue_collection_runs(
        user_id=user_id,
        games=["maimai"],
        trigger="manual",
        now=occurred,
    )[0]
    failed = store.transition_run(
        run_id=run.id,
        to_state="failed",
        event_type="providerFailed",
        message="取得に失敗しました",
        error_code="provider_outage",
        error_message="安全化済みエラー",
        now=occurred,
    )
    claim = store.claim_delivery(
        user_id=user_id,
        local_date=date(2026, 8, 29),
        delivery_kind="rating",
        game="maimai",
        channel_id=123,
        semantic_fingerprint="a" * 64,
        image_sha256="b" * 64,
        now=occurred,
    )

    assert store.latest_failed_runs(user_id=user_id)["maimai"].id == failed.id
    pending = store.delivery_statuses(user_id=user_id)["maimai"]
    assert pending.pending_or_failed_count == 1
    assert pending.last_posted_at is None

    store.mark_delivery_succeeded(
        delivery_id=claim.delivery_id,
        claim_attempt_count=claim.attempt_count,
        discord_message_id="discord-message",
        posted_at=occurred,
    )
    posted = store.delivery_statuses(user_id=user_id)["maimai"]
    assert posted.pending_or_failed_count == 0
    assert posted.last_posted_at == occurred.replace(tzinfo=None)
