from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from life_inbox.constants import EventStatus
from life_inbox.models import Event
from life_inbox.notion_payload import build_notion_event_plan
from life_inbox.rhythm_spending_capture import (
    RhythmSpendingCaptureService,
    parse_rhythm_spending,
    spending_event_uuid,
)
from life_inbox.rhythm_spending_models import RhythmSpendingEventLink


def test_parse_rhythm_spending_accepts_yen_and_short_action_button_forms():
    assert parse_rhythm_spending("マイ 600").game == "maimai"
    assert parse_rhythm_spending("マイ 600").amount_yen == 600
    assert parse_rhythm_spending("ウニ ¥100").game == "chunithm"
    assert parse_rhythm_spending("オンゲキ300円").game == "ongeki"
    assert parse_rhythm_spending("オンゲキ300円").amount_yen == 300


def test_parse_rhythm_spending_fails_closed_for_ambiguous_or_unrelated_numbers():
    assert parse_rhythm_spending("マイとゲキで600円") is None
    assert parse_rhythm_spending("マイ 600円 飲み物 150円") is None
    assert parse_rhythm_spending("マイナンバー 600") is None
    assert parse_rhythm_spending("maimai 0円") is None
    assert parse_rhythm_spending("maimaiのレート15432") is None


def test_reviewed_discord_spending_is_linked_and_notion_eligible_idempotently(db):
    with db.session() as session:
        event = Event(
            discord_message_id="rhythm-spending-message-1",
            discord_channel_id="30",
            discord_user_id="10",
            captured_at=datetime(2026, 9, 1, 9, 42),
            occurred_at=datetime(2026, 9, 1, 9, 42),
            raw_text="マイ 600",
            source="discord",
            status=EventStatus.PENDING.value,
            event_type="unknown",
            event_kinds=[],
        )
        session.add(event)
        session.commit()
        event_id = event.id

    capture = RhythmSpendingCaptureService(db.session_factory)
    first = capture.record_event(
        event_id,
        display_name="テスト",
        time_zone="Asia/Tokyo",
        allowed_discord_user_id=10,
    )
    second = capture.record_event(
        event_id,
        display_name="テスト",
        time_zone="Asia/Tokyo",
        allowed_discord_user_id=10,
    )

    assert first.created is True
    assert first.spending.game == "maimai"
    assert first.spending.amount_yen == 600
    assert first.spending.source == "shortcut"
    assert second.idempotent is True
    assert second.spending.event_id == spending_event_uuid("rhythm-spending-message-1")
    with db.session() as session:
        saved = session.get(Event, event_id)
        assert saved is not None
        assert saved.status == EventStatus.CONFIRMED.value
        assert saved.event_type == "rhythm_spending"
        assert saved.capture_context.route_category == "rhythm_spending"
        assert saved.rhythm_spending_link is not None
        assert saved.rhythm_spending_link.spending_event.amount_yen == 600
        links = list(session.scalars(select(RhythmSpendingEventLink)))
        assert len(links) == 1
        notion = build_notion_event_plan(saved)
        assert notion.eligible is True
        assert "Rhythm spending" in notion.draft.title
        assert '"amount_yen": 600' in notion.draft.body_markdown


def test_spending_timestamp_keeps_utc_fact_and_tokyo_local_date(db):
    with db.session() as session:
        event = Event(
            discord_message_id="rhythm-spending-midnight",
            discord_channel_id="30",
            discord_user_id="10",
            captured_at=datetime(2026, 8, 31, 15, 5),
            occurred_at=datetime(2026, 8, 31, 15, 5),
            raw_text="ウニ 100円",
            source="discord",
            status=EventStatus.PENDING.value,
            event_type="unknown",
            event_kinds=[],
        )
        session.add(event)
        session.commit()
        event_id = event.id

    result = RhythmSpendingCaptureService(db.session_factory).record_event(
        event_id,
        display_name="テスト",
        time_zone="Asia/Tokyo",
        allowed_discord_user_id=10,
    )

    assert result.spending.occurred_at == datetime(2026, 8, 31, 15, 5)
    assert result.spending.local_date.isoformat() == "2026-09-01"
    assert result.spending.synced_at.tzinfo is None
