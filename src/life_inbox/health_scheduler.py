"""Reconcile HealthKit summaries into Discord without treating time as success."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import Settings
from .health_discord import HealthDiscordPublisher
from .health_persistence import HealthPersistence

logger = logging.getLogger(__name__)


def _daily_time(cron: str) -> time:
    fields = cron.split()
    if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
        raise ValueError("daily cron must use 'minute hour * * *'")
    minute, hour = int(fields[0]), int(fields[1])
    if not 0 <= minute <= 59 or not 0 <= hour <= 23:
        raise ValueError("daily cron time is out of range")
    return time(hour=hour, minute=minute)


def sleep_post_instant(local_date: date, cron: str, timezone_name: str) -> datetime:
    local = datetime.combine(local_date, _daily_time(cron), tzinfo=ZoneInfo(timezone_name))
    return local.astimezone(UTC)


class HealthScheduler:
    def __init__(
        self,
        *,
        settings: Settings,
        persistence: HealthPersistence,
        publisher: HealthDiscordPublisher,
        user_id: int,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.persistence = persistence
        self.publisher = publisher
        self.user_id = user_id
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> bool:
        if (
            self._closed
            or self._task is not None
            or not self.settings.health_enabled
            or not self.settings.health_scheduler_enabled
        ):
            return False
        self._task = asyncio.create_task(self._loop(), name="health-discord-scheduler")
        return True

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def reconcile_once(self, *, now: datetime | None = None) -> list[str]:
        current = now or self.clock()
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        if not self.settings.health_enabled:
            return []
        channel_id = self.settings.discord_health_channel_id
        if channel_id is None:
            return ["health_channel_not_configured"]
        timezone = ZoneInfo(self.settings.health_time_zone)
        local_today = current.astimezone(timezone).date()
        date_from = local_today - timedelta(days=self.settings.health_lookback_days - 1)
        sleeps, workouts = await asyncio.to_thread(
            self.persistence.records_since,
            user_id=self.user_id,
            date_from=date_from,
        )
        outcomes: list[str] = []
        for record in sleeps:
            scheduled = sleep_post_instant(
                record.local_date,
                self.settings.health_sleep_post_cron,
                record.time_zone,
            )
            if current.astimezone(UTC) < scheduled:
                continue
            outcome = await self.publisher.publish_sleep(
                user_id=self.user_id,
                record=record,
                channel_id=channel_id,
                now=current,
            )
            outcomes.append(
                "sleep:dry-run"
                if outcome.dry_run
                else "sleep:posted"
                if outcome.posted
                else "sleep:skipped"
            )
        for record in workouts:
            outcome = await self.publisher.publish_workout(
                user_id=self.user_id,
                record=record,
                channel_id=channel_id,
                now=current,
            )
            outcomes.append(
                "workout:dry-run"
                if outcome.dry_run
                else "workout:posted"
                if outcome.posted
                else "workout:skipped"
            )
        return outcomes

    async def _loop(self) -> None:
        while not self._closed:
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never include health payloads or credentials in logs.
                logger.error("health reconciliation failed (%s)", type(exc).__name__)
            await self.sleep(self.settings.health_reconcile_seconds)
