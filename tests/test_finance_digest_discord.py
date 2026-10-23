from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from life_inbox.constants import EventKind, EventStatus
from life_inbox.finance_digest import DeliveryAction, FinanceDigestService
from life_inbox.finance_digest_discord import FinanceDigestPublisher
from life_inbox.models import Event, Expense
from life_inbox.rhythm_discord import DiscordSendError


class FakeTransport:
    def __init__(
        self,
        send_results: list[object] | None = None,
        edit_results: list[object] | None = None,
    ) -> None:
        self.send_results = list(send_results or [])
        self.edit_results = list(edit_results or [])
        self.sent: list[dict[str, object]] = []
        self.edited: list[dict[str, object]] = []
        self.next_message_id = 1

    async def send(self, **kwargs: object) -> str:
        self.sent.append(kwargs)
        if self.send_results:
            result = self.send_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return str(result)
        message_id = f"message-{self.next_message_id}"
        self.next_message_id += 1
        return message_id

    async def edit(self, **kwargs: object) -> str:
        self.edited.append(kwargs)
        if self.edit_results:
            result = self.edit_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return str(result)
        return str(kwargs["message_id"])


def _add_expense(db, *, message_id: str, occurred_at: datetime, amount: int) -> None:
    with db.session() as session:
        session.add(
            Event(
                discord_message_id=message_id,
                discord_channel_id="source",
                discord_user_id="owner",
                captured_at=occurred_at,
                occurred_at=occurred_at,
                raw_text="private source text",
                source="discord",
                status=EventStatus.CONFIRMED.value,
                event_type=EventKind.EXPENSE.value,
                event_kinds=[EventKind.EXPENSE.value],
                expenses=[Expense(amount=amount, category="食費", subcategory="外食")],
            )
        )
        session.commit()


@pytest.mark.asyncio
async def test_dry_run_builds_content_without_transport_or_delivery_state(db):
    service = FinanceDigestService(db.session_factory)
    transport = FakeTransport()
    publisher = FinanceDigestPublisher(service, transport, dry_run=True)

    outcome = await publisher.publish(channel_id=123, local_date=date(2026, 8, 30))

    assert outcome.action == DeliveryAction.CREATE
    assert outcome.dry_run is True
    assert outcome.posted is False
    assert "家計の毎日自動サマリー" in outcome.content
    assert transport.sent == []
    assert service.get_delivery(date(2026, 8, 30), 123) is None


@pytest.mark.asyncio
async def test_first_delivery_posts_repeat_skips_and_correction_edits(db):
    service = FinanceDigestService(db.session_factory)
    transport = FakeTransport()
    publisher = FinanceDigestPublisher(service, transport, dry_run=False)
    target = date(2026, 8, 30)

    first = await publisher.publish(channel_id=123, local_date=target)
    repeated = await publisher.publish(channel_id=123, local_date=target)
    _add_expense(
        db,
        message_id="later-correction",
        occurred_at=datetime(2026, 8, 30, 3),
        amount=680,
    )
    corrected = await publisher.publish(channel_id=123, local_date=target)

    assert first.posted is True
    assert first.edited is False
    assert first.message_id == "message-1"
    assert repeated.duplicate is True
    assert repeated.posted is False
    assert corrected.posted is True
    assert corrected.edited is True
    assert corrected.message_id == "message-1"
    assert len(transport.sent) == 1
    assert len(transport.edited) == 1
    assert "¥680" in str(transport.edited[0]["content"])
    state = service.get_delivery(target, 123)
    assert state is not None
    assert state.status == "posted"
    assert state.discord_message_id == "message-1"


@pytest.mark.asyncio
async def test_ambiguous_create_is_blocked_and_not_blindly_retried(db):
    error = DiscordSendError(
        "discord_delivery_unknown",
        "Discord送信の応答を確認できません。",
        retryable=False,
        outcome_unknown=True,
    )
    service = FinanceDigestService(db.session_factory)
    transport = FakeTransport([error])
    publisher = FinanceDigestPublisher(service, transport, dry_run=False)
    target = date(2026, 8, 30)

    first = await publisher.publish(channel_id=123, local_date=target)
    repeated = await publisher.publish(channel_id=123, local_date=target)

    assert first.posted is False
    assert first.uncertain is True
    assert first.blocked is True
    assert repeated.action == DeliveryAction.BLOCKED
    assert repeated.blocked is True
    assert len(transport.sent) == 1
    state = service.get_delivery(target, 123)
    assert state is not None
    assert state.status == "unknown"
    assert state.posted_at is None


@pytest.mark.asyncio
async def test_ambiguous_replacement_create_clears_deleted_target_and_blocks_retry(db):
    not_found = DiscordSendError(
        "discord_not_found",
        "以前の投稿が見つかりません。",
        retryable=False,
    )
    ambiguous_create = DiscordSendError(
        "discord_delivery_unknown",
        "Discord送信の応答を確認できません。",
        retryable=False,
        outcome_unknown=True,
    )
    service = FinanceDigestService(db.session_factory)
    transport = FakeTransport(
        ["original-message", ambiguous_create],
        edit_results=[not_found],
    )
    publisher = FinanceDigestPublisher(service, transport, dry_run=False)
    target = date(2026, 8, 30)

    first = await publisher.publish(channel_id=123, local_date=target)
    assert first.posted is True
    _add_expense(
        db,
        message_id="replacement-correction",
        occurred_at=datetime(2026, 8, 30, 3),
        amount=680,
    )

    replacement = await publisher.publish(channel_id=123, local_date=target)
    repeated = await publisher.publish(channel_id=123, local_date=target)

    assert replacement.posted is False
    assert replacement.uncertain is True
    assert replacement.blocked is True
    assert replacement.message_id is None
    assert repeated.action == DeliveryAction.BLOCKED
    assert repeated.blocked is True
    assert len(transport.sent) == 2
    assert len(transport.edited) == 1
    state = service.get_delivery(target, 123)
    assert state is not None
    assert state.status == "unknown"
    assert state.discord_message_id is None
    assert state.posted_at is None


@pytest.mark.asyncio
async def test_crash_during_replacement_create_recovers_as_unknown_not_retryable_edit(db):
    class SimulatedProcessCrash(BaseException):
        pass

    not_found = DiscordSendError(
        "discord_not_found",
        "以前の投稿が見つかりません。",
        retryable=False,
    )
    crash_time = datetime(2026, 8, 30, 14, 0, tzinfo=UTC)
    service = FinanceDigestService(db.session_factory, stale_after_seconds=60)
    transport = FakeTransport(
        ["original-message", SimulatedProcessCrash()],
        edit_results=[not_found],
    )
    publisher = FinanceDigestPublisher(service, transport, dry_run=False)
    target = date(2026, 8, 30)

    first = await publisher.publish(
        channel_id=123,
        local_date=target,
        now=crash_time - timedelta(minutes=1),
    )
    assert first.posted is True
    _add_expense(
        db,
        message_id="crash-window-correction",
        occurred_at=datetime(2026, 8, 30, 3),
        amount=680,
    )

    with pytest.raises(SimulatedProcessCrash):
        await publisher.publish(channel_id=123, local_date=target, now=crash_time)

    interrupted = service.get_delivery(target, 123)
    assert interrupted is not None
    assert interrupted.status == "posting"
    assert interrupted.discord_message_id is None
    assert interrupted.posted_at is None

    recovered = service.recover_stale_postings(
        now=crash_time + timedelta(seconds=61),
        stale_after_seconds=60,
    )
    assert len(recovered) == 1
    assert recovered[0].status == "unknown"
    assert recovered[0].discord_message_id is None

    repeated = await publisher.publish(
        channel_id=123,
        local_date=target,
        now=crash_time + timedelta(seconds=62),
    )
    assert repeated.action == DeliveryAction.BLOCKED
    assert repeated.blocked is True
    assert len(transport.sent) == 2
    assert len(transport.edited) == 1


@pytest.mark.asyncio
async def test_explicit_retryable_rejection_can_retry_then_succeed(db):
    rate_limit = DiscordSendError(
        "discord_rate_limited",
        "Discordのレート制限です。",
        retryable=True,
        retry_after=0,
        outcome_unknown=False,
    )
    service = FinanceDigestService(db.session_factory)
    transport = FakeTransport([rate_limit, "message-after-retry"])
    publisher = FinanceDigestPublisher(
        service,
        transport,
        dry_run=False,
        base_backoff_seconds=0,
    )

    outcome = await publisher.publish(channel_id=123, local_date=date(2026, 8, 30))

    assert outcome.posted is True
    assert outcome.message_id == "message-after-retry"
    assert len(transport.sent) == 2


@pytest.mark.asyncio
async def test_generic_transport_failure_is_safe_and_retryable(db):
    service = FinanceDigestService(db.session_factory)
    transport = FakeTransport([RuntimeError("response contained a secret marker")])
    publisher = FinanceDigestPublisher(service, transport, dry_run=False)
    target = date(2026, 8, 30)

    outcome = await publisher.publish(channel_id=123, local_date=target)

    assert outcome.posted is False
    assert outcome.error_code == "discord_send_failed:RuntimeError"
    state = service.get_delivery(target, 123)
    assert state is not None
    assert state.status == "failed"
    assert "secret marker" not in (state.last_error or "")


@pytest.mark.asyncio
async def test_zero_day_can_be_configured_to_skip_without_creating_state(db):
    service = FinanceDigestService(db.session_factory)
    transport = FakeTransport()
    publisher = FinanceDigestPublisher(
        service,
        transport,
        dry_run=False,
        post_zero_days=False,
    )
    target = date(2026, 8, 30)

    outcome = await publisher.publish(channel_id=123, local_date=target)

    assert outcome.action == DeliveryAction.SKIP
    assert outcome.duplicate is True
    assert transport.sent == []
    assert service.get_delivery(target, 123) is None


@pytest.mark.asyncio
async def test_existing_digest_is_edited_when_revision_reduces_day_to_zero(db):
    target = date(2026, 8, 30)
    _add_expense(
        db,
        message_id="removed-expense",
        occurred_at=datetime(2026, 8, 30, 3),
        amount=680,
    )
    service = FinanceDigestService(db.session_factory)
    transport = FakeTransport()
    publisher = FinanceDigestPublisher(
        service,
        transport,
        dry_run=False,
        post_zero_days=False,
    )

    first = await publisher.publish(channel_id=123, local_date=target)
    with db.session() as session:
        event = session.scalar(
            select(Event).where(Event.discord_message_id == "removed-expense")
        )
        assert event is not None
        event.status = EventStatus.IGNORED.value
        session.commit()
    corrected = await publisher.publish(channel_id=123, local_date=target)

    assert first.posted is True
    assert corrected.posted is True
    assert corrected.edited is True
    assert corrected.message_id == first.message_id
    assert len(transport.sent) == 1
    assert len(transport.edited) == 1
    assert "今日の支出：¥0" in str(transport.edited[0]["content"])


@pytest.mark.asyncio
async def test_jst_date_is_used_when_only_aware_now_is_supplied(db):
    service = FinanceDigestService(db.session_factory)
    publisher = FinanceDigestPublisher(service, FakeTransport(), dry_run=True)

    outcome = await publisher.publish(
        channel_id=123,
        now=datetime(2026, 8, 30, 15, 1, tzinfo=UTC),
    )

    assert outcome.local_date == date(2026, 8, 31)
