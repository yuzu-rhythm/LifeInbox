from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from life_inbox.constants import AccountKind, EventKind, EventStatus
from life_inbox.db import Database
from life_inbox.finance import FinanceService
from life_inbox.finance_digest import (
    DeliveryAction,
    DigestDeliveryState,
    FinanceDigestDeliveryRow,
    FinanceDigestService,
    format_daily_digest,
)
from life_inbox.models import Event, Expense, FinancialAccount, Meal, MonthlyBudget


def _add_event(
    db: Database,
    message_id: str,
    *,
    occurred_at: datetime,
    amount: int | None,
    category: str = "未分類",
    subcategory: str | None = None,
    merchant: str | None = None,
    status: EventStatus = EventStatus.CONFIRMED,
    outside_home: str | None = None,
) -> int:
    with db.session() as session:
        event = Event(
            discord_message_id=message_id,
            discord_channel_id="source-channel",
            discord_user_id="user",
            captured_at=occurred_at,
            occurred_at=occurred_at,
            raw_text="not included in digest output",
            source="discord",
            status=status.value,
            event_type=EventKind.EXPENSE.value,
            event_kinds=[EventKind.EXPENSE.value],
        )
        event.expenses.append(
            Expense(
                amount=amount,
                category=category,
                subcategory=subcategory,
                merchant_normalized=merchant,
                merchant_raw=merchant,
            )
        )
        if outside_home is not None:
            event.meals.append(
                Meal(
                    meal_type="dinner",
                    outside_home=outside_home,
                    restaurant_normalized=merchant,
                )
            )
        session.add(event)
        session.commit()
        return event.id


def _service(db: Database, **kwargs) -> FinanceDigestService:
    return FinanceDigestService(db.session_factory, **kwargs)


def test_jst_boundaries_and_category_totals_and_dining_notable(db):
    target = date(2026, 8, 31)
    _add_event(
        db,
        "inside-start",
        occurred_at=datetime(2026, 8, 30, 15, 0),
        amount=1_200,
        category="食費",
        subcategory="外食",
        merchant="夜ごはん店",
    )
    _add_event(
        db,
        "inside-end",
        occurred_at=datetime(2026, 8, 31, 14, 59, 59),
        amount=6_000,
        category="娯楽",
        merchant="大きな買い物",
    )
    _add_event(
        db,
        "outside-next-day",
        occurred_at=datetime(2026, 8, 31, 15, 0),
        amount=9_000,
        category="食費",
        subcategory="外食",
        merchant="翌日の店",
    )

    snapshot = _service(db).build_snapshot(target)

    assert snapshot.local_date == target
    assert snapshot.daily_total == 7_200
    assert snapshot.category_totals == {"食費": 1_200, "娯楽": 6_000}
    assert [(item.amount, item.merchant) for item in snapshot.notable_spending] == [
        (6_000, "大きな買い物"),
        (1_200, "夜ごはん店"),
    ]
    assert snapshot.notable_spending[0].is_large
    assert snapshot.notable_spending[1].is_dining
    content = format_daily_digest(snapshot)
    assert "家計の毎日自動サマリー" in content
    assert "外食" in content
    assert "¥7,200" in content
    assert "翌日の店" not in content
    assert "cr" not in content.casefold()


def test_now_uses_jst_date_and_month_to_date_budget_remaining_and_overage(db):
    _add_event(
        db,
        "previous-day",
        occurred_at=datetime(2026, 8, 1, 15, 0),
        amount=4_000,
        category="日用品",
    )
    _add_event(
        db,
        "target-day",
        occurred_at=datetime(2026, 8, 31, 14, 0),
        amount=3_000,
        category="食費",
    )
    with db.session() as session:
        session.add(
            MonthlyBudget(
                month="2026-08",
                scope="overall",
                category_key=MonthlyBudget.OVERALL_CATEGORY_KEY,
                amount=10_000,
            )
        )
        session.commit()

    service = _service(db)
    snapshot = service.build_snapshot(
        now=datetime(2026, 8, 31, 14, 59, tzinfo=UTC),
    )
    assert snapshot.local_date == date(2026, 8, 31)
    assert snapshot.month_to_date_total == 7_000
    assert snapshot.monthly_budget is not None
    assert snapshot.monthly_budget.remaining == 3_000
    assert snapshot.monthly_budget.overage == 0

    with db.session() as session:
        session.add(
            Event(
                discord_message_id="overage",
                discord_channel_id="source-channel",
                discord_user_id="user",
                captured_at=datetime(2026, 8, 31, 14, 30),
                occurred_at=datetime(2026, 8, 31, 14, 30),
                raw_text="not included",
                status=EventStatus.CONFIRMED.value,
                event_type=EventKind.EXPENSE.value,
                event_kinds=[EventKind.EXPENSE.value],
                expenses=[Expense(amount=4_000, category="交通")],
            )
        )
        session.commit()
    overage = service.build_snapshot(date(2026, 8, 31))
    assert overage.month_to_date_total == 11_000
    assert overage.monthly_budget is not None
    assert overage.monthly_budget.remaining == -1_000
    assert overage.monthly_budget.overage == 1_000
    assert "超過 ¥1,000" in format_daily_digest(overage)


def test_pending_unclassified_zero_day_and_invalid_amount_are_not_spending(db):
    target = date(2026, 8, 28)
    _add_event(
        db,
        "pending",
        occurred_at=datetime(2026, 8, 28, 1),
        amount=800,
        category="食費",
        status=EventStatus.PENDING,
    )
    _add_event(
        db,
        "unclassified",
        occurred_at=datetime(2026, 8, 28, 2),
        amount=400,
        category="未分類",
    )
    _add_event(
        db,
        "unreadable",
        occurred_at=datetime(2026, 8, 28, 3),
        amount=None,
        category="未分類",
    )
    _add_event(
        db,
        "ignored",
        occurred_at=datetime(2026, 8, 28, 4),
        amount=9_000,
        category="食費",
        status=EventStatus.IGNORED,
    )

    snapshot = _service(db).build_snapshot(target)
    assert snapshot.daily_total == 400
    assert snapshot.pending_expense_count == 1
    assert snapshot.unclassified_confirmed_expense_count == 2

    empty = _service(db).build_snapshot(date(2026, 8, 29))
    assert empty.daily_total == 0
    assert empty.category_totals == {}
    assert "今日の支出：¥0" in format_daily_digest(empty)
    assert "支出はありません。" in format_daily_digest(empty)


def test_active_account_balances_and_bank_card_warnings(db):
    finance = FinanceService(db.session_factory)
    finance.ensure_default_accounts()
    finance.create_account("予備口座", AccountKind.BANK, opening_balance=1_000)
    expense_bank = _add_event(
        db,
        "bank-expense",
        occurred_at=datetime(2026, 8, 28, 5),
        amount=2_000,
        category="日用品",
    )
    expense_card = _add_event(
        db,
        "card-expense",
        occurred_at=datetime(2026, 8, 28, 6),
        amount=3_000,
        category="食費",
    )
    finance.link_expense(expense_bank, "普通口座")
    finance.link_expense(expense_card, "クレジットカード")
    with db.session() as session:
        account = session.scalar(
            select(FinancialAccount).where(FinancialAccount.name == "現金")
        )
        assert account is not None
        account.active = False
        session.commit()

    snapshot = _service(db).build_snapshot(date(2026, 8, 28))
    accounts = {account.name: account for account in snapshot.accounts}
    assert "現金" not in accounts
    assert accounts["普通口座"].balance == -2_000
    assert accounts["クレジットカード"].balance == 3_000
    assert any("銀行口座" in warning for warning in snapshot.warnings)
    assert any("未精算額" in warning for warning in snapshot.warnings)
    assert "支払日" in format_daily_digest(snapshot)
    assert "限度額" in format_daily_digest(snapshot)


def test_fingerprint_ignores_generated_timestamp_but_changes_with_content(db):
    service = _service(db)
    first = service.build_snapshot(date(2026, 8, 28))
    second = replace(first, generated_at=first.generated_at + timedelta(hours=1))
    assert first.generated_at != second.generated_at
    assert first.semantic_fingerprint == second.semantic_fingerprint

    _add_event(
        db,
        "content-change",
        occurred_at=datetime(2026, 8, 28, 8),
        amount=100,
        category="食費",
    )
    changed = service.build_snapshot(date(2026, 8, 28))
    assert changed.semantic_fingerprint != first.semantic_fingerprint


def test_first_post_unchanged_skip_and_changed_edit(db):
    service = _service(db)
    snapshot = service.build_snapshot(date(2026, 8, 28))
    first = service.prepare_delivery(snapshot, "生活-記録", now=datetime(2026, 8, 29, tzinfo=UTC))
    assert first.action == DeliveryAction.CREATE
    assert first.claimed is True
    assert first.attempt_count == 1
    sent = service.mark_sent(first, discord_message_id="message-1")
    assert isinstance(sent, DigestDeliveryState)
    assert sent.status == "posted"
    assert sent.discord_message_id == "message-1"

    skipped = service.prepare_delivery(snapshot, "生活-記録")
    assert skipped.action == DeliveryAction.SKIP
    assert skipped.claimed is False
    assert skipped.message_id == "message-1"

    changed_snapshot = replace(snapshot, daily_total=1, category_totals={"その他": 1})
    changed = service.prepare_delivery(changed_snapshot, "生活-記録")
    assert changed.action == DeliveryAction.EDIT
    assert changed.claimed is True
    assert changed.message_id == "message-1"
    assert changed.attempt_count == 2
    service.mark_sent(changed, discord_message_id="message-1")


def test_failed_send_is_retryable_and_never_marked_sent(db):
    service = _service(db)
    snapshot = service.build_snapshot(date(2026, 8, 28))
    first = service.prepare_delivery(snapshot, 123, now=datetime(2026, 8, 29, tzinfo=UTC))
    failed = service.mark_failed(
        first,
        error_code="discord_forbidden",
        safe_message="送信できません",
    )
    assert failed.status == "failed"
    assert failed.discord_message_id is None
    assert failed.posted_at is None
    retry = service.prepare_delivery(snapshot, 123)
    assert retry.action == DeliveryAction.CREATE
    assert retry.claimed is True
    assert retry.attempt_count == 2
    with db.session() as session:
        row = session.get(FinanceDigestDeliveryRow, retry.delivery_id)
        assert row is not None
        assert row.status == "posting"
        assert row.discord_message_id is None


def test_unknown_create_is_protected_until_explicit_reconciliation(db):
    service = _service(db)
    snapshot = service.build_snapshot(date(2026, 8, 28))
    claim_time = datetime(2026, 8, 29, tzinfo=UTC)
    first = service.prepare_delivery(snapshot, "unknown-create", now=claim_time)

    unknown = service.mark_unknown(
        first,
        error_code="discord_timeout",
        safe_message="Discordの応答を確認できません",
        now=claim_time + timedelta(seconds=1),
    )
    assert unknown.status == "unknown"
    assert unknown.discord_message_id is None
    assert unknown.posted_at is None
    assert unknown.sent is False
    assert unknown.last_error_code == "discord_timeout"

    blocked = service.prepare_delivery(
        snapshot,
        "unknown-create",
        now=claim_time + timedelta(seconds=2),
    )
    assert blocked.action == DeliveryAction.BLOCKED
    assert blocked.claimed is False

    reconciled = service.prepare_delivery(
        snapshot,
        "unknown-create",
        now=claim_time + timedelta(seconds=3),
        force=True,
    )
    assert reconciled.action == DeliveryAction.CREATE
    assert reconciled.claimed is True
    assert reconciled.message_id is None
    assert reconciled.attempt_count == 2
    service.mark_failed(reconciled, error_code="reconciled_failure", safe_message="再試行失敗")


def test_force_cannot_bypass_an_active_delivery_claim(db):
    service = _service(db)
    snapshot = service.build_snapshot(date(2026, 8, 28))
    first = service.prepare_delivery(snapshot, "active-claim")

    forced = service.prepare_delivery(snapshot, "active-claim", force=True)

    assert first.claimed is True
    assert forced.action == DeliveryAction.IN_PROGRESS
    assert forced.claimed is False
    assert forced.attempt_count == first.attempt_count


def test_unknown_known_edit_keeps_the_existing_message_target(db):
    service = _service(db)
    snapshot = service.build_snapshot(date(2026, 8, 28))
    claim_time = datetime(2026, 8, 29, tzinfo=UTC)
    first = service.prepare_delivery(snapshot, "unknown-edit", now=claim_time)
    service.mark_sent(first, discord_message_id="known-message", posted_at=claim_time)

    changed_snapshot = replace(snapshot, daily_total=1, category_totals={"変更": 1})
    edit = service.prepare_delivery(
        changed_snapshot,
        "unknown-edit",
        now=claim_time + timedelta(seconds=1),
    )
    unknown = service.mark_unknown(
        edit,
        error_code="discord_timeout",
        safe_message="編集結果を確認できません",
        now=claim_time + timedelta(seconds=2),
    )
    assert unknown.status == "unknown"
    assert unknown.discord_message_id == "known-message"
    assert unknown.sent is False

    retry = service.prepare_delivery(
        changed_snapshot,
        "unknown-edit",
        now=claim_time + timedelta(seconds=3),
    )
    assert retry.action == DeliveryAction.EDIT
    assert retry.claimed is True
    assert retry.message_id == "known-message"
    service.mark_failed(retry, error_code="test_cleanup", safe_message="検証終了")


def test_stale_posting_recovery_distinguishes_new_create_and_known_edit(db):
    service = _service(db, stale_after_seconds=60)
    snapshot = service.build_snapshot(date(2026, 8, 28))
    claim_time = datetime(2026, 8, 29, tzinfo=UTC)

    create = service.prepare_delivery(snapshot, "stale-create", now=claim_time)
    with db.session() as session:
        row = session.get(FinanceDigestDeliveryRow, create.delivery_id)
        assert row is not None
        row.updated_at = claim_time.replace(tzinfo=None)
        session.commit()

    recovered_create = service.recover_stale_postings(
        now=claim_time + timedelta(seconds=61),
        stale_after_seconds=60,
    )
    assert len(recovered_create) == 1
    assert recovered_create[0].status == "unknown"
    assert recovered_create[0].discord_message_id is None
    assert recovered_create[0].posted_at is None
    assert recovered_create[0].sent is False
    assert recovered_create[0].last_error_code == "stale_create_outcome_unknown"

    blocked = service.prepare_delivery(
        snapshot,
        "stale-create",
        now=claim_time + timedelta(seconds=62),
    )
    assert blocked.action == DeliveryAction.BLOCKED
    assert blocked.claimed is False

    edit_source = service.prepare_delivery(snapshot, "stale-edit", now=claim_time)
    sent = service.mark_sent(
        edit_source,
        discord_message_id="known-message",
        posted_at=claim_time + timedelta(seconds=1),
    )
    changed_snapshot = replace(snapshot, daily_total=1, category_totals={"変更": 1})
    edit = service.prepare_delivery(
        changed_snapshot,
        "stale-edit",
        now=claim_time + timedelta(seconds=2),
    )
    assert edit.action == DeliveryAction.EDIT
    assert edit.attempt_count == 2
    with db.session() as session:
        row = session.get(FinanceDigestDeliveryRow, edit.delivery_id)
        assert row is not None
        row.updated_at = claim_time.replace(tzinfo=None)
        session.commit()

    recovered_edit = service.recover_stale_postings(
        now=claim_time + timedelta(seconds=61),
        stale_after_seconds=60,
    )
    assert len(recovered_edit) == 1
    assert recovered_edit[0].status == "failed"
    assert recovered_edit[0].discord_message_id == sent.discord_message_id
    assert recovered_edit[0].posted_at == sent.posted_at
    assert recovered_edit[0].last_error_code == "stale_edit_claim_recovered"
    assert recovered_edit[0].sent is False

    retry_edit = service.prepare_delivery(
        changed_snapshot,
        "stale-edit",
        now=claim_time + timedelta(seconds=62),
    )
    assert retry_edit.action == DeliveryAction.EDIT
    assert retry_edit.claimed is True
    assert retry_edit.message_id == "known-message"
    assert retry_edit.attempt_count == 3
    service.mark_failed(retry_edit, error_code="test_cleanup", safe_message="検証終了")


def test_concurrent_claims_have_one_winner(db):
    service = _service(db)
    snapshot = service.build_snapshot(date(2026, 8, 28))

    def claim() -> object:
        return service.prepare_delivery(
            snapshot,
            "concurrent",
            now=datetime(2026, 8, 29, tzinfo=UTC),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))
    assert sum(result.claimed for result in results) == 1
    assert sum(result.action == DeliveryAction.IN_PROGRESS for result in results) == 1
    with db.session() as session:
        assert session.query(FinanceDigestDeliveryRow).count() == 1


def test_dry_run_does_not_mutate_delivery_state(db):
    service = _service(db)
    snapshot = service.build_snapshot(date(2026, 8, 28))
    dry = service.prepare_delivery(snapshot, "dry-run", dry_run=True)
    assert dry.dry_run is True
    assert dry.action == DeliveryAction.CREATE
    assert dry.claimed is False
    assert service.get_delivery(snapshot.local_date, "dry-run") is None

    first = service.prepare_delivery(snapshot, "dry-run")
    service.mark_sent(first, "message-1")
    changed_snapshot = replace(snapshot, daily_total=1, category_totals={"その他": 1})
    changed_dry = service.prepare_delivery(changed_snapshot, "dry-run", dry_run=True)
    assert changed_dry.action == DeliveryAction.EDIT
    assert changed_dry.claimed is False
    state = service.get_delivery(snapshot.local_date, "dry-run")
    assert state is not None
    assert state.status == "posted"
    assert state.semantic_fingerprint == snapshot.semantic_fingerprint


def test_formatter_bounds_details_without_hiding_totals(db):
    with db.session() as session:
        for index in range(30):
            session.add(
                Event(
                    discord_message_id=f"long-{index}",
                    discord_channel_id="source-channel",
                    discord_user_id="user",
                    captured_at=datetime(2026, 8, 28, 10),
                    occurred_at=datetime(2026, 8, 28, 10),
                    raw_text="not included",
                    status=EventStatus.CONFIRMED.value,
                    event_type=EventKind.EXPENSE.value,
                    event_kinds=[EventKind.EXPENSE.value],
                    expenses=[
                        Expense(
                            amount=5_000 + index,
                            category=f"カテゴリ{index}",
                            merchant_normalized=f"店{index}",
                        )
                    ],
                )
            )
        session.commit()
    snapshot = _service(db).build_snapshot(date(2026, 8, 28))
    content = format_daily_digest(snapshot)
    assert len(content) <= 1_900
    assert len(format_daily_digest(snapshot, max_length=2_000)) <= 1_900
    assert "今日の支出：¥150,435" in content
    assert "合計値は全件分" in content
