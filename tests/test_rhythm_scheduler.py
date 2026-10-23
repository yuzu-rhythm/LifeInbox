from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from life_inbox.config import Settings
from life_inbox.rhythm_discord import RhythmDiscordPublisher
from life_inbox.rhythm_persistence import RhythmPersistence
from life_inbox.rhythm_scheduler import RhythmScheduler, next_daily_instant


class NullTransport:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return "message-1"

    async def edit(self, **kwargs):
        self.sent.append(kwargs)
        return kwargs["message_id"]


class ExplicitSuccessExecutor:
    def __init__(self, persistence: RhythmPersistence, image_root: Path) -> None:
        self.persistence = persistence
        self.image_root = image_root
        self.calls: list[str] = []

    async def execute(self, run) -> None:
        self.calls.append(run.id)
        current = run.state
        if current == "waitingForManualAction":
            self.persistence.transition_run(
                run_id=run.id,
                to_state="refreshingSource",
                event_type="manualConfirmed",
                message="明示確認を受けて再開",
                allowed_from={"waitingForManualAction"},
                explicit_confirmation=True,
            )
            current = "refreshingSource"
        path = (
            "openingProvider",
            "refreshingSource",
            "collectingData",
            "renderingImage",
            "comparingSnapshot",
            "readyToPost",
            "posting",
            "posted",
        )
        for state in path:
            if state == "openingProvider" and current != "queued":
                continue
            if state == "refreshingSource" and current == "refreshingSource":
                continue
            kwargs = {}
            if state == "posted":
                observed = datetime.now(UTC)
                snapshot_id = self.persistence.save_snapshot(
                    user_id=run.user_id,
                    game=run.game,
                    player_name="fixture",
                    rating=Decimal("1"),
                    previous_rating=None,
                    semantic_fingerprint=hashlib.sha256(run.id.encode()).hexdigest(),
                    source_updated_at=observed,
                    collected_at=observed,
                    raw_data={"source": "fixture"},
                    entries=[],
                )
                self.image_root.mkdir(parents=True, exist_ok=True)
                image_path = self.image_root / f"{run.id}.png"
                image_payload = b"\x89PNG\r\n\x1a\nfixture"
                image_path.write_bytes(image_payload)
                self.persistence.attach_image(
                    snapshot_id=snapshot_id,
                    game=run.game,
                    image_path=str(image_path),
                    image_sha256=hashlib.sha256(image_payload).hexdigest(),
                    width=1,
                    height=1,
                    byte_size=len(image_payload),
                    generated_at=observed,
                    renderer_version="scheduler-fixture-v1",
                    image_root=str(self.image_root),
                )
                kwargs["snapshot_id"] = snapshot_id
            self.persistence.transition_run(
                run_id=run.id,
                to_state=state,
                event_type=f"explicit:{state}",
                message=f"explicit event: {state}",
                allowed_from={current},
                **kwargs,
            )
            current = state


class FailingExecutor:
    async def execute(self, _run) -> None:
        raise RuntimeError("credential-marker-must-not-persist")


class BlockingExecutor:
    def __init__(self) -> None:
        self.started = False
        self.cancelled = False

    async def execute(self, _run) -> None:
        self.started = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class ManualBlockingExecutor:
    def __init__(self, persistence: RhythmPersistence) -> None:
        self.persistence = persistence
        self.calls: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, run) -> None:
        self.calls.append(run.id)
        self.started.set()
        await self.release.wait()
        self.persistence.transition_run(
            run_id=run.id,
            to_state="cancelled",
            event_type="manualOnlyFixture",
            message="手動一回実行fixture完了",
            allowed_from={"queued"},
        )


@pytest.fixture
def scheduler_environment(db, tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'scheduler.db'}",
        data_dir=tmp_path,
        rhythm_browser_session_dir=tmp_path / "rhythm-sessions",
        rhythm_image_dir=tmp_path / "rhythm-images",
        rhythm_enabled=True,
        rhythm_scheduler_enabled=True,
        rhythm_discord_dry_run=True,
        discord_credit_channel_id=999,
    )
    persistence = RhythmPersistence(db.session_factory)
    user_id = persistence.ensure_user(
        display_name="Scheduler Test",
        time_zone="Asia/Tokyo",
        discord_user_id="scheduler-user",
    )
    transport = NullTransport()
    publisher = RhythmDiscordPublisher(persistence, transport, dry_run=True)
    return settings, persistence, user_id, transport, publisher


def test_next_daily_instant_uses_japan_wall_clock_boundary():
    before = datetime(2026, 8, 29, 14, 54, tzinfo=UTC)  # 23:54 JST
    after = datetime(2026, 8, 29, 14, 56, tzinfo=UTC)  # 23:56 JST

    assert next_daily_instant("55 23 * * *", "Asia/Tokyo", now=before) == datetime(
        2026, 8, 29, 14, 55, tzinfo=UTC
    )
    assert next_daily_instant("55 23 * * *", "Asia/Tokyo", now=after) == datetime(
        2026, 8, 30, 14, 55, tzinfo=UTC
    )


def test_default_collection_schedule_next_instant_is_10am_jst(monkeypatch):
    monkeypatch.delenv("RHYTHM_DAILY_POST_CRON", raising=False)
    settings = Settings(_env_file=None)
    before = datetime(2026, 8, 29, 0, 59, tzinfo=UTC)  # 09:59 JST
    after = datetime(2026, 8, 29, 1, 1, tzinfo=UTC)  # 10:01 JST

    assert next_daily_instant(
        settings.rhythm_daily_post_cron,
        settings.rhythm_time_zone,
        now=before,
    ) == datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
    assert next_daily_instant(
        settings.rhythm_daily_post_cron,
        settings.rhythm_time_zone,
        now=after,
    ) == datetime(2026, 8, 30, 1, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_daily_enqueue_is_idempotent_and_uses_local_date(scheduler_environment):
    settings, persistence, user_id, _, publisher = scheduler_environment
    moment = datetime(2026, 8, 29, 15, 5, tzinfo=UTC)  # 2026-08-30 JST
    scheduler = RhythmScheduler(
        settings=settings,
        persistence=persistence,
        executor=ExplicitSuccessExecutor(persistence, settings.rhythm_image_dir),
        publisher=publisher,
        user_id=user_id,
        now=lambda: moment,
    )

    first = await scheduler.run_daily_once(moment=moment)
    repeated = await scheduler.run_daily_once(moment=moment)

    assert repeated == first
    assert len(first) == 3
    assert scheduler.local_date(moment) == date(2026, 8, 30)


@pytest.mark.asyncio
async def test_queue_processing_requires_explicit_executor_events(scheduler_environment):
    settings, persistence, user_id, _, publisher = scheduler_environment
    executor = ExplicitSuccessExecutor(persistence, settings.rhythm_image_dir)
    scheduler = RhythmScheduler(
        settings=settings,
        persistence=persistence,
        executor=executor,
        publisher=publisher,
        user_id=user_id,
    )
    run = persistence.queue_collection_runs(
        user_id=user_id, games=["chunithm"], trigger="test"
    )[0]

    processed = await scheduler.process_queue_once()

    assert processed == [run.id]
    assert persistence.get_run(run.id).state == "posted"
    assert executor.calls == [run.id]


@pytest.mark.asyncio
async def test_manual_once_processes_without_scheduler_and_is_idempotent(scheduler_environment):
    settings, persistence, user_id, _, publisher = scheduler_environment
    settings.rhythm_scheduler_enabled = False
    executor = ExplicitSuccessExecutor(persistence, settings.rhythm_image_dir)
    scheduler = RhythmScheduler(
        settings=settings,
        persistence=persistence,
        executor=executor,
        publisher=publisher,
        user_id=user_id,
    )
    run = persistence.queue_collection_runs(
        user_id=user_id, games=["chunithm"], trigger="discord", request_key="interaction:1"
    )[0]

    scheduler.start()
    assert scheduler.running is False
    assert await scheduler.run_daily_once() == []
    assert await scheduler.process_queue_once() == []

    first = await scheduler.run_manual_once([run, run])
    second = await scheduler.run_manual_once([run])

    assert first == [run.id]
    assert second == []
    assert persistence.get_run(run.id).state == "posted"
    assert executor.calls == [run.id]


@pytest.mark.asyncio
async def test_concurrent_manual_once_same_run_executes_once(scheduler_environment):
    settings, persistence, user_id, _, publisher = scheduler_environment
    settings.rhythm_scheduler_enabled = False
    executor = ManualBlockingExecutor(persistence)
    scheduler = RhythmScheduler(
        settings=settings,
        persistence=persistence,
        executor=executor,
        publisher=publisher,
        user_id=user_id,
    )
    run = persistence.queue_collection_runs(
        user_id=user_id, games=["maimai"], trigger="discord", request_key="interaction:2"
    )[0]

    first_task = asyncio.create_task(scheduler.run_manual_once([run]))
    await executor.started.wait()
    second_task = asyncio.create_task(scheduler.run_manual_once([run]))
    await asyncio.sleep(0)
    assert second_task.done() is False

    executor.release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first == [run.id]
    assert second == []
    assert executor.calls == [run.id]
    assert persistence.get_run(run.id).state == "cancelled"


@pytest.mark.asyncio
async def test_unconfirmed_manual_wait_is_not_processed(scheduler_environment):
    settings, persistence, user_id, _, publisher = scheduler_environment
    executor = ExplicitSuccessExecutor(persistence, settings.rhythm_image_dir)
    scheduler = RhythmScheduler(
        settings=settings,
        persistence=persistence,
        executor=executor,
        publisher=publisher,
        user_id=user_id,
    )
    run = persistence.queue_collection_runs(
        user_id=user_id, games=["maimai"], trigger="test"
    )[0]
    opening = persistence.transition_run(
        run_id=run.id,
        to_state="openingProvider",
        event_type="providerOpening",
        message="プロバイダーを開きます",
    )
    auth = persistence.transition_run(
        run_id=opening.id,
        to_state="authenticationRequired",
        event_type="authenticationRequired",
        message="ログインしてください",
    )
    persistence.transition_run(
        run_id=auth.id,
        to_state="waitingForManualAction",
        event_type="manualActionRequired",
        message="ログインしてください",
        manual_action="login",
    )

    assert await scheduler.process_queue_once() == []
    assert executor.calls == []
    assert persistence.get_run(run.id).state == "waitingForManualAction"

    persistence.confirm_manual_action(run_id=run.id)
    assert await scheduler.process_queue_once() == [run.id]
    assert persistence.get_run(run.id).state == "posted"


@pytest.mark.asyncio
async def test_executor_failure_is_sanitized_and_never_success(scheduler_environment):
    settings, persistence, user_id, _, publisher = scheduler_environment
    scheduler = RhythmScheduler(
        settings=settings,
        persistence=persistence,
        executor=FailingExecutor(),
        publisher=publisher,
        user_id=user_id,
    )
    run = persistence.queue_collection_runs(
        user_id=user_id, games=["ongeki"], trigger="test"
    )[0]

    assert await scheduler.process_queue_once() == []
    failed = persistence.get_run(run.id)
    assert failed.state == "failed"
    assert failed.last_error_code == "executor_failed"
    assert "credential-marker" not in (failed.last_error_message or "")


def test_restart_recovery_does_not_change_queued_or_manual_wait(scheduler_environment):
    _, persistence, user_id, _, _ = scheduler_environment
    queued, active, manual = persistence.queue_collection_runs(
        user_id=user_id,
        games=["chunithm", "maimai", "ongeki"],
        trigger="test",
    )
    active_opening = persistence.transition_run(
        run_id=active.id,
        to_state="openingProvider",
        event_type="providerOpening",
        message="開いています",
    )
    active_refreshing = persistence.transition_run(
        run_id=active_opening.id,
        to_state="refreshingSource",
        event_type="sourceRefreshing",
        message="更新中",
    )
    persistence.transition_run(
        run_id=active_refreshing.id,
        to_state="collectingData",
        event_type="dataEvent",
        message="収集中",
    )
    manual_opening = persistence.transition_run(
        run_id=manual.id,
        to_state="openingProvider",
        event_type="providerOpening",
        message="開いています",
    )
    manual_auth = persistence.transition_run(
        run_id=manual_opening.id,
        to_state="authenticationRequired",
        event_type="authenticationRequired",
        message="認証が必要です",
    )
    persistence.transition_run(
        run_id=manual_auth.id,
        to_state="waitingForManualAction",
        event_type="manualEvent",
        message="手動待ち",
        manual_action="login",
    )

    recovered = persistence.recover_interrupted_runs(
        now=datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    )

    assert recovered == [active.id]
    assert persistence.get_run(queued.id).state == "queued"
    assert persistence.get_run(active.id).state == "failed"
    assert persistence.get_run(manual.id).state == "waitingForManualAction"


@pytest.mark.asyncio
async def test_second_scheduler_cannot_duplicate_completed_run(scheduler_environment):
    settings, persistence, user_id, _, publisher = scheduler_environment
    first_executor = ExplicitSuccessExecutor(persistence, settings.rhythm_image_dir)
    second_executor = ExplicitSuccessExecutor(persistence, settings.rhythm_image_dir)
    first = RhythmScheduler(
        settings=settings,
        persistence=persistence,
        executor=first_executor,
        publisher=publisher,
        user_id=user_id,
    )
    second = RhythmScheduler(
        settings=settings,
        persistence=persistence,
        executor=second_executor,
        publisher=publisher,
        user_id=user_id,
    )
    run = persistence.queue_collection_runs(
        user_id=user_id, games=["maimai"], trigger="test"
    )[0]

    assert await first.process_queue_once() == [run.id]
    assert await second.process_queue_once() == []
    assert first_executor.calls == [run.id]
    assert second_executor.calls == []


@pytest.mark.asyncio
async def test_lost_lease_cancels_executor_before_it_can_publish(
    scheduler_environment,
):
    settings, persistence, user_id, _, publisher = scheduler_environment
    executor = BlockingExecutor()
    scheduler = RhythmScheduler(
        settings=settings,
        persistence=persistence,
        executor=executor,
        publisher=publisher,
        user_id=user_id,
    )

    async def lose_lease(_run):
        await asyncio.sleep(0)
        return False

    scheduler._lease_heartbeat = lose_lease
    run = persistence.queue_collection_runs(
        user_id=user_id, games=["maimai"], trigger="test"
    )[0]

    processed = await scheduler.process_queue_once()

    assert processed == []
    assert executor.started is True
    assert executor.cancelled is True
    assert persistence.get_run(run.id).state == "queued"
    assert persistence.lease_owned(
        user_id=user_id,
        game="maimai",
        owner_id=scheduler.owner_id,
    ) is False


@pytest.mark.asyncio
async def test_daily_spending_dry_run_never_calls_discord(scheduler_environment):
    settings, persistence, user_id, transport, publisher = scheduler_environment
    moment = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    target = moment.astimezone(ZoneInfo("Asia/Tokyo")).date()
    persistence.sync_spending(
        user_id=user_id,
        local_date=target,
        time_zone="Asia/Tokyo",
        chunithm_amount_yen=500,
        maimai_amount_yen=300,
        ongeki_amount_yen=400,
        total_amount_yen=1_200,
        revision=1,
    )
    scheduler = RhythmScheduler(
        settings=settings,
        persistence=persistence,
        executor=ExplicitSuccessExecutor(persistence, settings.rhythm_image_dir),
        publisher=publisher,
        user_id=user_id,
        now=lambda: moment,
    )

    await scheduler.run_daily_once(moment=moment)

    assert transport.sent == []
    assert target == date(2026, 8, 29)


@pytest.mark.asyncio
async def test_disabled_rhythm_never_processes_an_existing_queue(scheduler_environment):
    settings, persistence, user_id, _, publisher = scheduler_environment
    executor = ExplicitSuccessExecutor(persistence, settings.rhythm_image_dir)
    run = persistence.queue_collection_runs(
        user_id=user_id, games=["chunithm"], trigger="preexisting"
    )[0]
    settings.rhythm_enabled = False
    scheduler = RhythmScheduler(
        settings=settings,
        persistence=persistence,
        executor=executor,
        publisher=publisher,
        user_id=user_id,
    )

    scheduler.start()
    assert scheduler.running is False
    assert await scheduler.process_queue_once() == []
    assert executor.calls == []
    assert persistence.get_run(run.id).state == "queued"


@pytest.mark.asyncio
async def test_disabled_rhythm_daily_run_never_posts_spending(scheduler_environment):
    settings, persistence, user_id, transport, publisher = scheduler_environment
    moment = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    persistence.sync_spending(
        user_id=user_id,
        local_date=date(2026, 8, 29),
        time_zone="Asia/Tokyo",
        chunithm_amount_yen=100,
        maimai_amount_yen=0,
        ongeki_amount_yen=0,
        total_amount_yen=100,
        revision=1,
    )
    settings.rhythm_enabled = False
    scheduler = RhythmScheduler(
        settings=settings,
        persistence=persistence,
        executor=ExplicitSuccessExecutor(persistence, settings.rhythm_image_dir),
        publisher=publisher,
        user_id=user_id,
        now=lambda: moment,
    )

    assert await scheduler.run_daily_once(moment=moment) == []
    assert transport.sent == []


@pytest.mark.asyncio
async def test_posted_spending_is_edited_after_iphone_revision_but_not_reposted(
    scheduler_environment,
):
    settings, persistence, user_id, transport, _ = scheduler_environment
    publisher = RhythmDiscordPublisher(persistence, transport, dry_run=False)
    moment = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    target = date(2026, 8, 29)
    first_record = persistence.sync_spending(
        user_id=user_id,
        local_date=target,
        time_zone="Asia/Tokyo",
        chunithm_amount_yen=100,
        maimai_amount_yen=200,
        ongeki_amount_yen=300,
        total_amount_yen=600,
        revision=1,
        synced_at=moment,
    ).record
    first = await publisher.publish_spending(
        user_id=user_id,
        record=first_record,
        channel_id=settings.discord_credit_channel_id,
        post_zero_days=False,
        now=moment,
    )
    persistence.sync_spending(
        user_id=user_id,
        local_date=target,
        time_zone="Asia/Tokyo",
        chunithm_amount_yen=500,
        maimai_amount_yen=300,
        ongeki_amount_yen=400,
        total_amount_yen=1_200,
        revision=2,
        synced_at=moment,
    )
    scheduler = RhythmScheduler(
        settings=settings,
        persistence=persistence,
        executor=ExplicitSuccessExecutor(persistence, settings.rhythm_image_dir),
        publisher=publisher,
        user_id=user_id,
        now=lambda: moment,
    )

    updated = await scheduler.process_spending_revisions_once()
    duplicate_check = await scheduler.process_spending_revisions_once()

    assert first.posted is True
    assert updated == [target]
    assert duplicate_check == []
    assert len(transport.sent) == 2
    assert transport.sent[1]["message_id"] == first.message_id
    assert "合計 ¥1,200" in transport.sent[1]["content"]
