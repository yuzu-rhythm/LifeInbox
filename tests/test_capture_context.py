from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import inspect

from life_inbox.capture_context import (
    CaptureContextService,
    format_capture_location,
    location_map_url,
)
from life_inbox.inbox_classifier import InboxCandidate, InboxCategory, InboxPlannerItem
from life_inbox.models import Event


def _event(db) -> int:
    captured_at = datetime(2026, 9, 1, 3, 0)
    with db.session() as session:
        event = Event(
            discord_message_id="shortcut-fixture",
            discord_channel_id="shortcut-inbox:normal",
            discord_user_id="user",
            captured_at=captured_at,
            occurred_at=captured_at,
            raw_text="今日の記録",
            source="iphone_action_button",
            status="pending",
            event_type="unknown",
            event_kinds=[],
        )
        session.add(event)
        session.commit()
        return event.id


def test_capture_context_schema_and_location_are_explicit_single_capture(db):
    service = CaptureContextService(db.session_factory)
    event_id = _event(db)

    context = service.upsert_shortcut_context(
        event_id=event_id,
        capture_type="meal",
        source_label="actionButton",
        latitude=34.7692,
        longitude=137.3915,
        horizontal_accuracy_meters=12.4,
        location_label="豊橋駅付近",
        location_shared_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )

    assert {
        "event_capture_contexts",
        "shortcut_inbox_deliveries",
        "planner_image_items",
    }.issubset(inspect(db.engine).get_table_names())
    assert "豊橋駅付近" in format_capture_location(context)
    assert "精度 約12m" in format_capture_location(context)
    assert "maps.apple.com" in format_capture_location(context)
    assert location_map_url(34.7692, 137.3915).startswith("https://maps.apple.com/")


def test_shortcut_outbox_claim_retry_and_exactly_once_completion(db):
    service = CaptureContextService(db.session_factory)
    event_id = _event(db)
    service.ensure_shortcut_delivery(event_id)
    now = datetime(2026, 9, 1, 0, 0)

    claims = service.claim_shortcut_deliveries(now=now)
    assert [(claim.event_id, claim.attempt_count) for claim in claims] == [(event_id, 1)]
    assert service.claim_shortcut_deliveries(now=now + timedelta(seconds=5)) == []

    service.record_shortcut_source(
        event_id=event_id,
        attempt_count=1,
        source_channel_id="30",
        source_message_id="3001",
    )
    service.mark_shortcut_failed(
        event_id=event_id,
        attempt_count=1,
        error_code="TimeoutError",
        retryable=True,
        now=now,
    )
    assert service.claim_shortcut_deliveries(now=now + timedelta(seconds=1)) == []
    retry = service.claim_shortcut_deliveries(now=now + timedelta(seconds=3))
    assert [(claim.event_id, claim.attempt_count) for claim in retry] == [(event_id, 2)]

    service.mark_shortcut_posted(
        event_id=event_id,
        attempt_count=2,
        source_channel_id="30",
        source_message_id="3001",
        candidate_message_id="3002",
        now=now + timedelta(seconds=3),
    )
    delivery = service.get_shortcut_delivery(event_id)
    assert delivery is not None
    assert delivery.status == "posted"
    assert delivery.source_message_id == "3001"
    assert delivery.candidate_message_id == "3002"
    assert service.claim_shortcut_deliveries(now=now + timedelta(hours=1)) == []


def test_planner_items_are_durable_and_not_rewritten_by_model_retry(db):
    service = CaptureContextService(db.session_factory)
    event_id = _event(db)
    original = InboxPlannerItem(
        text="研究計画書を提出",
        candidates=(
            InboxCandidate(category=InboxCategory.TASK, confidence=0.95),
            InboxCandidate(category=InboxCategory.JOB_HUNTING, confidence=0.40),
        ),
    )

    rows = service.upsert_planner_items(event_id, (original,))
    repeated = service.upsert_planner_items(
        event_id,
        (
            InboxPlannerItem(
                text="別の推測結果",
                candidates=(
                    InboxCandidate(category=InboxCategory.MEMO, confidence=0.90),
                ),
            ),
        ),
    )

    assert len(rows) == 1
    assert repeated[0].raw_text == "研究計画書を提出"
    assert repeated[0].category == "task"
    service.mark_planner_item_posted(
        item_id=rows[0].id,
        channel_id="40",
        message_id="4001",
    )
    final = service.upsert_planner_items(event_id, (original,))[0]
    assert final.status == "posted"
    assert final.destination_message_id == "4001"
