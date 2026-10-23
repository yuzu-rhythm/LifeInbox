from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from life_inbox.analyzer import NullAnalyzer
from life_inbox.config import Settings
from life_inbox.constants import EventStatus, MealType, OutsideHome, RhythmTodoStatus
from life_inbox.discord_bot import LifeInboxBot, SocialDraftView
from life_inbox.inbox_classifier import InboxCategory
from life_inbox.models import (
    AIRun,
    Attachment,
    CareerApplication,
    CareerOpportunity,
    Event,
    LifeTask,
    Memo,
    RhythmResult,
    RhythmTodo,
)
from life_inbox.schemas import InboxRouteResult, MealAnalysisResult, TextAnalysisResult
from life_inbox.services import INBOX_ROUTE_INPUT_TYPE, InboxService
from life_inbox.social_models import SocialDraft


class QueueAnalyzer:
    provider = "fake"
    model = "fake-router-model"
    prompt_version = "test-analysis-1"
    routing_prompt_version = "test-router-1"

    def __init__(
        self,
        routes: list[InboxRouteResult] | None = None,
        *,
        text_results: list[TextAnalysisResult] | None = None,
        route_error: Exception | None = None,
        on_route=None,
    ) -> None:
        self.routes = list(routes or [])
        self.text_results = list(text_results or [])
        self.route_error = route_error
        self.on_route = on_route
        self.route_inputs: list[str] = []
        self.text_inputs: list[str] = []

    def route_inbox(self, text: str) -> InboxRouteResult:
        self.route_inputs.append(text)
        if self.on_route is not None:
            self.on_route()
        if self.route_error is not None:
            raise self.route_error
        if not self.routes:
            raise AssertionError("unexpected inbox route call")
        return self.routes.pop(0)

    def analyze_text(self, text: str) -> TextAnalysisResult:
        self.text_inputs.append(text)
        if not self.text_results:
            return TextAnalysisResult()
        return self.text_results.pop(0)

    def analyze_receipt(self, path, content_type, *, context_text=""):
        raise AssertionError("unexpected receipt analysis")

    def analyze_meal(self, path, content_type, *, context_text="") -> MealAnalysisResult:
        return MealAnalysisResult()


def _settings(
    tmp_path: Path,
    *,
    delete_original: bool = False,
    **channel_ids: int | None,
) -> Settings:
    return Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
        timezone="Asia/Tokyo",
        inbox_delete_original_after_route=delete_original,
        **channel_ids,
    )


def _message(
    message_id: str,
    content: str,
    *,
    reference_id: str | None = None,
    attachments: list[object] | None = None,
):
    channel = SimpleNamespace(id=30, send=AsyncMock())
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
        delete=AsyncMock(),
        reply=AsyncMock(),
    )


def _event_by_message_id(db, message_id: str) -> Event:
    with db.session() as session:
        event = session.scalar(
            select(Event).where(Event.discord_message_id == message_id)
        )
        assert event is not None
        return event


def test_inbox_route_schema_rejects_unsafe_cross_destination_fields():
    with pytest.raises(ValidationError):
        InboxRouteResult(destination="rhythm_result", game="sdvx")
    with pytest.raises(ValidationError):
        InboxRouteResult(
            destination="life_task",
            task_title="会議",
            task_due_time=time(15, 0),
        )
    with pytest.raises(ValidationError):
        InboxRouteResult(destination="unknown", task_title="混入")
    with pytest.raises(ValidationError):
        InboxRouteResult(destination="memo", content="本文", unexpected="拒否")
    career = InboxRouteResult(
        destination="career",
        company="Acme",
        role="Backend",
        kind="application",
        stage="applied",
        deadline="2026-09-05",
        deadline_time="15:00",
        url="https://example.com/jobs/1",
    )
    assert career.career_company == "Acme"
    assert career.career_title == "Backend"
    assert career.career_kind == "application"
    assert career.career_deadline_date == date(2026, 9, 5)
    assert career.career_deadline_time == time(15, 0)
    assert career.career_url == "https://example.com/jobs/1"
    with pytest.raises(ValidationError):
        InboxRouteResult(destination="memo", career_company="混入", content="本文")
    with pytest.raises(ValidationError):
        InboxRouteResult(
            destination="career",
            company="Acme",
            role="Backend",
            url="https://user:password@example.com/jobs/1",
        )


@pytest.mark.asyncio
async def test_inbox_finance_and_meal_routes_delegate_to_existing_analysis(db, tmp_path):
    analyzer = QueueAnalyzer(
        routes=[
            InboxRouteResult(destination="finance_or_meal", confidence=0.92),
            InboxRouteResult(destination="finance_or_meal", confidence=0.9),
        ],
        text_results=[
            TextAnalysisResult(
                event_kinds=["expense"],
                amount=740,
                merchant="コンビニ",
                category="食費",
                subcategory="コンビニ",
                description="買い物",
                confidence=0.88,
            ),
            TextAnalysisResult(
                event_kinds=["meal"],
                meal_type=MealType.DINNER,
                restaurant="食堂",
                meal_description="夕食",
                outside_home=OutsideHome.OUTSIDE,
                confidence=0.86,
            ),
        ],
    )
    settings = _settings(tmp_path)
    service = InboxService(db.session_factory, settings, analyzer)
    bot = LifeInboxBot(settings, service)
    expense_message = _message("route-expense", "未知の場所で何か購入した")
    meal_message = _message("route-meal", "外出先で済ませた内容を記録")

    try:
        await bot.on_message(expense_message)
        await bot.on_message(meal_message)
    finally:
        await bot.close()

    expense_event = _event_by_message_id(db, "route-expense")
    meal_event = _event_by_message_id(db, "route-meal")
    assert expense_event.expenses[0].amount == 740
    assert expense_event.expenses[0].category == "食費"
    assert meal_event.meals[0].meal_type == MealType.DINNER.value
    assert analyzer.route_inputs == [
        "未知の場所で何か購入した",
        "外出先で済ませた内容を記録",
    ]
    assert len(analyzer.text_inputs) == 2
    with db.session() as session:
        route_runs = session.scalar(
            select(func.count(AIRun.id)).where(AIRun.input_type == INBOX_ROUTE_INPUT_TYPE)
        )
    assert route_runs == 2
    expense_message.channel.send.assert_awaited_once()
    meal_message.channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_inbox_memo_route_preserves_body_and_duplicate_delivery_is_idempotent(
    db, tmp_path
):
    analyzer = QueueAnalyzer(
        [
            InboxRouteResult(
                destination="memo",
                content="短い言い換え",
                confidence=0.96,
            )
        ]
    )
    settings = _settings(tmp_path, memo_channel_id=60)
    service = InboxService(db.session_factory, settings, analyzer)
    bot = LifeInboxBot(settings, service)
    message = _message("route-memo", "研究の考えを残しておきたい")
    memo_channel = SimpleNamespace(id=60, send=AsyncMock())
    bot.get_channel = MagicMock(return_value=memo_channel)

    try:
        await bot.on_message(message)
        await bot.on_message(message)
    finally:
        await bot.close()

    event = _event_by_message_id(db, "route-memo")
    assert event.raw_text == "研究の考えを残しておきたい"
    assert event.memo is not None
    assert event.memo.body == "研究の考えを残しておきたい"
    assert analyzer.route_inputs == ["研究の考えを残しておきたい"]
    with db.session() as session:
        assert session.scalar(select(func.count(Memo.id))) == 1
        assert session.scalar(
            select(func.count(AIRun.id)).where(AIRun.input_type == INBOX_ROUTE_INPUT_TYPE)
        ) == 1
    message.channel.send.assert_not_awaited()
    message.reply.assert_awaited_once()
    reply_text = message.reply.await_args.args[0]
    assert "一言メモを保存しました: #1" in reply_text
    assert "保存先: #一言メモ" in reply_text
    assert "http://127.0.0.1:8765/events/1" in reply_text
    assert message.reply.await_args.kwargs["mention_author"] is False
    view = message.reply.await_args.kwargs["view"]
    assert view.timeout is None
    assert len(view.children) == 2
    assert view.children[0].style is discord.ButtonStyle.link
    assert view.children[0].label == "Webで開く"
    assert view.children[0].url.endswith("/events/1")
    assert view.children[1].style is discord.ButtonStyle.secondary
    assert view.children[1].label == "📚 Notionへバックアップ"
    assert view.children[1].custom_id == "life-inbox:notion-promote:1"
    memo_channel.send.assert_awaited_once()
    card_text = memo_channel.send.await_args.args[0]
    assert "一言メモ #1" in card_text
    assert "研究の考えを残しておきたい" in card_text
    card_view = memo_channel.send.await_args.kwargs["view"]
    assert card_view.children[0].url.endswith("/events/1")
    assert card_view.children[1].label == "📚 Notionへバックアップ"
    message.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbox_social_route_creates_local_outbox_and_maps_action_before_delete(
    db, tmp_path
):
    settings = _settings(tmp_path, delete_original=True, social_draft_channel_id=60)
    service = InboxService(db.session_factory, settings, NullAnalyzer())
    bot = LifeInboxBot(settings, service)
    source = _message("route-social", "今日の投稿: ローカルで確認する")
    destination = SimpleNamespace(
        id=60,
        name="毎日の投稿",
        send=AsyncMock(return_value=SimpleNamespace(id=900)),
    )
    bot.get_channel = MagicMock(return_value=destination)

    try:
        captured = await service.capture_discord_message_raw(source)
        assert captured.duplicate is False
        outcome = await bot._route_classified_inbox(
            source,
            captured.event_id,
            InboxCategory.SOCIAL_DRAFT,
        )
    finally:
        await bot.close()

    assert outcome == ("毎日の投稿", True)
    destination.send.assert_awaited_once()
    content = destination.send.await_args.args[0]
    assert "SNS下書き" in content
    assert "X・Instagram・両方・保留" in content
    assert "外部SNSには投稿していません" in content
    view = destination.send.await_args.kwargs["view"]
    assert isinstance(view, SocialDraftView)
    assert view.action_channel_id == 60
    source.delete.assert_awaited_once_with()

    with db.session() as session:
        event = session.scalar(select(Event).where(Event.discord_message_id == "route-social"))
        assert event is not None
        draft = session.scalar(
            select(SocialDraft).where(SocialDraft.event_id == event.id)
        )
        assert draft is not None
        assert draft.target == "pending"
        assert draft.state == "pending"
        assert draft.discord_action_channel_id == "60"
        assert draft.discord_action_message_id == "900"


@pytest.mark.asyncio
async def test_inbox_memo_card_delivery_failure_keeps_saved_memo_and_source(
    db, tmp_path, caplog
):
    secret = "private-card-delivery-body"
    analyzer = QueueAnalyzer(
        [InboxRouteResult(destination="memo", content="カード失敗でも残す")]
    )
    settings = _settings(tmp_path, delete_original=True, memo_channel_id=60)
    service = InboxService(db.session_factory, settings, analyzer)
    bot = LifeInboxBot(settings, service)
    message = _message("route-memo-card-failure", "カード投稿に失敗しても保存する")
    memo_channel = SimpleNamespace(
        id=60,
        send=AsyncMock(side_effect=RuntimeError(secret)),
    )
    bot.get_channel = MagicMock(return_value=memo_channel)
    caplog.set_level("WARNING")

    try:
        await bot.on_message(message)
    finally:
        await bot.close()

    event = _event_by_message_id(db, "route-memo-card-failure")
    assert event.memo is not None
    assert event.memo.body == "カード投稿に失敗しても保存する"
    assert memo_channel.send.await_count == 1
    message.delete.assert_not_awaited()
    message.reply.assert_awaited_once()
    response = message.reply.await_args.args[0]
    assert "DBへ保存しました" in response
    assert "元投稿は残しています" in response
    assert "http://127.0.0.1:8765/events/1" in response
    assert secret not in response
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_inbox_rhythm_results_only_complete_an_explicitly_linked_todo(db, tmp_path):
    analyzer = QueueAnalyzer(
        [
            InboxRouteResult(
                destination="rhythm_todo",
                game="maimai",
                content="でらっくす譜面を詰める",
            ),
            InboxRouteResult(
                destination="rhythm_result",
                game="maimai",
                content="SSS+を更新",
            ),
            InboxRouteResult(
                destination="rhythm_result",
                game=None,
                content="達成",
            ),
        ]
    )
    settings = _settings(tmp_path)
    service = InboxService(db.session_factory, settings, analyzer)
    bot = LifeInboxBot(settings, service)
    todo_message = _message("route-rhythm-todo", "次に詰めたい譜面がある")
    unlinked_result = _message("route-rhythm-result-1", "自己ベストを更新した")
    linked_result = _message(
        "route-rhythm-result-2",
        "達成できた",
        reference_id="route-rhythm-todo",
    )

    try:
        await bot.on_message(todo_message)
        await bot.on_message(unlinked_result)
        await bot.on_message(unlinked_result)
        with db.session() as session:
            todo = session.scalar(select(RhythmTodo))
            assert todo is not None
            assert todo.status == RhythmTodoStatus.OPEN.value
        await bot.on_message(linked_result)
    finally:
        await bot.close()

    with db.session() as session:
        todo = session.scalar(select(RhythmTodo))
        results = list(session.scalars(select(RhythmResult).order_by(RhythmResult.id)).all())
        assert todo is not None
        assert todo.status == RhythmTodoStatus.COMPLETED.value
        assert [result.todo_id for result in results] == [None, todo.id]
    assert len(analyzer.route_inputs) == 3
    assert "TODO #1" in todo_message.channel.send.await_args.args[0]
    assert "リザルト #1" in unlinked_result.channel.send.await_args.args[0]
    assert "TODO #1を完了" in linked_result.channel.send.await_args.args[0]


@pytest.mark.asyncio
async def test_inbox_unknown_stays_pending_and_explains_edit_and_retry(db, tmp_path):
    analyzer = QueueAnalyzer([InboxRouteResult(destination="unknown", confidence=0.2)])
    settings = _settings(tmp_path)
    service = InboxService(db.session_factory, settings, analyzer)
    bot = LifeInboxBot(settings, service)
    message = _message("route-unknown", "分類できる根拠がない断片")

    try:
        await bot.on_message(message)
    finally:
        await bot.close()

    event = _event_by_message_id(db, "route-unknown")
    assert event.status == EventStatus.PENDING.value
    assert event.event_type == "unknown"
    response = message.channel.send.await_args.args[0]
    assert "原文・添付を保存しました" in response
    assert "用途を安全に判定できなかった" in response
    assert "生活マイページで編集" in response
    assert "/retry event_id:" in response


@pytest.mark.asyncio
async def test_inbox_life_tasks_store_only_explicit_dates_and_are_duplicate_safe(db, tmp_path):
    analyzer = QueueAnalyzer(
        [
            InboxRouteResult(
                destination="life_task",
                task_title="研究計画書を提出",
                task_due_date=date(2026, 9, 5),
                task_due_time=time(15, 0),
                confidence=0.94,
            ),
            InboxRouteResult(
                destination="life_task",
                task_title="第3章を読む",
                # Even a structurally valid model output must be grounded in
                # the raw text; this invented absolute date is discarded.
                task_due_date=date(2026, 8, 30),
                task_due_time=time(9, 0),
                confidence=0.82,
            ),
        ]
    )
    settings = _settings(tmp_path)
    service = InboxService(db.session_factory, settings, analyzer)
    bot = LifeInboxBot(settings, service)
    explicit = _message(
        "route-task-explicit",
        "2026-09-05 15:00に研究計画書を提出する予定を登録",
    )
    ambiguous = _message("route-task-ambiguous", "明日の朝に第3章を読む")

    try:
        await bot.on_message(explicit)
        await bot.on_message(explicit)
        await bot.on_message(ambiguous)
    finally:
        await bot.close()

    with db.session() as session:
        tasks = {
            task.title: task
            for task in session.scalars(select(LifeTask).order_by(LifeTask.id)).all()
        }
        assert len(tasks) == 2
        assert tasks["研究計画書を提出"].due_at == datetime(2026, 9, 5, 6, 0)
        assert tasks["第3章を読む"].due_at is None
        explicit_event = session.scalar(
            select(Event).where(Event.discord_message_id == "route-task-explicit")
        )
        assert explicit_event is not None
        assert explicit_event.status == EventStatus.CONFIRMED.value
        assert explicit_event.event_type == "life_task"
        assert explicit_event.event_kinds == ["life_task"]
        assert session.scalar(
            select(func.count(AIRun.id)).where(AIRun.input_type == INBOX_ROUTE_INPUT_TYPE)
        ) == 2
    assert len(analyzer.route_inputs) == 2
    explicit.channel.send.assert_awaited_once()
    assert "2026-09-05 15:00" in explicit.channel.send.await_args.args[0]
    assert "期限なし" in ambiguous.channel.send.await_args.args[0]


@pytest.mark.asyncio
async def test_inbox_career_application_creates_one_linked_task_and_is_duplicate_safe(
    db, tmp_path
):
    analyzer = QueueAnalyzer(
        [
            InboxRouteResult(
                destination="career",
                career_company="Acme",
                career_title="バックエンドエンジニア",
                career_kind="application",
                career_stage="applied",
                career_deadline_date=date(2026, 9, 5),
                career_deadline_time=time(15, 0),
                career_url="https://example.com/jobs/backend",
                career_notes="履歴書を提出済み",
                career_external_id="acme-backend-1",
            )
        ]
    )
    settings = _settings(tmp_path, delete_original=True)
    service = InboxService(db.session_factory, settings, analyzer)
    bot = LifeInboxBot(settings, service)
    message = _message(
        "route-career-application",
        "Acme バックエンド 応募 2026-09-05 15:00",
    )

    try:
        await bot.on_message(message)
        await bot.on_message(message)
    finally:
        await bot.close()

    with db.session() as session:
        opportunities = list(session.scalars(select(CareerOpportunity)).all())
        applications = list(session.scalars(select(CareerApplication)).all())
        tasks = list(session.scalars(select(LifeTask)).all())
        event = session.scalar(
            select(Event).where(Event.discord_message_id == "route-career-application")
        )
        assert len(opportunities) == 1
        assert len(applications) == 1
        assert len(tasks) == 1
        assert event is not None
        assert applications[0].stage == "applied"
        assert applications[0].status == "applied"
        assert applications[0].linked_task_id == tasks[0].id
        assert tasks[0].source == "manual"
        assert tasks[0].due_at == datetime(2026, 9, 5, 6, 0)
        assert tasks[0].source_url == "https://example.com/jobs/backend"
        assert event.status == EventStatus.CONFIRMED.value
        assert event.event_type == "career"
        assert event.event_kinds == ["career"]

    assert analyzer.route_inputs == ["Acme バックエンド 応募 2026-09-05 15:00"]
    message.channel.send.assert_awaited_once()
    response = message.channel.send.await_args.args[0]
    assert "就活記録 #1" in response
    assert "Acme / バックエンドエンジニア" in response
    assert "期限: 2026-09-05 15:00" in response
    assert "LifeTask #1" in response
    assert message.channel.send.await_args.kwargs["allowed_mentions"] is not None
    message.delete.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_inbox_career_deterministic_route_and_inferred_deadline_stay_safe(db, tmp_path):
    analyzer = QueueAnalyzer(
        [
            InboxRouteResult(
                destination="career",
                career_company="Acme",
                career_title="インターン",
                career_kind="job",
                career_deadline_date=date(2026, 9, 6),
                career_deadline_time=time(10, 0),
            )
        ]
    )
    settings = _settings(tmp_path)
    service = InboxService(db.session_factory, settings, analyzer)
    bot = LifeInboxBot(settings, service)
    deterministic = _message(
        "route-career-deterministic",
        "就活: Acme | インターン 2026-09-06 10:00",
    )
    inferred = _message("route-career-inferred", "明日の朝にAcmeの求人を確認")

    try:
        await bot.on_message(deterministic)
        await bot.on_message(inferred)
    finally:
        await bot.close()

    with db.session() as session:
        opportunities = list(
            session.scalars(select(CareerOpportunity).order_by(CareerOpportunity.id)).all()
        )
        tasks = list(session.scalars(select(LifeTask)).all())
        inferred_event = session.scalar(
            select(Event).where(Event.discord_message_id == "route-career-inferred")
        )
        runs = list(
            session.scalars(
                select(AIRun)
                .where(AIRun.input_type == INBOX_ROUTE_INPUT_TYPE)
                .order_by(AIRun.id)
            ).all()
        )
        assert len(opportunities) == 2
        assert opportunities[0].deadline_at == datetime(2026, 9, 6, 1, 0)
        assert opportunities[1].deadline_at is None
        assert len(tasks) == 1
        assert inferred_event is not None
        assert inferred_event.status == EventStatus.CONFIRMED.value
        assert json.loads(runs[1].result_json)["result"]["career_deadline_date"] is None

    assert analyzer.route_inputs == ["明日の朝にAcmeの求人を確認"]
    assert "期限: 2026-09-06 10:00" in deterministic.channel.send.await_args.args[0]
    assert "期限: 期限なし" in inferred.channel.send.await_args.args[0]
    deterministic.channel.send.assert_awaited_once()
    inferred.channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_inbox_career_ack_failure_preserves_source_without_duplicate_rows(
    db, tmp_path, caplog
):
    secret = "private-career-ack-failure"
    analyzer = QueueAnalyzer(
        [
            InboxRouteResult(
                destination="career",
                career_company="Acme",
                career_title="営業",
                career_kind="job",
                career_deadline_date=date(2026, 9, 8),
            )
        ]
    )
    settings = _settings(tmp_path, delete_original=True)
    service = InboxService(db.session_factory, settings, analyzer)
    bot = LifeInboxBot(settings, service)
    message = _message("route-career-ack-failure", "Acme 営業 2026-09-08")
    message.channel.send.side_effect = RuntimeError(secret)
    caplog.set_level("WARNING")

    try:
        await bot.on_message(message)
        await bot.on_message(message)
    finally:
        await bot.close()

    with db.session() as session:
        assert session.scalar(select(func.count(CareerOpportunity.id))) == 1
        assert session.scalar(select(func.count(LifeTask.id))) == 1
        event = session.scalar(
            select(Event).where(Event.discord_message_id == "route-career-ack-failure")
        )
        assert event is not None
        assert event.status == EventStatus.CONFIRMED.value
        assert event.event_type == "career"
    assert analyzer.route_inputs == ["Acme 営業 2026-09-08"]
    assert message.channel.send.await_count == 1
    message.delete.assert_not_awaited()
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_unconfigured_router_keeps_existing_finance_fallback(db, tmp_path):
    settings = _settings(tmp_path)
    service = InboxService(db.session_factory, settings, NullAnalyzer())
    bot = LifeInboxBot(settings, service)
    obvious = _message("route-no-ai-expense", "680 マック")
    ambiguous = _message("route-no-ai-ambiguous", "知らない買い物")

    try:
        await bot.on_message(obvious)
        await bot.on_message(ambiguous)
    finally:
        await bot.close()

    obvious_event = _event_by_message_id(db, "route-no-ai-expense")
    ambiguous_event = _event_by_message_id(db, "route-no-ai-ambiguous")
    assert obvious_event.expenses[0].amount == 680
    assert ambiguous_event.status == EventStatus.PENDING.value
    with db.session() as session:
        route_runs = list(
            session.scalars(
                select(AIRun)
                .where(AIRun.input_type == INBOX_ROUTE_INPUT_TYPE)
                .order_by(AIRun.id)
            ).all()
        )
    payloads = [json.loads(run.result_json) for run in route_runs]
    assert [payload["source"] for payload in payloads] == [
        "deterministic",
        "finance_fallback",
    ]
    assert payloads[1]["error_type"] == "AnalyzerUnavailable"
    assert "AI analyzer is not configured" not in route_runs[1].result_json
    obvious.channel.send.assert_awaited_once()
    ambiguous.channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_routing_failure_is_raw_first_and_secret_safe(db, tmp_path, caplog):
    observed: dict[str, object] = {}
    secret = "sk-test-secret Authorization raw-user-private-note"

    def observe_raw_state() -> None:
        with db.session() as session:
            event = session.scalar(
                select(Event).where(Event.discord_message_id == "route-failure")
            )
            attachment = session.scalar(
                select(Attachment).where(Attachment.discord_attachment_id == "route-image")
            )
            observed["event"] = event is not None
            observed["attachment"] = attachment is not None
            observed["bytes"] = (
                Path(attachment.local_path).read_bytes() if attachment is not None else None
            )

    analyzer = QueueAnalyzer(
        route_error=RuntimeError(secret),
        on_route=observe_raw_state,
    )
    settings = _settings(tmp_path, delete_original=True, memo_channel_id=60)
    service = InboxService(db.session_factory, settings, analyzer)
    bot = LifeInboxBot(settings, service)
    attachment = SimpleNamespace(
        id="route-image",
        filename="private.png",
        content_type="image/png",
        size=9,
        read=AsyncMock(return_value=b"png-bytes"),
    )
    message = _message(
        "route-failure",
        "raw-user-private-note",
        attachments=[attachment],
    )

    caplog.set_level("WARNING")
    try:
        await bot.on_message(message)
    finally:
        await bot.close()

    assert observed == {
        "event": True,
        "attachment": True,
        "bytes": b"png-bytes",
    }
    assert analyzer.route_inputs == ["raw-user-private-note"]
    event = _event_by_message_id(db, "route-failure")
    assert event.status == EventStatus.PENDING.value
    with db.session() as session:
        run = session.scalar(
            select(AIRun).where(
                AIRun.event_id == event.id,
                AIRun.input_type == INBOX_ROUTE_INPUT_TYPE,
            )
        )
        assert run is not None
        payload = json.loads(run.result_json)
    assert payload["ok"] is False
    assert payload["routing_failed"] is True
    assert payload["error_type"] == "RuntimeError"
    response = message.channel.send.await_args.args[0]
    for output in (run.result_json, response, caplog.text):
        assert "sk-test-secret" not in output
        assert "Authorization" not in output
        assert "raw-user-private-note" not in output
    assert "分類サービスが完了しなかった" in response
    message.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_opt_in_delete_happens_only_after_raw_and_memo_are_durable(db, tmp_path):
    observed: dict[str, object] = {}

    def observe_route() -> None:
        with db.session() as session:
            event = session.scalar(
                select(Event).where(Event.discord_message_id == "route-delete-success")
            )
            attachment = session.scalar(
                select(Attachment).where(Attachment.discord_attachment_id == "delete-image")
            )
            observed["raw_before_route"] = event is not None and attachment is not None
            observed["bytes_before_route"] = (
                Path(attachment.local_path).read_bytes() if attachment is not None else None
            )

    analyzer = QueueAnalyzer(
        [InboxRouteResult(destination="memo", content="残す本文")],
        on_route=observe_route,
    )
    settings = _settings(tmp_path, delete_original=True, memo_channel_id=60)
    service = InboxService(db.session_factory, settings, analyzer)
    bot = LifeInboxBot(settings, service)
    attachment = SimpleNamespace(
        id="delete-image",
        filename="memo.png",
        content_type="image/png",
        size=9,
        read=AsyncMock(return_value=b"png-bytes"),
    )
    message = _message(
        "route-delete-success",
        "残しておきたい本文",
        attachments=[attachment],
    )
    memo_channel = SimpleNamespace(id=60, send=AsyncMock())
    bot.get_channel = MagicMock(return_value=memo_channel)

    def observe_delete() -> None:
        with db.session() as session:
            event = session.scalar(
                select(Event).where(Event.discord_message_id == "route-delete-success")
            )
            observed["domain_before_delete"] = event is not None and event.memo is not None
            observed["attachment_before_delete"] = (
                event is not None and len(event.attachments) == 1
            )
        observed["ack_before_delete"] = message.reply.await_count == 1

    message.delete.side_effect = observe_delete
    try:
        await bot.on_message(message)
    finally:
        await bot.close()

    assert observed == {
        "raw_before_route": True,
        "bytes_before_route": b"png-bytes",
        "domain_before_delete": True,
        "attachment_before_delete": True,
        "ack_before_delete": True,
    }
    message.delete.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_opt_in_deletes_successful_finance_record_but_not_its_acknowledgement(
    db, tmp_path
):
    settings = _settings(tmp_path, delete_original=True)
    service = InboxService(db.session_factory, settings, NullAnalyzer())
    bot = LifeInboxBot(settings, service)
    message = _message("delete-finance-success", "680 マック")

    try:
        await bot.on_message(message)
    finally:
        await bot.close()

    event = _event_by_message_id(db, "delete-finance-success")
    assert event.expenses[0].amount == 680
    message.channel.send.assert_awaited_once()
    message.delete.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_opt_in_never_deletes_unknown_missing_game_or_finance_failure(db, tmp_path):
    settings = _settings(tmp_path, delete_original=True)
    analyzer = QueueAnalyzer(
        [
            InboxRouteResult(destination="unknown"),
            InboxRouteResult(
                destination="rhythm_result",
                game=None,
                content="達成",
            ),
        ]
    )
    service = InboxService(db.session_factory, settings, analyzer)
    bot = LifeInboxBot(settings, service)
    unknown = _message("delete-unknown", "判定不能の断片")
    missing_game = _message("delete-missing-game", "何かを達成した")

    try:
        await bot.on_message(unknown)
        await bot.on_message(missing_game)
    finally:
        await bot.close()

    unknown.delete.assert_not_awaited()
    missing_game.delete.assert_not_awaited()

    fallback_service = InboxService(db.session_factory, settings, NullAnalyzer())
    fallback_bot = LifeInboxBot(settings, fallback_service)
    finance_failure = _message("delete-finance-failure", "知らない買い物")
    try:
        await fallback_bot.on_message(finance_failure)
    finally:
        await fallback_bot.close()
    finance_failure.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_attachment_partial_failure_skips_routing_and_delete(db, tmp_path):
    analyzer = QueueAnalyzer([InboxRouteResult(destination="memo", content="本文")])
    settings = _settings(tmp_path, delete_original=True)
    service = InboxService(db.session_factory, settings, analyzer)
    bot = LifeInboxBot(settings, service)
    attachment = SimpleNamespace(
        id="broken-image",
        filename="broken.png",
        content_type="image/png",
        size=9,
        read=AsyncMock(side_effect=OSError("private attachment failure")),
    )
    message = _message(
        "delete-partial-attachment",
        "メモかもしれない",
        attachments=[attachment],
    )

    try:
        await bot.on_message(message)
    finally:
        await bot.close()

    assert analyzer.route_inputs == []
    message.delete.assert_not_awaited()
    response = message.channel.send.await_args.args[0]
    assert "分類処理は実行せず保留" in response


@pytest.mark.asyncio
async def test_task_partial_failure_after_commit_never_deletes_or_duplicates(
    db,
    tmp_path,
    monkeypatch,
    caplog,
):
    analyzer = QueueAnalyzer(
        [InboxRouteResult(destination="life_task", task_title="申請書を提出")]
    )
    settings = _settings(tmp_path, delete_original=True)
    service = InboxService(db.session_factory, settings, analyzer)
    bot = LifeInboxBot(settings, service)
    message = _message("delete-partial-task", "申請書を提出する")
    secret = "private-event-mark-failure"

    def fail_event_mark(event_id: int, destination: str):
        raise RuntimeError(secret)

    monkeypatch.setattr(service, "mark_event_routed", fail_event_mark)
    caplog.set_level("WARNING")
    try:
        await bot.on_message(message)
        await bot.on_message(message)
    finally:
        await bot.close()

    with db.session() as session:
        assert session.scalar(select(func.count(LifeTask.id))) == 1
        event = session.scalar(
            select(Event).where(Event.discord_message_id == "delete-partial-task")
        )
        assert event is not None
        assert event.status == EventStatus.PENDING.value
        assert event.event_type == "unknown"
    assert len(analyzer.route_inputs) == 1
    message.delete.assert_not_awaited()
    response = message.channel.send.await_args.args[0]
    assert "生活タスクとして安全に保存できなかった" in response
    assert secret not in response
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_acknowledgement_failure_never_triggers_source_delete(db, tmp_path):
    settings = _settings(tmp_path, delete_original=True, memo_channel_id=60)
    service = InboxService(db.session_factory, settings, QueueAnalyzer())
    bot = LifeInboxBot(settings, service)
    message = _message("delete-ack-failure", "メモ: domainは先に保存")
    memo_channel = SimpleNamespace(id=60, send=AsyncMock())
    bot.get_channel = MagicMock(return_value=memo_channel)
    message.reply.side_effect = RuntimeError("private acknowledgement failure")

    try:
        await bot.on_message(message)
    finally:
        await bot.close()

    event = _event_by_message_id(db, "delete-ack-failure")
    assert event.memo is not None
    assert event.memo.body == "domainは先に保存"
    message.delete.assert_not_awaited()


@pytest.mark.parametrize(
    ("error_type", "status"),
    [
        (discord.Forbidden, 403),
        (discord.NotFound, 404),
        (discord.HTTPException, 500),
    ],
)
@pytest.mark.asyncio
async def test_delete_http_failures_are_sanitized_and_do_not_undo_success(
    db,
    tmp_path,
    caplog,
    error_type,
    status,
):
    settings = _settings(tmp_path, delete_original=True, memo_channel_id=60)
    analyzer = QueueAnalyzer()
    service = InboxService(db.session_factory, settings, analyzer)
    bot = LifeInboxBot(settings, service)
    message = _message(
        f"delete-http-{status}",
        "メモ: 削除失敗でも保存する",
    )
    memo_channel = SimpleNamespace(id=60, send=AsyncMock())
    bot.get_channel = MagicMock(return_value=memo_channel)
    response = SimpleNamespace(status=status, reason="secret-delete-reason")
    message.delete.side_effect = error_type(response, "secret-delete-body")

    caplog.set_level("WARNING")
    try:
        await bot.on_message(message)
    finally:
        await bot.close()

    event = _event_by_message_id(db, f"delete-http-{status}")
    assert event.memo is not None
    assert event.memo.body == "削除失敗でも保存する"
    message.delete.assert_awaited_once_with()
    assert error_type.__name__ in caplog.text
    assert "secret-delete-reason" not in caplog.text
    assert "secret-delete-body" not in caplog.text
