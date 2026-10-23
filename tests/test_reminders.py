from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import discord
import pytest
from sqlalchemy import select

from life_inbox.analyzer import NullAnalyzer
from life_inbox.config import Settings
from life_inbox.db import Database
from life_inbox.discord_bot import LifeInboxBot, LifeInboxCommands
from life_inbox.models import LifeTask, ReminderDelivery
from life_inbox.reminders import (
    ReminderDeliveryStatus,
    ReminderScheduler,
    ReminderService,
    RoutineService,
    select_task_reminder_phase,
)
from life_inbox.services import InboxService
from life_inbox.tasks import TaskService

TOKYO = ZoneInfo("Asia/Tokyo")


def _utc_naive(local_value: datetime) -> datetime:
    return local_value.replace(tzinfo=TOKYO).astimezone(UTC).replace(tzinfo=None)


def _task(*, task_id: int = 1, title: str = "提出物", due_local: datetime, status: str = "open"):
    return SimpleNamespace(
        id=task_id,
        title=title,
        due_at=_utc_naive(due_local),
        status=status,
    )


@pytest.fixture
def reminder_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'reminders.db'}")
    database.init()
    return database


def test_timed_task_phase_selection_is_single_and_ordered():
    task = _task(due_local=datetime(2026, 9, 2, 15, 0))

    assert select_task_reminder_phase(
        task, datetime(2026, 9, 1, 14, 59, tzinfo=TOKYO), TOKYO
    ) is None
    assert select_task_reminder_phase(
        task, datetime(2026, 9, 1, 15, 0, tzinfo=TOKYO), TOKYO
    ).phase == "24h"
    assert select_task_reminder_phase(
        task, datetime(2026, 9, 2, 14, 0, tzinfo=TOKYO), TOKYO
    ).phase == "1h"
    assert select_task_reminder_phase(
        task, datetime(2026, 9, 2, 15, 0, tzinfo=TOKYO), TOKYO
    ).phase == "due"

    # A late start selects only the daily overdue occurrence, not all missed
    # historical phases.
    late = select_task_reminder_phase(
        task, datetime(2026, 9, 3, 10, 0, tzinfo=TOKYO), TOKYO
    )
    assert late is not None
    assert late.phase == "overdue"
    assert late.occurrence_key == "2026-09-03"


def test_date_only_task_is_due_for_the_due_date_and_overdue_from_next_day():
    task = _task(due_local=datetime(2026, 9, 2, 0, 0))

    before = select_task_reminder_phase(
        task, datetime(2026, 9, 2, 8, 59, tzinfo=TOKYO), TOKYO
    )
    at_nine = select_task_reminder_phase(
        task, datetime(2026, 9, 2, 9, 0, tzinfo=TOKYO), TOKYO
    )
    after_nine = select_task_reminder_phase(
        task, datetime(2026, 9, 2, 10, 0, tzinfo=TOKYO), TOKYO
    )
    next_morning = select_task_reminder_phase(
        task, datetime(2026, 9, 3, 8, 59, tzinfo=TOKYO), TOKYO
    )
    next_day = select_task_reminder_phase(
        task, datetime(2026, 9, 3, 9, 0, tzinfo=TOKYO), TOKYO
    )

    assert before is None
    assert at_nine is not None and at_nine.phase == "due"
    assert after_nine is not None and after_nine.phase == "due"
    assert after_nine.occurrence_key == "2026-09-02"
    assert next_morning is None
    assert next_day is not None and next_day.phase == "overdue"
    assert next_day.occurrence_key == "2026-09-03"


def test_completed_task_is_not_a_candidate():
    task = _task(
        due_local=datetime(2026, 9, 2, 15, 0),
        status="completed",
    )
    assert select_task_reminder_phase(
        task, datetime(2026, 9, 1, 15, 0, tzinfo=TOKYO), TOKYO
    ) is None


@pytest.mark.asyncio
async def test_polling_persists_success_and_daily_overdue_dedupe(reminder_database: Database):
    due = _utc_naive(datetime(2026, 9, 1, 10, 0))
    with reminder_database.session() as session:
        session.add(
            LifeTask(
                source="manual",
                title="期限超過タスク",
                due_at=due,
                status="open",
                created_at=due - timedelta(days=2),
                updated_at=due - timedelta(days=2),
            )
        )
        session.commit()

    sender = AsyncMock()
    service = ReminderService(reminder_database.session_factory, sender=sender, owner_id="one")
    first = await service.poll_once(now=datetime(2026, 9, 2, 9, 0, tzinfo=TOKYO))
    second = await service.poll_once(now=datetime(2026, 9, 2, 12, 0, tzinfo=TOKYO))
    third = await service.poll_once(now=datetime(2026, 9, 3, 9, 0, tzinfo=TOKYO))

    assert [notification.phase for notification in first] == ["overdue"]
    assert second == []
    assert [notification.occurrence_key for notification in third] == ["2026-09-03"]
    assert sender.await_count == 2
    assert {delivery.status for delivery in service.list_deliveries()} == {
        ReminderDeliveryStatus.SENT.value
    }


@pytest.mark.asyncio
async def test_completion_suppresses_future_task_notification(reminder_database: Database):
    task_service = TaskService(reminder_database.session_factory, timezone=TOKYO)
    task = task_service.create_manual(
        "期限のあるタスク",
        due_at=datetime(2026, 9, 2, 15, 0, tzinfo=TOKYO),
    )
    task_service.complete(task.id)
    sender = AsyncMock()
    service = ReminderService(reminder_database.session_factory, sender=sender)

    assert await service.poll_once(now=datetime(2026, 9, 1, 15, 0, tzinfo=TOKYO)) == []
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_routine_notifies_once_per_day_and_completion_suppresses(reminder_database):
    routine_service = RoutineService(
        reminder_database.session_factory,
        timezone=TOKYO,
        clock=lambda: datetime(2026, 9, 1, 9, 0, tzinfo=TOKYO),
    )
    routine = routine_service.create("朝の読書", "09:00")
    sender = AsyncMock()
    service = ReminderService(reminder_database.session_factory, sender=sender)

    first = await service.poll_once(now=datetime(2026, 9, 1, 9, 0, tzinfo=TOKYO))
    repeated = await service.poll_once(now=datetime(2026, 9, 1, 18, 0, tzinfo=TOKYO))
    routine_service.complete(routine.id, local_date=date(2026, 9, 2))
    completed_day = await service.poll_once(now=datetime(2026, 9, 2, 9, 1, tzinfo=TOKYO))

    assert [(item.subject_type, item.subject_id, item.phase) for item in first] == [
        ("routine", routine.id, "routine")
    ]
    assert repeated == []
    assert completed_day == []
    assert sender.await_count == 1


@pytest.mark.asyncio
async def test_failed_routine_is_not_replayed_on_a_later_date(reminder_database):
    routine_service = RoutineService(
        reminder_database.session_factory,
        timezone=TOKYO,
        clock=lambda: datetime(2026, 9, 1, 9, 0, tzinfo=TOKYO),
    )
    routine = routine_service.create("朝の読書", "09:00")
    sender = AsyncMock(side_effect=[RuntimeError("temporary"), None])
    service = ReminderService(reminder_database.session_factory, sender=sender)

    assert await service.poll_once(now=datetime(2026, 9, 1, 9, 0, tzinfo=TOKYO)) == []
    sent = await service.poll_once(now=datetime(2026, 9, 2, 9, 0, tzinfo=TOKYO))

    assert [(item.subject_id, item.occurrence_key) for item in sent] == [
        (routine.id, "2026-09-02")
    ]
    assert sender.await_count == 2
    deliveries = service.list_deliveries()
    assert [(item.occurrence_key, item.status) for item in deliveries] == [
        ("2026-09-01", "failed"),
        ("2026-09-02", "sent"),
    ]


@pytest.mark.asyncio
async def test_failed_send_is_retryable_but_success_is_deduped(reminder_database: Database):
    task = _task(due_local=datetime(2026, 9, 2, 15, 0))
    with reminder_database.session() as session:
        session.add(
            LifeTask(
                source="manual",
                title=task.title,
                due_at=task.due_at,
                status="open",
                created_at=task.due_at,
                updated_at=task.due_at,
            )
        )
        session.commit()

    sender = AsyncMock(side_effect=[RuntimeError("not persisted"), None])
    service = ReminderService(reminder_database.session_factory, sender=sender)
    moment = datetime(2026, 9, 1, 15, 0, tzinfo=TOKYO)

    assert await service.poll_once(now=moment) == []
    delivery = service.list_deliveries()[0]
    assert delivery.status == ReminderDeliveryStatus.FAILED.value
    assert delivery.attempt_count == 1
    assert delivery.last_error == "send_failed:RuntimeError"

    assert len(await service.poll_once(now=moment)) == 1
    assert await service.poll_once(now=moment) == []
    delivery = service.list_deliveries()[0]
    assert delivery.status == ReminderDeliveryStatus.SENT.value
    assert delivery.attempt_count == 2
    assert sender.await_count == 2


@pytest.mark.asyncio
async def test_failed_early_phase_is_not_replayed_after_phase_changes(
    reminder_database: Database,
):
    task = _task(due_local=datetime(2026, 9, 2, 15, 0))
    with reminder_database.session() as session:
        session.add(
            LifeTask(
                source="manual",
                title=task.title,
                due_at=task.due_at,
                status="open",
                created_at=task.due_at,
                updated_at=task.due_at,
            )
        )
        session.commit()

    sender = AsyncMock(side_effect=[RuntimeError("first"), None])
    service = ReminderService(reminder_database.session_factory, sender=sender)
    early = datetime(2026, 9, 1, 15, 0, tzinfo=TOKYO)
    one_hour = datetime(2026, 9, 2, 14, 0, tzinfo=TOKYO)

    assert await service.poll_once(now=early) == []
    assert [item.phase for item in await service.poll_once(now=one_hour)] == ["1h"]
    assert await service.poll_once(now=one_hour) == []
    assert sender.await_count == 2
    assert [item.phase for item in service.list_deliveries()] == ["24h", "1h"]
    assert [item.status for item in service.list_deliveries()] == ["failed", "sent"]


@pytest.mark.asyncio
async def test_stale_claim_is_recovered_after_restart(reminder_database: Database):
    task = _task(due_local=datetime(2026, 9, 2, 15, 0))
    moment = datetime(2026, 9, 1, 15, 0, tzinfo=TOKYO)
    with reminder_database.session() as session:
        session.add(
            LifeTask(
                source="manual",
                title=task.title,
                due_at=task.due_at,
                status="open",
                created_at=task.due_at,
                updated_at=task.due_at,
            )
        )
        session.commit()
    occurrence = "2026-09-02T06:00:00+00:00"
    with reminder_database.session() as session:
        session.add(
            ReminderDelivery(
                subject_type="task",
                subject_id=1,
                phase="24h",
                occurrence_key=occurrence,
                dedupe_key=f"task:1:24h:{occurrence}",
                status="claiming",
                attempt_count=1,
                claim_token="crashed-worker",
                claim_expires_at=datetime(2026, 9, 1, 5, 0),
                created_at=datetime(2026, 9, 1, 5, 0),
                updated_at=datetime(2026, 9, 1, 5, 0),
            )
        )
        session.commit()

    sender = AsyncMock()
    service = ReminderService(reminder_database.session_factory, sender=sender, owner_id="new")
    assert await service.poll_once(now=moment) != []
    delivery = service.list_deliveries()[0]
    assert delivery.status == ReminderDeliveryStatus.SENT.value
    assert delivery.attempt_count == 2


@pytest.mark.asyncio
async def test_concurrent_pollers_share_one_sqlite_claim(reminder_database: Database):
    due = _utc_naive(datetime(2026, 9, 2, 15, 0))
    with reminder_database.session() as session:
        session.add(
            LifeTask(
                source="manual",
                title="同時実行テスト",
                due_at=due,
                status="open",
                created_at=due,
                updated_at=due,
            )
        )
        session.commit()

    sends: list[str] = []

    async def sender(notification):
        sends.append(notification.occurrence_key)
        await asyncio.sleep(0.01)

    first = ReminderService(reminder_database.session_factory, sender=sender, owner_id="one")
    second = ReminderService(reminder_database.session_factory, sender=sender, owner_id="two")
    moment = datetime(2026, 9, 1, 15, 0, tzinfo=TOKYO)
    results = await asyncio.gather(
        first.poll_once(now=moment),
        second.poll_once(now=moment),
    )

    assert sum(len(result) for result in results) == 1
    assert len(sends) == 1
    with reminder_database.session() as session:
        assert session.execute(select(ReminderDelivery)).scalar_one().status == "sent"


def test_routine_time_validation_and_config_default():
    database = Database("sqlite:///:memory:")
    database.init()
    routines = RoutineService(database.session_factory)
    with pytest.raises(ValueError, match="HH:MM"):
        routines.create("不正な時刻", "9:00")
    with pytest.raises(ValueError, match="HH:MM"):
        routines.create("不正な時刻", "24:00")
    settings = Settings(_env_file=None)
    assert settings.reminder_scheduler_enabled is False
    assert settings.reminder_poll_seconds == 60


@pytest.mark.asyncio
async def test_scheduler_default_disabled_and_close_is_clean(reminder_database):
    service = ReminderService(reminder_database.session_factory, sender=AsyncMock())
    scheduler = ReminderScheduler(service, AsyncMock(), enabled=False)
    scheduler.start()
    assert scheduler.running is False
    await scheduler.close()


@pytest.mark.asyncio
async def test_discord_commands_and_lifecycle_wire_reminders_without_network(
    reminder_database, tmp_path
):
    settings = Settings(
        _env_file=None,
        discord_bot_token="test-token",
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        records_channel_id=40,
        reminder_channel_id=50,
        reminder_scheduler_enabled=False,
        database_url=f"sqlite:///{tmp_path / 'bot.db'}",
        data_dir=tmp_path,
    )
    service = InboxService(reminder_database.session_factory, settings, NullAnalyzer())
    bot = LifeInboxBot(settings, service)
    cog = LifeInboxCommands(bot)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=10),
        guild_id=20,
        channel_id=30,
        response=SimpleNamespace(
            is_done=lambda: False,
            send_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    try:
        await LifeInboxCommands.routine_add.callback(cog, interaction, "散歩", "07:30")
        assert "日課 #" in interaction.response.send_message.await_args.args[0]
        assert bot.reminder_scheduler.running is False
        with patch.object(bot.tree, "sync", new=AsyncMock(return_value=[])):
            await bot.setup_hook()
        assert {command.name for command in bot.tree.get_commands()} >= {
            "タスク一覧",
            "日課追加",
            "日課一覧",
            "日課完了",
        }
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_reminder_destination_rejects_non_text_or_wrong_guild_without_network(
    reminder_database, tmp_path
):
    settings = Settings(
        _env_file=None,
        discord_bot_token="test-token",
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        reminder_channel_id=50,
        database_url=f"sqlite:///{tmp_path / 'bot.db'}",
        data_dir=tmp_path,
    )
    service = InboxService(reminder_database.session_factory, settings, NullAnalyzer())
    bot = LifeInboxBot(settings, service)
    try:
        bot.get_channel = MagicMock(
            return_value=SimpleNamespace(
                type=discord.ChannelType.voice,
                guild=SimpleNamespace(id=20),
                send=AsyncMock(),
            )
        )
        with pytest.raises(RuntimeError, match="unavailable"):
            await bot._reminder_destination_channel()

        bot.get_channel.return_value = SimpleNamespace(
            type=discord.ChannelType.text,
            guild=SimpleNamespace(id=21),
            send=AsyncMock(),
        )
        with pytest.raises(RuntimeError, match="unavailable"):
            await bot._reminder_destination_channel()
    finally:
        await bot.close()
