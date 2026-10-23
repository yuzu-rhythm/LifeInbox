from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import select

from life_inbox.analyzer import NullAnalyzer
from life_inbox.config import Settings
from life_inbox.constants import AccountKind, RhythmGame
from life_inbox.discord_bot import (
    LifeInboxBot,
    LifeInboxCommands,
    _display_event,
    _display_event_detail,
    _event_web_url,
    _message_chunks,
)
from life_inbox.models import (
    CareerApplication,
    CareerOpportunity,
    Event,
    Expense,
    LedgerActivity,
    LifeTask,
    Meal,
    Memo,
    ReceiptItem,
)
from life_inbox.rhythm_discord import RhythmDiscordPublisher
from life_inbox.rhythm_persistence import RhythmPersistence
from life_inbox.rhythm_scheduler import RhythmScheduler
from life_inbox.services import InboxService
from life_inbox.tasks import TaskService


def test_long_discord_output_is_chunked_without_data_loss():
    message = "\n".join(f"record-{index:03d}" for index in range(500))
    chunks = _message_chunks(message, limit=100)

    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "\n".join(chunks) == message


@pytest.mark.parametrize("event_id", [0, -1, True, "7"])
def test_event_web_url_requires_a_positive_integer(tmp_path, event_id):
    settings = _bot_settings(tmp_path)

    with pytest.raises(ValueError, match="positive integer"):
        _event_web_url(settings, event_id)


def _bot_settings(tmp_path, *, records_channel_id=None, **channel_ids):
    return Settings(
        _env_file=None,
        discord_bot_token="test-token",
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        records_channel_id=records_channel_id,
        openai_api_key="test-openai-key",
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
        **channel_ids,
    )


def _interaction(*, channel_id, user_id=10, guild_id=20, interaction_id=1):
    response = SimpleNamespace(
        is_done=MagicMock(return_value=False),
        send_message=AsyncMock(),
        defer=AsyncMock(),
    )

    def mark_deferred(*_args, **_kwargs):
        response.is_done.return_value = True

    response.defer.side_effect = mark_deferred
    followup = SimpleNamespace(send=AsyncMock())
    return SimpleNamespace(
        id=interaction_id,
        user=SimpleNamespace(id=user_id),
        guild_id=guild_id,
        channel_id=channel_id,
        response=response,
        followup=followup,
    )


def _event_with_details(*, event_id=7, occurred_at=None):
    event = Event(
        id=event_id,
        discord_message_id=f"message-{event_id}",
        discord_channel_id="30",
        discord_user_id="10",
        captured_at=occurred_at or datetime(2026, 8, 28, 3, 0),
        occurred_at=occurred_at or datetime(2026, 8, 28, 3, 0),
        raw_text="レシート\n朝食 480",
        source="discord",
        status="confirmed",
        event_type="expense+meal",
        event_kinds=["expense", "meal"],
        copied_to_planner_at=datetime(2026, 8, 28, 4, 0),
    )
    expense = Expense(
        id=1,
        event_id=event_id,
        amount=480,
        merchant_raw="セブン",
        merchant_normalized="セブン-イレブン",
        category="食費",
        subcategory="コンビニ",
        description="朝食",
        confidence=0.95,
    )
    expense.receipt_items = [
        ReceiptItem(
            id=2,
            name="おにぎり",
            quantity=2,
            unit_price=150,
            amount=300,
            order_index=1,
        ),
        ReceiptItem(
            id=1,
            name="お茶",
            quantity=None,
            unit_price=180,
            amount=180,
            order_index=0,
        ),
    ]
    event.expenses = [expense]
    event.meals = [
        Meal(
            event_id=event_id,
            meal_type="lunch",
            restaurant_raw="セブン",
            restaurant_normalized="セブン-イレブン",
            description="軽食",
            outside_home="outside",
            confidence=0.82,
        )
    ]
    return event


def _discord_message(
    *,
    message_id,
    channel_id,
    content,
    reference_id=None,
    attachments=None,
):
    channel = SimpleNamespace(id=channel_id, send=AsyncMock())
    return SimpleNamespace(
        id=message_id,
        content=content,
        author=SimpleNamespace(id=10, bot=False),
        guild=SimpleNamespace(id=20),
        channel=channel,
        attachments=attachments or [],
        reference=(
            SimpleNamespace(message_id=reference_id) if reference_id is not None else None
        ),
        created_at=datetime(2026, 8, 29, 3, 0),
        webhook_id=None,
        reply=AsyncMock(),
    )


def test_event_display_and_detail_include_persisted_receipt_items():
    event = _event_with_details()

    initial = _display_event(event)
    detail = _display_event_detail(event, timezone_name="Asia/Tokyo")

    assert "レシート明細:" in initial
    assert "商品: お茶" in initial
    assert "商品: おにぎり" in initial
    assert "ステータス: confirmed" in detail
    assert "発生日時（Asia/Tokyo）: 2026-08-28 12:00:00" in detail
    assert "レシート\n朝食 480" in detail
    assert "金額: 480円" in detail
    assert "店舗: セブン-イレブン" in detail
    assert "カテゴリ: 食費" in detail
    assert "サブカテゴリ: コンビニ" in detail
    assert "内容: 朝食" in detail
    assert "信頼度: 高（0.95）" in detail
    assert "数量: 2" in detail
    assert "単価: 150円" in detail
    assert "行合計（小計）: 300円" in detail
    assert "区分: lunch" in detail
    assert "自宅外: outside" in detail
    assert "手帳転記: 転記済み" in detail


@pytest.mark.asyncio
async def test_detail_command_is_ephemeral_and_available_in_records_channel(service, tmp_path):
    settings = _bot_settings(tmp_path, records_channel_id=40)
    bot = LifeInboxBot(settings, service)
    event = _event_with_details()
    service.get_event = MagicMock(return_value=event)
    interaction = _interaction(channel_id=40)
    cog = LifeInboxCommands(bot)

    try:
        await LifeInboxCommands.detail.callback(cog, interaction, event.id)
    finally:
        await bot.close()

    interaction.response.send_message.assert_awaited_once()
    content, = interaction.response.send_message.await_args.args
    assert "おにぎり" in content
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_detail_command_handles_missing_event_without_traceback(service, tmp_path):
    settings = _bot_settings(tmp_path, records_channel_id=40)
    bot = LifeInboxBot(settings, service)
    service.get_event = MagicMock(return_value=None)
    interaction = _interaction(channel_id=40)
    cog = LifeInboxCommands(bot)

    try:
        await LifeInboxCommands.detail.callback(cog, interaction, 404)
    finally:
        await bot.close()

    interaction.response.send_message.assert_awaited_once_with(
        "Event #404 は見つかりません。", ephemeral=True
    )


@pytest.mark.asyncio
async def test_history_renders_newest_first_with_detail_hint(service, tmp_path):
    settings = _bot_settings(tmp_path, records_channel_id=40)
    bot = LifeInboxBot(settings, service)
    older = _event_with_details(event_id=1, occurred_at=datetime(2026, 8, 27, 15, 0))
    newer = _event_with_details(event_id=2, occurred_at=datetime(2026, 8, 28, 3, 0))
    service.list_confirmed = MagicMock(return_value=[older, newer])
    interaction = _interaction(channel_id=40)
    cog = LifeInboxCommands(bot)

    try:
        await LifeInboxCommands.history.callback(cog, interaction, 2)
    finally:
        await bot.close()

    interaction.response.send_message.assert_awaited_once()
    content, = interaction.response.send_message.await_args.args
    assert content.index("#2") < content.index("#1")
    assert "2026-08-28 12:00:00" in content
    assert "480円 / セブン-イレブン / 食費 > コンビニ" in content
    assert "/detail event_id:2" in content
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is False


@pytest.mark.asyncio
async def test_records_channel_rejects_capture_and_mutating_commands(service, tmp_path):
    settings = _bot_settings(tmp_path, records_channel_id=40)
    bot = LifeInboxBot(settings, service)
    message = SimpleNamespace(
        author=SimpleNamespace(id=10, bot=False),
        guild=SimpleNamespace(id=20),
        channel=SimpleNamespace(id=40),
        webhook_id=None,
    )
    service.capture_discord_message = AsyncMock()
    interaction = _interaction(channel_id=40)
    cog = LifeInboxCommands(bot)

    try:
        await bot.on_message(message)
        await LifeInboxCommands.copied.callback(cog, interaction, 7, False)
    finally:
        await bot.close()

    service.capture_discord_message.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        "この操作は許可されていません。", ephemeral=True
    )


@pytest.mark.asyncio
async def test_budget_set_is_account_only_and_budget_read_is_finance_read_only(service, tmp_path):
    settings = _bot_settings(
        tmp_path,
        records_channel_id=40,
        account_channel_id=50,
    )
    bot = LifeInboxBot(settings, service)
    cog = LifeInboxCommands(bot)

    try:
        account_interaction = _interaction(channel_id=50)
        await LifeInboxCommands.budget_set.callback(
            cog,
            account_interaction,
            12_000,
            " 食費 ",
            "2026-08",
        )
        account_interaction.response.send_message.assert_awaited_once()
        assert account_interaction.response.send_message.await_args.kwargs["ephemeral"] is True
        assert "2026-08" in account_interaction.response.send_message.await_args.args[0]
        assert "食費" in account_interaction.response.send_message.await_args.args[0]

        records_interaction = _interaction(channel_id=40)
        await LifeInboxCommands.budget.callback(cog, records_interaction, "2026-08")
        records_interaction.response.send_message.assert_awaited_once()
        budget_text = records_interaction.response.send_message.await_args.args[0]
        assert "上限 12,000円" in budget_text
        assert "使用 0円" in budget_text
        assert "残り 12,000円" in budget_text
        assert records_interaction.response.send_message.await_args.kwargs["ephemeral"] is True

        denied_interaction = _interaction(channel_id=40)
        await LifeInboxCommands.budget_set.callback(
            cog,
            denied_interaction,
            10_000,
            None,
            "2026-08",
        )
        denied_interaction.response.send_message.assert_awaited_once_with(
            "この操作は #口座 だけで使えます。", ephemeral=True
        )
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_budget_commands_hide_validation_details_and_keep_responses_ephemeral(
    service, tmp_path
):
    settings = _bot_settings(tmp_path, account_channel_id=50, records_channel_id=40)
    bot = LifeInboxBot(settings, service)
    cog = LifeInboxCommands(bot)

    try:
        invalid = _interaction(channel_id=50)
        await LifeInboxCommands.budget_set.callback(cog, invalid, 0, None, "bad-month")
        invalid.response.send_message.assert_awaited_once()
        text = invalid.response.send_message.await_args.args[0]
        assert "予算を設定できませんでした" in text
        assert "Traceback" not in text
        assert "IntegrityError" not in text
        assert invalid.response.send_message.await_args.kwargs["ephemeral"] is True
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_settlement_account_commands_manage_links_privately_and_show_unlinked_cards(
    service, tmp_path
):
    settings = _bot_settings(tmp_path, account_channel_id=50)
    bot = LifeInboxBot(settings, service)
    cog = LifeInboxCommands(bot)

    try:
        bot.finance.ensure_default_accounts()
        bot.finance.create_account("第二カード", AccountKind.CARD)

        link_interaction = _interaction(channel_id=50)
        await LifeInboxCommands.settlement_account_link.callback(
            cog,
            link_interaction,
            "クレジットカード",
            "普通口座",
        )
        link_interaction.response.send_message.assert_awaited_once()
        link_text = link_interaction.response.send_message.await_args.args[0]
        assert "クレジットカード → 普通口座" in link_text
        assert link_interaction.response.send_message.await_args.kwargs["ephemeral"] is True

        list_interaction = _interaction(channel_id=50)
        await LifeInboxCommands.settlement_account_list.callback(cog, list_interaction)
        list_interaction.response.send_message.assert_awaited_once()
        list_text = list_interaction.response.send_message.await_args.args[0]
        assert "クレジットカード → 普通口座" in list_text
        assert "第二カード → 未設定" in list_text
        assert list_interaction.response.send_message.await_args.kwargs["ephemeral"] is True

        unlink_interaction = _interaction(channel_id=50)
        await LifeInboxCommands.settlement_account_unlink.callback(
            cog,
            unlink_interaction,
            "クレジットカード",
        )
        assert "引落口座を解除しました" in (
            unlink_interaction.response.send_message.await_args.args[0]
        )
        assert unlink_interaction.response.send_message.await_args.kwargs["ephemeral"] is True
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_settlement_account_commands_hide_validation_errors_and_require_account_channel(
    service, tmp_path
):
    settings = _bot_settings(tmp_path, account_channel_id=50)
    bot = LifeInboxBot(settings, service)
    cog = LifeInboxCommands(bot)

    try:
        bot.finance.ensure_default_accounts()
        invalid = _interaction(channel_id=50)
        await LifeInboxCommands.settlement_account_link.callback(
            cog,
            invalid,
            "普通口座",
            "現金",
        )
        invalid_text = invalid.response.send_message.await_args.args[0]
        assert "登録済み" in invalid_text
        assert "settlement" not in invalid_text
        assert "Traceback" not in invalid_text
        assert invalid.response.send_message.await_args.kwargs["ephemeral"] is True

        denied = _interaction(channel_id=40)
        await LifeInboxCommands.settlement_account_unlink.callback(
            cog,
            denied,
            "クレジットカード",
        )
        denied.response.send_message.assert_awaited_once_with(
            "この操作は #口座 だけで使えます。", ephemeral=True
        )
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_account_and_memo_channels_persist_deterministically(db, tmp_path):
    settings = _bot_settings(
        tmp_path,
        account_channel_id=50,
        memo_channel_id=60,
    )
    service = InboxService(db.session_factory, settings, NullAnalyzer())
    bot = LifeInboxBot(settings, service)
    account_message = _discord_message(
        message_id="account-income",
        channel_id=50,
        content="[普通口座] 入金 10000 給料",
    )
    memo_message = _discord_message(
        message_id="memo-one",
        channel_id=60,
        content="明日は早めに出る",
    )

    try:
        bot.finance.ensure_default_accounts()
        await bot.on_message(account_message)
        await bot.on_message(memo_message)
    finally:
        await bot.close()

    assert bot.finance.get_account_by_name("普通口座").balance == 10_000
    assert [memo.body for memo in bot.finance.list_memos()] == ["明日は早めに出る"]
    with db.session() as session:
        assert session.scalar(select(LedgerActivity).where(LedgerActivity.kind == "income"))
        assert session.scalar(select(Memo).where(Memo.body == "明日は早めに出る"))
    account_message.channel.send.assert_awaited_once()
    memo_message.channel.send.assert_not_awaited()
    memo_message.reply.assert_awaited_once()
    memo_reply = memo_message.reply.await_args
    assert "一言メモを保存しました: #2" in memo_reply.args[0]
    assert "http://127.0.0.1:8765/events/2" in memo_reply.args[0]
    assert memo_reply.kwargs["mention_author"] is False
    memo_view = memo_reply.kwargs["view"]
    assert memo_view.children[0].style is discord.ButtonStyle.link
    assert memo_view.children[0].label == "Webで開く"
    assert memo_view.children[0].url.endswith("/events/2")


@pytest.mark.asyncio
async def test_card_channel_keeps_prefix_raw_but_links_selected_card(db, tmp_path):
    settings = _bot_settings(tmp_path, card_channel_id=41)
    service = InboxService(db.session_factory, settings, NullAnalyzer())
    bot = LifeInboxBot(settings, service)
    message = _discord_message(
        message_id="card-expense",
        channel_id=41,
        content="[クレジットカード] 680 マック",
    )

    try:
        bot.finance.ensure_default_accounts()
        await bot.on_message(message)
    finally:
        await bot.close()

    event = service.list_pending()[0]
    assert event.raw_text == "[クレジットカード] 680 マック"
    assert event.expenses[0].merchant_normalized == "マクドナルド"
    assert event.ledger_activity is not None
    assert event.ledger_activity.source_account.name == "クレジットカード"
    message.channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_rhythm_reply_image_completes_todo_and_keeps_result_attachment(db, tmp_path):
    settings = _bot_settings(tmp_path, rhythm_channel_id=70)
    service = InboxService(db.session_factory, settings, NullAnalyzer())
    bot = LifeInboxBot(settings, service)
    todo_message = _discord_message(
        message_id="rhythm-todo",
        channel_id=70,
        content="todo maimai でらっくす譜面を詰める",
    )
    attachment = SimpleNamespace(
        id="rhythm-result-image",
        filename="result.png",
        content_type="image/png",
        size=9,
        read=AsyncMock(return_value=b"png-bytes"),
    )
    result_message = _discord_message(
        message_id="rhythm-result",
        channel_id=70,
        content="",
        reference_id="rhythm-todo",
        attachments=[attachment],
    )

    try:
        await bot.on_message(todo_message)
        await bot.on_message(result_message)
    finally:
        await bot.close()

    assert bot.rhythm.list_open_todos() == []
    results = bot.rhythm.list_results()
    assert len(results) == 1
    assert results[0].todo_id == 1
    result_event = service.get_event(results[0].event_id)
    assert result_event is not None
    assert len(result_event.attachments) == 1
    assert result_event.attachments[0].original_filename == "result.png"
    todo_message.channel.send.assert_awaited_once()
    result_message.channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_progress_channel_accepts_text_led_results_and_requires_images(db, tmp_path):
    settings = _bot_settings(
        tmp_path,
        rhythm_channel_id=70,
        progress_channel_id=80,
    )
    service = InboxService(db.session_factory, settings, NullAnalyzer())
    bot = LifeInboxBot(settings, service)
    todo_message = _discord_message(
        message_id="1001",
        channel_id=70,
        content="todo maimai でらっくす譜面を詰める",
    )
    attachment = SimpleNamespace(
        id="progress-image-1",
        filename="progress.png",
        content_type="image/png",
        size=9,
        read=AsyncMock(return_value=b"png-bytes"),
    )
    progress_message = _discord_message(
        message_id="1002",
        channel_id=80,
        content="maimai SSS+",
        attachments=[attachment],
    )
    no_text_reply = _discord_message(
        message_id="1003",
        channel_id=80,
        content="",
        reference_id="1001",
        attachments=[
            SimpleNamespace(
                id="progress-image-2",
                filename="reply.png",
                content_type="image/png",
                size=9,
                read=AsyncMock(return_value=b"reply-bytes"),
            )
        ],
    )
    image_only_non_reply = _discord_message(
        message_id="1004",
        channel_id=80,
        content="",
        attachments=[
            SimpleNamespace(
                id="progress-image-3",
                filename="unlinked.png",
                content_type="image/png",
                size=9,
                read=AsyncMock(return_value=b"unlinked-bytes"),
            )
        ],
    )
    no_image = _discord_message(
        message_id="1005",
        channel_id=80,
        content="オンゲキ AB",
    )

    try:
        await bot.on_message(todo_message)
        await bot.on_message(progress_message)
        await bot.on_message(no_text_reply)
        await bot.on_message(image_only_non_reply)
        await bot.on_message(no_image)
    finally:
        await bot.close()

    results = bot.rhythm.list_results()
    assert len(results) == 2
    assert results[0].note is None
    assert results[0].todo_id == 1
    assert results[0].attachment_count == 1
    assert results[1].note == "SSS+"
    assert results[1].game is RhythmGame.MAIMAI
    assert results[1].attachment_count == 1
    assert service.get_event(4).rhythm_result is None
    assert service.get_event(5).rhythm_result is None
    assert len(service.get_event(4).attachments) == 1
    assert "ゲーム名" in image_only_non_reply.channel.send.await_args.args[0]
    assert "画像を1枚以上" in no_image.channel.send.await_args.args[0]
    progress_message.channel.send.assert_awaited_once()
    assert "リザルト #1" in progress_message.channel.send.await_args.args[0]
    assert "画像1件" in progress_message.channel.send.await_args.args[0]


@pytest.mark.asyncio
async def test_rhythm_favorite_and_progress_commands_are_scoped_ephemeral_and_linked(
    service, tmp_path
):
    settings = _bot_settings(
        tmp_path,
        rhythm_channel_id=70,
        progress_channel_id=80,
        records_channel_id=40,
    )
    bot = LifeInboxBot(settings, service)
    cog = LifeInboxCommands(bot)
    favorite_choice = SimpleNamespace(value="maimai")

    try:
        add_interaction = _interaction(channel_id=80)
        await LifeInboxCommands.favorite_add.callback(
            cog,
            add_interaction,
            favorite_choice,
            "  ＡＢ　Song  ",
            "練習",
        )
        assert add_interaction.response.send_message.await_args.kwargs["ephemeral"] is True
        assert "AB Song" in add_interaction.response.send_message.await_args.args[0]

        list_interaction = _interaction(channel_id=40)
        await LifeInboxCommands.maimai_favorites.callback(cog, list_interaction)
        list_text = list_interaction.response.send_message.await_args.args[0]
        assert "AB Song" in list_text
        assert "練習" in list_text
        assert "出典: 手動" in list_text
        assert list_interaction.response.send_message.await_args.kwargs["ephemeral"] is True

        cog._today = MagicMock(return_value=date(2026, 8, 29))
        empty_progress = _interaction(channel_id=80)
        await LifeInboxCommands.progress_today.callback(cog, empty_progress, favorite_choice)
        empty_text = empty_progress.response.send_message.await_args.args[0]
        assert "今日の進捗はまだありません" in empty_text
        assert empty_progress.response.send_message.await_args.kwargs["ephemeral"] is True

        progress_message = _discord_message(
            message_id="2002",
            channel_id=80,
            content="maimai SSS+",
            attachments=[
                SimpleNamespace(
                    id="today-image",
                    filename="today.png",
                    content_type="image/png",
                    size=9,
                    read=AsyncMock(return_value=b"today-bytes"),
                )
            ],
        )
        await bot.on_message(progress_message)
        progress_interaction = _interaction(channel_id=80)
        await LifeInboxCommands.progress_today.callback(cog, progress_interaction, None)
        progress_text = progress_interaction.response.send_message.await_args.args[0]
        assert "Result #1" in progress_text
        assert "Event #1" in progress_text
        assert "画像1件" in progress_text
        assert "https://discord.com/channels/20/80/2002" in progress_text
        assert progress_interaction.response.send_message.await_args.kwargs["ephemeral"] is True

        denied = _interaction(channel_id=40)
        await LifeInboxCommands.favorite_add.callback(
            cog,
            denied,
            favorite_choice,
            "Denied song",
            None,
        )
        denied.response.send_message.assert_awaited_once()
        assert "この操作は" in denied.response.send_message.await_args.args[0]
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_life_task_commands_are_scoped_and_ephemeral(service, tmp_path):
    settings = _bot_settings(tmp_path, records_channel_id=40)
    bot = LifeInboxBot(settings, service, tasks=TaskService(service.session_factory))
    cog = LifeInboxCommands(bot)

    try:
        add_interaction = _interaction(channel_id=30)
        await LifeInboxCommands.task_add.callback(
            cog,
            add_interaction,
            "  提出物  ",
            "2020-01-01",
            "明日の朝に確認",
            None,
        )
        add_interaction.response.send_message.assert_awaited_once()
        assert add_interaction.response.send_message.await_args.kwargs["ephemeral"] is True

        read_interaction = _interaction(channel_id=40)
        await LifeInboxCommands.tasks.callback(cog, read_interaction)
        task_content, = read_interaction.response.send_message.await_args.args
        assert "#1 提出物" in task_content
        assert "期限: 2020-01-01" in task_content
        assert "期限超過" in task_content
        assert "出典: 手動" in task_content
        assert read_interaction.response.send_message.await_args.kwargs["ephemeral"] is True

        denied_done = _interaction(channel_id=40)
        await LifeInboxCommands.task_done.callback(cog, denied_done, 1)
        denied_done.response.send_message.assert_awaited_once_with(
            "この操作は許可されていません。", ephemeral=True
        )

        done_interaction = _interaction(channel_id=30)
        await LifeInboxCommands.task_done.callback(cog, done_interaction, 1)
        done_interaction.response.send_message.assert_awaited_once()
        assert "完了済み" in done_interaction.response.send_message.await_args.args[0]
        assert done_interaction.response.send_message.await_args.kwargs["ephemeral"] is True

        outsider = _interaction(channel_id=30, user_id=999)
        await LifeInboxCommands.task_add.callback(
            cog, outsider, "他人のタスク", None, None, None
        )
        outsider.response.send_message.assert_awaited_once_with(
            "この操作は許可されていません。", ephemeral=True
        )

        timed = _interaction(channel_id=30)
        await LifeInboxCommands.task_add_japanese.callback(
            cog,
            timed,
            "面接の準備",
            "2026-09-08",
            "質問を確認",
            "18:30",
        )
        assert "期限: 2026-09-08 18:30" in timed.response.send_message.await_args.args[0]

        invalid_time = _interaction(channel_id=30)
        await LifeInboxCommands.task_add_japanese.callback(
            cog,
            invalid_time,
            "時刻だけのタスク",
            None,
            None,
            "18:30",
        )
        assert "タスクを追加できませんでした" in (
            invalid_time.response.send_message.await_args.args[0]
        )
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_career_commands_add_list_and_update_application_with_reminder(service, tmp_path):
    settings = _bot_settings(tmp_path, records_channel_id=40)
    bot = LifeInboxBot(settings, service)
    cog = LifeInboxCommands(bot)

    try:
        add_interaction = _interaction(channel_id=30)
        await LifeInboxCommands.career_add.callback(
            cog,
            add_interaction,
            company="Acme",
            title="バックエンド",
            kind="application",
            stage="applied",
            deadline="2026-09-05 15:00",
            notes="履歴書を提出済み",
            location="東京",
            url="https://example.com/jobs/backend",
        )
        add_interaction.response.send_message.assert_awaited_once()
        add_content = add_interaction.response.send_message.await_args.args[0]
        assert "就活記録 #1" in add_content
        assert "Acme / バックエンド" in add_content
        assert "期限: 2026-09-05 15:00" in add_content
        assert add_interaction.response.send_message.await_args.kwargs["ephemeral"] is True

        with service.session_factory() as session:
            application = session.scalar(select(CareerApplication))
            opportunity = session.scalar(select(CareerOpportunity))
            task = session.scalar(select(LifeTask))
            assert application is not None
            assert opportunity is not None
            assert task is not None
            assert application.stage == "applied"
            assert application.status == "applied"
            application_id = application.id
            task_id = task.id

        list_interaction = _interaction(channel_id=40)
        await LifeInboxCommands.career_list.callback(
            cog,
            list_interaction,
            kind="application",
            include_closed=False,
        )
        list_interaction.response.send_message.assert_awaited_once()
        list_content = list_interaction.response.send_message.await_args.args[0]
        assert "Acme / バックエンド" in list_content
        assert "応募済み" in list_content
        assert list_interaction.response.send_message.await_args.kwargs["ephemeral"] is True

        update_interaction = _interaction(channel_id=30)
        await LifeInboxCommands.career_update.callback(
            cog,
            update_interaction,
            record_id=application_id,
            record_type="application",
            stage="interview",
            deadline="2026-09-06 10:00",
            next_action="面接準備",
        )
        update_interaction.response.send_message.assert_awaited_once()
        update_content = update_interaction.response.send_message.await_args.args[0]
        assert f"就活記録 #{application_id}" in update_content
        assert "面接" in update_content
        assert "期限: 2026-09-06 10:00" in update_content
        assert update_interaction.response.send_message.await_args.kwargs["ephemeral"] is True

        with service.session_factory() as session:
            application = session.get(CareerApplication, application_id)
            task = session.get(LifeTask, task_id)
            assert application is not None
            assert task is not None
            assert application.stage == "interview"
            assert application.status == "in_progress"
            assert application.linked_task_id == task_id
            assert task.status == "open"
            assert task.due_at == datetime(2026, 9, 6, 1, 0)
            assert task.description is not None
            assert "面接準備" in task.description
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_ai_review_command_is_private_scoped_and_local_by_default(service, tmp_path):
    settings = _bot_settings(
        tmp_path,
        records_channel_id=40,
        ai_review_channel_id=50,
    )
    bot = LifeInboxBot(settings, service)
    cog = LifeInboxCommands(bot)

    try:
        review = _interaction(channel_id=50)
        await LifeInboxCommands.ai_review_summary.callback(cog, review, None)
        content = review.response.send_message.await_args.args[0]
        assert "日次AI振り返り" in content
        assert "外部AIへの送信なし" in content
        assert review.response.send_message.await_args.kwargs["ephemeral"] is True

        denied = _interaction(channel_id=30)
        await LifeInboxCommands.ai_review_summary.callback(cog, denied, None)
        denied.response.send_message.assert_awaited_once_with(
            "この閲覧コマンドは #ai-振り返り または #生活-記録 で使えます。",
            ephemeral=True,
        )
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_setup_hook_registers_all_slash_commands_without_network(service, tmp_path):
    settings = Settings(
        _env_file=None,
        discord_bot_token="test-token",
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        openai_api_key="test-openai-key",
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
    )
    bot = LifeInboxBot(settings, service)

    try:
        with patch.object(bot.tree, "sync", new=AsyncMock(return_value=[])) as sync:
            await bot.setup_hook()

        expected = {
            "today",
            "month",
            "uncopied",
            "planner",
            "copied",
            "inbox",
            "retry",
            "history",
            "detail",
            "spending",
            "budget_set",
            "budget",
            "dining",
            "meals",
            "accounts",
            "memos",
            "家計簿今日",
            "家計簿配信確認",
            "振り返り",
            "rhythm",
        "task_add",
        "タスク追加",
        "tasks",
        "タスク一覧",
        "task_done",
        "タスク完了",
            "就活追加",
            "就活一覧",
            "就活更新",
            "日課追加",
        "日課一覧",
        "日課完了",
            "account_add",
            "引落口座設定",
            "引落口座解除",
            "引落口座一覧",
            "transfer",
            "mypage",
            "favorite_add",
            "maimai_favorites",
            "maimai_net_favorites",
            "maimai_mybest",
            "progress_today",
                "レート",
                "レート更新",
                "get",
                "取得確認",
                "取得状況",
            "今日の利用額",
            "再投稿",
            "認証状況",
            "コマンド一覧",
        }
        assert {command.name for command in bot.tree.get_commands()} == expected
        guild = discord.Object(id=settings.discord_guild_id)
        assert {command.name for command in bot.tree.get_commands(guild=guild)} == expected
        sync.assert_awaited_once()
    finally:
        await bot.close()


class ManualCommandExecutor:
    def __init__(self, persistence: RhythmPersistence) -> None:
        self.persistence = persistence
        self.calls: list[str] = []

    async def execute(self, run) -> None:
        self.calls.append(run.id)
        self.persistence.transition_run(
            run_id=run.id,
            to_state="cancelled",
            event_type="manualOnlyFixture",
            message="手動一回実行fixture完了",
            allowed_from={"queued"},
        )


@pytest.mark.asyncio
async def test_setup_hook_builds_manual_scheduler_without_starting_loops(service, tmp_path):
    settings = _bot_settings(
        tmp_path,
        rhythm_channel_id=80,
        rhythm_enabled=True,
        rhythm_scheduler_enabled=False,
        rhythm_discord_dry_run=True,
    )
    bot = LifeInboxBot(settings, service)

    try:
        with patch.object(bot.tree, "sync", new=AsyncMock(return_value=[])):
            await bot.setup_hook()

        assert bot.rating_scheduler is not None
        assert bot.rating_scheduler.running is False
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_rating_update_runs_one_manual_request_when_scheduler_is_disabled(service, tmp_path):
    settings = _bot_settings(
        tmp_path,
        rhythm_channel_id=80,
        rhythm_enabled=True,
        rhythm_scheduler_enabled=False,
        rhythm_discord_dry_run=True,
    )
    persistence = RhythmPersistence(service.session_factory)
    user_id = persistence.ensure_user(
        display_name=settings.rhythm_player_display_name,
        time_zone=settings.rhythm_time_zone,
        discord_user_id=settings.allowed_user_id,
    )
    publisher = RhythmDiscordPublisher(persistence, MagicMock(), dry_run=True)
    executor = ManualCommandExecutor(persistence)
    scheduler = RhythmScheduler(
        settings=settings,
        persistence=persistence,
        executor=executor,
        publisher=publisher,
        user_id=user_id,
    )
    bot = LifeInboxBot(
        settings,
        service,
        rating_store=persistence,
        rating_publisher=publisher,
        rating_scheduler=scheduler,
    )
    cog = LifeInboxCommands(bot)
    choice = SimpleNamespace(value="maimai")

    try:
        first = _interaction(channel_id=80, interaction_id=9001)
        await LifeInboxCommands.rating_update.callback(cog, first, choice)
        second = _interaction(channel_id=80, interaction_id=9001)
        await LifeInboxCommands.rating_update.callback(cog, second, choice)
        get_alias = _interaction(channel_id=80, interaction_id=9001)
        await LifeInboxCommands.get_rating.callback(cog, get_alias, choice)

        assert scheduler.running is False
        assert len(executor.calls) == 1
        run = persistence.latest_runs(user_id=user_id)["maimai"]
        assert run.state == "cancelled"
        assert first.response.defer.await_count == 1
        assert second.response.defer.await_count == 1
        assert get_alias.response.defer.await_count == 1
        first.followup.send.assert_awaited()
        second.followup.send.assert_awaited()
        get_alias.followup.send.assert_awaited()
    finally:
        await bot.close()


def test_bot_rejects_injected_publisher_that_bypasses_dry_run(service, tmp_path):
    settings = _bot_settings(tmp_path, rhythm_discord_dry_run=True)
    persistence = RhythmPersistence(service.session_factory)
    publisher = RhythmDiscordPublisher(persistence, MagicMock(), dry_run=False)

    with pytest.raises(ValueError, match="dry-run"):
        LifeInboxBot(
            settings,
            service,
            rating_store=persistence,
            rating_publisher=publisher,
        )


def test_bot_rejects_injected_scheduler_publisher_that_bypasses_dry_run(service, tmp_path):
    settings = _bot_settings(
        tmp_path,
        rhythm_enabled=True,
        rhythm_scheduler_enabled=False,
        rhythm_discord_dry_run=True,
    )
    persistence = RhythmPersistence(service.session_factory)
    safe_publisher = RhythmDiscordPublisher(persistence, MagicMock(), dry_run=True)
    unsafe_publisher = RhythmDiscordPublisher(persistence, MagicMock(), dry_run=False)
    scheduler = RhythmScheduler(
        settings=settings,
        persistence=persistence,
        executor=ManualCommandExecutor(persistence),
        publisher=unsafe_publisher,
        user_id=1,
    )

    with pytest.raises(ValueError, match="dry-run"):
        LifeInboxBot(
            settings,
            service,
            rating_store=persistence,
            rating_publisher=safe_publisher,
            rating_scheduler=scheduler,
        )


@pytest.mark.asyncio
async def test_repost_is_blocked_when_rhythm_feature_is_disabled(service, tmp_path):
    settings = _bot_settings(
        tmp_path,
        rhythm_channel_id=80,
        rhythm_enabled=False,
        rhythm_discord_dry_run=True,
    )
    bot = LifeInboxBot(settings, service)
    cog = LifeInboxCommands(bot)

    try:
        interaction = _interaction(channel_id=80)
        await LifeInboxCommands.repost_rating.callback(
            cog,
            interaction,
            "2026-08-29",
            SimpleNamespace(value="chunithm"),
        )

        interaction.response.send_message.assert_awaited_once_with(
            "音ゲー収集は無効です。設定後に再実行してください。", ephemeral=True
        )
    finally:
        await bot.close()
