from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx2
import pytest
from openai import OpenAI
from pydantic import ValidationError

from life_inbox.ai_review import (
    AIReviewService,
    OpenAIReviewAdapter,
    ReviewCategory,
    ReviewFacts,
    ReviewProviderOutputError,
    ReviewProviderRequestError,
    ReviewResult,
    format_discord_review,
    render_local_review,
)
from life_inbox.models import (
    CareerApplication,
    CareerOpportunity,
    DailyRoutine,
    Event,
    Expense,
    LifeTask,
    Meal,
    RoutineCompletion,
)
from life_inbox.rhythm_models import RatingSnapshotRow, RhythmUserRow


def _event(
    db,
    *,
    message_id: str,
    user_id: str,
    occurred_at: datetime,
    status: str = "confirmed",
    raw_text: str = "private raw text",
    amount: int | None = None,
    category: str = "食費",
    meal: bool = False,
    outside_home: str = "unknown",
) -> int:
    with db.session() as session:
        event = Event(
            discord_message_id=message_id,
            discord_channel_id="private-channel-id",
            discord_user_id=user_id,
            captured_at=occurred_at,
            occurred_at=occurred_at,
            raw_text=raw_text,
            source="iphone_shortcut" if message_id.startswith("shortcut") else "discord",
            status=status,
            event_type="expense" if amount is not None else "meal" if meal else "unknown",
            event_kinds=[
                kind
                for kind, present in (("expense", amount is not None), ("meal", meal))
                if present
            ],
        )
        if amount is not None:
            event.expenses.append(Expense(amount=amount, category=category))
        if meal:
            event.meals.append(Meal(outside_home=outside_home))
        session.add(event)
        session.commit()
        return event.id


def _review_service(db) -> AIReviewService:
    return AIReviewService(
        db.session_factory,
        allowed_user_id="10",
        timezone="Asia/Tokyo",
    )


def test_daily_and_weekly_boundaries_use_configured_jst_calendar(db):
    service = _review_service(db)

    assert service.daily_bounds(date(2026, 8, 30)) == (
        datetime(2026, 8, 29, 15, 0),
        datetime(2026, 8, 30, 15, 0),
    )
    assert service.weekly_bounds(date(2026, 8, 30)) == (
        datetime(2026, 8, 23, 15, 0),
        datetime(2026, 8, 30, 15, 0),
    )

    _event(
        db,
        message_id="before-day",
        user_id="10",
        occurred_at=datetime(2026, 8, 29, 14, 59, 59),
        amount=100,
    )
    _event(
        db,
        message_id="at-day-start",
        user_id="10",
        occurred_at=datetime(2026, 8, 29, 15, 0),
        amount=200,
    )
    _event(
        db,
        message_id="at-next-day-start",
        user_id="10",
        occurred_at=datetime(2026, 8, 30, 15, 0),
        amount=400,
    )

    daily = service.build_daily(date(2026, 8, 30))
    weekly = service.build_weekly(date(2026, 8, 30))
    assert daily.expense_total_yen == 200
    assert weekly.period_start == date(2026, 8, 24)
    assert weekly.period_end == date(2026, 8, 30)
    assert weekly.expense_total_yen == 300


def test_facts_filter_event_owner_and_exclude_unapproved_columns_by_construction(db):
    _event(
        db,
        message_id="authorized",
        user_id="10",
        occurred_at=datetime(2026, 8, 29, 16, 0),
        raw_text="RAW-SECRET-MUST-NOT-CROSS",
        amount=1200,
        category="食費",
        meal=True,
        outside_home="outside",
    )
    _event(
        db,
        message_id="shortcut-authorized",
        user_id="10",
        occurred_at=datetime(2026, 8, 30, 10, 0),
        amount=300,
        category="api_key=category-secret-value",
    )
    _event(
        db,
        message_id="unauthorized",
        user_id="other-user",
        occurred_at=datetime(2026, 8, 30, 10, 0),
        raw_text="OTHER-RAW-SECRET",
        amount=9000,
        category="秘密",
        meal=True,
        outside_home="outside",
    )
    with db.session() as session:
        session.add_all(
            [
                LifeTask(
                    title="task title not sent",
                    description="TASK-DESCRIPTION-SECRET",
                    source_url="https://user:password@example.test/task?token=secret",
                    status="open",
                    due_at=datetime(2026, 8, 30, 2, 0),
                ),
                LifeTask(
                    title="completed task",
                    status="completed",
                    completed_at=datetime(2026, 8, 30, 3, 0),
                ),
            ]
        )
        routine = DailyRoutine(title="routine title", reminder_time="08:00", enabled=True)
        routine.completions.append(RoutineCompletion(local_date=date(2026, 8, 30)))
        session.add(routine)
        session.commit()

    service = _review_service(db)
    facts = service.build_daily(date(2026, 8, 30))
    payload = facts.to_provider_json()

    assert facts.expense_total_yen == 1500
    assert facts.expense_count == 2
    assert facts.meal_count == 1
    assert facts.dining_count == 1
    assert facts.open_task_count == 1
    assert facts.completed_task_count == 1
    assert facts.task_deadline_count == 1
    assert facts.routine_completion_count == 1
    assert facts.enabled_routine_count == 1
    assert "9000" not in payload
    assert "OTHER-RAW-SECRET" not in payload
    assert "RAW-SECRET-MUST-NOT-CROSS" not in payload
    assert "TASK-DESCRIPTION-SECRET" not in payload
    assert "password@example.test" not in payload
    assert "category-secret-value" not in payload
    assert "raw_text" not in payload
    assert "source_url" not in payload


def test_career_and_rhythm_scope_uses_structured_owner_markers_without_raw_data(db):
    authorized_event_id = _event(
        db,
        message_id="career-authorized",
        user_id="10",
        occurred_at=datetime(2026, 8, 29, 16, 0),
    )
    unauthorized_event_id = _event(
        db,
        message_id="career-unauthorized",
        user_id="other-user",
        occurred_at=datetime(2026, 8, 29, 16, 0),
    )
    with db.session() as session:
        authorized = CareerOpportunity(
            source_event_id=authorized_event_id,
            company="authorized company",
            title="authorized title",
            status="open",
            deadline_at=datetime(2026, 8, 30, 2, 0),
            url="https://example.test/?token=career-secret",
            notes="CAREER-NOTES-SECRET",
        )
        unauthorized = CareerOpportunity(
            source_event_id=unauthorized_event_id,
            company="other company",
            title="other title",
            status="open",
            deadline_at=datetime(2026, 8, 30, 3, 0),
        )
        session.add_all([authorized, unauthorized])
        session.flush()
        session.add(
            CareerApplication(
                opportunity_id=authorized.id,
                status="applied",
                stage="applied",
                deadline_at=datetime(2026, 8, 30, 4, 0),
                notes="APPLICATION-NOTES-SECRET",
            )
        )
        rhythm_user = RhythmUserRow(
            display_name="owner",
            time_zone="Asia/Tokyo",
            discord_user_id="10",
        )
        other_rhythm_user = RhythmUserRow(
            display_name="other",
            time_zone="Asia/Tokyo",
            discord_user_id="other-user",
        )
        session.add_all([rhythm_user, other_rhythm_user])
        session.flush()
        session.add_all(
            [
                RatingSnapshotRow(
                    user_id=rhythm_user.id,
                    game="maimai",
                    player_name="owner",
                    rating=Decimal("12.34"),
                    previous_rating=Decimal("12.00"),
                    rating_delta=Decimal("0.34"),
                    semantic_fingerprint="a" * 64,
                    collected_at=datetime(2026, 8, 29, 16, 0),
                    raw_data={"Authorization": "Bearer rhythm-secret"},
                ),
                RatingSnapshotRow(
                    user_id=other_rhythm_user.id,
                    game="ongeki",
                    player_name="other",
                    rating=Decimal("99.99"),
                    rating_delta=Decimal("9.99"),
                    semantic_fingerprint="b" * 64,
                    collected_at=datetime(2026, 8, 29, 16, 0),
                    raw_data={"cookie": "other-secret"},
                ),
            ]
        )
        session.commit()

    facts = _review_service(db).build_daily(date(2026, 8, 30))
    payload = facts.to_provider_json()
    assert facts.active_career_count == 1
    assert facts.active_application_count == 1
    assert facts.career_deadline_count == 2
    assert {row.kind for row in facts.upcoming_career_deadlines} == {
        "career_opportunity",
        "career_application",
    }
    assert [(row.game, row.rating, row.rating_delta) for row in facts.rhythm_ratings] == [
        ("maimai", 12.34, 0.34)
    ]
    assert "career-secret" not in payload
    assert "CAREER-NOTES-SECRET" not in payload
    assert "APPLICATION-NOTES-SECRET" not in payload
    assert "rhythm-secret" not in payload
    assert "99.99" not in payload
    assert "other company" not in payload


def test_facts_are_bounded_json_safe_and_fingerprint_is_stable_for_equivalent_content():
    categories = [
        ReviewCategory(category="A", amount_yen=10, count=1),
        ReviewCategory(category="B", amount_yen=20, count=1),
    ]
    first = ReviewFacts(
        period="weekly",
        period_start=date(2026, 8, 24),
        period_end=date(2026, 8, 30),
        timezone="Asia/Tokyo",
        expense_total_yen=30,
        expense_count=2,
        expense_categories=categories,
    )
    second = first.model_copy(update={"expense_categories": list(reversed(categories))})
    changed = first.model_copy(update={"expense_total_yen": 31})

    assert json.loads(first.to_provider_json())["period_start"] == "2026-08-24"
    assert first.semantic_fingerprint == second.semantic_fingerprint
    assert first.semantic_fingerprint != changed.semantic_fingerprint
    assert len(first.semantic_fingerprint) == 64
    with pytest.raises(ValidationError):
        ReviewFacts(
            period="daily",
            period_start=date(2026, 8, 30),
            period_end=date(2026, 8, 30),
            timezone="Asia/Tokyo",
            expense_categories=[
                ReviewCategory(category=str(index), amount_yen=1, count=1)
                for index in range(21)
            ],
        )
    with pytest.raises(ValidationError):
        ReviewResult(
            headline="headline",
            observations=["ok"],
            next_actions=["x" * 241],
        )
    with pytest.raises(ValidationError):
        ReviewFacts(
            **first.model_dump(),
            generated_at="2026-08-30T00:00:00+09:00",
        )


class _FakeResponses:
    def __init__(self, output_parsed=None, error: Exception | None = None):
        self.calls: list[dict[str, object]] = []
        self.output_parsed = output_parsed
        self.error = error

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(output_parsed=self.output_parsed)


def _facts() -> ReviewFacts:
    return ReviewFacts(
        period_type="daily",
        period_start=date(2026, 8, 30),
        period_end=date(2026, 8, 30),
        timezone="Asia/Tokyo",
        expense_total_yen=680,
        expense_count=1,
        expense_categories=[ReviewCategory(category="食費", amount_yen=680, count=1)],
    )


def test_openai_review_adapter_uses_store_false_and_strict_parsed_schema():
    parsed = ReviewResult(
        headline="記録を確認",
        observations=["確定支出は680円でした。"],
        next_actions=["次の予定を1件確認する。"],
        caveats=["比較データはありません。"],
    )
    responses = _FakeResponses(parsed)
    adapter = OpenAIReviewAdapter(client=SimpleNamespace(responses=responses), model="test-model")

    result = adapter.review(_facts())

    assert result == parsed
    call = responses.calls[0]
    assert call["store"] is False
    assert call["text_format"] is ReviewResult
    input_rows = call["input"]
    assert isinstance(input_rows, list)
    assert input_rows[1]["content"][0]["type"] == "input_text"
    assert "680" in input_rows[1]["content"][0]["text"]
    assert "医療" in input_rows[0]["content"][0]["text"]
    assert "金融" in input_rows[0]["content"][0]["text"]


def test_openai_review_adapter_serializes_a_strict_json_schema_without_network():
    seen: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        return httpx2.Response(400, json={"error": {"message": "private provider response"}})

    with OpenAI(
        api_key="unused",
        base_url="https://example.invalid/v1",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    ) as client:
        adapter = OpenAIReviewAdapter(client=client)
        with pytest.raises(ReviewProviderRequestError) as caught:
            adapter.review(_facts())

    body = seen["body"]
    assert isinstance(body, dict)
    assert body["store"] is False
    schema = body["text"]["format"]["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert "private provider response" not in str(caught.value)
    assert "680" not in str(caught.value)


def test_openai_review_adapter_redacts_request_and_output_errors():
    facts = _facts()
    request_error = RuntimeError("raw facts sk-proj-12345678901234567890")
    request_responses = _FakeResponses(error=request_error)
    request_adapter = OpenAIReviewAdapter(
        client=SimpleNamespace(responses=request_responses),
    )
    with pytest.raises(ReviewProviderRequestError) as caught:
        request_adapter.review(facts)
    assert str(caught.value) == "ReviewProviderRequestError"
    assert "sk-proj" not in str(caught.value)
    assert "680" not in str(caught.value)

    output_responses = _FakeResponses(output_parsed=None)
    output_adapter = OpenAIReviewAdapter(
        client=SimpleNamespace(responses=output_responses),
    )
    with pytest.raises(ReviewProviderOutputError) as caught:
        output_adapter.review(facts)
    assert str(caught.value) == "ReviewProviderOutputError"


def test_empty_and_meaningful_local_fallbacks_are_deterministic_without_trend_claims():
    empty = _facts().model_copy(
        update={
            "expense_total_yen": 0,
            "expense_count": 0,
            "expense_categories": [],
        }
    )
    empty_result = render_local_review(empty)
    assert "記録" in empty_result.headline
    assert "ありません" in empty_result.observations[0]
    assert "傾向" in empty_result.caveats[0]
    assert all(
        word not in "".join(empty_result.observations)
        for word in ("増えた", "減った", "トレンド")
    )

    meaningful = _facts().model_copy(
        update={
            "expense_total_yen": 680,
            "expense_count": 1,
            "open_task_count": 2,
            "task_deadline_count": 1,
            "upcoming_task_deadlines": [{"kind": "task", "due_date": date(2026, 8, 30)}],
        }
    )
    meaningful_result = render_local_review(meaningful)
    assert any("680円" in item for item in meaningful_result.observations)
    assert any("未完了タスク" in item for item in meaningful_result.observations)
    assert meaningful_result.next_actions
    assert "増減" in meaningful_result.caveats[0]


def test_discord_formatter_neutralizes_mentions_credentials_and_is_bounded():
    facts = _facts()
    malicious = ReviewResult(
        headline="@everyone sk-proj-12345678901234567890",
        observations=[
            "<@123456789> token=top-secret https://user:pass@example.test/a",
        ],
        next_actions=["@here api_key=another-secret"],
        caveats=["Authorization: Bearer bearer-secret-value"],
    )

    message = format_discord_review(malicious, facts)

    assert len(message) < 1900
    assert "@everyone" not in message
    assert "@here" not in message
    assert "<@" not in message
    assert "sk-proj-12345678901234567890" not in message
    assert "top-secret" not in message
    assert "another-secret" not in message
    assert "bearer-secret-value" not in message
    assert "2026-08-30" in message
    assert "根拠" in message
    assert "注意" in message
