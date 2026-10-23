from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from life_inbox.budgets import BudgetMonthView, BudgetService
from life_inbox.constants import EventKind, EventStatus
from life_inbox.db import Database
from life_inbox.models import Base, Event, Expense, MonthlyBudget


def _add_expense(
    db: Database,
    message_id: str,
    occurred_at: datetime,
    amount: int | None,
    category: str = "食費",
    status: EventStatus = EventStatus.CONFIRMED,
) -> int:
    with db.session() as session:
        event = Event(
            discord_message_id=message_id,
            discord_channel_id="budget-channel",
            discord_user_id="budget-user",
            captured_at=occurred_at,
            occurred_at=occurred_at,
            raw_text=message_id,
            source="discord",
            status=status.value,
            event_type=EventKind.EXPENSE.value,
            event_kinds=[EventKind.EXPENSE.value],
        )
        event.expenses.append(
            Expense(
                amount=amount,
                merchant_raw=message_id,
                merchant_normalized=message_id,
                category=category,
            )
        )
        session.add(event)
        session.commit()
        return event.id


def test_monthly_budget_schema_has_non_nullable_scope_key_and_additive_indexes(db):
    table = MonthlyBudget.__table__

    assert table.c.id.primary_key
    assert not table.c.month.nullable
    assert not table.c.scope.nullable
    assert not table.c.category_key.nullable
    assert not table.c.amount.nullable
    assert not table.c.created_at.nullable
    assert not table.c.updated_at.nullable
    assert {
        "uq_monthly_budgets_month_scope_category",
        "ck_monthly_budgets_month",
        "ck_monthly_budgets_scope",
        "ck_monthly_budgets_scope_category_shape",
        "ck_monthly_budgets_category_key",
        "ck_monthly_budgets_amount",
    } <= {constraint.name for constraint in table.constraints}
    assert {
        "ix_monthly_budgets_month_scope",
        "ix_monthly_budgets_month_category",
    } <= {index.name for index in table.indexes}


def test_budget_validation_canonicalizes_month_and_category_and_rejects_invalid_values(db):
    service = BudgetService(
        db.session_factory,
        timezone="Asia/Tokyo",
        allowed_categories=("食費", "娯楽"),
    )

    progress = service.set_budget(50_000, month=" 2026-08 ", category=" 食費 ")

    assert progress.month == "2026-08"
    assert progress.category == "食費"
    assert progress.amount == 50_000

    for month in ("2026-8", "2026-00", "2026-13", "2026-08-01", "202x-08"):
        with pytest.raises(ValueError, match="month"):
            service.set_budget(1_000, month=month)
    for amount in (0, -1, 10_000_001, True, 1.5, "1000"):
        with pytest.raises(ValueError, match="amount"):
            service.set_budget(amount, month="2026-08")
    for category in ("", " 未設定 ", "__overall__"):
        with pytest.raises(ValueError, match="category"):
            service.set_budget(1_000, month="2026-08", category=category)


def test_budget_default_month_uses_configured_timezone(db, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 8, 31, 15, 30, tzinfo=UTC)
            return value.astimezone(tz) if tz is not None else value

    monkeypatch.setattr("life_inbox.budgets.datetime", FrozenDateTime)
    service = BudgetService(
        db.session_factory,
        timezone="Asia/Tokyo",
        allowed_categories=("食費",),
    )

    assert service.get_month().month == "2026-09"
    assert service.set_budget(1_000).month == "2026-09"


def test_budget_spending_uses_local_month_bounds_and_confirmed_expenses_only(db):
    service = BudgetService(
        db.session_factory,
        timezone="Asia/Tokyo",
        allowed_categories=("食費", "娯楽", "その他"),
    )
    service.set_budget(10_000, month="2026-08")
    service.set_budget(4_000, month="2026-08", category="食費")
    service.set_budget(1_000, month="2026-08", category="娯楽")

    # Tokyo August starts at 2026-07-31 15:00 UTC and ends at
    # 2026-08-31 15:00 UTC. The half-open bounds include both edge records
    # inside the month and exclude the records immediately outside it.
    _add_expense(db, "at-start", datetime(2026, 7, 31, 15, 0), 1_000, "食費")
    _add_expense(db, "before-start", datetime(2026, 7, 31, 14, 59, 59), 20_000, "食費")
    _add_expense(db, "at-end", datetime(2026, 8, 31, 14, 59, 59), 2_000, "娯楽")
    _add_expense(db, "at-next-start", datetime(2026, 8, 31, 15, 0), 20_000, "娯楽")
    _add_expense(
        db,
        "pending",
        datetime(2026, 8, 15, 12, 0),
        30_000,
        "食費",
        EventStatus.PENDING,
    )
    _add_expense(
        db,
        "ignored",
        datetime(2026, 8, 16, 12, 0),
        30_000,
        "娯楽",
        EventStatus.IGNORED,
    )
    _add_expense(db, "unknown-amount", datetime(2026, 8, 17, 12, 0), None, "その他")

    snapshot = service.get_month("2026-08")

    assert isinstance(snapshot, BudgetMonthView)
    assert snapshot.overall is not None
    assert snapshot.overall.spent == 3_000
    assert snapshot.overall.remaining == 7_000
    assert snapshot.overall.percent == pytest.approx(30)
    assert snapshot.overall.visual_percent == pytest.approx(30)
    by_category = {item.category: item for item in snapshot.categories}
    assert by_category["食費"].spent == 1_000
    assert by_category["食費"].remaining == 3_000
    assert by_category["娯楽"].spent == 2_000
    assert by_category["娯楽"].remaining == -1_000
    assert by_category["娯楽"].percent == pytest.approx(200)
    assert by_category["娯楽"].visual_percent == pytest.approx(100)


def test_budget_updates_are_idempotent_and_keep_one_row_per_scope(db):
    service = BudgetService(db.session_factory, allowed_categories=("食費",))

    first = service.set_budget(10_000, month="2026-08")
    with db.session() as session:
        stored_first = session.scalar(
            select(MonthlyBudget).where(
                MonthlyBudget.month == "2026-08",
                MonthlyBudget.scope == "overall",
            )
        )
        assert stored_first is not None
        first_id = stored_first.id
        created_at = stored_first.created_at

    second = service.set_budget(12_000, month="2026-08")
    category_first = service.set_budget(3_000, month="2026-08", category="食費")
    category_second = service.set_budget(4_000, month="2026-08", category="食費")

    assert first.amount == 10_000
    assert second.amount == 12_000
    assert category_first.amount == 3_000
    assert category_second.amount == 4_000
    with db.session() as session:
        rows = list(
            session.scalars(
                select(MonthlyBudget).where(MonthlyBudget.month == "2026-08")
            )
        )
        assert len(rows) == 2
        overall = next(row for row in rows if row.scope == "overall")
        category = next(row for row in rows if row.scope == "category")
        assert overall.id == first_id
        assert overall.amount == 12_000
        assert overall.created_at == created_at
        assert overall.updated_at >= created_at
        assert category.category_key == "食費"
        assert category.category is not None
        assert category.amount == 4_000

    with pytest.raises(IntegrityError):
        with db.session() as session:
            session.add(
                MonthlyBudget(
                    month="2026-08",
                    scope="overall",
                    category_key=MonthlyBudget.OVERALL_CATEGORY_KEY,
                    amount=1_000,
                )
            )
            session.commit()


def test_database_init_adds_monthly_budgets_without_losing_existing_event(tmp_path):
    database_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{database_path}")
    legacy_table_names = {
        "events",
        "expenses",
        "meals",
        "attachments",
        "aliases",
        "ai_runs",
        "receipt_items",
    }
    Base.metadata.create_all(
        engine,
        tables=[
            table for name, table in Base.metadata.tables.items() if name in legacy_table_names
        ],
    )
    occurred_at = datetime(2026, 8, 28, 12, 0)
    with engine.begin() as connection:
        connection.execute(
            Event.__table__.insert().values(
                discord_message_id="legacy-budget-event",
                discord_channel_id="legacy-channel",
                discord_user_id="legacy-user",
                captured_at=occurred_at,
                occurred_at=occurred_at,
                raw_text="legacy budget event",
                source="discord",
                status=EventStatus.CONFIRMED.value,
                event_type=EventKind.EXPENSE.value,
                event_kinds=[EventKind.EXPENSE.value],
            )
        )
    before = set(inspect(engine).get_table_names())

    Database(f"sqlite:///{database_path}").init()

    after = set(inspect(engine).get_table_names())
    assert "monthly_budgets" in after - before
    with Session(engine) as session:
        event = session.scalar(
            select(Event).where(Event.discord_message_id == "legacy-budget-event")
        )
        assert event is not None
        assert event.raw_text == "legacy budget event"
        assert event.status == EventStatus.CONFIRMED.value
