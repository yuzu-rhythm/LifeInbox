from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from life_inbox.constants import EventKind, EventStatus
from life_inbox.kiseki_models import KisekiMomentSyncRecord
from life_inbox.kiseki_sync import (
    KisekiMomentPayload,
    KisekiMomentRevisionConflictError,
    KisekiSyncService,
    StaleKisekiMomentRevisionError,
    kiseki_content_sha256,
)
from life_inbox.models import Event, Memo

MOMENT_ID = "550e8400-e29b-41d4-a716-446655440000"
SYNC_ONE = datetime(2026, 8, 30, 3, 0, tzinfo=UTC)


def _payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "momentId": MOMENT_ID,
        "revision": 1,
        "occurredAt": "2026-08-30T12:00:00+09:00",
        "title": "散歩の記録",
        "note": "今日のメモ",
        "mood": 4,
        "favorite": True,
        "origin": "iphone",
        "privacyProcessed": True,
    }
    value.update(changes)
    return value


def _sync(db) -> KisekiSyncService:
    return KisekiSyncService(db.session_factory)


def test_kiseki_table_has_required_columns_and_no_location_columns():
    columns = {column.name.casefold() for column in KisekiMomentSyncRecord.__table__.columns}
    assert {
        "moment_id",
        "event_id",
        "revision",
        "semantic_fingerprint",
        "deleted_at",
        "synced_at",
        "created_at",
        "updated_at",
    } <= columns
    forbidden = {"latitude", "longitude", "accuracy", "address", "location"}
    assert not any(
        any(marker in column_name for marker in forbidden) for column_name in columns
    )

    payload_fields = {field.casefold() for field in KisekiMomentPayload.model_fields}
    assert not any(
        any(marker in field_name for marker in forbidden) for field_name in payload_fields
    )


def test_create_update_delete_and_restore_reuses_one_event(db):
    sync = _sync(db)
    first = sync.upsert_moment(
        _payload(momentId=MOMENT_ID.upper()),
        synced_at=SYNC_ONE,
    )

    with db.session() as session:
        event = session.get(Event, first.event_id)
        assert event is not None
        assert event.source == "kiseki"
        assert event.discord_channel_id == "kiseki-sync"
        assert event.discord_user_id == "kiseki"
        assert event.discord_message_id == f"kiseki-moment:{MOMENT_ID}"
        assert event.status == EventStatus.CONFIRMED.value
        assert event.event_type == "memo+kiseki_moment"
        assert event.event_kinds == [EventKind.MEMO.value, "kiseki_moment"]
        assert event.occurred_at == datetime(2026, 8, 30, 3, 0)
        assert event.memo is not None
        assert event.memo.body == "散歩の記録\n今日のメモ"

    updated = sync.upsert_moment(
        _payload(
            revision=2,
            occurredAt="2026-08-31T08:30:00+09:00",
            title="更新後の記録",
            note="更新されたメモ",
            mood=2,
            favorite=False,
        ),
        synced_at=datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
    )
    assert updated.created is False
    assert updated.idempotent is False
    assert updated.record.event_id == first.event_id
    assert updated.record.revision == 2
    assert updated.record.deleted is False

    deleted = sync.delete_moment(
        MOMENT_ID,
        3,
        synced_at=datetime(2026, 8, 31, 1, 0, tzinfo=UTC),
    )
    assert deleted.record.event_id == first.event_id
    assert deleted.record.deleted is True
    assert deleted.record.deleted_at == datetime(2026, 8, 31, 1, 0)

    with db.session() as session:
        event = session.get(Event, first.event_id)
        assert event is not None
        assert event.status == EventStatus.IGNORED.value
        assert event.raw_text == ""
        assert event.memo is None
        assert session.scalar(select(func.count(Memo.id)).where(Memo.event_id == event.id)) == 0

    restored = sync.upsert_moment(
        _payload(
            revision=4,
            title="復元された記録",
            note="あとから復元",
            favorite=False,
        ),
        synced_at=datetime(2026, 8, 31, 2, 0, tzinfo=UTC),
    )
    assert restored.record.event_id == first.event_id
    assert restored.record.deleted is False

    with db.session() as session:
        events = list(session.scalars(select(Event)).all())
        assert len(events) == 1
        event = events[0]
        assert event.id == first.event_id
        assert event.status == EventStatus.CONFIRMED.value
        assert event.raw_text == "復元された記録\nあとから復元"
        assert event.memo is not None
        assert event.memo.body == "復元された記録\nあとから復元"


def test_same_revision_same_content_is_idempotent_and_ignores_sync_time(db):
    sync = _sync(db)
    first = sync.upsert_moment(_payload(), synced_at=SYNC_ONE)
    repeated = sync.upsert_moment(
        _payload(),
        synced_at=datetime(2026, 8, 30, 4, 0, tzinfo=UTC),
    )

    assert first.record.semantic_fingerprint == repeated.record.semantic_fingerprint
    assert repeated.created is False
    assert repeated.idempotent is True
    assert repeated.record.synced_at == datetime(2026, 8, 30, 3, 0)
    with db.session() as session:
        assert session.scalar(select(func.count(Event.id))) == 1
        assert session.scalar(select(func.count(KisekiMomentSyncRecord.id))) == 1


def test_same_revision_different_content_conflicts_and_keeps_old_content(db):
    sync = _sync(db)
    first = sync.upsert_moment(_payload(), synced_at=SYNC_ONE)

    with pytest.raises(KisekiMomentRevisionConflictError):
        sync.upsert_moment(
            _payload(title="別の内容"),
            synced_at=datetime(2026, 8, 30, 4, 0, tzinfo=UTC),
        )

    with db.session() as session:
        event = session.get(Event, first.event_id)
        assert event is not None
        assert event.memo is not None
        assert event.memo.body == "散歩の記録\n今日のメモ"
        row = session.get(KisekiMomentSyncRecord, first.record.id)
        assert row is not None
        assert row.revision == 1


def test_older_active_or_delete_revision_is_stale(db):
    sync = _sync(db)
    sync.upsert_moment(_payload(revision=3), synced_at=SYNC_ONE)

    with pytest.raises(StaleKisekiMomentRevisionError):
        sync.upsert_moment(_payload(revision=2, title="古い内容"))
    with pytest.raises(StaleKisekiMomentRevisionError):
        sync.delete_moment(MOMENT_ID, 2)


def test_delete_requires_privacy_attestation_and_can_create_a_tombstone(db):
    sync = _sync(db)

    with pytest.raises(ValidationError):
        sync.delete({"momentId": MOMENT_ID, "revision": 1})
    with pytest.raises(ValidationError):
        sync.delete(
            {"momentId": MOMENT_ID, "revision": 1, "privacyProcessed": False}
        )

    deleted = sync.delete(
        {"momentId": MOMENT_ID, "revision": 1, "privacyProcessed": True},
        synced_at=SYNC_ONE,
    )
    assert deleted.created is True
    assert deleted.record.deleted is True
    with db.session() as session:
        event = session.get(Event, deleted.event_id)
        assert event is not None
        assert event.status == EventStatus.IGNORED.value
        assert event.raw_text == ""


def test_delete_same_revision_is_idempotent_and_different_content_conflicts(db):
    sync = _sync(db)
    first = sync.upsert_moment(_payload(), synced_at=SYNC_ONE)
    deleted = sync.delete_moment(MOMENT_ID, 2, synced_at=SYNC_ONE)
    repeated = sync.delete_moment(
        MOMENT_ID,
        2,
        synced_at=datetime(2026, 8, 30, 4, 0, tzinfo=UTC),
    )

    assert deleted.record.event_id == first.event_id
    assert repeated.idempotent is True
    assert repeated.record.deleted is True
    with pytest.raises(KisekiMomentRevisionConflictError):
        sync.upsert_moment(_payload(revision=2), synced_at=SYNC_ONE)


def test_payload_normalizes_uuid_and_requires_timezone_aware_occurred_at():
    payload = KisekiMomentPayload.model_validate(_payload(momentId=MOMENT_ID.upper()))
    assert payload.moment_id == MOMENT_ID
    assert payload.occurred_at == datetime(2026, 8, 30, 3, 0, tzinfo=UTC)

    with pytest.raises(ValidationError, match="occurredAt"):
        KisekiMomentPayload.model_validate(
            _payload(occurredAt=datetime(2026, 8, 30, 12, 0))
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "x" * 201),
        ("note", "x" * 10_001),
        ("mood", 0),
        ("mood", 6),
        ("origin", "x" * 33),
        ("origin", "iphone app"),
        ("revision", 0),
        ("revision", True),
    ],
)
def test_payload_bounds_are_rejected(field: str, value: object):
    with pytest.raises(ValidationError):
        KisekiMomentPayload.model_validate(_payload(**{field: value}))


def test_payload_rejects_location_fields_and_false_or_missing_privacy_flag():
    for field in ("latitude", "longitude", "accuracy", "address", "location"):
        with pytest.raises(ValidationError):
            KisekiMomentPayload.model_validate(_payload(**{field: "forbidden"}))

    with pytest.raises(ValidationError):
        KisekiMomentPayload.model_validate(_payload(privacyProcessed=False))
    missing = _payload()
    del missing["privacyProcessed"]
    with pytest.raises(ValidationError):
        KisekiMomentPayload.model_validate(missing)


def test_fingerprint_changes_with_semantic_content_but_not_sync_timestamp():
    first = KisekiMomentPayload.model_validate(_payload())
    same_content = KisekiMomentPayload.model_validate(_payload())
    changed = KisekiMomentPayload.model_validate(_payload(note="別のメモ"))
    assert kiseki_content_sha256(first) == kiseki_content_sha256(same_content)
    assert kiseki_content_sha256(first) != kiseki_content_sha256(changed)
