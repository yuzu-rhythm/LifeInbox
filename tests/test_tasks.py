from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError

from life_inbox.db import Database
from life_inbox.models import LifeTask
from life_inbox.tasks import TaskService, TaskStatus

TOKYO = ZoneInfo("Asia/Tokyo")
FIXED_NOW = datetime(2026, 8, 29, 3, 0, tzinfo=UTC)


@pytest.fixture
def task_service(tmp_path: Path) -> TaskService:
    database = Database(f"sqlite:///{tmp_path / 'app.db'}")
    database.init()
    return TaskService(database.session_factory, timezone=TOKYO, now=lambda: FIXED_NOW)


def test_life_task_table_is_additive_and_enforces_safe_values(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'app.db'}")
    database.init()

    assert "life_tasks" in inspect(database.engine).get_table_names()
    with database.session() as session:
        session.add_all(
            [
                LifeTask(source="manual", title="一つ目"),
                LifeTask(source="manual", title="二つ目"),
            ]
        )
        session.commit()
        assert session.scalar(select(func.count(LifeTask.id))) == 2

    with database.session() as session:
        session.add(LifeTask(source="unsupported", title="拒否"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_manual_task_normalizes_text_and_interprets_local_date(task_service: TaskService):
    task = task_service.create_manual(
        "  Ａ　 B\n  C ",
        "  補足\r\n次の行  ",
        due_date="2026-08-31",
    )

    assert task.title == "A B C"
    assert task.description == "補足\n次の行"
    assert task.source == "manual"
    assert task.status == TaskStatus.OPEN.value
    assert task.due_date == date(2026, 8, 31)
    assert task.due_at == datetime(2026, 8, 30, 15, 0)

    with pytest.raises(ValueError, match="must not be blank"):
        task_service.create_manual("   ")
    with pytest.raises(ValueError, match="too long"):
        task_service.create_manual("x" * 256)
    with pytest.raises(ValueError, match="either"):
        task_service.create_manual(
            "重複指定",
            due_date=date(2026, 8, 31),
            due_at=datetime(2026, 8, 31, 12, tzinfo=TOKYO),
        )


def test_external_upsert_is_idempotent_scoped_by_source_and_preserves_completion(
    task_service: TaskService,
):
    first = task_service.upsert_external(
        "notion",
        "page-1",
        "Notionの課題",
        description="最初",
        due_at=datetime(2026, 8, 30, 12, tzinfo=TOKYO),
        source_url="https://notion.so/page-1",
    )
    repeated = task_service.upsert_external(
        "notion",
        "page-1",
        "Notionの課題",
        description="最初",
        due_at=datetime(2026, 8, 30, 12, tzinfo=TOKYO),
        source_url="https://notion.so/page-1",
    )

    assert repeated.id == first.id
    assert repeated.updated_at == first.updated_at
    assert len(task_service.list_tasks()) == 1

    refreshed = task_service.upsert_external(
        "notion",
        "page-1",
        "更新された課題",
        description=None,
        due_at=None,
        source_url=None,
    )
    assert refreshed.id == first.id
    assert refreshed.title == "更新された課題"
    assert refreshed.description is None
    assert refreshed.due_at is None
    assert refreshed.source_url is None

    same_external_id_other_source = task_service.upsert_external(
        "google_classroom",
        "page-1",
        "Classroomの課題",
    )
    assert same_external_id_other_source.id != first.id
    assert len(task_service.list_tasks()) == 2

    completed = task_service.complete(first.id)
    completed_again = task_service.complete(first.id)
    assert completed.status == TaskStatus.COMPLETED.value
    assert completed_again.completed_at == completed.completed_at
    assert completed_again.updated_at == completed.updated_at

    with pytest.raises(ValueError, match="http or https"):
        task_service.upsert_external(
            "notion",
            "bad-url",
            "URL確認",
            source_url="ftp://example.com/task",
        )
    with pytest.raises(ValueError, match="must not be blank"):
        task_service.upsert_external("notion", "   ", "IDなし")
    assert len(task_service.list_tasks()) == 2


def test_list_order_is_open_due_order_then_undated_then_completed(task_service: TaskService):
    task_service.create_manual("期限超過", due_date=date(2026, 8, 28))
    soon = task_service.create_manual("次の期限", due_date=date(2026, 8, 30))
    undated = task_service.create_manual("期限なし")
    task_service.create_manual("先の期限", due_date=date(2026, 9, 1))

    completed = task_service.complete(soon.id)
    listed = task_service.list_tasks()

    assert [task.title for task in listed] == ["期限超過", "先の期限", "期限なし", "次の期限"]
    assert listed[0].is_overdue is True
    assert listed[-1].status == TaskStatus.COMPLETED.value
    assert completed.completed_at is not None
    assert undated.due_date is None


def test_complete_requires_existing_positive_id_and_is_idempotent(task_service: TaskService):
    task = task_service.create_manual("完了する")
    first = task_service.complete(task.id)
    second = task_service.complete(task.id)

    assert first.completed_at == second.completed_at
    assert first.status == second.status == TaskStatus.COMPLETED.value
    with pytest.raises(ValueError, match="positive"):
        task_service.complete(0)
    with pytest.raises(ValueError, match="not found"):
        task_service.complete(999)
