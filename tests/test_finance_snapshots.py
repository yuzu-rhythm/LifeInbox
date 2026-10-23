from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from life_inbox.constants import AccountKind, EventStatus
from life_inbox.finance import AccountSnapshotMeasure, FinanceService
from life_inbox.finance_digest import FinanceDigestService, format_daily_digest
from life_inbox.finance_digest_discord import FinanceDigestPublisher
from life_inbox.finance_digest_scheduler import FinanceDigestScheduler
from life_inbox.models import Event, FinancialAccountSnapshot


def _event(db, message_id: str, occurred_at: datetime) -> int:
    with db.session() as session:
        event = Event(
            discord_message_id=message_id,
            discord_channel_id="source-channel",
            discord_user_id="user",
            captured_at=occurred_at,
            occurred_at=occurred_at,
            raw_text="not included in digest output",
            source="test",
            status=EventStatus.PENDING.value,
            event_type="unknown",
            event_kinds=[],
        )
        session.add(event)
        session.commit()
        return event.id


def test_capture_uses_confirmed_ledger_as_of_local_day_and_sets_measure(db):
    finance = FinanceService(db.session_factory)
    finance.ensure_default_accounts()
    income_id = _event(db, "snapshot-income", datetime(2026, 8, 30, 14, 0))
    next_day_income_id = _event(db, "next-day-income", datetime(2026, 8, 30, 15, 0))

    finance.record_income(income_id, "普通口座", 500)
    finance.record_income(next_day_income_id, "普通口座", 700)
    rows = finance.capture_daily_snapshots(
        date(2026, 8, 30),
        observed_at=datetime(2026, 8, 30, 15, 1, tzinfo=UTC),
    )

    by_name = {row.account_name: row for row in rows}
    assert by_name["普通口座"].value == 500
    assert by_name["普通口座"].source == "ledger"
    assert by_name["普通口座"].measure == AccountSnapshotMeasure.BALANCE
    assert by_name["クレジットカード"].value == 0
    assert by_name["クレジットカード"].measure == AccountSnapshotMeasure.OUTSTANDING
    assert all(row.local_date == date(2026, 8, 30) for row in rows)


def test_snapshot_comparison_uses_previous_available_day_and_signed_deltas(db):
    finance = FinanceService(db.session_factory)
    finance.ensure_default_accounts()
    first_day = date(2026, 8, 1)
    skipped_day = date(2026, 8, 3)
    negative_day = date(2026, 8, 4)
    zero_change_day = date(2026, 8, 5)

    first = finance.upsert_daily_snapshot("普通口座", first_day, 1_000)
    finance.upsert_daily_snapshot("普通口座", skipped_day, 1_300)
    negative = finance.upsert_daily_snapshot("普通口座", negative_day, 1_100)
    zero = finance.upsert_daily_snapshot("普通口座", zero_change_day, 1_100)

    assert first.value == 1_000
    first_comparison = {
        comparison.account_name: comparison
        for comparison in finance.list_daily_snapshot_comparisons(first_day)
    }["普通口座"]
    assert first_comparison.previous_value is None

    comparisons = {
        comparison.account_name: comparison
        for comparison in finance.list_daily_snapshot_comparisons(skipped_day)
    }
    assert comparisons["普通口座"].previous_local_date == first_day
    assert comparisons["普通口座"].previous_value == 1_000
    assert comparisons["普通口座"].delta == 300
    assert comparisons["普通口座"].previous_day_local_date is None
    assert comparisons["普通口座"].previous_day_value is None

    negative_comparison = {
        comparison.account_name: comparison
        for comparison in finance.list_daily_snapshot_comparisons(negative.local_date)
    }["普通口座"]
    assert negative_comparison.delta == -200

    zero_comparison = {
        comparison.account_name: comparison
        for comparison in finance.list_daily_snapshot_comparisons(zero.local_date)
    }["普通口座"]
    assert zero_comparison.delta == 0


def test_same_day_snapshot_upsert_is_single_row_and_revises_only_on_change(db):
    finance = FinanceService(db.session_factory)
    finance.ensure_default_accounts()
    target = date(2026, 8, 10)

    first = finance.upsert_daily_snapshot(
        "普通口座",
        target,
        1_000,
        observed_at=datetime(2026, 8, 10, 1, tzinfo=UTC),
    )
    same_value = finance.upsert_daily_snapshot(
        "普通口座",
        target,
        1_000,
        observed_at=datetime(2026, 8, 10, 2, tzinfo=UTC),
    )
    corrected = finance.upsert_daily_snapshot(
        "普通口座",
        target,
        1_250,
        observed_at=datetime(2026, 8, 10, 3, tzinfo=UTC),
    )

    assert first.id == same_value.id == corrected.id
    assert same_value.revision == 1
    assert corrected.revision == 2
    with db.session() as session:
        rows = list(session.scalars(select(FinancialAccountSnapshot)).all())
    assert len(rows) == 1
    assert rows[0].value == 1_250
    assert rows[0].revision == 2


def test_multiple_cards_can_share_settlement_account_and_invalid_links_are_rejected(db):
    finance = FinanceService(db.session_factory)
    finance.ensure_default_accounts()
    finance.create_account("第二カード", AccountKind.CARD)
    finance.create_account("投資口座", AccountKind.INVESTMENT)

    first = finance.link_settlement_account("クレジットカード", "普通口座")
    second = finance.link_settlement_account("第二カード", "普通口座")
    assert first.settlement_account_name == "普通口座"
    assert second.settlement_account_name == "普通口座"

    with pytest.raises(ValueError, match="must differ"):
        finance.link_settlement_account("クレジットカード", "クレジットカード")
    with pytest.raises(ValueError, match="source must be a card"):
        finance.link_settlement_account("普通口座", "現金")
    with pytest.raises(ValueError, match="must be a bank account"):
        finance.link_settlement_account("クレジットカード", "現金")
    with pytest.raises(ValueError, match="must be a bank account"):
        finance.link_settlement_account("クレジットカード", "第二カード")
    with pytest.raises(ValueError, match="must be a bank account"):
        finance.link_settlement_account("クレジットカード", "投資口座")


def test_settlement_links_are_listed_in_card_order_and_unlink_is_idempotent(db):
    finance = FinanceService(db.session_factory)
    finance.ensure_default_accounts()
    finance.create_account("第二カード", AccountKind.CARD)
    finance.link_settlement_account("第二カード", "普通口座")
    finance.link_settlement_account("クレジットカード", "普通口座")

    links = finance.list_settlement_account_links()

    assert [link.card_account_name for link in links] == ["クレジットカード", "第二カード"]
    assert [link.settlement_account_name for link in links] == ["普通口座", "普通口座"]
    assert finance.unlink_settlement_account("クレジットカード") is True
    assert finance.unlink_settlement_account("クレジットカード") is False
    assert [link.card_account_name for link in finance.list_settlement_account_links()] == [
        "第二カード"
    ]
    assert finance.get_account_by_name("クレジットカード") is not None
    assert finance.get_account_by_name("普通口座") is not None


def test_missing_current_value_is_unavailable_and_not_zero_filled(db):
    finance = FinanceService(db.session_factory)
    finance.ensure_default_accounts()
    finance.create_account("第二カード", AccountKind.CARD, opening_balance=999)
    finance.upsert_daily_snapshot("クレジットカード", date(2026, 8, 19), 750)
    target = date(2026, 8, 20)

    history_only_snapshot = FinanceDigestService(db.session_factory).build_snapshot(target)
    history_only_accounts = {
        account.name: account for account in history_only_snapshot.accounts
    }
    assert history_only_accounts["普通口座"].balance is None
    assert history_only_accounts["クレジットカード"].balance is None
    assert history_only_accounts["クレジットカード"].previous_value == 750

    finance.upsert_daily_snapshot("普通口座", target, 321)

    comparisons = {
        comparison.account_name: comparison
        for comparison in finance.list_daily_snapshot_comparisons(target)
    }
    assert comparisons["普通口座"].value == 321
    assert comparisons["クレジットカード"].value is None
    assert comparisons["第二カード"].value is None

    snapshot = FinanceDigestService(db.session_factory).build_snapshot(target)
    cards = {account.name: account for account in snapshot.accounts}
    assert cards["クレジットカード"].balance is None
    assert cards["第二カード"].balance is None
    assert cards["クレジットカード"].previous_value == 750
    content = format_daily_digest(snapshot)
    assert "クレジットカード：利用残高 未取得" in content
    assert "第二カード：利用残高 未取得" in content
    assert "クレジットカード：利用残高 ¥0" not in content
    assert "第二カード：利用残高 ¥0" not in content


def test_first_snapshot_digest_shows_measure_delta_and_settlement_link(db):
    finance = FinanceService(db.session_factory)
    finance.ensure_default_accounts()
    finance.link_settlement_account("クレジットカード", "普通口座")
    target = date(2026, 8, 21)
    finance.upsert_daily_snapshot("普通口座", target, 10_000)
    finance.upsert_daily_snapshot("クレジットカード", target, 4_000)

    snapshot = FinanceDigestService(db.session_factory).build_snapshot(target)
    accounts = {account.name: account for account in snapshot.accounts}
    assert accounts["普通口座"].measure == AccountSnapshotMeasure.BALANCE
    assert accounts["クレジットカード"].measure == AccountSnapshotMeasure.OUTSTANDING
    assert accounts["普通口座"].delta is None
    assert accounts["クレジットカード"].delta is None
    content = format_daily_digest(snapshot)
    assert "普通口座：残高 ¥10,000（比較なし：初回）" in content
    assert "クレジットカード：利用残高 ¥4,000（比較なし：初回）" in content
    assert "前日比較なし" in content
    assert "引落口座：普通口座" in content


def test_digest_lists_multiple_typed_accounts_with_masked_identities_and_totals(db):
    finance = FinanceService(db.session_factory)
    finance.create_account("ゆうちょ 1234 5678", AccountKind.BANK)
    finance.create_account("地方銀行", AccountKind.BANK)
    finance.create_account("楽天カード 1111 2222 3333 4444", AccountKind.CARD)
    finance.create_account("予備カード", AccountKind.CARD)
    previous = date(2026, 8, 24)
    target = date(2026, 8, 25)

    for name, value in (
        ("ゆうちょ 1234 5678", 90_000),
        ("地方銀行", 50_000),
        ("楽天カード 1111 2222 3333 4444", 8_000),
        ("予備カード", 5_000),
    ):
        finance.upsert_daily_snapshot(name, previous, value)
    for name, value in (
        ("ゆうちょ 1234 5678", 100_000),
        ("地方銀行", 50_000),
        ("楽天カード 1111 2222 3333 4444", 12_000),
        ("予備カード", 3_000),
    ):
        finance.upsert_daily_snapshot(name, target, value)

    snapshot = FinanceDigestService(db.session_factory).build_snapshot(target)
    accounts = {account.name: account for account in snapshot.accounts}
    content = format_daily_digest(snapshot)

    assert snapshot.asset_total == 150_000
    assert snapshot.liability_total == 15_000
    assert snapshot.net_assets == 135_000
    assert accounts["ゆうちょ 1234 5678"].account_type == AccountKind.BANK
    assert accounts["ゆうちょ 1234 5678"].display_identity == "ゆうちょ •••• 5678"
    assert accounts["楽天カード 1111 2222 3333 4444"].account_type == AccountKind.CARD
    assert "ゆうちょ •••• 5678：残高 ¥100,000" in content
    assert "楽天カード •••• 4444：利用残高 ¥12,000" in content
    assert "【種別：銀行口座（bank）／資産】" in content
    assert "【種別：クレジットカード（card）／負債】" in content
    assert "【前日差／資産増】" in content
    assert "【前日差／負債増】" in content
    assert "【前日差／負債減】" in content
    assert "資産合計：¥150,000" in content
    assert "負債合計：¥15,000" in content
    assert "純資産：¥135,000" in content
    assert "1234 5678" not in content
    assert "1111 2222 3333 4444" not in content


def test_missing_immediately_previous_snapshot_is_not_zero_filled(db):
    finance = FinanceService(db.session_factory)
    finance.create_account("孤立銀行", AccountKind.BANK)
    finance.upsert_daily_snapshot("孤立銀行", date(2026, 8, 22), 7_000)
    target = date(2026, 8, 24)
    finance.upsert_daily_snapshot("孤立銀行", target, 9_000)

    snapshot = FinanceDigestService(db.session_factory).build_snapshot(target)
    account = snapshot.accounts[0]
    content = format_daily_digest(snapshot)

    assert account.previous_value is None
    assert account.previous_available_value == 7_000
    assert account.previous_day_value is None
    assert account.delta is None
    assert snapshot.asset_total == 9_000
    assert snapshot.liability_total == 0
    assert snapshot.net_assets == 9_000
    assert "孤立銀行：残高 ¥9,000（前日比較なし）" in content
    assert "前日比較なし" in content
    assert "との差" not in content
    assert "±¥0" not in content


def test_zero_snapshots_are_real_zero_values_and_no_account_data_has_no_totals(db):
    finance = FinanceService(db.session_factory)
    finance.create_account("ゼロ銀行", AccountKind.BANK)
    finance.create_account("ゼロカード", AccountKind.CARD)
    target = date(2026, 8, 26)
    finance.upsert_daily_snapshot("ゼロ銀行", target, 0)
    finance.upsert_daily_snapshot("ゼロカード", target, 0)

    snapshot = FinanceDigestService(db.session_factory).build_snapshot(target)
    content = format_daily_digest(snapshot)

    assert snapshot.asset_total == 0
    assert snapshot.liability_total == 0
    assert snapshot.net_assets == 0
    assert "ゼロ銀行：残高 ¥0" in content
    assert "ゼロカード：利用残高 ¥0" in content
    assert "資産合計：¥0" in content
    assert "負債合計：¥0" in content
    assert "純資産：¥0" in content
    assert content.count("前日比較なし") >= 2

    empty = FinanceDigestService(db.session_factory).build_snapshot(date(2026, 8, 27))
    empty_content = format_daily_digest(empty)
    assert "今日の支出：¥0" in empty_content
    assert "資産合計：" not in empty_content
    assert "負債合計：" not in empty_content


class _RecordingTransport:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.edited: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> str:
        self.sent.append(kwargs)
        return "snapshot-message"

    async def edit(self, **kwargs: object) -> str:
        self.edited.append(kwargs)
        return str(kwargs["message_id"])


class _FailingTransport:
    async def send(self, **kwargs: object) -> str:
        raise RuntimeError("transport failure")

    async def edit(self, **kwargs: object) -> str:
        raise RuntimeError("transport failure")


@pytest.mark.asyncio
async def test_scheduler_capture_edits_same_day_digest_after_ledger_correction(db):
    finance = FinanceService(db.session_factory)
    finance.create_account("対象銀行", AccountKind.BANK, opening_balance=1_000)
    finance.upsert_daily_snapshot("対象銀行", date(2026, 8, 29), 1_000)
    moment = datetime(2026, 8, 30, 14, 50, tzinfo=UTC)
    transport = _RecordingTransport()
    publisher = FinanceDigestPublisher(
        FinanceDigestService(db.session_factory),
        transport,
        dry_run=False,
    )
    scheduler = FinanceDigestScheduler(
        publisher,
        enabled=True,
        channel_id=123,
        now=lambda: moment,
    )

    first = await scheduler.run_daily_once(moment=moment)
    assert first is not None and first.posted is True
    event_id = _event(db, "same-day-correction", datetime(2026, 8, 30, 13, 0))
    finance.record_income(event_id, "対象銀行", 500)
    corrected = await scheduler.run_daily_once(moment=moment)
    repeated_correction = await scheduler.run_daily_once(moment=moment)

    assert corrected is not None and corrected.edited is True
    assert repeated_correction is not None and repeated_correction.duplicate is True
    assert len(transport.sent) == 1
    assert len(transport.edited) == 1
    assert "対象銀行：残高 ¥1,500" in str(transport.edited[0]["content"])
    assert "との差 +¥500" in str(transport.edited[0]["content"])


@pytest.mark.asyncio
async def test_discord_failure_does_not_lose_captured_snapshot(db):
    finance = FinanceService(db.session_factory)
    finance.create_account("送信失敗銀行", AccountKind.BANK, opening_balance=2_000)
    moment = datetime(2026, 8, 30, 14, 50, tzinfo=UTC)
    service = FinanceDigestService(db.session_factory)
    publisher = FinanceDigestPublisher(service, _FailingTransport(), dry_run=False)
    scheduler = FinanceDigestScheduler(
        publisher,
        enabled=True,
        channel_id=456,
        now=lambda: moment,
    )

    outcome = await scheduler.run_daily_once(moment=moment)

    assert outcome is not None
    assert outcome.posted is False
    assert outcome.error_code == "discord_send_failed:RuntimeError"
    with db.session() as session:
        row = session.scalar(
            select(FinancialAccountSnapshot)
            .join(FinancialAccountSnapshot.account)
            .where(
                FinancialAccountSnapshot.local_date == date(2026, 8, 30),
            )
        )
        assert row is not None
        assert row.value == 2_000
    delivery = service.get_delivery(date(2026, 8, 30), 456)
    assert delivery is not None
    assert delivery.status == "failed"
