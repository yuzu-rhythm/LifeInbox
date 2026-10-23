"""Japan-time daily scheduling and durable queued-run processing."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import Settings
from .rhythm_discord import RhythmDiscordPublisher
from .rhythm_persistence import CollectionRunRecord, RhythmPersistence


class CollectionExecutor(Protocol):
    async def execute(self, run: CollectionRunRecord) -> None: ...


def next_daily_instant(
    cron: str,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> datetime:
    """Return the next exact daily wall-clock event as aware UTC."""

    minute_text, hour_text, *_ = cron.split()
    timezone = ZoneInfo(timezone_name)
    current = (now or datetime.now(UTC)).astimezone(timezone)
    candidate = datetime.combine(
        current.date(),
        time(hour=int(hour_text), minute=int(minute_text)),
        tzinfo=timezone,
    )
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


class RhythmScheduler:
    """Own two loops: exact daily enqueue and resumable DB queue processing.

    Sleeps only decide when to inspect durable state.  They never transition a
    provider run to success; only the injected executor can do that after an
    explicit provider/domain event.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        persistence: RhythmPersistence,
        executor: CollectionExecutor,
        publisher: RhythmDiscordPublisher,
        user_id: int,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        queue_poll_seconds: float = 5.0,
        lease_seconds: int = 300,
    ) -> None:
        self.settings = settings
        self.persistence = persistence
        self.executor = executor
        self.publisher = publisher
        self.user_id = user_id
        self.now = now or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.queue_poll_seconds = queue_poll_seconds
        self.lease_seconds = lease_seconds
        self.owner_id = f"bot-{uuid4()}"
        self._tasks: list[asyncio.Task] = []
        self._run_locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._closed = False

    @property
    def running(self) -> bool:
        return any(not task.done() for task in self._tasks)

    def start(self) -> None:
        if (
            self.running
            or not self.settings.rhythm_enabled
            or not self.settings.rhythm_scheduler_enabled
        ):
            return
        self._closed = False
        self.persistence.recover_interrupted_runs(now=self.now())
        self._tasks = [
            asyncio.create_task(self._daily_loop(), name="rhythm-daily-scheduler"),
            asyncio.create_task(self._queue_loop(), name="rhythm-collection-queue"),
        ]

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

    async def run_daily_once(self, *, moment: datetime | None = None) -> list[str]:
        """Idempotently enqueue today's games and plan/post synced spending."""

        if not self.settings.rhythm_enabled or not self.settings.rhythm_scheduler_enabled:
            return []
        current = moment or self.now()
        local_date = self.local_date(current)
        runs = self.persistence.queue_collection_runs(
            user_id=self.user_id,
            games=("chunithm", "maimai", "ongeki"),
            trigger="daily",
            request_key=f"daily:{local_date.isoformat()}",
            now=current,
        )
        run_ids = [run.id for run in runs]
        spending = self.persistence.get_spending(
            user_id=self.user_id, local_date=local_date
        )
        if (
            spending is not None
            and self.settings.discord_credit_channel_id is not None
            and not self.settings.rhythm_spending_daily_summary_enabled
        ):
            await self.publisher.publish_spending(
                user_id=self.user_id,
                record=spending,
                channel_id=self.settings.discord_credit_channel_id,
                post_zero_days=self.settings.rhythm_post_zero_spending_days,
                now=current,
            )
        return run_ids

    async def process_queue_once(self) -> list[str]:
        if not self.settings.rhythm_enabled or not self.settings.rhythm_scheduler_enabled:
            return []
        processed: list[str] = []
        for run in self.persistence.processable_runs():
            if await self._process_run_once(run):
                processed.append(run.id)
        return processed

    async def run_manual_once(
        self,
        runs: list[CollectionRunRecord] | tuple[CollectionRunRecord, ...],
    ) -> list[str]:
        """Process only the supplied runs once without starting scheduler loops.

        The Discord manual command uses the persistence idempotency key to get
        the same run on an interaction retry, then calls this method directly.
        A terminal or already-claimed run is skipped, while a lease-loss keeps
        the run retryable instead of pretending that it completed.
        """

        if not self.settings.rhythm_enabled:
            return []
        unique_runs: list[CollectionRunRecord] = []
        seen_ids: set[str] = set()
        for run in runs:
            if run.user_id != self.user_id:
                raise ValueError("manual collection runs must belong to the scheduler user")
            if run.id in seen_ids:
                continue
            seen_ids.add(run.id)
            unique_runs.append(run)

        processed: list[str] = []
        for run in unique_runs:
            if await self._process_run_once(run):
                processed.append(run.id)
        return processed

    @staticmethod
    def _is_processable(run: CollectionRunRecord) -> bool:
        return run.state == "queued" or (
            run.state == "waitingForManualAction"
            and run.manual_confirmed_at is not None
            and run.manual_action is None
        )

    def _run_lock(self, run: CollectionRunRecord) -> asyncio.Lock:
        key = (run.user_id, run.game)
        lock = self._run_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._run_locks[key] = lock
        return lock

    async def _process_run_once(self, run: CollectionRunRecord) -> bool:
        """Run one durable collection under an in-process and DB lease guard."""

        if not self.settings.rhythm_enabled:
            return False
        async with self._run_lock(run):
            return await self._process_run_once_locked(run)

    async def _process_run_once_locked(self, run: CollectionRunRecord) -> bool:
        acquired = self.persistence.acquire_lease(
            user_id=run.user_id,
            game=run.game,
            owner_id=self.owner_id,
            now=self.now(),
            lease_seconds=self.lease_seconds,
        )
        if not acquired:
            return False
        heartbeat = asyncio.create_task(
            self._lease_heartbeat(run),
            name=f"rhythm-lease-{run.game}-{run.id}",
        )
        execution: asyncio.Task[None] | None = None
        try:
            current = self.persistence.get_run(run.id)
            if current is None or not self._is_processable(current):
                return False
            execution = asyncio.create_task(
                self.executor.execute(current),
                name=f"rhythm-executor-{run.game}-{run.id}",
            )
            done, _ = await asyncio.wait(
                {execution, heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                # Losing a lease is never collection success. Cancel the
                # coroutine so it cannot continue to persistence/publish
                # after another Bot has taken ownership.
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                return False
            await execution
            if not self.persistence.lease_owned(
                user_id=run.user_id,
                game=run.game,
                owner_id=self.owner_id,
                now=self.now(),
            ):
                return False
            return True
        except Exception as exc:
            # Do not persist exception text: provider/library exceptions can
            # contain URLs or personal data.  The stable code is enough for
            # retry and administrator reporting.
            try:
                if not self.persistence.lease_owned(
                    user_id=run.user_id,
                    game=run.game,
                    owner_id=self.owner_id,
                    now=self.now(),
                ):
                    return False
                self.persistence.transition_run(
                    run_id=run.id,
                    to_state="failed",
                    event_type="executorFailed",
                    message="取得処理で予期しないエラーが発生しました。再試行できます",
                    error_code="executor_failed",
                    error_message=f"安全化済み: {type(exc).__name__}",
                    now=self.now(),
                )
            except Exception:
                # A concurrent terminal transition wins; do not overwrite it.
                pass
            return False
        finally:
            if execution is not None and not execution.done():
                execution.cancel()
            heartbeat.cancel()
            await asyncio.gather(
                *(task for task in (execution, heartbeat) if task is not None),
                return_exceptions=True,
            )
            self.persistence.release_lease(
                user_id=run.user_id,
                game=run.game,
                owner_id=self.owner_id,
            )

    async def process_spending_revisions_once(self) -> list[date]:
        """Edit only pre-existing daily messages after iPhone revisions.

        A newly synced day is intentionally absent from this query, so this
        reconciliation cannot move the first daily post ahead of its scheduled
        wall-clock time.
        """

        channel_id = self.settings.discord_credit_channel_id
        if (
            not self.settings.rhythm_enabled
            or not self.settings.rhythm_scheduler_enabled
            or self.settings.rhythm_spending_daily_summary_enabled
            or channel_id is None
        ):
            return []
        updated: list[date] = []
        records = self.persistence.spending_records_with_posted_deliveries(
            user_id=self.user_id,
            channel_id=channel_id,
        )
        for record in records:
            outcome = await self.publisher.publish_spending(
                user_id=self.user_id,
                record=record,
                channel_id=channel_id,
                post_zero_days=self.settings.rhythm_post_zero_spending_days,
                now=self.now(),
            )
            if outcome.posted:
                updated.append(record.local_date)
        return updated

    async def _lease_heartbeat(self, run: CollectionRunRecord) -> bool:
        """Renew a long provider run without interpreting elapsed time as success."""

        interval = max(1.0, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            renewed = self.persistence.renew_lease(
                user_id=run.user_id,
                game=run.game,
                owner_id=self.owner_id,
                now=self.now(),
                lease_seconds=self.lease_seconds,
            )
            if not renewed:
                return False

    async def _daily_loop(self) -> None:
        while not self._closed:
            current = self.now()
            target = next_daily_instant(
                self.settings.rhythm_daily_post_cron,
                self.settings.rhythm_time_zone,
                now=current,
            )
            await self.sleep(max(0.0, (target - current).total_seconds()))
            if self._closed:
                return
            # Re-check the clock after wakeup; waking itself is not success.
            if self.now() >= target:
                await self.run_daily_once(moment=self.now())

    async def _queue_loop(self) -> None:
        while not self._closed:
            await self.process_queue_once()
            await self.process_spending_revisions_once()
            await self.sleep(self.queue_poll_seconds)
