from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from life_inbox.config import Settings
from life_inbox.discord_bot import (
    EventActionView,
    InboxCandidateView,
    LifeInboxBot,
    SocialDraftView,
    inbox_category_for_reaction,
)
from life_inbox.inbox_classifier import (
    InboxCandidate,
    InboxCategory,
    InboxClassification,
    InboxClassifier,
    InboxClassifierOutput,
    InboxImageInput,
)
from life_inbox.models import CareerOpportunity, Event, LifeTask, Memo, RhythmTodo
from life_inbox.rhythm_spending_models import (
    RhythmSpendingEventLink,
    RhythmSpendingEventRow,
)
from life_inbox.schemas import ReceiptAnalysisResult
from life_inbox.services import InboxService
from life_inbox.social_models import SocialDraft


class FakeResponses:
    def __init__(self, parsed=None, error: Exception | None = None):
        self.parsed = parsed
        self.error = error
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(output_parsed=self.parsed)


def test_candidate_reaction_normalizes_unicode_variation_selector_and_rejects_custom_emoji():
    assert inbox_category_for_reaction(discord.PartialEmoji.from_str("🍽️")) is (
        InboxCategory.DINING_OUT
    )
    assert inbox_category_for_reaction(discord.PartialEmoji.from_str("🍽")) is (
        InboxCategory.DINING_OUT
    )
    assert inbox_category_for_reaction(discord.PartialEmoji(name="memo", id=123)) is None


class StaticClassifier:
    def __init__(self, result: InboxClassification):
        self.result = result

    def classify(
        self,
        text: str,
        *,
        attachment_count: int = 0,
        images=(),
    ) -> InboxClassification:
        return self.result


def _message(message_id: str, content: str, *, attachments=None):
    source_channel = SimpleNamespace(id=30, name="なんでも入力", send=AsyncMock())
    return SimpleNamespace(
        id=message_id,
        content=content,
        author=SimpleNamespace(id=10, bot=False),
        guild=SimpleNamespace(id=20, text_channels=[]),
        channel=source_channel,
        attachments=attachments or [],
        created_at=datetime(2026, 8, 30, 3, 0),
        add_reaction=AsyncMock(),
        delete=AsyncMock(),
    )


def _interaction():
    response = SimpleNamespace(
        is_done=MagicMock(return_value=False),
        send_message=AsyncMock(),
    )
    return SimpleNamespace(
        user=SimpleNamespace(id=10),
        guild_id=20,
        channel_id=30,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _candidate_choice(message, label: str):
    view = message.channel.send.await_args.kwargs["view"]
    return next(child for child in view.children if child.label == label)


class _DeferredInteractionResponse:
    def __init__(self) -> None:
        self.done = False
        self.send_message = AsyncMock()
        self.defer = AsyncMock(side_effect=self._mark_done)

    def _mark_done(self, **_kwargs) -> None:
        self.done = True

    def is_done(self) -> bool:
        return self.done


def test_local_classifier_covers_reviewed_destinations_and_keeps_unknown_retryable():
    cases = {
        "680円 レシート": InboxCategory.EXPENSES,
        "昼ご飯 ラーメン": InboxCategory.MEALS,
        "メモ: 研究の気づき": InboxCategory.MEMO,
        "独り言: 今日はちょっと疲れた": InboxCategory.SELF_TALK,
        "ひとりごと: 明日は早起きしたい": InboxCategory.SELF_TALK,
        "つぶやき: 今日は静かに過ごしたい": InboxCategory.SELF_TALK,
        "これは独り言": InboxCategory.SELF_TALK,
        "外食: サイゼリヤ 1200円": InboxCategory.DINING_OUT,
        "レストランで夕食 1800円": InboxCategory.DINING_OUT,
        "思い出として残す: 初めてAJを取った日": InboxCategory.MEMORIES,
        "今日の投稿: 今日も一日おつかれさま": InboxCategory.SOCIAL_DRAFT,
        "期限 2026-09-05 に提出": InboxCategory.TASK,
        "牛乳を買う": InboxCategory.SHOPPING,
        "求人 面接の日程": InboxCategory.JOB_HUNTING,
        "https://example.com/reference": InboxCategory.SAVED_LINK,
        "マイ 600": InboxCategory.RHYTHM_SPENDING,
        "maimai SSS+": InboxCategory.RHYTHM_PROGRESS,
        "口座から入金": InboxCategory.FINANCE_ACCOUNT,
        "クレカの利用明細": InboxCategory.FINANCE_CARD,
    }
    for text, expected in cases.items():
        result = InboxClassifier.local(text)
        assert result.primary is not None
        assert result.primary.category is expected
        assert len(result.candidates) <= 3

    unknown = InboxClassifier.local("意味のない断片")
    assert unknown.is_unknown is True
    assert unknown.primary is not None
    assert unknown.primary.category is InboxCategory.UNKNOWN


def test_local_classifier_does_not_infer_review_categories_from_generic_text_or_attachments():
    assert InboxClassifier.local("メモ: 思い出した研究の気づき").primary is not None
    assert (
        InboxClassifier.local("メモ: 思い出した研究の気づき").primary.category
        is InboxCategory.MEMO
    )
    assert InboxClassifier.local("なんとなく疲れた").is_unknown is True
    assert InboxClassifier.local("なんとなく疲れた", attachment_count=1).is_unknown is True
    assert InboxClassifier.local("昼ご飯 自炊").primary is not None
    assert InboxClassifier.local("昼ご飯 自炊").primary.category is InboxCategory.MEALS


def test_classifier_output_is_closed_and_unknown_cannot_be_mixed():
    with pytest.raises(ValidationError):
        InboxClassifierOutput(
            candidates=[
                {"category": "memo", "confidence": 0.9, "reason": "秘密"},
            ]
        )
    with pytest.raises(ValidationError):
        InboxClassifierOutput(
            candidates=[
                {"category": "unknown", "confidence": 0.9},
                {"category": "memo", "confidence": 0.1},
            ]
        )


def test_openai_classifier_uses_bounded_strict_request_without_secret_in_payload():
    responses = FakeResponses(
        parsed={
            "candidates": [
                {"category": "saved_link", "confidence": 0.94},
                {"category": "job_hunting", "confidence": 0.52},
            ]
        }
    )
    classifier = InboxClassifier(
        api_key="classifier-secret",
        model="fixture-model",
        timeout_seconds=3,
        client=SimpleNamespace(responses=responses),
    )

    result = classifier.classify("https://example.com/private", attachment_count=1)

    assert result.source == "openai"
    assert [candidate.category for candidate in result.candidates] == [
        InboxCategory.SAVED_LINK,
        InboxCategory.JOB_HUNTING,
    ]
    call = responses.calls[0]
    assert call["model"] == "fixture-model"
    assert call["store"] is False
    assert call["timeout"] == 3
    assert call["text_format"] is InboxClassifierOutput
    assert "classifier-secret" not in repr(call)
    assert "https://example.com/private" in repr(call)
    assert "tools" not in call
    prompt = call["input"][0]["content"][0]["text"]
    assert "self_talk" in prompt
    assert "dining_out" in prompt
    assert "memories" in prompt
    assert "social_draft" in prompt
    assert "rhythm_spending" in prompt


def test_openai_classifier_failure_uses_local_fallback_without_provider_text():
    provider_message = "provider-private-response classifier-secret"
    responses = FakeResponses(error=RuntimeError(provider_message))
    classifier = InboxClassifier(
        api_key="classifier-secret",
        client=SimpleNamespace(responses=responses),
    )

    result = classifier.classify("https://example.com/article")

    assert result.source == "local_fallback"
    assert result.primary is not None
    assert result.primary.category is InboxCategory.SAVED_LINK
    assert result.fallback_reason == "RuntimeError"
    assert provider_message not in repr(result)


def test_openai_classifier_sends_bounded_image_and_returns_planner_items(tmp_path):
    image_path = tmp_path / "planner.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    responses = FakeResponses(
        parsed={
            "candidates": [
                {"category": "task", "confidence": 0.97},
                {"category": "memo", "confidence": 0.62},
            ],
            "document_kind": "planner",
            "planner_items": [
                {
                    "text": "9月3日 研究計画書を提出",
                    "candidates": [
                        {"category": "task", "confidence": 0.96},
                        {"category": "job_hunting", "confidence": 0.44},
                    ],
                }
            ],
        }
    )
    classifier = InboxClassifier(
        api_key="classifier-secret",
        model="gpt-5.6-luna",
        client=SimpleNamespace(responses=responses),
    )

    result = classifier.classify(
        "今日の手帳",
        attachment_count=1,
        images=(InboxImageInput(image_path, "image/png"),),
    )

    assert result.source == "openai"
    assert result.document_kind == "planner"
    assert result.planner_items[0].text == "9月3日 研究計画書を提出"
    assert result.planner_items[0].candidates[0].category is InboxCategory.TASK
    user_blocks = responses.calls[0]["input"][1]["content"]
    assert user_blocks[1]["type"] == "input_image"
    assert user_blocks[1]["image_url"].startswith("data:image/png;base64,")
    assert "classifier-secret" not in repr(responses.calls[0])


def test_classifier_settings_are_optional_and_secret_safe(monkeypatch):
    monkeypatch.setenv("OPENAI_INBOX_CLASSIFIER_API_KEY", "classifier-secret")
    monkeypatch.setenv("OPENAI_INBOX_CLASSIFIER_TIMEOUT_SECONDS", "4")
    settings = Settings(_env_file=None)

    assert settings.openai_inbox_classifier_api_key is not None
    assert settings.openai_inbox_classifier_timeout_seconds == 4
    assert settings.safe_summary()["openai_inbox_classifier_configured"] is True
    assert "classifier-secret" not in repr(settings.safe_summary())


@pytest.mark.asyncio
async def test_candidate_button_sends_copy_before_deleting_and_second_click_is_noop(
    db, tmp_path
):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        records_channel_id=40,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classification = InboxClassification(
        candidates=(InboxCandidate(category=InboxCategory.EXPENSES, confidence=0.9),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=40,
        name="生活-記録",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(),
    )
    bot.get_channel = MagicMock(return_value=destination)
    message = _message("candidate-1", "680円 マック")

    try:
        await bot.on_message(message)
        view = message.channel.send.await_args.kwargs["view"]
        choice = next(
            child
            for child in view.children
            if isinstance(child, discord.ui.Button) and child.label == "支出・レシート"
        )

        def interaction():
            response = SimpleNamespace(
                is_done=MagicMock(return_value=False),
                send_message=AsyncMock(),
            )
            return SimpleNamespace(
                user=SimpleNamespace(id=10),
                guild_id=20,
                channel_id=30,
                response=response,
                followup=SimpleNamespace(send=AsyncMock()),
            )

        first = interaction()
        await choice.callback(first)
        second = interaction()
        await choice.callback(second)
    finally:
        await bot.close()

    destination.send.assert_awaited_once()
    destination_text = destination.send.await_args.args[0]
    assert "Event #1" in destination_text
    assert "680円 マック" in destination_text
    message.delete.assert_awaited_once_with()
    assert "支出・レシートへ振り分けました" in first.response.send_message.await_args.args[0]
    assert "すでに終了" in second.response.send_message.await_args.args[0]
    with db.session() as session:
        event = session.scalar(select(Event).where(Event.discord_message_id == "candidate-1"))
        assert event is not None
        assert len(event.expenses) == 1
        assert event.expenses[0].amount == 680


@pytest.mark.asyncio
async def test_candidate_button_defers_before_structured_write_and_uses_followup(db, tmp_path):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        records_channel_id=40,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / 'candidate-defer.db'}",
        data_dir=tmp_path,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classification = InboxClassification(
        candidates=(InboxCandidate(category=InboxCategory.EXPENSES, confidence=0.99),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=40,
        name="生活-記録",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(),
    )
    bot.get_channel = MagicMock(return_value=destination)
    message = _message("candidate-defer", "730円 マック")
    response = _DeferredInteractionResponse()
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=10),
        guild_id=20,
        channel_id=30,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )

    try:
        await bot.on_message(message)
        choice = _candidate_choice(message, "支出・レシート")
        await choice.callback(interaction)
    finally:
        await bot.close()

    response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    response.send_message.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()
    assert "支出・レシートへ振り分けました" in (
        interaction.followup.send.await_args.args[0]
    )
    destination.send.assert_awaited_once()
    assert isinstance(destination.send.await_args.kwargs["view"], EventActionView)
    message.delete.assert_awaited_once_with()
    with db.session() as session:
        event = session.scalar(
            select(Event).where(Event.discord_message_id == "candidate-defer")
        )
        assert event is not None
        assert len(event.expenses) == 1
        assert event.expenses[0].amount == 730


@pytest.mark.asyncio
async def test_candidate_button_with_expired_defer_keeps_source_and_does_not_route(
    db, tmp_path
):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        records_channel_id=40,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / 'candidate-expired.db'}",
        data_dir=tmp_path,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classification = InboxClassification(
        candidates=(InboxCandidate(category=InboxCategory.EXPENSES, confidence=0.99),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=40,
        name="生活-記録",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(),
    )
    bot.get_channel = MagicMock(return_value=destination)
    message = _message("candidate-expired", "680円 マック")
    response = SimpleNamespace(
        is_done=MagicMock(return_value=False),
        send_message=AsyncMock(),
        defer=AsyncMock(
            side_effect=discord.NotFound(
                SimpleNamespace(status=404, reason="expired"),
                "expired interaction",
            )
        ),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=10),
        guild_id=20,
        channel_id=30,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )

    try:
        await bot.on_message(message)
        choice = _candidate_choice(message, "支出・レシート")
        await choice.callback(interaction)
    finally:
        await bot.close()

    response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    response.send_message.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()
    destination.send.assert_not_awaited()
    message.delete.assert_not_awaited()
    with db.session() as session:
        event = session.scalar(
            select(Event).where(Event.discord_message_id == "candidate-expired")
        )
        assert event is not None
        assert event.status == "pending"
        assert event.expenses == []


@pytest.mark.asyncio
async def test_candidate_route_reconstructed_view_does_not_duplicate_destination(
    db, tmp_path
):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        records_channel_id=40,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / 'candidate-reconstructed.db'}",
        data_dir=tmp_path,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classification = InboxClassification(
        candidates=(InboxCandidate(category=InboxCategory.EXPENSES, confidence=0.99),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=40,
        name="生活-記録",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(),
    )
    bot.get_channel = MagicMock(return_value=destination)
    message = _message("candidate-reconstructed", "680円 マック")

    try:
        await bot.on_message(message)
        first_choice = _candidate_choice(message, "支出・レシート")
        await first_choice.callback(_interaction())

        reconstructed = InboxCandidateView(bot, message, 1, classification)
        second_choice = next(
            child for child in reconstructed.children if child.label == "支出・レシート"
        )
        await second_choice.callback(_interaction())
    finally:
        await bot.close()

    destination.send.assert_awaited_once()
    message.delete.assert_awaited_once_with()
    with db.session() as session:
        assert session.scalar(select(func.count(Event.id))) == 1
        event = session.scalar(
            select(Event).where(Event.discord_message_id == "candidate-reconstructed")
        )
        assert event is not None
        assert len(event.expenses) == 1


@pytest.mark.asyncio
async def test_candidate_destination_failure_keeps_source_and_allows_retry(db, tmp_path):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        records_channel_id=40,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classification = InboxClassification(
        candidates=(InboxCandidate(category=InboxCategory.EXPENSES, confidence=0.9),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=40,
        name="生活-記録",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(side_effect=RuntimeError("private destination failure")),
    )
    bot.get_channel = MagicMock(return_value=destination)
    message = _message("candidate-failure", "保存先を試す")

    try:
        await bot.on_message(message)
        view = message.channel.send.await_args.kwargs["view"]
        choice = next(child for child in view.children if child.label == "支出・レシート")
        response = SimpleNamespace(
            is_done=MagicMock(return_value=False),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10),
            guild_id=20,
            channel_id=30,
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await choice.callback(interaction)
    finally:
        await bot.close()

    message.delete.assert_not_awaited()
    assert "元投稿は残しています" in response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_candidate_copy_includes_saved_attachment_file_before_delete(db, tmp_path):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        records_channel_id=40,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
    )
    analyzer = MagicMock()
    analyzer.analyze_text.return_value = {}
    analyzer.analyze_receipt.return_value = ReceiptAnalysisResult(
        merchant="マック",
        total=680,
        confidence=0.95,
    )
    service = InboxService(db.session_factory, settings, analyzer=analyzer)
    classification = InboxClassification(
        candidates=(InboxCandidate(category=InboxCategory.EXPENSES, confidence=0.9),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=40,
        name="生活-記録",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(),
    )
    bot.get_channel = MagicMock(return_value=destination)
    attachment = SimpleNamespace(
        id="receipt-1",
        filename="receipt.png",
        content_type="image/png",
        size=8,
        read=AsyncMock(return_value=b"png-bytes"),
    )
    message = _message("candidate-attachment", "レシート", attachments=[attachment])

    try:
        await bot.on_message(message)
        view = message.channel.send.await_args.kwargs["view"]
        choice = next(child for child in view.children if child.label == "支出・レシート")
        response = SimpleNamespace(
            is_done=MagicMock(return_value=False),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10),
            guild_id=20,
            channel_id=30,
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await choice.callback(interaction)
    finally:
        await bot.close()

    send_kwargs = destination.send.await_args.kwargs
    assert len(send_kwargs["files"]) == 1
    assert send_kwargs["files"][0].filename == "receipt.png"
    message.delete.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_candidate_meals_persists_meal_with_explicit_intent_without_rewriting_raw_text(
    db, tmp_path
):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        meal_channel_id=40,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / 'candidate-meal.db'}",
        data_dir=tmp_path,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classification = InboxClassification(
        candidates=(InboxCandidate(category=InboxCategory.MEALS, confidence=0.99),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=40,
        name="食事",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(),
    )
    bot.get_channel = MagicMock(return_value=destination)
    message = _message("candidate-meal", "昼ご飯 ラーメン")

    try:
        await bot.on_message(message)
        choice = _candidate_choice(message, "食事")
        interaction = _interaction()
        await choice.callback(interaction)
    finally:
        await bot.close()

    with db.session() as session:
        event = session.scalar(select(Event).where(Event.discord_message_id == "candidate-meal"))
        assert event is not None
        assert event.raw_text == "昼ご飯 ラーメン"
        assert len(event.meals) == 1
        assert event.meals[0].meal_type == "lunch"
    destination.send.assert_awaited_once()
    message.delete.assert_awaited_once_with()
    assert "構造化保存" in destination.send.await_args.args[0]


@pytest.mark.asyncio
async def test_candidate_memo_uses_durable_body_and_retry_does_not_duplicate(db, tmp_path):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        memo_channel_id=41,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / 'candidate-memo.db'}",
        data_dir=tmp_path,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classification = InboxClassification(
        candidates=(InboxCandidate(category=InboxCategory.MEMO, confidence=0.99),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=41,
        name="一言メモ",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(side_effect=[RuntimeError("destination unavailable"), None]),
    )
    bot.get_channel = MagicMock(return_value=destination)
    message = _message("candidate-memo", " 研究の気づき ")

    try:
        await bot.on_message(message)
        choice = _candidate_choice(message, "一言メモ")
        first = _interaction()
        await choice.callback(first)
        second = _interaction()
        await choice.callback(second)
    finally:
        await bot.close()

    with db.session() as session:
        event = session.scalar(select(Event).where(Event.discord_message_id == "candidate-memo"))
        assert event is not None
        assert event.memo is not None
        assert event.memo.body == " 研究の気づき "
        assert session.scalar(select(func.count(Memo.id))) == 1
    assert destination.send.await_count == 2
    message.delete.assert_awaited_once_with()
    assert "元投稿は残しています" in first.response.send_message.await_args.args[0]
    assert "一言メモへ振り分けました" in second.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_candidate_task_uses_grounded_route_and_creates_one_life_task(db, tmp_path):
    analyzer = MagicMock()
    analyzer.route_inbox.return_value = {
        "destination": "life_task",
        "task_title": "研究計画書を提出",
        "task_due_date": "2026-09-05",
        "task_due_time": "15:00",
    }
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        reminder_channel_id=42,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / 'candidate-task.db'}",
        data_dir=tmp_path,
    )
    service = InboxService(db.session_factory, settings, analyzer=analyzer)
    classification = InboxClassification(
        candidates=(InboxCandidate(category=InboxCategory.TASK, confidence=0.99),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=42,
        name="日課・リマインダー",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(),
    )
    bot.get_channel = MagicMock(return_value=destination)
    message = _message("candidate-task", "研究計画書を提出 2026-09-05 15:00")

    try:
        await bot.on_message(message)
        choice = _candidate_choice(message, "タスク・期限")
        first = _interaction()
        await choice.callback(first)
        second = _interaction()
        await choice.callback(second)
    finally:
        await bot.close()

    with db.session() as session:
        event = session.scalar(select(Event).where(Event.discord_message_id == "candidate-task"))
        assert event is not None
        assert event.event_type == "life_task"
        assert event.status == "confirmed"
        tasks = list(session.scalars(select(LifeTask)).all())
        assert len(tasks) == 1
        assert tasks[0].title == "研究計画書を提出"
        assert tasks[0].due_at == datetime(2026, 9, 5, 6, 0)
    analyzer.route_inbox.assert_called_once_with("研究計画書を提出 2026-09-05 15:00")
    destination.send.assert_awaited_once()
    message.delete.assert_awaited_once_with()
    assert "すでに終了" in second.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_candidate_job_hunting_uses_grounded_existing_career_row(db, tmp_path):
    analyzer = MagicMock()
    analyzer.route_inbox.return_value = {
        "destination": "career",
        "career_company": "Acme",
        "career_title": "バックエンドエンジニア",
        "career_kind": "job",
        "career_deadline_date": "2026-09-05",
    }
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / 'candidate-career.db'}",
        data_dir=tmp_path,
    )
    service = InboxService(db.session_factory, settings, analyzer=analyzer)
    classification = InboxClassification(
        candidates=(InboxCandidate(category=InboxCategory.JOB_HUNTING, confidence=0.99),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=43,
        name="就活情報",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(),
    )
    bot.get_channel = MagicMock(return_value=None)
    message = _message("candidate-career", "Acme バックエンドエンジニア 2026-09-05")
    message.guild.text_channels = [destination]

    try:
        await bot.on_message(message)
        choice = _candidate_choice(message, "就活情報")
        interaction = _interaction()
        await choice.callback(interaction)
    finally:
        await bot.close()

    with db.session() as session:
        event = session.scalar(select(Event).where(Event.discord_message_id == "candidate-career"))
        assert event is not None
        assert event.event_type == "career"
        assert session.scalar(select(func.count(CareerOpportunity.id))) == 1
    destination.send.assert_awaited_once()
    message.delete.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_candidate_rhythm_progress_uses_existing_parser_for_todo(db, tmp_path):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        progress_channel_id=44,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / 'candidate-rhythm.db'}",
        data_dir=tmp_path,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classification = InboxClassification(
        candidates=(InboxCandidate(category=InboxCategory.RHYTHM_PROGRESS, confidence=0.99),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=44,
        name="今日の進捗",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(),
    )
    bot.get_channel = MagicMock(return_value=destination)
    message = _message("candidate-rhythm", "todo maimai SSS+を詰める")

    try:
        await bot.on_message(message)
        choice = _candidate_choice(message, "音ゲー進捗・リザルト")
        interaction = _interaction()
        await choice.callback(interaction)
    finally:
        await bot.close()

    with db.session() as session:
        event = session.scalar(select(Event).where(Event.discord_message_id == "candidate-rhythm"))
        assert event is not None
        assert event.rhythm_todo is not None
        assert event.rhythm_todo.game == "maimai"
        assert event.rhythm_todo.title == "SSS+を詰める"
        assert session.scalar(select(func.count(RhythmTodo.id))) == 1
    destination.send.assert_awaited_once()
    message.delete.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_candidate_rhythm_spending_posts_yen_once_and_links_notion_event(db, tmp_path):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        discord_credit_channel_id=47,
        inbox_delete_original_after_route=True,
        rhythm_enabled=True,
        rhythm_discord_dry_run=False,
        rhythm_spending_immediate_post_enabled=True,
        rhythm_spending_post_mode="eachEvent",
        database_url=f"sqlite:///{tmp_path / 'candidate-rhythm-spending.db'}",
        data_dir=tmp_path,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classification = InboxClassification(
        candidates=(
            InboxCandidate(category=InboxCategory.RHYTHM_SPENDING, confidence=0.99),
        ),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=47,
        name="音ゲー利用額",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(return_value=SimpleNamespace(id=47001)),
    )
    bot.get_channel = MagicMock(return_value=destination)
    message = _message("candidate-rhythm-spending", "マイ 600")

    try:
        await bot.on_message(message)
        choice = _candidate_choice(message, "音ゲー利用額")
        interaction = _interaction()
        await choice.callback(interaction)
    finally:
        await bot.close()

    destination.send.assert_awaited_once()
    content = destination.send.await_args.kwargs["content"]
    assert "【利用記録】" in content
    assert "マイ ¥600" in content
    assert "cr" not in content.casefold()
    message.delete.assert_awaited_once_with()
    with db.session() as session:
        event = session.scalar(
            select(Event).where(
                Event.discord_message_id == "candidate-rhythm-spending"
            )
        )
        assert event is not None
        assert event.status == "confirmed"
        assert event.event_type == "rhythm_spending"
        assert session.scalar(select(func.count(RhythmSpendingEventRow.id))) == 1
        assert session.scalar(select(func.count(RhythmSpendingEventLink.event_id))) == 1
        assert event.rhythm_spending_link.spending_event.discord_message_id == "47001"


@pytest.mark.asyncio
async def test_candidate_unsafe_rhythm_input_stays_retryable_without_send_or_delete(db, tmp_path):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        progress_channel_id=45,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / 'candidate-unsafe.db'}",
        data_dir=tmp_path,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classification = InboxClassification(
        candidates=(InboxCandidate(category=InboxCategory.RHYTHM_PROGRESS, confidence=0.99),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=45,
        name="今日の進捗",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(),
    )
    bot.get_channel = MagicMock(return_value=destination)
    message = _message("candidate-unsafe", "進捗だけ")

    try:
        await bot.on_message(message)
        choice = _candidate_choice(message, "音ゲー進捗・リザルト")
        interaction = _interaction()
        await choice.callback(interaction)
    finally:
        await bot.close()

    with db.session() as session:
        assert session.scalar(select(func.count(RhythmTodo.id))) == 0
        event = session.scalar(select(Event).where(Event.discord_message_id == "candidate-unsafe"))
        assert event is not None
        assert event.status == "pending"
    destination.send.assert_not_awaited()
    message.delete.assert_not_awaited()
    assert "元投稿は残しています" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_candidate_expense_send_failure_keeps_source_then_retries_without_duplicate(
    db, tmp_path
):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        records_channel_id=46,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / 'candidate-send-retry.db'}",
        data_dir=tmp_path,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classification = InboxClassification(
        candidates=(InboxCandidate(category=InboxCategory.EXPENSES, confidence=0.99),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=46,
        name="生活-記録",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(side_effect=[RuntimeError("destination unavailable"), None]),
    )
    bot.get_channel = MagicMock(return_value=destination)
    message = _message("candidate-send-retry", "680円 マック")

    try:
        await bot.on_message(message)
        choice = _candidate_choice(message, "支出・レシート")
        first = _interaction()
        await choice.callback(first)
        second = _interaction()
        await choice.callback(second)
    finally:
        await bot.close()

    with db.session() as session:
        event = session.scalar(
            select(Event).where(Event.discord_message_id == "candidate-send-retry")
        )
        assert event is not None
        assert len(event.expenses) == 1
        assert session.scalar(select(func.count(Event.id))) == 1
    assert destination.send.await_count == 2
    message.delete.assert_awaited_once_with()
    assert "元投稿は残しています" in first.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_candidate_social_draft_keeps_outbox_choice_and_attachment_copy_behavior(
    db, tmp_path
):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        social_draft_channel_id=47,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / 'candidate-social.db'}",
        data_dir=tmp_path,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classification = InboxClassification(
        candidates=(InboxCandidate(category=InboxCategory.SOCIAL_DRAFT, confidence=0.99),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=47,
        name="毎日の投稿",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(return_value=SimpleNamespace(id=900)),
    )
    bot.get_channel = MagicMock(return_value=destination)
    message = _message("candidate-social", "今日の投稿: 今日は休む")

    try:
        await bot.on_message(message)
        choice = _candidate_choice(message, "毎日の投稿")
        interaction = _interaction()
        await choice.callback(interaction)
    finally:
        await bot.close()

    with db.session() as session:
        event = session.scalar(select(Event).where(Event.discord_message_id == "candidate-social"))
        assert event is not None
        draft = session.scalar(select(SocialDraft).where(SocialDraft.event_id == event.id))
        assert draft is not None
        assert draft.target == "pending"
        assert draft.state == "pending"
        assert draft.discord_action_channel_id == "47"
        assert draft.discord_action_message_id == "900"
    destination.send.assert_awaited_once()
    assert isinstance(destination.send.await_args.kwargs["view"], SocialDraftView)
    assert "SNS下書き" in destination.send.await_args.args[0]
    assert "外部SNSには投稿していません" in destination.send.await_args.args[0]
    message.delete.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "label", "settings_field"),
    [
        (InboxCategory.SELF_TALK, "独り言", "monologue_channel_id"),
        (InboxCategory.DINING_OUT, "外食", "dining_out_channel_id"),
        (InboxCategory.MEMORIES, "思い出", "memories_channel_id"),
        (InboxCategory.SOCIAL_DRAFT, "毎日の投稿", "social_draft_channel_id"),
    ],
)
async def test_new_reviewed_candidate_routes_use_configured_ids_and_are_idempotent(
    db, tmp_path, category, label, settings_field
):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / (category.value + '.db')}",
        data_dir=tmp_path,
        **{settings_field: 41},
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classification = InboxClassification(
        candidates=(InboxCandidate(category=category, confidence=0.99),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=41,
        name=label,
        guild=SimpleNamespace(id=20),
        send=AsyncMock(),
    )
    bot.get_channel = MagicMock(return_value=destination)
    message = _message(f"candidate-{category.value}", f"{label}のテスト")

    def interaction():
        response = SimpleNamespace(
            is_done=MagicMock(return_value=False),
            send_message=AsyncMock(),
        )
        return SimpleNamespace(
            user=SimpleNamespace(id=10),
            guild_id=20,
            channel_id=30,
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )

    try:
        await bot.on_message(message)
        view = message.channel.send.await_args.kwargs["view"]
        choice = next(child for child in view.children if child.label == label)
        first = interaction()
        await choice.callback(first)
        second = interaction()
        await choice.callback(second)
    finally:
        await bot.close()

    bot.get_channel.assert_called_once_with(41)
    destination.send.assert_awaited_once()
    assert label in first.response.send_message.await_args.args[0]
    assert "すでに終了" in second.response.send_message.await_args.args[0]
    message.delete.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "label"),
    [
        (InboxCategory.SELF_TALK, "独り言"),
        (InboxCategory.DINING_OUT, "外食"),
        (InboxCategory.MEMORIES, "思い出"),
        (InboxCategory.SOCIAL_DRAFT, "毎日の投稿"),
    ],
)
async def test_new_reviewed_candidate_routes_use_exact_name_fallback(
    db, tmp_path, category, label
):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / (category.value + '-fallback.db')}",
        data_dir=tmp_path,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classification = InboxClassification(
        candidates=(InboxCandidate(category=category, confidence=0.99),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=41,
        name=label,
        guild=SimpleNamespace(id=20),
        send=AsyncMock(),
    )
    bot.get_channel = MagicMock(return_value=None)
    message = _message(f"candidate-{category.value}-fallback", f"{label}のテスト")
    message.guild.text_channels = [destination]

    try:
        await bot.on_message(message)
        view = message.channel.send.await_args.kwargs["view"]
        choice = next(child for child in view.children if child.label == label)
        response = SimpleNamespace(
            is_done=MagicMock(return_value=False),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10),
            guild_id=20,
            channel_id=30,
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await choice.callback(interaction)
    finally:
        await bot.close()

    bot.get_channel.assert_not_called()
    destination.send.assert_awaited_once()
    message.delete.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_reviewed_destination_fallback_fails_closed_when_exact_name_is_missing_or_duplicated(
    db, tmp_path
):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        database_url=f"sqlite:///{tmp_path / 'destination-safety.db'}",
        data_dir=tmp_path,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(InboxClassification(
        candidates=(InboxCandidate(category=InboxCategory.SELF_TALK, confidence=0.99),),
        source="local",
    )))
    try:
        missing = _message("missing-self-talk", "独り言のテスト")
        with pytest.raises(RuntimeError, match="unavailable"):
            await bot._inbox_destination_channel(missing, InboxCategory.SELF_TALK)

        duplicate = _message("duplicate-self-talk", "独り言のテスト")
        duplicate.guild.text_channels = [
            SimpleNamespace(id=41, name="独り言", guild=SimpleNamespace(id=20), send=AsyncMock()),
            SimpleNamespace(id=42, name="独り言", guild=SimpleNamespace(id=20), send=AsyncMock()),
        ]
        with pytest.raises(RuntimeError, match="unavailable"):
            await bot._inbox_destination_channel(duplicate, InboxCategory.SELF_TALK)
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_candidate_prompt_adds_reactions_without_disabling_buttons_on_add_failure(
    db, tmp_path
):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / 'reaction-affordance.db'}",
        data_dir=tmp_path,
        memo_channel_id=41,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classification = InboxClassification(
        candidates=(
            InboxCandidate(category=InboxCategory.MEMO, confidence=0.99),
            InboxCandidate(category=InboxCategory.SELF_TALK, confidence=0.8),
        ),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=41,
        name="一言メモ",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(),
    )
    bot.get_channel = MagicMock(return_value=destination)
    message = _message("reaction-affordance", "メモ: リアクション対応")
    message.add_reaction.side_effect = [RuntimeError("reaction permission denied"), None]

    try:
        await bot.on_message(message)
        candidate_prompt = message.channel.send.await_args.args[0]
        view = message.channel.send.await_args.kwargs["view"]
        choice = next(child for child in view.children if child.label == "一言メモ")
        response = SimpleNamespace(
            is_done=MagicMock(return_value=False),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10, bot=False),
            guild_id=20,
            channel_id=30,
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await choice.callback(interaction)
    finally:
        await bot.close()

    assert [call.args[0] for call in message.add_reaction.await_args_list] == ["📝", "💭"]
    assert "候補: 📝 一言メモ / 💭 独り言" in candidate_prompt
    destination.send.assert_awaited_once()
    message.delete.assert_awaited_once_with()
    assert "一言メモへ振り分けました" in response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_candidate_reaction_routes_only_displayed_candidate_in_configured_scope(
    db, tmp_path
):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / 'reaction-routing.db'}",
        data_dir=tmp_path,
        memo_channel_id=41,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classification = InboxClassification(
        candidates=(InboxCandidate(category=InboxCategory.MEMO, confidence=0.99),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=41,
        name="一言メモ",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(),
    )
    bot.get_channel = MagicMock(return_value=destination)
    message = _message("reaction-routing", "メモ: 正しいリアクション")

    def payload(*, user_id=10, guild_id=20, channel_id=30, emoji="📝", bot=False):
        return SimpleNamespace(
            message_id=message.id,
            user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
            emoji=discord.PartialEmoji.from_str(emoji),
            member=SimpleNamespace(bot=bot),
        )

    try:
        await bot.on_message(message)
        await bot.on_raw_reaction_add(payload(emoji="💭"))
        await bot.on_raw_reaction_add(payload(user_id=11))
        await bot.on_raw_reaction_add(payload(guild_id=21))
        await bot.on_raw_reaction_add(payload(channel_id=31))
        await bot.on_raw_reaction_add(payload(bot=True))
        assert destination.send.await_count == 0
        assert message.delete.await_count == 0

        await bot.on_raw_reaction_add(payload())
        await bot.on_raw_reaction_add(payload())
    finally:
        await bot.close()

    destination.send.assert_awaited_once()
    message.delete.assert_awaited_once_with()
    assert "正しいリアクション" in destination.send.await_args.args[0]


@pytest.mark.asyncio
async def test_candidate_reaction_route_failure_keeps_source_and_allows_retry(db, tmp_path):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        inbox_delete_original_after_route=True,
        database_url=f"sqlite:///{tmp_path / 'reaction-retry.db'}",
        data_dir=tmp_path,
        memo_channel_id=41,
    )
    service = InboxService(db.session_factory, settings, analyzer=MagicMock())
    classification = InboxClassification(
        candidates=(InboxCandidate(category=InboxCategory.MEMO, confidence=0.99),),
        source="local",
    )
    bot = LifeInboxBot(settings, service, inbox_classifier=StaticClassifier(classification))
    destination = SimpleNamespace(
        id=41,
        name="一言メモ",
        guild=SimpleNamespace(id=20),
        send=AsyncMock(side_effect=[RuntimeError("destination unavailable"), None]),
    )
    bot.get_channel = MagicMock(return_value=destination)
    message = _message("reaction-retry", "メモ: 再試行できる")
    payload = SimpleNamespace(
        message_id=message.id,
        user_id=10,
        guild_id=20,
        channel_id=30,
        emoji=discord.PartialEmoji.from_str("📝"),
        member=SimpleNamespace(bot=False, send=AsyncMock()),
    )

    try:
        await bot.on_message(message)
        await bot.on_raw_reaction_add(payload)
        assert message.delete.await_count == 0
        await bot.on_raw_reaction_add(payload)
    finally:
        await bot.close()

    assert destination.send.await_count == 2
    message.delete.assert_awaited_once_with()
    assert payload.member.send.await_count == 2
    assert "元投稿は残しています" in payload.member.send.await_args_list[0].args[0]
    assert "一言メモへ振り分けました" in payload.member.send.await_args_list[1].args[0]
