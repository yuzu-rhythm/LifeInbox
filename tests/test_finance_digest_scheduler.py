from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import pytest

from life_inbox.constants import EventKind, EventStatus
from life_inbox.finance_digest import FinanceDigestService
from life_inbox.finance_digest_discord import FinanceDigestPublisher
from life_inbox.finance_digest_scheduler import (
    FinanceDigestScheduler,
    next_finance_digest_instant,
    scheduled_finance_digest_instant,
)
from life_inbox.models import Event, Expense


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.edited: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> str:
        self.sent.append(kwargs)
        return "daily-message"

    async def edit(self, **kwargs: object) -> str:
        self.edited.append(kwargs)
        return str(kwargs["message_id"])


def _scheduler(db, *, moment: datetime, dry_run: bool = False):
    service = FinanceDigestService(db.session_factory)
    transport = FakeTransport()
    publisher = FinanceDigestPublisher(service, transport, dry_run=dry_run)
    blocker = asyncio.Event()

    async def sleep(_seconds: float) -> None:
        await blocker.wait()

    scheduler = FinanceDigestScheduler(
        publisher,
        enabled=True,
        channel_id=123,
        cron="45 23 * * *",
        timezone_name="Asia/Tokyo",
        now=lambda: moment,
        sleep=sleep,
    )
    return scheduler, service, transport


def test_daily_instants_follow_japan_wall_clock():
    target = scheduled_finance_digest_instant(
        date(2026, 8, 30), "45 23 * * *", "Asia/Tokyo"
    )
    assert target == datetime(2026, 8, 30, 14, 45, tzinfo=UTC)
    assert next_finance_digest_instant(
        "45 23 * * *",
        "Asia/Tokyo",
        now=datetime(2026, 8, 30, 14, 44, tzinfo=UTC),
    ) == datetime(2026, 8, 30, 14, 45, tzinfo=UTC)
    assert next_finance_digest_instant(
        "45 23 * * *",
        "Asia/Tokyo",
        now=datetime(2026, 8, 30, 14, 46, tzinfo=UTC),
    ) == datetime(2026, 8, 31, 14, 45, tzinfo=UTC)


@pytest.mark.asyncio
async def test_manual_daily_run_uses_jst_date_and_is_idempotent(db):
    moment = datetime(2026, 8, 30, 15, 1, tzinfo=UTC)
    scheduler, service, transport = _scheduler(db, moment=moment)

    first = await scheduler.run_daily_once(moment=moment)
    repeated = await scheduler.run_daily_once(moment=moment)

    assert first is not None and first.local_date == date(2026, 8, 31)
    assert first.posted is True
    assert repeated is not None and repeated.duplicate is True
    assert len(transport.sent) == 1
    assert service.get_delivery(date(2026, 8, 31), 123) is not None


@pytest.mark.asyncio
async def test_reconcile_edits_only_an_existing_delivery(db):
    moment = datetime(2026, 8, 30, 14, 50, tzinfo=UTC)
    scheduler, _, transport = _scheduler(db, moment=moment)
    first = await scheduler.run_daily_once(moment=moment)
    assert first is not None and first.posted
    with db.session() as session:
        event = Event(
            discord_message_id="corrected",
            discord_channel_id="source",
            discord_user_id="owner",
            captured_at=datetime(2026, 8, 30, 3),
            occurred_at=datetime(2026, 8, 30, 3),
            raw_text="private",
            status=EventStatus.CONFIRMED.value,
            event_type=EventKind.EXPENSE.value,
            event_kinds=[EventKind.EXPENSE.value],
            expenses=[Expense(amount=1_200, category="日用品")],
        )
        session.add(event)
        session.commit()

    outcomes = await scheduler.run_reconcile_once(moment=moment)

    assert any(outcome.posted and outcome.edited for outcome in outcomes)
    assert len(transport.sent) == 1
    assert len(transport.edited) == 1


@pytest.mark.asyncio
async def test_reconcile_does_not_create_a_new_day_before_schedule(db):
    moment = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)  # 19:00 JST
    scheduler, service, transport = _scheduler(db, moment=moment)

    outcomes = await scheduler.run_reconcile_once(moment=moment)

    assert outcomes == []
    assert transport.sent == []
    assert service.get_delivery(date(2026, 8, 30), 123) is None


@pytest.mark.asyncio
async def test_start_after_schedule_catches_up_but_before_schedule_waits(db):
    after = datetime(2026, 8, 30, 14, 50, tzinfo=UTC)
    after_scheduler, _, after_transport = _scheduler(db, moment=after)
    after_scheduler.start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(after_transport.sent) == 1
    await after_scheduler.close()

    # A distinct channel avoids seeing the previous durable delivery row.
    before = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
    service = FinanceDigestService(db.session_factory)
    before_transport = FakeTransport()
    publisher = FinanceDigestPublisher(service, before_transport, dry_run=False)
    blocker = asyncio.Event()

    async def sleep(_seconds: float) -> None:
        await blocker.wait()

    before_scheduler = FinanceDigestScheduler(
        publisher,
        enabled=True,
        channel_id=456,
        cron="45 23 * * *",
        timezone_name="Asia/Tokyo",
        now=lambda: before,
        sleep=sleep,
    )
    before_scheduler.start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert before_transport.sent == []
    await before_scheduler.close()


@pytest.mark.asyncio
async def test_wake_after_midnight_posts_the_scheduled_date_not_the_wake_date(db):
    before_target = datetime(2026, 8, 30, 14, 44, tzinfo=UTC)  # 23:44 JST
    after_midnight = datetime(2026, 8, 31, 0, 10, tzinfo=UTC)  # 09:10 JST
    clock = {"now": before_target}
    sleep_count = 0
    blocker = asyncio.Event()

    async def sleep(_seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            clock["now"] = after_midnight
            await asyncio.sleep(0)
            return
        await blocker.wait()

    service = FinanceDigestService(db.session_factory)
    transport = FakeTransport()
    publisher = FinanceDigestPublisher(service, transport, dry_run=False)
    scheduler = FinanceDigestScheduler(
        publisher,
        enabled=True,
        channel_id=789,
        cron="45 23 * * *",
        timezone_name="Asia/Tokyo",
        now=lambda: clock["now"],
        sleep=sleep,
    )

    scheduler.start()
    for _ in range(20):
        if transport.sent:
            break
        await asyncio.sleep(0)

    assert len(transport.sent) == 1
    assert "2026-08-30" in str(transport.sent[0]["content"])
    assert service.get_delivery(date(2026, 8, 30), 789) is not None
    assert service.get_delivery(date(2026, 8, 31), 789) is None
    await scheduler.close()


@pytest.mark.asyncio
async def test_disabled_scheduler_never_starts_or_delivers(db):
    service = FinanceDigestService(db.session_factory)
    transport = FakeTransport()
    publisher = FinanceDigestPublisher(service, transport, dry_run=False)
    scheduler = FinanceDigestScheduler(
        publisher,
        enabled=False,
        channel_id=123,
    )

    scheduler.start()

    assert scheduler.running is False
    assert await scheduler.run_daily_once() is None
    assert await scheduler.run_reconcile_once() == []
    assert transport.sent == []
