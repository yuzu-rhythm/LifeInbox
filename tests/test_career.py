from __future__ import annotations

from datetime import date, datetime, time

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError

from life_inbox.career import (
    ApplicationStage,
    CareerKind,
    CareerService,
    CareerStatus,
    normalize_url,
)
from life_inbox.models import CareerApplication, CareerOpportunity, Event, LifeTask
from life_inbox.reminders import select_task_reminder_phase
from life_inbox.schemas import InboxRouteResult


def _event(db, *, message_id: str = "career-message", raw_text: str = "") -> int:
    with db.session() as session:
        event = Event(
            discord_message_id=message_id,
            discord_channel_id="30",
            discord_user_id="10",
            captured_at=datetime(2026, 8, 30, 0, 0),
            occurred_at=datetime(2026, 8, 30, 0, 0),
            raw_text=raw_text,
            source="discord",
            status="pending",
            event_type="unknown",
            event_kinds=[],
        )
        session.add(event)
        session.commit()
        return event.id


def test_career_tables_are_additive_and_preserve_existing_rows(db):
    with db.session() as session:
        existing_event = Event(
            discord_message_id="existing-career-event",
            discord_channel_id="30",
            discord_user_id="10",
            captured_at=datetime(2026, 8, 29),
            occurred_at=datetime(2026, 8, 29),
            raw_text="既存メモ",
            source="discord",
            status="confirmed",
            event_type="memo",
            event_kinds=["memo"],
        )
        existing_task = LifeTask(
            source="manual",
            title="既存タスク",
            status="open",
            created_at=datetime(2026, 8, 29),
            updated_at=datetime(2026, 8, 29),
        )
        session.add_all([existing_event, existing_task])
        session.commit()

    table_names = set(inspect(db.engine).get_table_names())
    assert {"career_opportunities", "career_applications"} <= table_names
    with db.session() as session:
        assert session.scalar(select(func.count(Event.id))) == 1
        assert session.scalar(select(func.count(LifeTask.id))) == 1

    service = CareerService(db.session_factory, timezone="Asia/Tokyo")
    record = service.create_opportunity("既存社", "研究職")
    assert record.kind == CareerKind.JOB.value
    with db.session() as session:
        assert session.scalar(select(func.count(Event.id))) == 1
        assert session.scalar(select(func.count(LifeTask.id))) == 1


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "//example.com/path",
        "https://user:password@example.com/jobs/1",
        "https://example.com/a path",
        "https://example.com:not-a-port/jobs/1",
        "ftp://example.com/jobs/1",
    ],
)
def test_career_url_validation_rejects_unsafe_values(value):
    with pytest.raises(ValueError):
        normalize_url(value)


def test_career_validation_rejects_unbounded_values_invalid_enums_and_bad_ids(db):
    service = CareerService(db.session_factory, timezone="Asia/Tokyo")
    with pytest.raises(ValueError):
        service.create_opportunity(" ", "職種")
    with pytest.raises(ValueError):
        service.create_opportunity("会社", "職種", kind="unknown")
    with pytest.raises(ValueError):
        service.create_opportunity("会社", "職種", url="javascript:bad")
    with pytest.raises(ValueError):
        service.create_opportunity("会社", "職種", source=0)
    with pytest.raises(ValueError, match="requires an explicit date"):
        service.create_opportunity("会社", "職種", deadline_time="15:00")
    with pytest.raises(ValueError, match="positive integer"):
        service.get_opportunity(0)
    with pytest.raises(ValueError, match="positive integer"):
        service.update_application(-1, stage=ApplicationStage.INTERVIEW)

    with db.session() as session:
        invalid = CareerOpportunity(
            id=-1,
            company="会社",
            title="職種",
            kind="job",
            status="open",
            source="manual",
        )
        session.add(invalid)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_create_update_and_stage_transition_sync_one_open_lifetask(db):
    now = datetime(2026, 8, 30, 0, 0)
    service = CareerService(db.session_factory, timezone="Asia/Tokyo", now=lambda: now)
    opportunity = service.create_opportunity(
        "Acme",
        "バックエンド",
        kind=CareerKind.JOB,
        deadline_date=date(2026, 9, 5),
        deadline_time=time(15, 0),
    )
    assert opportunity.deadline_at == datetime(2026, 9, 5, 6, 0)
    assert opportunity.linked_task_id is not None

    with db.session() as session:
        task = session.get(LifeTask, opportunity.linked_task_id)
        assert task is not None
        assert task.source == "manual"
        assert task.title == "[就活] Acme / バックエンド"
        assert task.status == "open"
        assert task.due_at == datetime(2026, 9, 5, 6, 0)
        candidate = select_task_reminder_phase(
            task,
            datetime(2026, 9, 5, 6, 0),
            timezone="Asia/Tokyo",
        )
        assert candidate is not None
        assert candidate.phase == "due"

    updated = service.update_opportunity(
        opportunity.id,
        deadline="2026-09-06 10:00",
        next_action="応募書類を確認",
    )
    assert updated.linked_task_id == opportunity.linked_task_id
    with db.session() as session:
        tasks = list(session.scalars(select(LifeTask)).all())
        assert len(tasks) == 1
        assert tasks[0].due_at == datetime(2026, 9, 6, 1, 0)
        assert tasks[0].status == "open"
        assert tasks[0].description is not None
        assert "応募書類を確認" in tasks[0].description

    closed = service.update_opportunity(updated.id, status=CareerStatus.REJECTED)
    assert closed.status == CareerStatus.REJECTED.value
    with db.session() as session:
        task = session.get(LifeTask, opportunity.linked_task_id)
        assert task is not None
        assert task.status == "completed"
        assert task.completed_at == now


def test_application_stage_and_deadline_are_separate_from_opportunity(db):
    service = CareerService(db.session_factory, timezone="Asia/Tokyo")
    opportunity = service.create_opportunity("Acme", "データ職")
    application = service.create_application(
        opportunity_id=opportunity.id,
        stage=ApplicationStage.APPLIED,
        deadline="2026-09-10 09:30",
        notes="履歴書を提出済み",
    )
    assert application.record_type == "application"
    assert application.company == "Acme"
    assert application.stage == ApplicationStage.APPLIED.value
    assert application.status == CareerStatus.APPLIED.value
    assert application.deadline_at == datetime(2026, 9, 10, 0, 30)

    progressed = service.update_application(
        application.id,
        stage=ApplicationStage.INTERVIEW,
        deadline="2026-09-12 14:00",
    )
    assert progressed.stage == ApplicationStage.INTERVIEW.value
    assert progressed.status == CareerStatus.IN_PROGRESS.value
    assert progressed.deadline_at == datetime(2026, 9, 12, 5, 0)
    assert progressed.linked_task_id == application.linked_task_id

    withdrawn = service.withdraw_application(progressed.id)
    assert withdrawn.stage == ApplicationStage.WITHDRAWN.value
    with db.session() as session:
        task = session.get(LifeTask, application.linked_task_id)
        assert task is not None
        assert task.status == "completed"


def test_source_event_and_external_identifier_upserts_are_idempotent(db):
    service = CareerService(db.session_factory, timezone="Asia/Tokyo")
    event_id = _event(db, raw_text="2026-09-05 15:00 Acme Backend")
    route = InboxRouteResult(
        destination="career",
        career_company="Acme",
        career_title="Backend",
        career_kind="application",
        career_stage="applied",
        career_deadline_date="2026-09-05",
        career_deadline_time="15:00",
        career_external_id="application-1",
    )
    first = service.create_from_route(event_id, route)
    second = service.create_from_route(event_id, route)
    assert second.id == first.id
    assert second.linked_task_id == first.linked_task_id

    with db.session() as session:
        assert session.scalar(select(func.count(CareerOpportunity.id))) == 1
        assert session.scalar(select(func.count(CareerApplication.id))) == 1
        assert session.scalar(select(func.count(LifeTask.id))) == 1
        event = session.get(Event, event_id)
        assert event is not None
        assert event.event_type == "career"
        assert event.status == "confirmed"

    one = service.create_opportunity(
        "Other",
        "説明会",
        kind=CareerKind.EVENT,
        source="fixture",
        external_id="event-1",
        deadline="2026-10-01",
    )
    two = service.create_opportunity(
        "Other",
        "説明会更新",
        kind=CareerKind.EVENT,
        source="fixture",
        external_id="event-1",
        deadline="2026-10-02",
    )
    assert two.id == one.id
    with db.session() as session:
        assert session.scalar(
            select(func.count(CareerOpportunity.id)).where(
                CareerOpportunity.source == "fixture"
            )
        ) == 1
        task = session.get(LifeTask, one.linked_task_id)
        assert task is not None
        assert task.due_at == datetime(2026, 10, 1, 15, 0)


def test_no_deadline_creates_no_lifetask_and_relative_text_is_not_parsed(db):
    service = CareerService(db.session_factory, timezone="Asia/Tokyo")
    record = service.create_opportunity("Acme", "求人", notes="明日確認")
    assert record.deadline_at is None
    assert record.linked_task_id is None
    with db.session() as session:
        assert session.scalar(select(func.count(LifeTask.id))) == 0


def test_list_records_application_filter_does_not_leak_opportunities(db):
    service = CareerService(db.session_factory, timezone="Asia/Tokyo")
    service.create_opportunity("Acme", "求人")
    application = service.create_application("Acme", "求人", stage=ApplicationStage.APPLIED)

    records = service.list_records(kind=CareerKind.APPLICATION)

    assert [record.id for record in records] == [application.id]
    assert records[0].record_type == "application"
