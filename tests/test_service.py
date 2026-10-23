from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from life_inbox.analyzer import AnalyzerUnavailable
from life_inbox.constants import EventStatus, MealType, OutsideHome
from life_inbox.models import AIRun, Attachment, Event
from life_inbox.planner import render_planner
from life_inbox.schemas import (
    MealAnalysisResult,
    ReceiptAnalysisResult,
    ReceiptItemResult,
    TextAnalysisResult,
)
from life_inbox.services import InboxService, IncomingAttachment, IncomingMessage


class FakeAnalyzer:
    provider = "fake"
    model = "fake-model"
    prompt_version = "test-1"

    def __init__(self, *, text=None, meal=None, receipt=None, fail_text=False):
        self.text = text
        self.meal = meal
        self.receipt = receipt
        self.fail_text = fail_text
        self.text_calls = 0
        self.meal_calls = 0
        self.receipt_calls = 0

    def analyze_text(self, text):
        self.text_calls += 1
        if self.fail_text:
            raise AnalyzerUnavailable("test failure")
        return self.text or TextAnalysisResult()

    def analyze_receipt(self, path, content_type, *, context_text=""):
        self.receipt_calls += 1
        if self.receipt is None:
            raise AnalyzerUnavailable("receipt not configured")
        return self.receipt

    def analyze_meal(self, path, content_type, *, context_text=""):
        self.meal_calls += 1
        return self.meal or MealAnalysisResult()


def _message(message_id, text, *, attachment=None):
    attachments = [attachment] if attachment else []
    return IncomingMessage(
        discord_message_id=message_id,
        discord_channel_id=123,
        discord_user_id=456,
        raw_text=text,
        captured_at=datetime(2026, 8, 28, 3, 0, tzinfo=UTC),
        attachments=attachments,
    )


def test_capture_persists_raw_first_and_uses_alias_without_ai(service, db):
    result = service.capture(_message("m-1", "680 マック"))
    assert not result.duplicate
    event = service.get_event(result.event_id)
    assert event is not None
    assert event.raw_text == "680 マック"
    assert event.status == EventStatus.PENDING
    assert len(event.expenses) == 1
    assert event.expenses[0].amount == 680
    assert event.expenses[0].merchant_raw == "マック"
    assert event.expenses[0].merchant_normalized == "マクドナルド"


def test_duplicate_discord_message_id_does_not_create_second_event(service, db):
    first = service.capture(_message("same", "680 マック"))
    second = service.capture(_message("same", "999 別の店"))
    assert second.duplicate
    assert second.event_id == first.event_id
    with db.session() as session:
        assert session.scalar(select(func.count(Event.id))) == 1


def test_capture_raw_preserves_literal_event_without_invoking_analyzer(db, settings):
    analyzer = FakeAnalyzer(fail_text=True)
    raw_service = InboxService(db.session_factory, settings, analyzer)

    result = raw_service.capture_raw(_message("raw-only", "忘れずに牛乳を買う"))

    event = raw_service.get_event(result.event_id)
    assert event is not None
    assert event.raw_text == "忘れずに牛乳を買う"
    assert event.status == EventStatus.PENDING
    assert event.event_kinds == []
    assert analyzer.text_calls == 0


def test_card_channel_account_prefix_is_not_treated_as_merchant(db, settings):
    card_settings = settings.model_copy(update={"card_channel_id": 123})
    card_service = InboxService(db.session_factory, card_settings, FakeAnalyzer())

    result = card_service.capture(_message("card-prefix", "[楽天カード] 680 マック"))

    event = card_service.get_event(result.event_id)
    assert event is not None
    assert event.raw_text == "[楽天カード] 680 マック"
    assert event.expenses[0].amount == 680
    assert event.expenses[0].merchant_raw == "マック"
    assert event.expenses[0].merchant_normalized == "マクドナルド"


def test_card_channel_image_only_forces_receipt_analysis(db, settings):
    card_settings = settings.model_copy(update={"card_channel_id": 123})
    analyzer = FakeAnalyzer(
        receipt=ReceiptAnalysisResult(
            merchant="スーパー",
            total=1_280,
            confidence=0.88,
        )
    )
    card_service = InboxService(db.session_factory, card_settings, analyzer)

    result = card_service.capture(
        _message(
            "card-receipt-only",
            "[楽天カード]",
            attachment=IncomingAttachment(
                "card-receipt-image", "receipt.png", "image/png", b"png-bytes"
            ),
        )
    )

    event = card_service.get_event(result.event_id)
    assert event is not None
    assert event.expenses[0].amount == 1_280
    assert analyzer.receipt_calls == 1
    assert analyzer.meal_calls == 0


def test_meal_channel_forces_food_image_analysis_only(db, settings):
    meal_settings = settings.model_copy(update={"meal_channel_id": 123})
    analyzer = FakeAnalyzer(
        meal=MealAnalysisResult(
            meal_type=MealType.DINNER,
            description="カレー",
            outside_home=OutsideHome.HOME,
            confidence=0.91,
        )
    )
    meal_service = InboxService(db.session_factory, meal_settings, analyzer)

    result = meal_service.capture(
        _message(
            "meal-channel",
            "カレー",
            attachment=IncomingAttachment(
                "meal-channel-image", "dinner.png", "image/png", b"png-bytes"
            ),
        )
    )

    event = meal_service.get_event(result.event_id)
    assert event is not None
    assert event.raw_text == "カレー"
    assert len(event.meals) == 1
    assert event.meals[0].meal_type == MealType.DINNER
    assert analyzer.meal_calls == 1
    assert analyzer.receipt_calls == 0


def test_combined_expense_and_meal_share_one_event(service, db, settings):
    analyzer = FakeAnalyzer(
        meal=MealAnalysisResult(
            meal_type=MealType.LUNCH,
            description="ラーメン",
            restaurant="魂心家",
            outside_home=OutsideHome.OUTSIDE,
            confidence=0.92,
        )
    )
    combined = InboxService(db.session_factory, settings, analyzer)
    result = combined.capture(
        _message(
            "combined",
            "980 魂心家",
            attachment=IncomingAttachment("att-1", "meal.png", "image/png", b"png-bytes"),
        )
    )
    event = combined.get_event(result.event_id)
    assert event is not None
    assert event.event_type == "expense+meal"
    assert len(event.expenses) == 1
    assert len(event.meals) == 1
    assert event.expenses[0].event_id == event.meals[0].event_id == event.id
    assert analyzer.meal_calls == 1


def test_ai_failure_leaves_pending_and_records_safe_run(service, db, settings):
    analyzer = FakeAnalyzer(fail_text=True)
    failing = InboxService(db.session_factory, settings, analyzer)
    result = failing.capture(_message("ai-fail", "知らない買い物"))
    event = failing.get_event(result.event_id)
    assert event is not None
    assert event.status == EventStatus.PENDING
    assert result.analysis_error == "AIAnalysisFailed"
    with db.session() as session:
        run = session.scalar(select(AIRun).where(AIRun.event_id == event.id))
        assert run is not None
        payload = json.loads(run.result_json)
        assert payload["ok"] is False
        assert payload["error_type"] == "AnalyzerUnavailable"
        assert "test failure" not in run.result_json


def test_ai_text_result_is_validated_and_stored(service, db, settings):
    analyzer = FakeAnalyzer(
        text=TextAnalysisResult(
            event_kinds=["expense"],
            amount=1200,
            merchant="未知店",
            category="食費",
            subcategory="外食",
            description="昼食",
            confidence=0.8,
        )
    )
    analyzed = InboxService(db.session_factory, settings, analyzer)
    result = analyzed.capture(_message("ai-text", "何か買った"))
    event = analyzed.get_event(result.event_id)
    assert event is not None
    assert event.expenses[0].amount == 1200
    assert event.expenses[0].category == "食費"
    assert analyzer.text_calls == 1


def test_confirm_learns_alias_and_copy_state(service):
    result = service.capture(_message("confirm", "680 マック"))
    confirmed = service.confirm_event(result.event_id)
    assert confirmed.status == EventStatus.CONFIRMED
    assert service.list_uncopied()[0].id == result.event_id
    assert service.mark_copied([result.event_id]) == [result.event_id]
    assert service.list_uncopied() == []
    event = service.get_event(result.event_id)
    assert event is not None
    planner = render_planner([event], datetime(2026, 8, 28).date())
    assert "マクドナルド" in planner
    assert "680" in planner
    assert "合計" in planner


def test_attachment_failure_does_not_rollback_an_earlier_attachment(service, db):
    result = service.capture(
        IncomingMessage(
            discord_message_id="partial-attachments",
            discord_channel_id=123,
            discord_user_id=456,
            raw_text="レシート",
            captured_at=datetime(2026, 8, 28, 3, 0, tzinfo=UTC),
            attachments=[
                IncomingAttachment("valid", "receipt.png", "image/png", b"png-bytes"),
                IncomingAttachment("invalid", "notes.txt", "text/plain", b"not-an-image"),
            ],
        )
    )

    assert result.attachment_errors == ["AttachmentError"]
    with db.session() as session:
        attachments = list(
            session.scalars(select(Attachment).where(Attachment.event_id == result.event_id)).all()
        )
    assert len(attachments) == 1
    assert attachments[0].discord_attachment_id == "valid"
    assert Path(attachments[0].local_path).is_file()


def test_editing_one_event_kind_does_not_create_the_other(service):
    expense_result = service.capture(_message("edit-expense", "680 マック"))
    expense_event = service.edit_event(
        expense_result.event_id,
        amount="700",
        merchant="マクドナルド",
        category="食費",
        description="昼食",
    )
    assert len(expense_event.expenses) == 1
    assert expense_event.meals == []
    assert expense_event.expenses[0].merchant_raw == "マック"
    assert expense_event.expenses[0].merchant_normalized == "マクドナルド"

    meal_result = service.capture(_message("edit-meal", "昼飯 魂心家"))
    meal_event = service.edit_event(
        meal_result.event_id,
        meal_type="昼",
        restaurant="魂心家",
        meal_description="ラーメン",
    )
    assert meal_event.expenses == []
    assert len(meal_event.meals) == 1


def test_retry_is_limited_to_pending_events(service):
    result = service.capture(_message("retry-confirmed", "680 マック"))
    service.confirm_event(result.event_id)

    with pytest.raises(ValueError, match="only pending"):
        service.retry_event(result.event_id)


def test_receipt_datetime_without_offset_uses_configured_timezone(db, settings):
    analyzer = FakeAnalyzer(
        receipt=ReceiptAnalysisResult(
            merchant="セブン",
            purchase_datetime=datetime(2026, 8, 28, 12, 0),
            subtotal=100,
            tax=0,
            total=100,
            confidence=0.95,
        )
    )
    receipt_service = InboxService(db.session_factory, settings, analyzer)
    result = receipt_service.capture(
        _message(
            "receipt-timezone",
            "レシート",
            attachment=IncomingAttachment(
                "receipt-timezone-image",
                "receipt.png",
                "image/png",
                b"png-bytes",
            ),
        )
    )

    event = receipt_service.get_event(result.event_id)
    assert event is not None
    assert event.occurred_at == datetime(2026, 8, 28, 3, 0)
    assert event.expenses[0].amount == 100
    assert event.expenses[0].merchant_normalized == "セブン-イレブン"


def test_receipt_items_are_preserved_when_total_is_unreadable(db, settings):
    analyzer = FakeAnalyzer(
        receipt=ReceiptAnalysisResult(
            merchant="近所のスーパー",
            subtotal=398,
            total=None,
            items=[
                ReceiptItemResult(name="牛乳", quantity=1, unit_price=198, amount=198),
                ReceiptItemResult(name="食パン", quantity=1, unit_price=200, amount=200),
            ],
            confidence=0.72,
        )
    )
    receipt_service = InboxService(db.session_factory, settings, analyzer)
    result = receipt_service.capture(
        _message(
            "receipt-items-without-total",
            "レシート",
            attachment=IncomingAttachment(
                "receipt-items-without-total-image",
                "receipt.png",
                "image/png",
                b"png-bytes",
            ),
        )
    )

    event = receipt_service.get_event(result.event_id)
    assert event is not None
    assert len(event.expenses) == 1
    expense = event.expenses[0]
    assert expense.amount is None
    assert expense.merchant_normalized == "近所のスーパー"
    assert [item.name for item in expense.receipt_items] == ["牛乳", "食パン"]
    assert [item.amount for item in expense.receipt_items] == [198, 200]


def test_retry_replaces_old_receipt_items_with_an_empty_result(db, settings):
    analyzer = FakeAnalyzer(
        receipt=ReceiptAnalysisResult(
            merchant="セブン",
            total=100,
            items=[ReceiptItemResult(name="お茶", quantity=1, amount=100)],
            confidence=0.9,
        )
    )
    receipt_service = InboxService(db.session_factory, settings, analyzer)
    result = receipt_service.capture(
        _message(
            "receipt-items-retry",
            "レシート",
            attachment=IncomingAttachment(
                "receipt-items-retry-image",
                "receipt.png",
                "image/png",
                b"png-bytes",
            ),
        )
    )
    first = receipt_service.get_event(result.event_id)
    assert first is not None
    assert [item.name for item in first.expenses[0].receipt_items] == ["お茶"]

    analyzer.receipt = ReceiptAnalysisResult(
        merchant="セブン",
        total=100,
        items=[],
        confidence=0.8,
    )
    retried = receipt_service.retry_event(result.event_id)

    assert retried.expenses[0].receipt_items == []


def test_empty_meal_analysis_does_not_create_false_meal(db, settings):
    analyzer = FakeAnalyzer(meal=MealAnalysisResult())
    image_service = InboxService(db.session_factory, settings, analyzer)
    result = image_service.capture(
        _message(
            "unknown-image",
            "",
            attachment=IncomingAttachment(
                "unknown-image-attachment",
                "unknown.png",
                "image/png",
                b"png-bytes",
            ),
        )
    )

    event = image_service.get_event(result.event_id)
    assert event is not None
    assert event.meals == []
    assert event.event_type == "unknown"
