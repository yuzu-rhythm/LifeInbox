from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import select

from life_inbox.analyzer import NullAnalyzer
from life_inbox.config import Settings
from life_inbox.constants import EventStatus
from life_inbox.discord_bot import (
    EventActionView,
    LifeInboxBot,
    MemoWebView,
    NotionPromotionView,
    create_bot,
)
from life_inbox.inbox_classifier import InboxCategory
from life_inbox.models import Event
from life_inbox.notion_http import NotionHttpTransport
from life_inbox.notion_models import NotionSyncRecord, NotionSyncStatus
from life_inbox.services import InboxService


def _settings(tmp_path, **overrides):
    values = {
        "_env_file": None,
        "discord_guild_id": 20,
        "allowed_user_id": 10,
        "inbox_channel_id": 30,
        "records_channel_id": 40,
        "database_url": f"sqlite:///{tmp_path / 'app.db'}",
        "data_dir": tmp_path,
    }
    values.update(overrides)
    return Settings(**values)


def _event_id(db, *, event_type="memo", event_kinds=None, status="confirmed", raw_text="メモ"):
    occurred_at = datetime(2026, 8, 30, 12, 0)
    with db.session() as session:
        event = Event(
            discord_message_id=f"notion-discord-{event_type}-{raw_text}",
            discord_channel_id="30",
            discord_user_id="10",
            captured_at=occurred_at,
            occurred_at=occurred_at,
            raw_text=raw_text,
            source="discord",
            status=status,
            event_type=event_type,
            event_kinds=event_kinds if event_kinds is not None else [event_type],
        )
        session.add(event)
        session.commit()
        return event.id


def _interaction(*, user_id=10, guild_id=20, channel_id=40):
    response = SimpleNamespace(
        is_done=MagicMock(return_value=False),
        send_message=AsyncMock(),
    )
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, bot=False),
        guild_id=guild_id,
        channel_id=channel_id,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _promotion_button(view):
    return next(
        child
        for child in view.children
        if isinstance(child, discord.ui.Button)
        and child.label == "📚 Notionへバックアップ"
    )


@pytest.mark.asyncio
async def test_confirmed_memo_web_view_exposes_the_same_explicit_promotion_button(db, tmp_path):
    settings = _settings(tmp_path)
    service = InboxService(db.session_factory, settings, NullAnalyzer())
    event_id = _event_id(db)
    bot = LifeInboxBot(settings, service)

    try:
        view = MemoWebView(settings, event_id, bot=bot)
        assert _promotion_button(view).custom_id == f"life-inbox:notion-promote:{event_id}"
    finally:
        await bot.close()


class FakeNotionSync:
    def __init__(self):
        self.event_ids = []
        self.transport = None

    def enqueue_event(self, event_id):
        self.event_ids.append(event_id)
        return SimpleNamespace(
            event_id=event_id,
            eligible=True,
            reason_code="eligible",
            created=True,
            changed=False,
        )


class FakeNotionWorker:
    def __init__(self):
        self.wake_calls = 0
        self.start_calls = 0
        self.close_calls = 0

    def start(self):
        self.start_calls += 1

    def wake(self):
        self.wake_calls += 1

    async def close(self):
        self.close_calls += 1


@pytest.mark.asyncio
async def test_notion_promotion_wakes_separate_background_worker(db, tmp_path):
    settings = _settings(
        tmp_path,
        notion_sync_enabled=True,
        notion_api_token="synthetic-notion-token",
        notion_parent_page_id="synthetic-parent-id",
    )
    service = InboxService(db.session_factory, settings, NullAnalyzer())
    notion_sync = FakeNotionSync()
    notion_sync.transport = object()
    worker = FakeNotionWorker()
    event_id = _event_id(db)
    bot = LifeInboxBot(
        settings,
        service,
        notion_sync=notion_sync,
        notion_sync_worker=worker,
    )
    interaction = _interaction()

    try:
        await _promotion_button(NotionPromotionView(bot, event_id)).callback(interaction)
    finally:
        await bot.close()

    assert notion_sync.event_ids == [event_id]
    assert worker.wake_calls == 1
    assert worker.close_calls == 1
    response = interaction.response.send_message.await_args.args[0]
    assert "バックグラウンド処理へ渡します" in response


@pytest.mark.asyncio
async def test_create_bot_wires_http_transport_without_performing_live_request(db, tmp_path):
    settings = _settings(
        tmp_path,
        discord_bot_token="synthetic-discord-token",
        notion_sync_enabled=True,
        notion_api_token="synthetic-notion-token",
        notion_parent_page_id="synthetic-parent-id",
        notion_sync_poll_seconds=17,
    )
    service = InboxService(db.session_factory, settings, NullAnalyzer())

    bot = create_bot(settings, service=service)
    try:
        assert isinstance(bot.notion_sync.transport, NotionHttpTransport)
        assert bot.notion_sync_worker is not None
        assert bot.notion_sync_worker.running is False
        assert bot.notion_sync_worker.poll_seconds == 17
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_destination_copy_button_enqueues_allowed_confirmed_event(db, tmp_path):
    settings = _settings(tmp_path, memo_channel_id=41)
    service = InboxService(db.session_factory, settings, NullAnalyzer())
    event_id = _event_id(db)
    bot = LifeInboxBot(settings, service)
    destination = SimpleNamespace(
        id=41,
        name="生活-記録",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(),
    )
    bot.get_channel = MagicMock(return_value=destination)
    source = SimpleNamespace(
        id=900,
        guild=SimpleNamespace(id=20),
        channel=SimpleNamespace(id=30),
        delete=AsyncMock(),
    )

    try:
        await bot._route_classified_inbox(source, event_id, InboxCategory.MEMO)
        view = destination.send.await_args.kwargs["view"]
        interaction = _interaction()
        await _promotion_button(view).callback(interaction)
    finally:
        await bot.close()

    response = interaction.response.send_message.await_args.args[0]
    assert "ローカルのNotion保存予約キューに登録しました" in response
    assert "外部Notionへのライブ配信アダプタは未設定" in response
    assert "配信に成功" not in response
    with db.session() as session:
        record = session.scalar(
            select(NotionSyncRecord).where(NotionSyncRecord.event_id == event_id)
        )
        assert record is not None
        assert record.status == NotionSyncStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id,guild_id", [(11, 20), (10, 21)])
async def test_notion_promotion_rejects_wrong_user_or_guild(db, tmp_path, user_id, guild_id):
    settings = _settings(tmp_path)
    service = InboxService(db.session_factory, settings, NullAnalyzer())
    notion_sync = FakeNotionSync()
    bot = LifeInboxBot(settings, service, notion_sync=notion_sync)
    view = NotionPromotionView(bot, 7)
    interaction = _interaction(user_id=user_id, guild_id=guild_id)

    try:
        await _promotion_button(view).callback(interaction)
    finally:
        await bot.close()

    interaction.response.send_message.assert_awaited_once_with(
        "この操作は許可されていません。", ephemeral=True
    )
    assert notion_sync.event_ids == []


@pytest.mark.asyncio
async def test_notion_promotion_queues_safe_expense_summary_without_event_mutation(
    db, tmp_path
):
    settings = _settings(tmp_path)
    service = InboxService(db.session_factory, settings, NullAnalyzer())
    event_id = _event_id(
        db,
        event_type="expense",
        event_kinds=["expense"],
        raw_text="カード利用",
    )
    bot = LifeInboxBot(settings, service)
    interaction = _interaction()

    try:
        await _promotion_button(NotionPromotionView(bot, event_id)).callback(interaction)
    finally:
        await bot.close()

    response = interaction.response.send_message.await_args.args[0]
    assert "Notion保存予約キューに登録" in response
    assert "外部Notionへのライブ配信アダプタは未設定" in response
    event = service.get_event(event_id)
    assert event is not None
    assert event.status == EventStatus.CONFIRMED.value
    assert event.raw_text == "カード利用"
    with db.session() as session:
        record = session.scalar(
            select(NotionSyncRecord).where(NotionSyncRecord.event_id == event_id)
        )
        assert record is not None
        assert record.status == NotionSyncStatus.PENDING.value


@pytest.mark.asyncio
async def test_duplicate_notion_promotion_reuses_one_local_record(db, tmp_path):
    settings = _settings(tmp_path)
    service = InboxService(db.session_factory, settings, NullAnalyzer())
    event_id = _event_id(db)
    bot = LifeInboxBot(settings, service)
    view = NotionPromotionView(bot, event_id)
    first = _interaction()
    second = _interaction()

    try:
        button = _promotion_button(view)
        await button.callback(first)
        await button.callback(second)
    finally:
        await bot.close()

    assert "ローカルのNotion保存予約キューに登録しました" in (
        first.response.send_message.await_args.args[0]
    )
    assert "重複は作成していません" in second.response.send_message.await_args.args[0]
    with db.session() as session:
        records = list(
            session.scalars(
                select(NotionSyncRecord).where(NotionSyncRecord.event_id == event_id)
            )
        )
        assert len(records) == 1


@pytest.mark.asyncio
async def test_routing_and_confirmation_do_not_implicitly_enqueue_notion(db, tmp_path):
    settings = _settings(tmp_path, memo_channel_id=41)
    service = InboxService(db.session_factory, settings, NullAnalyzer())
    notion_sync = FakeNotionSync()
    event_id = _event_id(
        db,
        event_type="unknown",
        event_kinds=["unknown"],
        status="pending",
        raw_text="通常ルーティング",
    )
    bot = LifeInboxBot(settings, service, notion_sync=notion_sync)
    destination = SimpleNamespace(
        id=41,
        name="生活-記録",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(),
    )
    bot.get_channel = MagicMock(return_value=destination)
    source = SimpleNamespace(
        id=901,
        guild=SimpleNamespace(id=20),
        channel=SimpleNamespace(id=30),
        delete=AsyncMock(),
    )
    confirmation = _interaction(channel_id=30)

    try:
        await bot._route_classified_inbox(source, event_id, InboxCategory.MEMO)
        action_view = EventActionView(service, bot.guard, event_id)
        await action_view.children[0].callback(confirmation)
    finally:
        await bot.close()

    assert notion_sync.event_ids == []
    assert destination.send.await_count == 1
    assert confirmation.response.send_message.await_count == 1
    with db.session() as session:
        assert session.scalar(select(NotionSyncRecord)) is None
