from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from life_inbox.constants import EventKind, EventStatus, RhythmGame, RhythmTodoStatus
from life_inbox.db import Database
from life_inbox.models import Attachment, Base, Event, RhythmFavorite, RhythmResult, RhythmTodo
from life_inbox.rhythm import (
    RhythmFavoriteRecord,
    RhythmResultRecord,
    RhythmService,
    RhythmTodoResult,
    local_day_window,
)


def _event(
    db: Database,
    message_id: str,
    occurred_at: datetime | None = None,
    *,
    channel_id: str = "rhythm-channel",
) -> int:
    occurred_at = occurred_at or datetime(2026, 8, 28, 12, 0)
    with db.session() as session:
        event = Event(
            discord_message_id=message_id,
            discord_channel_id=channel_id,
            discord_user_id="user",
            captured_at=occurred_at,
            occurred_at=occurred_at,
            raw_text=message_id,
            source="discord",
            status=EventStatus.PENDING.value,
            event_type="unknown",
            event_kinds=[],
        )
        session.add(event)
        session.commit()
        return event.id


def test_create_todo_is_normalized_idempotent_and_marks_event(db):
    service = RhythmService(db.session_factory)
    event_id = _event(db, "todo-message")

    first = service.create_todo(event_id, RhythmGame.MAIMAI, "  Lv12 clear  ")
    second = service.create_todo(event_id, "MAIMAI", "Lv12 clear")

    assert isinstance(first, RhythmTodoResult)
    assert first == second
    assert first.game is RhythmGame.MAIMAI
    assert first.title == "Lv12 clear"
    assert first.status is RhythmTodoStatus.OPEN
    assert service.get_todo_by_event_message_id("todo-message") == first
    assert service.get_todo_by_event_message_id("missing-message") is None

    with db.session() as session:
        event = session.get(Event, event_id)
        assert event is not None
        assert event.status == EventStatus.CONFIRMED.value
        assert event.event_type == EventKind.RHYTHM_TODO.value
        assert event.event_kinds == [EventKind.RHYTHM_TODO.value]
        assert session.scalar(select(func.count(RhythmTodo.id))) == 1

    with pytest.raises(ValueError, match="different rhythm TODO"):
        service.create_todo(event_id, RhythmGame.ONGEKI, "Lv12 clear")


def test_record_result_infers_game_completes_todo_and_allows_history(db):
    service = RhythmService(db.session_factory)
    todo_event_id = _event(db, "todo-for-result")
    todo = service.create_todo(todo_event_id, RhythmGame.ONGEKI, "ABFB練習")
    first_result_event_id = _event(db, "result-message-1")
    second_result_event_id = _event(db, "result-message-2")

    first = service.record_result(
        first_result_event_id,
        note="  初回クリア  ",
        todo_id=todo.id,
    )
    second = service.record_result(
        second_result_event_id,
        RhythmGame.ONGEKI,
        "2回目",
        todo.id,
    )

    assert isinstance(first, RhythmResultRecord)
    assert first.game is RhythmGame.ONGEKI
    assert first.note == "初回クリア"
    assert first.todo_id == todo.id
    assert second.todo_id == todo.id
    assert [result.id for result in service.list_results()] == [second.id, first.id]
    assert service.list_results(RhythmGame.MAIMAI) == []
    assert service.list_open_todos() == []

    with db.session() as session:
        stored_todo = session.get(RhythmTodo, todo.id)
        assert stored_todo is not None
        assert stored_todo.status == RhythmTodoStatus.COMPLETED.value
        assert stored_todo.completed_at is not None
        completion_time = stored_todo.completed_at
        assert len(stored_todo.results) == 2

        result_event = session.get(Event, first_result_event_id)
        assert result_event is not None
        assert result_event.status == EventStatus.CONFIRMED.value
        assert result_event.event_type == EventKind.RHYTHM_RESULT.value
        assert result_event.event_kinds == [EventKind.RHYTHM_RESULT.value]

    # A later result keeps the original completion time while adding history.
    with db.session() as session:
        stored_todo = session.get(RhythmTodo, todo.id)
        assert stored_todo is not None
        assert stored_todo.completed_at == completion_time


def test_list_results_orders_by_occurred_at_and_detaches_channel_time_and_count(db):
    service = RhythmService(db.session_factory)
    newer_event_id = _event(db, "occurred-newer", datetime(2026, 8, 29, 12, 0))
    older_event_id = _event(db, "occurred-older", datetime(2026, 8, 28, 12, 0))

    newer = service.record_result(newer_event_id, RhythmGame.MAIMAI, "newer")
    older = service.record_result(older_event_id, RhythmGame.MAIMAI, "older")
    with db.session() as session:
        session.add(
            Attachment(
                event_id=newer_event_id,
                discord_attachment_id="newer-image",
                original_filename="newer.png",
                content_type="image/png",
                local_path="data/newer.png",
                sha256="newer-sha",
            )
        )
        session.add(
            Attachment(
                event_id=newer_event_id,
                discord_attachment_id="newer-image-2",
                original_filename="newer-2.png",
                content_type="image/png",
                local_path="data/newer-2.png",
                sha256="newer-sha-2",
            )
        )
        session.commit()

    listed = service.list_results()
    assert [item.id for item in listed] == [newer.id, older.id]
    assert listed[0].occurred_at == datetime(2026, 8, 29, 12, 0)
    assert listed[0].discord_channel_id == "rhythm-channel"
    assert listed[0].attachment_count == 2
    assert listed[0].todo_title is None


def test_favorites_normalize_idempotently_filter_and_upsert_without_delete(db):
    service = RhythmService(db.session_factory)
    manual = service.add_favorite(
        "MAIMAI",
        "  ＡＢ　Song  ",
        "  練習用　メモ ",
    )
    repeated = service.add_favorite("maimai", "AB Song", "練習用 メモ")
    assert isinstance(manual, RhythmFavoriteRecord)
    assert repeated == manual
    assert manual.song_title == "AB Song"
    assert manual.normalized_title == "ab song"
    assert manual.source == "manual"

    ongeki = service.add_favorite("ongeki", "Beta")
    no_external = service.upsert_provider_favorites(
        "maimai_net",
        [{"game": "maimai", "song_title": "Provider A"}],
    )[0]
    provider = service.upsert_provider_favorites(
        "maimai_net",
        [
            {
                "game": "maimai",
                "song_title": "Provider B",
                "external_id": "song-b",
                "note": "NET由来",
            }
        ],
    )[0]
    provider_update = service.upsert_provider_favorites(
        "maimai_net",
        [
            {
                "game": "maimai",
                "song_title": "Provider B updated",
                "external_id": "song-b",
            }
        ],
    )[0]
    assert provider_update.id == provider.id
    assert provider_update.song_title == "Provider B updated"
    assert provider_update.note is None
    assert service.upsert_provider_favorites("maimai_net", []) == []

    all_favorites = service.list_favorites()
    assert [favorite.song_title for favorite in all_favorites] == [
        "AB Song",
        "Provider A",
        "Provider B updated",
        "Beta",
    ]
    assert [favorite.song_title for favorite in service.list_favorites("MAIMAI")] == [
        "AB Song",
        "Provider A",
        "Provider B updated",
    ]
    assert no_external.id != manual.id
    assert ongeki.game is RhythmGame.ONGEKI


def test_favorite_model_constraints_and_local_day_boundaries(db):
    with pytest.raises(IntegrityError):
        with db.session() as session:
            session.add(
                RhythmFavorite(
                    game="beatmania",
                    song_title="invalid",
                    normalized_title="invalid",
                    source="manual",
                )
            )
            session.flush()

    start, end = local_day_window(date(2026, 8, 29), "Asia/Tokyo")
    assert start == datetime(2026, 8, 28, 15, 0)
    assert end == datetime(2026, 8, 29, 15, 0)
    service = RhythmService(db.session_factory)
    before_id = _event(db, "day-before", start - timedelta(microseconds=1))
    start_id = _event(db, "day-start", start)
    end_id = _event(db, "day-end", end - timedelta(microseconds=1))
    after_id = _event(db, "day-after", end)
    service.record_result(before_id, RhythmGame.MAIMAI, "before")
    start_result = service.record_result(start_id, RhythmGame.MAIMAI, "start")
    end_result = service.record_result(end_id, RhythmGame.CHUNITHM, "end")
    service.record_result(after_id, RhythmGame.MAIMAI, "after")

    today = service.list_results_today(
        timezone_name="Asia/Tokyo",
        target_date=date(2026, 8, 29),
    )
    assert [result.id for result in today] == [end_result.id, start_result.id]
    assert all(result.occurred_at is not None for result in today)


def test_result_requires_supported_game_or_todo_and_rejects_conflicts(db):
    service = RhythmService(db.session_factory)
    event_id = _event(db, "validation-result")
    todo_event_id = _event(db, "validation-todo")
    todo = service.create_todo(todo_event_id, RhythmGame.CHUNITHM, "AJ練習")

    with pytest.raises(ValueError, match="unsupported"):
        service.create_todo(_event(db, "unsupported-todo"), "beatmania", "曲")
    with pytest.raises(ValueError, match="blank"):
        service.create_todo(_event(db, "blank-todo"), RhythmGame.MAIMAI, " \n\t ")
    with pytest.raises(ValueError, match="not found"):
        service.create_todo(999_999, RhythmGame.MAIMAI, "曲")
    with pytest.raises(ValueError, match="game is required"):
        service.record_result(event_id)
    with pytest.raises(ValueError, match="TODO not found"):
        service.record_result(event_id, todo_id=999_999)
    with pytest.raises(ValueError, match="does not match"):
        service.record_result(event_id, RhythmGame.MAIMAI, todo_id=todo.id)

    first = service.record_result(event_id, RhythmGame.CHUNITHM, "記録")
    assert service.record_result(event_id, RhythmGame.CHUNITHM, "記録") == first
    with pytest.raises(ValueError, match="different rhythm result"):
        service.record_result(event_id, RhythmGame.CHUNITHM, "別の記録")

    blank_note_result = service.record_result(
        _event(db, "blank-note-result"), RhythmGame.CHUNITHM, " \n\t "
    )
    assert blank_note_result.note is None


def test_rhythm_foreign_keys_have_additive_delete_behavior(db):
    service = RhythmService(db.session_factory)
    todo_event_id = _event(db, "delete-todo-event")
    todo = service.create_todo(todo_event_id, RhythmGame.MAIMAI, "削除テスト")
    result_event_id = _event(db, "delete-result-event")
    result = service.record_result(result_event_id, RhythmGame.MAIMAI, todo_id=todo.id)

    with db.session() as session:
        session.delete(session.get(RhythmTodo, todo.id))
        session.commit()
        stored_result = session.get(RhythmResult, result.id)
        assert stored_result is not None
        assert stored_result.todo_id is None

        session.delete(session.get(Event, result_event_id))
        session.delete(session.get(Event, todo_event_id))
        session.commit()
        assert session.get(RhythmResult, result.id) is None
        assert session.get(RhythmTodo, todo.id) is None


def test_database_init_only_adds_rhythm_tables_to_legacy_and_finance_schema(tmp_path):
    database_path = tmp_path / "legacy.db"
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{database_path}")
    existing_table_names = {
        "events",
        "expenses",
        "meals",
        "attachments",
        "aliases",
        "ai_runs",
        "receipt_items",
        "financial_accounts",
        "financial_account_snapshots",
        "financial_account_settlement_links",
        "ledger_activities",
        "memos",
    }
    Base.metadata.create_all(
        engine,
        tables=[
            table
            for name, table in Base.metadata.tables.items()
            if name in existing_table_names
        ],
    )
    occurred_at = datetime(2026, 8, 28, 12, 0)
    with engine.begin() as connection:
        connection.execute(
            Event.__table__.insert().values(
                discord_message_id="legacy-event",
                discord_channel_id="legacy-channel",
                discord_user_id="legacy-user",
                captured_at=occurred_at,
                occurred_at=occurred_at,
                raw_text="legacy raw text",
                source="discord",
                status=EventStatus.CONFIRMED.value,
                event_type=EventKind.EXPENSE.value,
                event_kinds=[EventKind.EXPENSE.value],
            )
        )
    before = set(inspect(engine).get_table_names())

    Database(f"sqlite:///{database_path}").init()

    after = set(inspect(engine).get_table_names())
    assert after - before == {
        "life_tasks",
        "rhythm_todos",
        "rhythm_results",
        "rhythm_favorites",
        "maimai_net_favorite_entries",
        "maimai_mybest_entries",
        "monthly_budgets",
        "rhythm_users",
        "rhythm_provider_accounts",
        "rating_snapshots",
        "rating_entries",
        "rating_images",
        "rating_snapshot_images",
        "discord_deliveries",
        "daily_game_spending",
        "collection_runs",
        "collection_run_events",
        "collection_leases",
        "career_opportunities",
        "career_applications",
        "daily_routines",
        "routine_completions",
        "reminder_deliveries",
        "rhythm_spending_events",
        "rhythm_spending_event_links",
        "health_sleep_summaries",
        "health_workouts",
        "kiseki_moment_sync_records",
        "notion_sync_records",
        "shortcut_inbox_deliveries",
        "event_capture_contexts",
        "planner_image_items",
        "discord_archive_records",
        "archive_entries",
        "archive_attachments",
        "archive_deliveries",
        "archive_relations",
        "social_drafts",
    }
    with Session(engine) as session:
        event = session.scalar(select(Event).where(Event.discord_message_id == "legacy-event"))
        assert event is not None
        assert event.raw_text == "legacy raw text"
        assert event.status == EventStatus.CONFIRMED.value
        assert event.event_kinds == [EventKind.EXPENSE.value]
