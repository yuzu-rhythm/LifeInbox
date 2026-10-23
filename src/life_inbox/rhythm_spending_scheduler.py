"""Bot-side immediate delivery and 23:55 rhythm-spending summaries."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from .config import Settings
from .rhythm_discord import DiscordPublishOutcome, RhythmDiscordPublisher
from .rhythm_persistence import RhythmPersistence
from .rhythm_scheduler import next_daily_instant
from .rhythm_spending import RhythmSpendingEventPersistence, SpendingDayTotals
from .rhythm_spending_discord import (
    RhythmSpendingDiscordPublisher,
    SpendingEventPublishOutcome,
    publish_daily_summary,
)

logger = logging.getLogger(__name__)


class RhythmSpendingScheduler:
    def __init__(
        self,
        *,
        settings: Settings,
        event_store: RhythmSpendingEventPersistence,
        delivery_store: RhythmPersistence,
        event_publisher: RhythmSpendingDiscordPublisher,
        daily_publisher: RhythmDiscordPublisher,
        rhythm_user_id: int,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.event_store = event_store
        self.delivery_store = delivery_store
        self.event_publisher = event_publisher
        self.daily_publisher = daily_publisher
        self.rhythm_user_id = rhythm_user_id
        self.now = now or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self._tasks: list[asyncio.Task] = []
        self._closed = False

    @property
    def running(self) -> bool:
        return any(not task.done() for task in self._tasks)

    def start(self) -> None:
        channel_id = self.settings.discord_credit_channel_id
        if (
            self.running
            or not self.settings.rhythm_enabled
            or channel_id is None
            or not (
                self.settings.rhythm_spending_immediate_post_enabled
                or self.settings.rhythm_spending_daily_summary_enabled
            )
        ):
            return
        self._closed = False
        self.event_store.recover_stale_deliveries(now=self.now())
        if self.settings.rhythm_spending_immediate_post_enabled:
            self._tasks.append(
                asyncio.create_task(
                    self._immediate_loop(), name="rhythm-spending-immediate"
                )
            )
        if self.settings.rhythm_spending_daily_summary_enabled:
            self._tasks.append(
                asyncio.create_task(
                    self._daily_loop(), name="rhythm-spending-daily-summary"
                )
            )

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def local_date(self, moment: datetime | None = None) -> date:
        return (moment or self.now()).astimezone(
            ZoneInfo(self.settings.rhythm_time_zone)
        ).date()

    def _totals(self, local_date: date) -> SpendingDayTotals:
        totals = self.event_store.daily_totals(
            rhythm_user_id=self.rhythm_user_id,
            local_date=local_date,
            time_zone=self.settings.rhythm_time_zone,
        )
        if totals.has_events:
            return totals
        # Backward-compatible fallback while an older iPhone build still sends
        # the daily aggregate endpoint rather than event-level records.
        legacy = self.delivery_store.get_spending(
            user_id=self.rhythm_user_id, local_date=local_date
        )
        if legacy is None or legacy.deleted:
            return totals
        return SpendingDayTotals(
            rhythm_user_id=self.rhythm_user_id,
            local_date=local_date,
            time_zone=legacy.time_zone,
            chunithm_amount_yen=legacy.chunithm_amount_yen,
            maimai_amount_yen=legacy.maimai_amount_yen,
            ongeki_amount_yen=legacy.ongeki_amount_yen,
            total_amount_yen=legacy.total_amount_yen,
            active_event_count=0,
            has_events=False,
            latest_revision=legacy.revision,
            updated_at=legacy.synced_at,
        )

    async def process_pending_once(
        self,
    ) -> list[SpendingEventPublishOutcome | DiscordPublishOutcome]:
        channel_id = self.settings.discord_credit_channel_id
        if (
            not self.settings.rhythm_enabled
            or not self.settings.rhythm_spending_immediate_post_enabled
            or channel_id is None
        ):
            return []
        pending = self.event_store.pending_events(
            rhythm_user_id=self.rhythm_user_id, now=self.now()
        )
        if not pending:
            return []
        mode = self.settings.rhythm_spending_post_mode
        if mode == "dailyOnly":
            if not self.event_publisher.dry_run:
                return []
            # A dry-run still exposes every queued event and its recalculated
            # total even though dailyOnly will not publish it immediately.
            return [
                preview
                for record in pending
                if (
                    preview := self.event_publisher.preview_event(
                        rhythm_user_id=self.rhythm_user_id,
                        event_id=record.event_id,
                        channel_id=channel_id,
                    )
                )
                is not None
            ]
        if mode == "updateDailyMessage":
            outcomes: list[DiscordPublishOutcome] = []
            for local_date in dict.fromkeys(record.local_date for record in pending):
                totals = self._totals(local_date)
                outcome = await self.event_publisher.publish_daily_live(
                    totals=totals,
                    channel_id=channel_id,
                    now=self.now(),
                )
                outcomes.append(outcome)
                await self._reconcile_posted_daily_summary(local_date)
            return outcomes

        outcomes: list[SpendingEventPublishOutcome] = []
        for record in pending:
            outcome = await self.event_publisher.publish_event(
                rhythm_user_id=self.rhythm_user_id,
                event_id=record.event_id,
                channel_id=channel_id,
                now=self.now(),
            )
            if outcome is not None:
                outcomes.append(outcome)
                if outcome.dry_run:
                    logger.info(
                        "spending dry-run event=%s revision=%s channel=%s total_yen=%s",
                        outcome.event_id,
                        outcome.revision,
                        channel_id,
                        outcome.totals.total_amount_yen,
                    )
                elif outcome.posted:
                    await self._reconcile_posted_daily_summary(
                        outcome.totals.local_date
                    )
        return outcomes

    async def run_daily_summary_once(
        self, *, local_date: date | None = None
    ) -> DiscordPublishOutcome | None:
        channel_id = self.settings.discord_credit_channel_id
        if (
            not self.settings.rhythm_enabled
            or not self.settings.rhythm_spending_daily_summary_enabled
            or channel_id is None
        ):
            return None
        target_date = local_date or self.local_date()
        totals = self._totals(target_date)
        if (
            totals.total_amount_yen == 0
            and not totals.has_events
            and not self.settings.rhythm_spending_post_zero_days
        ):
            return None
        outcome = await publish_daily_summary(
            publisher=self.daily_publisher,
            totals=totals,
            channel_id=channel_id,
            post_zero_days=self.settings.rhythm_spending_post_zero_days,
            now=self.now(),
        )
        if (
            self.settings.rhythm_spending_post_mode == "dailyOnly"
            and not outcome.dry_run
            and outcome.message_id is not None
            and (outcome.posted or outcome.duplicate)
        ):
            self.event_store.mark_day_delivery(
                rhythm_user_id=self.rhythm_user_id,
                local_date=target_date,
                channel_id=channel_id,
                message_id=outcome.message_id,
                now=self.now(),
            )
        return outcome

    async def _reconcile_posted_daily_summary(self, local_date: date) -> None:
        channel_id = self.settings.discord_credit_channel_id
        if (
            channel_id is None
            or not self.settings.rhythm_spending_daily_summary_enabled
            or not self.delivery_store.has_posted_spending_delivery(
                user_id=self.rhythm_user_id,
                local_date=local_date,
                channel_id=channel_id,
                delivery_kind="spending",
            )
        ):
            return
        await self.run_daily_summary_once(local_date=local_date)

    async def _immediate_loop(self) -> None:
        while not self._closed:
            try:
                await self.process_pending_once()
            except Exception as exc:
                logger.warning(
                    "rhythm spending reconciliation failed (%s)", type(exc).__name__
                )
            await self.sleep(self.settings.rhythm_spending_reconcile_seconds)

    def _scheduled_today(self, now: datetime) -> datetime:
        timezone = ZoneInfo(self.settings.rhythm_time_zone)
        local_now = now.astimezone(timezone)
        minute, hour, *_ = self.settings.rhythm_spending_daily_summary_cron.split()
        return datetime.combine(
            local_now.date(),
            time(hour=int(hour), minute=int(minute)),
            tzinfo=timezone,
        ).astimezone(UTC)

    async def _daily_loop(self) -> None:
        while not self._closed:
            current = self.now()
            scheduled_today = self._scheduled_today(current)
            if current >= scheduled_today:
                try:
                    await self.run_daily_summary_once(
                        local_date=self.local_date(current)
                    )
                except Exception as exc:
                    logger.warning(
                        "rhythm spending daily summary failed (%s)",
                        type(exc).__name__,
                    )
            target = next_daily_instant(
                self.settings.rhythm_spending_daily_summary_cron,
                self.settings.rhythm_time_zone,
                now=current,
            )
            await self.sleep(max(0.0, (target - current).total_seconds()))
            if self._closed:
                return
