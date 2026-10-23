"""Japan-time daily scheduling for the household-budget Discord digest."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .finance_digest_discord import FinanceDigestPublisher, FinanceDigestPublishOutcome

logger = logging.getLogger(__name__)


def _daily_time(cron: str) -> time:
    fields = cron.split()
    if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
        raise ValueError("daily cron must use 'minute hour * * *'")
    try:
        minute, hour = int(fields[0]), int(fields[1])
    except ValueError as exc:
        raise ValueError("daily cron minute and hour must be integers") from exc
    if not 0 <= minute <= 59 or not 0 <= hour <= 23:
        raise ValueError("daily cron time is out of range")
    return time(hour=hour, minute=minute)


def scheduled_finance_digest_instant(
    local_date: date,
    cron: str,
    timezone_name: str,
) -> datetime:
    """Return the exact scheduled wall-clock instant as aware UTC."""

    timezone = ZoneInfo(timezone_name)
    local = datetime.combine(local_date, _daily_time(cron), tzinfo=timezone)
    return local.astimezone(UTC)


def next_finance_digest_instant(
    cron: str,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> datetime:
    """Return the next exact daily instant strictly after ``now``."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    timezone = ZoneInfo(timezone_name)
    local_now = current.astimezone(timezone)
    candidate = scheduled_finance_digest_instant(local_now.date(), cron, timezone_name)
    if candidate <= current.astimezone(UTC):
        candidate = scheduled_finance_digest_instant(
            local_now.date() + timedelta(days=1), cron, timezone_name
        )
    return candidate


class FinanceDigestScheduler:
    """Schedule one daily create and reconcile edits to existing digests.

    Wall-clock time only decides when an attempt begins.  Delivery success is
    established solely by the publisher receiving a Discord message ID and
    finalizing the durable claim.
    """

    def __init__(
        self,
        publisher: FinanceDigestPublisher,
        *,
        enabled: bool,
        channel_id: int | None,
        cron: str = "45 23 * * *",
        timezone_name: str = "Asia/Tokyo",
        reconcile_seconds: int = 60,
        lookback_days: int = 7,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if channel_id is not None and (isinstance(channel_id, bool) or channel_id <= 0):
            raise ValueError("channel_id must be a positive integer")
        if not 5 <= reconcile_seconds <= 3_600:
            raise ValueError("reconcile_seconds must be between 5 and 3600")
        if not 1 <= lookback_days <= 31:
            raise ValueError("lookback_days must be between 1 and 31")
        _daily_time(cron)
        ZoneInfo(timezone_name)
        self.publisher = publisher
        self.enabled = enabled
        self.channel_id = channel_id
        self.cron = cron
        self.timezone_name = timezone_name
        self.reconcile_seconds = reconcile_seconds
        self.lookback_days = lookback_days
        self.now = now or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False

    @property
    def running(self) -> bool:
        return any(not task.done() for task in self._tasks)

    def local_date(self, moment: datetime | None = None) -> date:
        current = moment or self.now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return current.astimezone(ZoneInfo(self.timezone_name)).date()

    def start(self) -> None:
        if self.running or not self.enabled or self.channel_id is None:
            return
        self._closed = False
        self.publisher.service.recover_stale_postings(now=self.now())
        self._tasks = [
            asyncio.create_task(self._daily_loop(), name="finance-digest-daily"),
            asyncio.create_task(self._reconcile_loop(), name="finance-digest-reconcile"),
        ]

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run_daily_once(
        self,
        *,
        moment: datetime | None = None,
        target_date: date | None = None,
        force: bool = False,
    ) -> FinanceDigestPublishOutcome | None:
        if not self.enabled or self.channel_id is None:
            return None
        current = moment or self.now()
        selected_date = target_date or self.local_date(current)
        if not self.publisher.dry_run:
            self.publisher.service.capture_daily_snapshots(
                selected_date,
                now=current,
            )
        return await self.publisher.publish(
            channel_id=self.channel_id,
            local_date=selected_date,
            now=current,
            force=force,
        )

    async def run_reconcile_once(
        self,
        *,
        moment: datetime | None = None,
    ) -> list[FinanceDigestPublishOutcome]:
        """Edit/retry only dates that already have a durable delivery row."""

        if not self.enabled or self.channel_id is None:
            return []
        current = moment or self.now()
        current_date = self.local_date(current)
        outcomes: list[FinanceDigestPublishOutcome] = []
        for offset in range(self.lookback_days):
            target = current_date - timedelta(days=offset)
            state = self.publisher.service.get_delivery(target, self.channel_id)
            if state is None:
                continue
            if not self.publisher.dry_run:
                self.publisher.service.capture_daily_snapshots(
                    target,
                    now=current,
                )
            outcomes.append(
                await self.publisher.publish(
                    channel_id=self.channel_id,
                    local_date=target,
                    now=current,
                )
            )
        return outcomes

    async def _daily_loop(self) -> None:
        # If the Bot starts after today's scheduled time, an idempotent catch-up
        # attempt is made immediately.  Starting before the time never moves the
        # first daily post forward.
        current = self.now()
        today_target = scheduled_finance_digest_instant(
            self.local_date(current), self.cron, self.timezone_name
        )
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        if current.astimezone(UTC) >= today_target:
            try:
                await self.run_daily_once(
                    moment=current,
                    target_date=self.local_date(current),
                )
            except Exception as exc:
                logger.warning("finance digest catch-up failed (%s)", type(exc).__name__)

        while not self._closed:
            current = self.now()
            if current.tzinfo is None:
                current = current.replace(tzinfo=UTC)
            target = next_finance_digest_instant(
                self.cron,
                self.timezone_name,
                now=current,
            )
            target_date = target.astimezone(ZoneInfo(self.timezone_name)).date()
            await self.sleep(max(0.0, (target - current.astimezone(UTC)).total_seconds()))
            if self._closed:
                return
            wake_moment = self.now()
            if wake_moment.tzinfo is None:
                wake_moment = wake_moment.replace(tzinfo=UTC)
            if wake_moment.astimezone(UTC) >= target:
                try:
                    # Preserve the date associated with the scheduled target.
                    # A Mac waking after midnight must finish yesterday's
                    # digest, not post today's digest before today's schedule.
                    await self.run_daily_once(
                        moment=wake_moment,
                        target_date=target_date,
                    )
                except Exception as exc:
                    logger.warning("finance digest daily run failed (%s)", type(exc).__name__)

    async def _reconcile_loop(self) -> None:
        while not self._closed:
            try:
                await self.run_reconcile_once(moment=self.now())
            except Exception as exc:
                logger.warning("finance digest reconcile failed (%s)", type(exc).__name__)
            if self._closed:
                return
            await self.sleep(self.reconcile_seconds)


__all__ = [
    "FinanceDigestScheduler",
    "next_finance_digest_instant",
    "scheduled_finance_digest_instant",
]
