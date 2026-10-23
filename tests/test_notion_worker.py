from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from life_inbox.notion_worker import NotionSyncWorker


class FakeSyncService:
    def __init__(self, *, transport=object(), fail_first: bool = False):
        self.transport = transport
        self.fail_first = fail_first
        self.calls = 0
        self.called = asyncio.Event()

    def process_pending(self, *, limit: int):
        self.calls += 1
        self.called.set()
        if self.fail_first and self.calls == 1:
            raise RuntimeError("synthetic private provider detail")
        return (SimpleNamespace(event_id=self.calls, limit=limit),)


@pytest.mark.asyncio
async def test_disabled_or_unconfigured_worker_never_starts_or_processes():
    disabled_service = FakeSyncService()
    disabled = NotionSyncWorker(disabled_service, enabled=False, poll_seconds=1)
    disabled.start()
    assert disabled.running is False
    assert await disabled.process_pending_once() == ()
    assert disabled_service.calls == 0

    missing_transport_service = FakeSyncService(transport=None)
    missing_transport = NotionSyncWorker(
        missing_transport_service,
        enabled=True,
        poll_seconds=1,
    )
    missing_transport.start()
    assert missing_transport.running is False
    assert await missing_transport.process_pending_once() == ()
    assert missing_transport_service.calls == 0


@pytest.mark.asyncio
async def test_enabled_worker_drains_immediately_and_wake_triggers_next_pass():
    service = FakeSyncService()
    worker = NotionSyncWorker(service, enabled=True, poll_seconds=60, batch_limit=7)

    worker.start()
    try:
        await asyncio.wait_for(service.called.wait(), timeout=1)
        assert service.calls == 1
        service.called.clear()
        worker.wake()
        await asyncio.wait_for(service.called.wait(), timeout=1)
        assert service.calls == 2
    finally:
        await worker.close()

    assert worker.running is False


@pytest.mark.asyncio
async def test_unexpected_pass_failure_does_not_kill_worker():
    service = FakeSyncService(fail_first=True)
    worker = NotionSyncWorker(service, enabled=True, poll_seconds=60)

    worker.start()
    try:
        await asyncio.wait_for(service.called.wait(), timeout=1)
        service.called.clear()
        worker.wake()
        await asyncio.wait_for(service.called.wait(), timeout=1)
        assert service.calls == 2
        assert worker.running is True
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_manual_process_is_serialized_and_uses_batch_limit():
    service = FakeSyncService()
    worker = NotionSyncWorker(service, enabled=True, poll_seconds=60, batch_limit=3)

    first, second = await asyncio.gather(
        worker.process_pending_once(),
        worker.process_pending_once(),
    )

    assert first[0].limit == 3
    assert second[0].limit == 3
    assert service.calls == 2
