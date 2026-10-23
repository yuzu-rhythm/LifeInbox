from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import func, select

from life_inbox.config import Settings
from life_inbox.discord_bot import InboxCandidateView, LifeInboxBot
from life_inbox.inbox_classifier import InboxCandidate, InboxCategory, InboxClassification
from life_inbox.models import DiscordArchiveRecord, Event, LifeTask
from life_inbox.notion_models import NotionSyncRecord
from life_inbox.notion_sync import NotionSyncService
from life_inbox.scope import ScopeGuard
from life_inbox.services import InboxService


class RecordingChannel:
    def __init__(
        self,
        channel_id: int,
        name: str,
        *,
        guild_id: int = 20,
        error: Exception | None = None,
        response_prefix: str = "message",
    ) -> None:
        self.id = channel_id
        self.name = name
        self.guild = SimpleNamespace(id=guild_id)
        self.error = error
        self.response_prefix = response_prefix
        self.sent: list[tuple[str, dict[str, object]]] = []
        self.file_bytes: list[bytes] = []
        self.send = AsyncMock(side_effect=self._send)

    async def _send(self, content: str, **kwargs: object) -> SimpleNamespace:
        for file in kwargs.get("files", []) or []:
            file.fp.seek(0)
            self.file_bytes.append(file.fp.read())
        self.sent.append((content, kwargs))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(id=f"{self.response_prefix}-{len(self.sent)}")


class StaticClassifier:
    def __init__(self, category: InboxCategory) -> None:
        self.category = category
        self.calls = 0

    def classify(
        self,
        text: str,
        *,
        attachment_count: int = 0,
        images=(),
    ) -> InboxClassification:
        self.calls += 1
        return InboxClassification(
            candidates=(InboxCandidate(category=self.category, confidence=0.99),),
            source="local",
        )


class FailingNotionQueue:
    def enqueue_event_if_eligible(self, event_id: int) -> object:
        raise RuntimeError("private Notion queue failure")


def _settings(tmp_path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "database_url": f"sqlite:///{tmp_path / 'app.db'}",
        "data_dir": tmp_path,
        "discord_guild_id": 20,
        "allowed_user_id": 10,
        "general_inbox_channel_id": 30,
        "general_archive_channel_id": 31,
        "memo_channel_id": 40,
        "inbox_delete_original_after_route": True,
    }
    values.update(overrides)
    return Settings(**values)


def _message(
    source_channel: RecordingChannel,
    content: str,
    *,
    message_id: str = "source-1",
    attachments: list[object] | None = None,
) -> SimpleNamespace:
    message = SimpleNamespace(
        id=message_id,
        content=content,
        author=SimpleNamespace(id=10, bot=False),
        guild=SimpleNamespace(id=20, text_channels=[]),
        channel=source_channel,
        attachments=attachments or [],
        created_at=datetime(2026, 8, 31, 3, 0),
        add_reaction=AsyncMock(),
        delete=AsyncMock(),
        webhook_id=None,
    )
    return message


def _wire_channels(
    bot: LifeInboxBot,
    archive: RecordingChannel,
    destination: RecordingChannel,
) -> None:
    channels = {archive.id: archive, destination.id: destination}
    bot.get_channel = MagicMock(side_effect=lambda channel_id: channels.get(channel_id))


def _interaction() -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=10, bot=False),
        guild_id=20,
        channel_id=30,
        response=SimpleNamespace(
            is_done=MagicMock(return_value=False),
            send_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _assert_mentions_disabled(kwargs: dict[str, object]) -> None:
    mentions = kwargs["allowed_mentions"]
    assert isinstance(mentions, discord.AllowedMentions)
    assert mentions.everyone is False
    assert mentions.users is False
    assert mentions.roles is False
    assert mentions.replied_user is False


@pytest.mark.asyncio
async def test_general_inbox_persists_then_archives_then_routes_then_deletes(db, tmp_path):
    settings = _settings(tmp_path)
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classifier = StaticClassifier(InboxCategory.MEMO)
    bot = LifeInboxBot(settings, service, inbox_classifier=classifier)
    source = RecordingChannel(30, "一般", response_prefix="candidate")
    archive = RecordingChannel(31, "一般アーカイブ", response_prefix="archive")
    destination = RecordingChannel(40, "一言メモ", response_prefix="destination")
    _wire_channels(bot, archive, destination)
    order: list[str] = []

    original_archive_send = archive.send.side_effect

    async def inspect_archive(content: str, **kwargs: object) -> SimpleNamespace:
        order.append("archive")
        with db.session() as session:
            event = session.scalar(select(Event).where(Event.discord_message_id == "1001"))
            assert event is not None
            assert event.raw_text == "メモ: 原文 <@999>"
            assert len(event.attachments) == 1
        return await original_archive_send(content, **kwargs)

    archive.send.side_effect = inspect_archive

    original_destination_send = destination.send.side_effect

    async def inspect_destination(content: str, **kwargs: object) -> SimpleNamespace:
        order.append("destination")
        return await original_destination_send(content, **kwargs)

    destination.send.side_effect = inspect_destination

    async def record_delete() -> None:
        order.append("delete")

    source_message = _message(
        source,
        "メモ: 原文 <@999>",
        message_id="1001",
        attachments=[
            SimpleNamespace(
                id="attachment-1",
                filename="note.png",
                content_type="image/png",
                size=8,
                read=AsyncMock(return_value=b"png-bytes"),
            )
        ],
    )
    source_message.delete = AsyncMock(side_effect=record_delete)
    source_message.guild.text_channels = [archive, destination]

    try:
        await bot.on_message(source_message)
        assert classifier.calls == 1
        assert len(archive.sent) == 1
        assert "原文（改変なし）:\nメモ: 原文 <@999>" in archive.sent[0][0]
        assert "https://discord.com/channels/20/30/1001" in archive.sent[0][0]
        _assert_mentions_disabled(archive.sent[0][1])
        assert archive.file_bytes == [b"png-bytes"]

        view = source.sent[0][1]["view"]
        assert isinstance(view, InboxCandidateView)
        order.append("candidate")
        choice = next(child for child in view.children if child.label == "一言メモ")
        await choice.callback(_interaction())
    finally:
        await bot.close()

    assert destination.send.await_count == 1
    assert order == ["archive", "candidate", "destination", "delete"]
    source_message.delete.assert_awaited_once_with()
    assert destination.file_bytes == [b"png-bytes"]
    with db.session() as session:
        record = session.scalar(select(DiscordArchiveRecord))
        assert record is not None
        assert record.status == "succeeded"
        assert record.source_message_id == "1001"
        assert record.source_channel_id == "30"
        assert record.destination_channel_id == "31"
        assert record.archive_message_id == "archive-1"
        assert record.archive_message_ids == ["archive-1"]


@pytest.mark.asyncio
async def test_reviewed_task_falls_back_to_deadline_free_raw_text_and_routes_once(
    db,
    tmp_path,
):
    settings = _settings(tmp_path, reminder_channel_id=41)
    analyzer = MagicMock()
    analyzer.route_inbox.side_effect = RuntimeError("private classifier disagreement")
    service = InboxService(db.session_factory, settings, analyzer=analyzer)
    bot = LifeInboxBot(
        settings,
        service,
        inbox_classifier=StaticClassifier(InboxCategory.TASK),
    )
    source = RecordingChannel(30, "一般", response_prefix="candidate")
    archive = RecordingChannel(31, "一般アーカイブ", response_prefix="archive")
    destination = RecordingChannel(41, "日課・リマインダー", response_prefix="task")
    _wire_channels(bot, archive, destination)
    message = _message(
        source,
        "  研究計画の続きを   整理する  ",
        message_id="reviewed-task-1",
    )

    try:
        await bot.on_message(message)
        view = source.sent[0][1]["view"]
        choice = next(child for child in view.children if child.label == "タスク・期限")
        await choice.callback(_interaction())
        # A repeated Discord interaction is safe and cannot create a second
        # local task or destination message in the same Bot process.
        await choice.callback(_interaction())
    finally:
        await bot.close()

    assert archive.send.await_count == 1
    assert destination.send.await_count == 1
    assert message.delete.await_count == 1
    with db.session() as session:
        tasks = list(session.scalars(select(LifeTask)).all())
        assert len(tasks) == 1
        assert tasks[0].title == "研究計画の続きを 整理する"
        assert tasks[0].due_at is None
        event = session.scalar(
            select(Event).where(Event.discord_message_id == "reviewed-task-1")
        )
        assert event is not None
        assert event.event_type == "life_task"
        assert event.event_kinds == ["life_task"]


@pytest.mark.asyncio
async def test_reaction_restores_candidate_after_restart_and_routes_archived_task(
    db,
    tmp_path,
):
    settings = _settings(tmp_path, reminder_channel_id=41)
    analyzer = MagicMock()
    analyzer.route_inbox.side_effect = RuntimeError("private route failure")
    service = InboxService(db.session_factory, settings, analyzer=analyzer)
    source = RecordingChannel(30, "一般", response_prefix="candidate")
    archive = RecordingChannel(31, "一般アーカイブ", response_prefix="archive")
    destination = RecordingChannel(41, "日課・リマインダー", response_prefix="task")
    message = _message(source, "分類機能の仕上げを進める", message_id="restart-task-1")

    first_bot = LifeInboxBot(
        settings,
        service,
        inbox_classifier=StaticClassifier(InboxCategory.TASK),
    )
    _wire_channels(first_bot, archive, destination)
    try:
        await first_bot.on_message(message)
    finally:
        await first_bot.close()

    source.fetch_message = AsyncMock(return_value=message)
    second_bot = LifeInboxBot(
        settings,
        service,
        inbox_classifier=StaticClassifier(InboxCategory.TASK),
    )
    channels = {source.id: source, archive.id: archive, destination.id: destination}
    second_bot.get_channel = MagicMock(side_effect=lambda channel_id: channels.get(channel_id))
    second_bot.get_user = MagicMock(return_value=None)
    member = SimpleNamespace(id=10, bot=False, send=AsyncMock())
    payload = SimpleNamespace(
        user_id=10,
        guild_id=20,
        channel_id=30,
        message_id="restart-task-1",
        emoji="📌",
        member=member,
    )

    try:
        assert second_bot._inbox_candidate_views == {}
        await second_bot.on_raw_reaction_add(payload)
    finally:
        await second_bot.close()

    assert source.fetch_message.await_count == 1
    assert destination.send.await_count == 1
    assert message.delete.await_count == 1
    assert member.send.await_count == 1
    with db.session() as session:
        tasks = list(session.scalars(select(LifeTask)).all())
        assert len(tasks) == 1
        assert tasks[0].title == "分類機能の仕上げを進める"
        event = session.scalar(
            select(Event).where(Event.discord_message_id == "restart-task-1")
        )
        assert event is not None
        assert event.event_type == "life_task"


@pytest.mark.asyncio
async def test_general_archive_failure_is_retryable_and_hides_candidates(db, tmp_path):
    settings = _settings(tmp_path)
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classifier = StaticClassifier(InboxCategory.MEMO)
    bot = LifeInboxBot(settings, service, inbox_classifier=classifier)
    source = RecordingChannel(30, "一般", response_prefix="pending")
    archive = RecordingChannel(31, "一般アーカイブ", error=RuntimeError("private response"))
    destination = RecordingChannel(40, "一言メモ")
    _wire_channels(bot, archive, destination)
    message = _message(source, "メモ: 保留")

    try:
        await bot.on_message(message)
    finally:
        await bot.close()

    assert classifier.calls == 0
    assert destination.send.await_count == 0
    message.delete.assert_not_awaited()
    assert len(source.sent) == 1
    assert "アーカイブへ保存できなかった" in source.sent[0][0]
    with db.session() as session:
        record = session.scalar(select(DiscordArchiveRecord))
        assert record is not None
        assert record.status == "failed"
        assert record.last_error_code == "RuntimeError"
        assert "private response" not in (record.last_error_message or "")


@pytest.mark.asyncio
async def test_notion_enqueue_failure_is_fail_closed_after_archive(db, tmp_path):
    settings = _settings(
        tmp_path,
        notion_sync_enabled=True,
        notion_api_token="fixture-token",
        notion_parent_page_id="fixture-parent",
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classifier = StaticClassifier(InboxCategory.MEMO)
    bot = LifeInboxBot(
        settings,
        service,
        inbox_classifier=classifier,
        notion_sync=FailingNotionQueue(),
    )
    source = RecordingChannel(30, "一般")
    archive = RecordingChannel(31, "一般アーカイブ")
    destination = RecordingChannel(40, "一言メモ")
    _wire_channels(bot, archive, destination)
    message = _message(source, "メモ: Notionキュー失敗")

    try:
        await bot.on_message(message)
    finally:
        await bot.close()

    assert archive.send.await_count == 1
    assert classifier.calls == 0
    destination.send.assert_not_awaited()
    message.delete.assert_not_awaited()
    assert "Notion二次アーカイブ" in source.sent[0][0]


@pytest.mark.asyncio
async def test_general_attachment_failure_stops_before_archive_and_candidates(db, tmp_path):
    settings = _settings(tmp_path)
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classifier = StaticClassifier(InboxCategory.MEMO)
    bot = LifeInboxBot(settings, service, inbox_classifier=classifier)
    source = RecordingChannel(30, "一般")
    archive = RecordingChannel(31, "一般アーカイブ")
    destination = RecordingChannel(40, "一言メモ")
    _wire_channels(bot, archive, destination)
    attachment = SimpleNamespace(
        id="broken-attachment",
        filename="broken.png",
        content_type="image/png",
        size=8,
        read=AsyncMock(side_effect=RuntimeError("private attachment failure")),
    )
    message = _message(source, "メモ: 添付失敗", attachments=[attachment])

    try:
        await bot.on_message(message)
    finally:
        await bot.close()

    assert attachment.read.await_count == 1
    assert classifier.calls == 0
    archive.send.assert_not_awaited()
    message.delete.assert_not_awaited()
    with db.session() as session:
        event = session.scalar(select(Event).where(Event.discord_message_id == "source-1"))
        assert event is not None
        assert event.attachments == []


@pytest.mark.asyncio
async def test_duplicate_general_delivery_reuses_archive_and_candidate_card(db, tmp_path):
    settings = _settings(tmp_path, inbox_delete_original_after_route=False)
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classifier = StaticClassifier(InboxCategory.MEMO)
    bot = LifeInboxBot(settings, service, inbox_classifier=classifier)
    source = RecordingChannel(30, "一般")
    archive = RecordingChannel(31, "一般アーカイブ")
    destination = RecordingChannel(40, "一言メモ")
    _wire_channels(bot, archive, destination)
    attachment = SimpleNamespace(
        id="duplicate-attachment",
        filename="note.png",
        content_type="image/png",
        size=8,
        read=AsyncMock(return_value=b"png-bytes"),
    )
    message = _message(source, "メモ: 重複", attachments=[attachment])

    try:
        await bot.on_message(message)
        await bot.on_message(message)
    finally:
        await bot.close()

    assert attachment.read.await_count == 1
    assert archive.send.await_count == 1
    assert classifier.calls == 1
    assert source.send.await_count == 1
    message.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_reuses_succeeded_archive_mapping_without_sending_again(db, tmp_path):
    settings = _settings(tmp_path, inbox_delete_original_after_route=False)
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    source = RecordingChannel(30, "一般")
    archive = RecordingChannel(31, "一般アーカイブ")
    destination = RecordingChannel(40, "一言メモ")
    source_message = _message(source, "メモ: 再起動")
    source_message.guild.text_channels = [archive, destination]

    first_bot = LifeInboxBot(
        settings,
        service,
        inbox_classifier=StaticClassifier(InboxCategory.MEMO),
    )
    _wire_channels(first_bot, archive, destination)
    try:
        captured = await service.capture_discord_message_raw(source_message)
        delivery = await first_bot._archive_general_inbox_message(
            source_message,
            captured.event_id,
        )
        assert delivery.succeeded is True
    finally:
        await first_bot.close()

    second_bot = LifeInboxBot(
        settings,
        service,
        inbox_classifier=StaticClassifier(InboxCategory.MEMO),
    )
    _wire_channels(second_bot, archive, destination)
    try:
        delivery = await second_bot._archive_general_inbox_message(
            source_message,
            captured.event_id,
        )
    finally:
        await second_bot.close()

    assert delivery.succeeded is True
    assert archive.send.await_count == 1
    with db.session() as session:
        assert session.scalar(select(func.count(DiscordArchiveRecord.id))) == 1


@pytest.mark.asyncio
async def test_archive_name_fallback_is_unique_and_partial_send_resumes(db, tmp_path):
    settings = _settings(
        tmp_path,
        general_archive_channel_id=None,
        inbox_delete_original_after_route=False,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    source = RecordingChannel(30, "一般")
    archive = RecordingChannel(31, "一般アーカイブ", response_prefix="archive")
    destination = RecordingChannel(40, "一言メモ")
    message = _message(source, "x" * 2_050)
    message.guild.text_channels = [archive, destination]
    first_bot = LifeInboxBot(
        settings,
        service,
        inbox_classifier=StaticClassifier(InboxCategory.MEMO),
    )
    first_bot.get_channel = MagicMock(return_value=None)
    first_bot.fetch_channel = AsyncMock(return_value=None)
    original_send = archive.send.side_effect
    calls = 0

    async def fail_second_send(content: str, **kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("temporary archive failure")
        return await original_send(content, **kwargs)

    archive.send.side_effect = fail_second_send
    try:
        captured = await service.capture_discord_message_raw(message)
        first = await first_bot._archive_general_inbox_message(message, captured.event_id)
        assert first.succeeded is False
    finally:
        await first_bot.close()

    second_bot = LifeInboxBot(
        settings,
        service,
        inbox_classifier=StaticClassifier(InboxCategory.MEMO),
    )
    second_bot.get_channel = MagicMock(return_value=None)
    second_bot.fetch_channel = AsyncMock(return_value=archive)
    try:
        second = await second_bot._archive_general_inbox_message(message, captured.event_id)
    finally:
        await second_bot.close()

    assert second.succeeded is True
    assert archive.send.await_count == 3
    with db.session() as session:
        record = session.scalar(select(DiscordArchiveRecord))
        assert record is not None
        assert record.status == "succeeded"
        assert len(record.archive_message_ids) == 2


@pytest.mark.asyncio
async def test_general_route_rechecks_archive_before_destination_and_deletion(db, tmp_path):
    settings = _settings(tmp_path)
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    bot = LifeInboxBot(
        settings,
        service,
        inbox_classifier=StaticClassifier(InboxCategory.MEMO),
    )
    source = RecordingChannel(30, "一般")
    archive = RecordingChannel(31, "一般アーカイブ")
    destination = RecordingChannel(40, "一言メモ")
    _wire_channels(bot, archive, destination)
    message = _message(source, "メモ: アーカイブ再確認")

    try:
        await bot.on_message(message)
        with db.session() as session:
            record = session.scalar(select(DiscordArchiveRecord))
            assert record is not None
            record.status = "failed"
            session.commit()
        view = source.sent[0][1]["view"]
        choice = next(child for child in view.children if child.label == "一言メモ")
        interaction = _interaction()
        await choice.callback(interaction)
    finally:
        await bot.close()

    destination.send.assert_not_awaited()
    message.delete.assert_not_awaited()
    assert "元投稿は残しています" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_archive_gated_auto_notion_enqueue_is_eligible_and_idempotent(db, tmp_path):
    settings = _settings(
        tmp_path,
        notion_sync_enabled=True,
        notion_api_token="fixture-token",
        notion_parent_page_id="fixture-parent",
        inbox_delete_original_after_route=False,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    notion = NotionSyncService(db.session_factory, settings, data_dir=tmp_path)
    bot = LifeInboxBot(
        settings,
        service,
        inbox_classifier=StaticClassifier(InboxCategory.MEMO),
        notion_sync=notion,
    )
    source = RecordingChannel(30, "一般")
    archive = RecordingChannel(31, "一般アーカイブ")
    destination = RecordingChannel(40, "一言メモ")
    _wire_channels(bot, archive, destination)
    message = _message(source, "メモ: Notionへ")

    try:
        await bot.on_message(message)
        view = source.sent[0][1]["view"]
        choice = next(child for child in view.children if child.label == "一言メモ")
        await choice.callback(_interaction())
    finally:
        await bot.close()

    with db.session() as session:
        records = list(session.scalars(select(NotionSyncRecord)).all())
        assert len(records) == 1
        assert records[0].status == "pending"
        assert session.scalar(select(func.count(DiscordArchiveRecord.id))) == 1


def test_notion_auto_boundary_skips_sensitive_finance_event(db, settings):
    notion = NotionSyncService(db.session_factory, settings, data_dir=settings.data_dir)
    with db.session() as session:
        event = Event(
            discord_message_id="finance-event",
            discord_channel_id="30",
            discord_user_id="10",
            captured_at=datetime(2026, 8, 31, 3, 0),
            occurred_at=datetime(2026, 8, 31, 3, 0),
            raw_text="残高 100000",
            source="discord",
            status="confirmed",
            event_type="finance_account",
            event_kinds=["finance_account"],
        )
        session.add(event)
        session.commit()
        event_id = event.id

    assert notion.enqueue_event_if_eligible(event_id) is None
    with db.session() as session:
        assert session.scalar(select(func.count(NotionSyncRecord.id))) == 0


def test_general_and_legacy_inbox_settings_are_explicit_and_scope_is_compatible():
    general = Settings(
        _env_file=None,
        general_inbox_channel_id=30,
        inbox_channel_id=31,
    )
    assert general.primary_inbox_channel_id == 30
    assert general.legacy_inbox_channel_id == 31
    assert general.safe_summary()["general_inbox_channel_id"] == 30
    assert general.safe_summary()["legacy_inbox_channel_id"] == 31

    aliased = Settings(_env_file=None, general_inbox_channel_id=30, inbox_channel_id=30)
    assert aliased.primary_inbox_channel_id == 30
    assert aliased.legacy_inbox_channel_id is None

    old_only = Settings(_env_file=None, inbox_channel_id=30)
    assert old_only.primary_inbox_channel_id == 30
    assert old_only.legacy_inbox_channel_id is None

    guard = ScopeGuard(10, 20, 30, legacy_channel_id=31)
    message = SimpleNamespace(
        author=SimpleNamespace(id=10, bot=False),
        guild=SimpleNamespace(id=20),
        channel=SimpleNamespace(id=31),
        webhook_id=None,
    )
    assert guard.message_role(message) == "inbox_compat"
    assert guard.allows_message(message)
