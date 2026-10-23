"""Background delivery worker for explicitly promoted Notion archive rows."""

from __future__ import annotations

import asyncio
import logging

from .notion_sync import NotionSyncResult, NotionSyncService

logger = logging.getLogger(__name__)


class NotionSyncWorker:
    """Drain the durable Notion outbox without coupling capture to the network.

    The worker never discovers or enqueues Events. Only rows created by the
    explicit ``📚 Notionへ保存`` action are eligible for delivery.
    """

    def __init__(
        self,
        service: NotionSyncService,
        *,
        enabled: bool,
        poll_seconds: float = 60,
        batch_limit: int = 20,
        close_transport_on_shutdown: bool = False,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if batch_limit < 1 or batch_limit > 100:
            raise ValueError("batch_limit must be between 1 and 100")
        self.service = service
        self.enabled = enabled
        self.poll_seconds = float(poll_seconds)
        self.batch_limit = batch_limit
        self.close_transport_on_shutdown = close_transport_on_shutdown
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._process_lock = asyncio.Lock()
        self._closed = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running or not self.enabled or self.service.transport is None:
            return
        self._closed = False
        self._task = asyncio.create_task(self._run(), name="notion-sync-worker")

    def wake(self) -> None:
        """Wake the worker after an explicit promotion was durably enqueued."""

        if self.running:
            self._wake.set()

    async def process_pending_once(self) -> tuple[NotionSyncResult, ...]:
        if not self.enabled or self.service.transport is None:
            return ()
        async with self._process_lock:
            return await asyncio.to_thread(
                self.service.process_pending,
                limit=self.batch_limit,
            )

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.close_transport_on_shutdown:
            close = getattr(self.service.transport, "close", None)
            if callable(close):
                await asyncio.to_thread(close)

    async def _run(self) -> None:
        while not self._closed:
            try:
                await self.process_pending_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Provider errors should normally be converted to durable rows
                # by NotionSyncService. Keep an unexpected worker failure to a
                # stable class label so user content and credentials stay out
                # of logs.
                logger.warning("notion sync worker failed (%s)", type(exc).__name__)

            if self._closed:
                break
            if self._wake.is_set():
                self._wake.clear()
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
            finally:
                self._wake.clear()


__all__ = ["NotionSyncWorker"]
