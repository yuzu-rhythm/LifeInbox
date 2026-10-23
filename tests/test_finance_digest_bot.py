from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from life_inbox.config import Settings
from life_inbox.constants import EventKind, EventStatus
from life_inbox.discord_bot import LifeInboxBot, LifeInboxCommands
from life_inbox.finance_digest import FinanceDigestService
from life_inbox.finance_digest_discord import FinanceDigestPublisher
from life_inbox.models import Event, Expense


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.edited: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> str:
        self.sent.append(kwargs)
        return "fake-message"

    async def edit(self, **kwargs: object) -> str:
        self.edited.append(kwargs)
        return str(kwargs["message_id"])


class FakeFinanceScheduler:
    def __init__(self) -> None:
        self.start_calls = 0
        self.close_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    async def close(self) -> None:
        self.close_calls += 1


def _settings(tmp_path, **overrides):
    values = {
        "_env_file": None,
        "discord_bot_token": "test-token",
        "discord_guild_id": 20,
        "allowed_user_id": 10,
        "inbox_channel_id": 30,
        "records_channel_id": 40,
        "database_url": f"sqlite:///{tmp_path / 'app.db'}",
        "data_dir": tmp_path,
        "finance_digest_enabled": True,
        "finance_digest_channel_id": 40,
        "finance_digest_dry_run": True,
    }
    values.update(overrides)
    return Settings(**values)


def _interaction(*, channel_id: int, user_id: int = 10, guild_id: int = 20):
    response = SimpleNamespace(
        is_done=MagicMock(return_value=False),
        send_message=AsyncMock(),
    )
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=guild_id,
        channel_id=channel_id,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _add_expense(db, *, occurred_at: datetime, amount: int, raw_text: str) -> None:
    with db.session() as session:
        session.add(
            Event(
                discord_message_id=f"message-{amount}",
                discord_channel_id="source",
                discord_user_id="owner",
                captured_at=occurred_at,
                occurred_at=occurred_at,
                raw_text=raw_text,
                source="discord",
                status=EventStatus.CONFIRMED.value,
                event_type=EventKind.EXPENSE.value,
                event_kinds=[EventKind.EXPENSE.value],
                expenses=[Expense(amount=amount, category="食費", subcategory="外食")],
            )
        )
        session.commit()


@pytest.mark.asyncio
async def test_finance_digest_dependencies_are_injected_and_lifecycle_is_managed(
    service, db, tmp_path
):
    settings = _settings(tmp_path)
    digest = FinanceDigestService(
        db.session_factory,
        timezone=settings.finance_digest_time_zone,
    )
    transport = FakeTransport()
    publisher = FinanceDigestPublisher(digest, transport, dry_run=True)
    scheduler = FakeFinanceScheduler()
    bot = LifeInboxBot(
        settings,
        service,
        finance_digest_service=digest,
        finance_digest_publisher=publisher,
        finance_digest_scheduler=scheduler,
    )

    try:
        assert bot.finance_digest is digest
        assert bot.finance_digest_service is digest
        assert bot.finance_digest_publisher is publisher
        assert bot.finance_digest_scheduler is scheduler
        with patch.object(bot.tree, "sync", new=AsyncMock(return_value=[])):
            await bot.setup_hook()
        assert scheduler.start_calls == 1
    finally:
        await bot.close()
    assert scheduler.close_calls == 1
    assert transport.sent == []


def test_finance_digest_injected_publisher_cannot_bypass_dry_run(service, tmp_path):
    settings = _settings(tmp_path, finance_digest_dry_run=True)
    digest = FinanceDigestService(service.session_factory)
    publisher = FinanceDigestPublisher(digest, FakeTransport(), dry_run=False)

    with pytest.raises(ValueError, match="dry-run"):
        LifeInboxBot(
            settings,
            service,
            finance_digest_service=digest,
            finance_digest_publisher=publisher,
        )


@pytest.mark.asyncio
async def test_finance_digest_today_is_ephemeral_and_does_not_deliver(
    service, db, tmp_path
):
    moment = datetime(2026, 8, 30, 15, 0, tzinfo=UTC)
    settings = _settings(tmp_path)
    digest = FinanceDigestService(
        db.session_factory,
        timezone=settings.finance_digest_time_zone,
        clock=lambda: moment,
    )
    transport = FakeTransport()
    publisher = FinanceDigestPublisher(digest, transport, dry_run=True)
    bot = LifeInboxBot(
        settings,
        service,
        finance_digest_service=digest,
        finance_digest_publisher=publisher,
    )
    _add_expense(
        db,
        occurred_at=datetime(2026, 8, 30, 3),
        amount=1_234,
        raw_text="private raw event text",
    )
    interaction = _interaction(channel_id=40)

    try:
        cog = LifeInboxCommands(bot)
        await LifeInboxCommands.finance_digest_today.callback(cog, interaction)

        response = interaction.response.send_message
        response.assert_awaited_once()
        content = response.await_args.args[0]
        assert "¥1,234" in content
        assert "private raw event text" not in content
        assert response.await_args.kwargs["ephemeral"] is True
        assert transport.sent == []
        assert digest.get_delivery(moment, 40) is None
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_finance_digest_delivery_check_is_read_only_even_for_real_publisher(
    service, db, tmp_path
):
    moment = datetime(2026, 8, 30, 15, 0, tzinfo=UTC)
    settings = _settings(tmp_path, finance_digest_dry_run=False)
    digest = FinanceDigestService(
        db.session_factory,
        timezone=settings.finance_digest_time_zone,
        clock=lambda: moment,
    )
    transport = FakeTransport()
    publisher = FinanceDigestPublisher(digest, transport, dry_run=False)
    bot = LifeInboxBot(
        settings,
        service,
        finance_digest_service=digest,
        finance_digest_publisher=publisher,
    )
    _add_expense(
        db,
        occurred_at=datetime(2026, 8, 30, 3),
        amount=1_234,
        raw_text="private raw event text",
    )
    interaction = _interaction(channel_id=40)

    try:
        cog = LifeInboxCommands(bot)
        await LifeInboxCommands.finance_digest_delivery_check.callback(cog, interaction)

        response = interaction.response.send_message
        response.assert_awaited_once()
        content = response.await_args.args[0]
        assert "予定 action: create" in content
        assert "配信先チャンネルID: 40" in content
        assert "fingerprint: " in content
        assert "家計の毎日自動サマリー" in content
        assert "実送信・delivery state変更はありません。" in content
        assert response.await_args.kwargs["ephemeral"] is True
        assert transport.sent == []
        assert transport.edited == []
        assert digest.get_delivery(moment, 40) is None
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_finance_digest_commands_keep_scope_guard(service, tmp_path):
    settings = _settings(tmp_path)
    bot = LifeInboxBot(settings, service)
    interaction = _interaction(channel_id=999)

    try:
        cog = LifeInboxCommands(bot)
        await LifeInboxCommands.finance_digest_today.callback(cog, interaction)
        interaction.response.send_message.assert_awaited_once_with(
            "この閲覧コマンドは #生活-記録 など設定済みの生活チャンネルで使えます。",
            ephemeral=True,
        )
    finally:
        await bot.close()
