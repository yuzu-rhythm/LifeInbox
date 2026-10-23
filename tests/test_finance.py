from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from life_inbox.constants import AccountKind, ActivityKind, EventKind, EventStatus
from life_inbox.db import Database
from life_inbox.finance import FinanceService
from life_inbox.models import Base, Event, Expense


def _event(
    db: Database,
    message_id: str,
    *,
    status: EventStatus = EventStatus.PENDING,
    amount: int | None = None,
    occurred_at: datetime | None = None,
) -> int:
    occurred = occurred_at or datetime(2026, 8, 28, 12, 0)
    with db.session() as session:
        event = Event(
            discord_message_id=message_id,
            discord_channel_id="channel",
            discord_user_id="user",
            captured_at=occurred,
            occurred_at=occurred,
            raw_text=message_id,
            status=status.value,
            event_type=EventKind.EXPENSE.value if amount is not None else "unknown",
            event_kinds=[EventKind.EXPENSE.value] if amount is not None else [],
        )
        if amount is not None:
            event.expenses.append(Expense(amount=amount, category="未分類"))
        session.add(event)
        session.commit()
        return event.id


def _event_status(db: Database, event_id: int) -> Event:
    with db.session() as session:
        event = session.scalar(select(Event).where(Event.id == event_id))
        assert event is not None
        session.expunge(event)
        return event


def test_default_accounts_are_idempotent_and_same_kind_accounts_are_allowed(db):
    finance = FinanceService(db.session_factory)

    first = finance.ensure_default_accounts()
    second = finance.ensure_default_accounts()

    assert [account.name for account in first] == ["現金", "普通口座", "クレジットカード"]
    assert [account.name for account in second] == ["現金", "普通口座", "クレジットカード"]
    assert all(account.currency == "JPY" for account in second)

    another_bank = finance.create_account("給与口座", AccountKind.BANK, opening_balance=50_000)
    investment = finance.create_account(
        "NISA", AccountKind.INVESTMENT, opening_balance=1_000_000
    )

    assert another_bank.kind == AccountKind.BANK
    assert another_bank.balance == 50_000
    assert investment.kind == AccountKind.INVESTMENT
    assert investment.balance == 1_000_000
    assert len(finance.list_accounts()) == 5


def test_account_validation_rejects_blank_duplicate_and_invalid_values(db):
    finance = FinanceService(db.session_factory)
    finance.ensure_default_accounts()

    with pytest.raises(ValueError, match="blank"):
        finance.create_account("   ", AccountKind.CASH)
    with pytest.raises(ValueError, match="invalid account kind"):
        finance.create_account("不正", "crypto")
    with pytest.raises(ValueError, match="already exists"):
        finance.create_account("現金", AccountKind.BANK)
    with pytest.raises(ValueError, match="opening_balance"):
        finance.create_account("不正残高", AccountKind.CASH, opening_balance=True)


def test_pending_ignored_and_confirmed_expense_links_affect_only_confirmed_balances(db):
    finance = FinanceService(db.session_factory)
    finance.ensure_default_accounts()
    pending_id = _event(db, "pending-expense", amount=1_000)
    ignored_id = _event(db, "ignored-expense", amount=2_000, status=EventStatus.IGNORED)
    confirmed_id = _event(db, "confirmed-expense", amount=3_000, status=EventStatus.CONFIRMED)

    pending_link = finance.link_expense(pending_id, "普通口座")
    ignored_link = finance.link_expense(ignored_id, "普通口座")
    confirmed_link = finance.link_expense(confirmed_id, "普通口座")

    assert pending_link.amount == 1_000
    assert ignored_link.amount == 2_000
    assert confirmed_link.amount == 3_000
    assert finance.get_account_by_name("普通口座").balance == -3_000


def test_expense_amount_is_resolved_from_event_after_edit_and_card_debt_is_positive(db):
    finance = FinanceService(db.session_factory)
    finance.ensure_default_accounts()
    event_id = _event(db, "card-expense", amount=500, status=EventStatus.CONFIRMED)

    activity = finance.link_expense(event_id, "クレジットカード")
    assert activity.amount == 500
    assert finance.get_account_by_name("クレジットカード").balance == 500

    with db.session() as session:
        expense = session.scalar(select(Expense).where(Expense.event_id == event_id))
        assert expense is not None
        expense.amount = 750
        session.commit()

    refreshed = finance.list_activities()[0]
    assert refreshed.amount == 750
    assert finance.get_account_by_name("クレジットカード").balance == 750


def test_income_and_transfer_cover_asset_and_card_directions_without_expense(db):
    finance = FinanceService(db.session_factory)
    finance.ensure_default_accounts()
    income_id = _event(db, "salary")
    transfer_id = _event(db, "bank-to-card")
    reverse_transfer_id = _event(db, "card-to-cash")

    income = finance.record_income(income_id, "普通口座", 100_000, "給与")
    transfer = finance.record_transfer(
        transfer_id, "普通口座", "クレジットカード", 20_000, "カード引落し"
    )
    reverse_transfer = finance.record_transfer(
        reverse_transfer_id, "クレジットカード", "現金", 5_000
    )

    assert income.kind == ActivityKind.INCOME
    assert transfer.kind == ActivityKind.TRANSFER
    assert reverse_transfer.kind == ActivityKind.TRANSFER
    assert finance.get_account_by_name("普通口座").balance == 80_000
    # The transfer pays down the card by 20,000, then a card-originating
    # transfer adds 5,000 of debt: 0 - 20,000 + 5,000.
    assert finance.get_account_by_name("クレジットカード").balance == -15_000
    assert finance.get_account_by_name("現金").balance == 5_000
    assert _event_status(db, income_id).event_kinds == [EventKind.INCOME.value]
    assert _event_status(db, transfer_id).event_type == EventKind.TRANSFER.value

    with db.session() as session:
        assert session.scalar(select(Expense).where(Expense.event_id == transfer_id)) is None
        assert (
            session.scalar(select(Expense).where(Expense.event_id == reverse_transfer_id)) is None
        )


def test_investment_is_asset_like_for_normal_activity_math(db):
    finance = FinanceService(db.session_factory)
    finance.ensure_default_accounts()
    finance.create_account("NISA", AccountKind.INVESTMENT, opening_balance=10_000)
    income_id = _event(db, "investment-income")
    expense_id = _event(db, "investment-expense", amount=1_000, status=EventStatus.CONFIRMED)

    finance.record_income(income_id, "NISA", 2_000)
    finance.link_expense(expense_id, "NISA")

    assert finance.get_account_by_name("NISA").balance == 11_000


def test_income_transfer_and_expense_validation_and_missing_accounts(db):
    finance = FinanceService(db.session_factory)
    finance.ensure_default_accounts()
    income_id = _event(db, "invalid-income")
    transfer_id = _event(db, "invalid-transfer")
    expense_id = _event(db, "valid-expense", amount=100)

    with pytest.raises(ValueError, match="positive"):
        finance.record_income(income_id, "普通口座", 0)
    with pytest.raises(ValueError, match="positive"):
        finance.record_transfer(transfer_id, "普通口座", "現金", -1)
    with pytest.raises(ValueError, match="differ"):
        finance.record_transfer(transfer_id, "現金", " 現金 ", 1)
    with pytest.raises(ValueError, match="account not found"):
        finance.record_income(income_id, "存在しない口座", 1)
    with pytest.raises(ValueError, match="account not found"):
        finance.link_expense(expense_id, "存在しない口座")
    with pytest.raises(ValueError, match="event not found"):
        finance.link_expense(999_999, "現金")


def test_event_linkage_and_reprocessing_are_idempotent_but_conflicts_are_rejected(db):
    finance = FinanceService(db.session_factory)
    finance.ensure_default_accounts()
    expense_id = _event(db, "idempotent-expense", amount=250, status=EventStatus.CONFIRMED)
    income_id = _event(db, "idempotent-income")
    transfer_id = _event(db, "idempotent-transfer")

    first_expense = finance.link_expense(expense_id, "現金")
    second_expense = finance.link_expense(expense_id, "現金")
    first_income = finance.record_income(income_id, "普通口座", 500, "給与")
    second_income = finance.record_income(income_id, "普通口座", 500, "給与")
    first_transfer = finance.record_transfer(transfer_id, "普通口座", "現金", 100)
    second_transfer = finance.record_transfer(transfer_id, "普通口座", "現金", 100)

    assert first_expense.id == second_expense.id
    assert first_income.id == second_income.id
    assert first_transfer.id == second_transfer.id
    assert len(finance.list_activities()) == 3

    with pytest.raises(ValueError, match="different ledger activity"):
        finance.link_expense(expense_id, "普通口座")
    with pytest.raises(ValueError, match="different ledger activity"):
        finance.record_income(income_id, "現金", 500, "給与")
    with pytest.raises(ValueError, match="different ledger activity"):
        finance.record_transfer(transfer_id, "現金", "普通口座", 100)


def test_memo_is_literal_idempotent_and_marks_event(db):
    finance = FinanceService(db.session_factory)
    event_id = _event(db, "literal-memo")
    body = "  そのままのメモ\n改行も保持  "

    first = finance.record_memo(event_id, body)
    second = finance.record_memo(event_id, body)

    assert first.id == second.id
    assert first.body == body
    assert finance.list_memos()[0].body == body
    event = _event_status(db, event_id)
    assert event.status == EventStatus.CONFIRMED.value
    assert event.event_type == EventKind.MEMO.value
    assert event.event_kinds == [EventKind.MEMO.value]

    with pytest.raises(ValueError, match="blank"):
        finance.record_memo(_event(db, "blank-memo"), " \n\t ")
    with pytest.raises(ValueError, match="different memo"):
        finance.record_memo(event_id, "別の本文")


def test_lists_are_newest_first_and_results_are_detached_snapshots(db):
    finance = FinanceService(db.session_factory)
    finance.ensure_default_accounts()
    older = _event(
        db,
        "older-activity",
        amount=100,
        status=EventStatus.CONFIRMED,
        occurred_at=datetime(2026, 8, 27, 12, 0),
    )
    newer = _event(
        db,
        "newer-activity",
        amount=200,
        status=EventStatus.CONFIRMED,
        occurred_at=datetime(2026, 8, 28, 12, 0),
    )
    finance.link_expense(older, "現金")
    finance.link_expense(newer, "現金")

    activities = finance.list_activities()
    assert [activity.event_id for activity in activities] == [newer, older]
    assert activities[0].amount == 200
    assert finance.list_accounts()[0].balance == -300


def test_create_all_adds_finance_tables_to_legacy_schema_and_preserves_event(tmp_path):
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
    with engine.begin() as connection:
        connection.execute(
            Event.__table__.insert().values(
                discord_message_id="legacy-1",
                discord_channel_id="channel",
                discord_user_id="user",
                captured_at=datetime(2026, 8, 28, 12, 0),
                occurred_at=datetime(2026, 8, 28, 12, 0),
                raw_text="legacy",
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
        "financial_accounts",
        "financial_account_snapshots",
        "financial_account_settlement_links",
        "ledger_activities",
        "life_tasks",
        "memos",
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
        event = session.scalar(select(Event).where(Event.discord_message_id == "legacy-1"))
        assert event is not None
        assert event.id == 1
        assert event.event_kinds == [EventKind.EXPENSE.value]
        assert event.status == EventStatus.CONFIRMED.value
