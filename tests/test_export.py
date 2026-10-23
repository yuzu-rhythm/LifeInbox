from __future__ import annotations

import codecs
import csv
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from life_inbox.cli import build_parser, main
from life_inbox.constants import AccountKind, EventKind, EventStatus
from life_inbox.db import Database
from life_inbox.export import ExportError, ExportService
from life_inbox.finance import FinanceService
from life_inbox.models import Event, Expense, FinancialAccount, ReceiptItem


def _database(tmp_path: Path) -> tuple[Database, Path]:
    database_path = tmp_path / "app.db"
    database = Database(f"sqlite:///{database_path}")
    database.init()
    return database, database_path


def _utc_from_local(
    year: int,
    month: int,
    day: int,
    hour: int = 12,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    local = datetime(
        year,
        month,
        day,
        hour,
        minute,
        second,
        tzinfo=ZoneInfo("Asia/Tokyo"),
    )
    return local.astimezone(UTC).replace(tzinfo=None)


def _add_event(
    database: Database,
    message_id: str,
    occurred_at: datetime,
    *,
    status: EventStatus = EventStatus.CONFIRMED,
    amount: int | None = None,
    merchant: str | None = "店舗",
    normalized_merchant: str | None = None,
    category: str = "食費",
    subcategory: str | None = "外食",
    description: str | None = "説明",
    raw_text: str | None = None,
) -> tuple[int, int | None]:
    with database.session() as session:
        event = Event(
            discord_message_id=message_id,
            discord_channel_id="channel",
            discord_user_id="user",
            captured_at=occurred_at,
            occurred_at=occurred_at,
            raw_text=raw_text or message_id,
            source="test",
            status=status.value,
            event_type=EventKind.EXPENSE.value if amount is not None else "unknown",
            event_kinds=[EventKind.EXPENSE.value] if amount is not None else [],
        )
        expense_id: int | None = None
        if amount is not None:
            expense = Expense(
                amount=amount,
                merchant_raw=merchant,
                merchant_normalized=normalized_merchant,
                category=category,
                subcategory=subcategory,
                description=description,
            )
            event.expenses.append(expense)
        session.add(event)
        session.commit()
        if event.expenses:
            expense_id = event.expenses[0].id
        return event.id, expense_id


def _add_receipt_item(
    database: Database,
    expense_id: int,
    *,
    name: str,
    quantity: int | None,
    unit_price: int | None,
    amount: int | None,
    order_index: int = 0,
) -> int:
    with database.session() as session:
        item = ReceiptItem(
            expense_id=expense_id,
            name=name,
            quantity=quantity,
            unit_price=unit_price,
            amount=amount,
            order_index=order_index,
        )
        session.add(item)
        session.commit()
        return item.id


def _archive_contents(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _csv_rows(payload: bytes) -> list[dict[str, str]]:
    assert payload.startswith(codecs.BOM_UTF8)
    return list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))))


def _manifest(payload: bytes) -> dict[str, object]:
    return json.loads(payload)


def test_export_all_and_tokyo_month_boundary_confirmed_only_and_linked_receipts(
    tmp_path: Path,
):
    database, database_path = _database(tmp_path)
    finance = FinanceService(database.session_factory)
    finance.ensure_default_accounts()

    inside_id, inside_expense_id = _add_event(
        database,
        "inside",
        _utc_from_local(2026, 8, 31, 23, 59, 59),
        amount=1_200,
        normalized_merchant="正規化店",
        raw_text="8月末の買い物",
    )
    boundary_id, boundary_expense_id = _add_event(
        database,
        "boundary",
        _utc_from_local(2026, 9, 1, 0, 0),
        amount=2_000,
        raw_text="9月初日の買い物",
    )
    pending_id, pending_expense_id = _add_event(
        database,
        "pending",
        _utc_from_local(2026, 8, 20),
        status=EventStatus.PENDING,
        amount=3_000,
    )
    ignored_id, ignored_expense_id = _add_event(
        database,
        "ignored",
        _utc_from_local(2026, 8, 21),
        status=EventStatus.IGNORED,
        amount=4_000,
    )
    assert inside_expense_id is not None
    assert boundary_expense_id is not None
    assert pending_expense_id is not None
    assert ignored_expense_id is not None
    finance.link_expense(inside_id, "現金")
    finance.link_expense(pending_id, "現金")
    finance.link_expense(ignored_id, "現金")
    _add_receipt_item(
        database,
        inside_expense_id,
        name="末日商品",
        quantity=2,
        unit_price=600,
        amount=1_200,
    )
    _add_receipt_item(
        database,
        pending_expense_id,
        name="未確定商品",
        quantity=1,
        unit_price=3_000,
        amount=3_000,
    )

    service = ExportService(
        database_url=f"sqlite:///{database_path}",
        data_dir=tmp_path,
        timezone="Asia/Tokyo",
    )
    result = service.create_export()
    contents = _archive_contents(result.archive_path)
    assert list(contents) == [
        "expenses.csv",
        "receipt_items.csv",
        "accounts.csv",
        "account_transactions.csv",
        "manifest.json",
    ]

    all_expenses = _csv_rows(contents["expenses.csv"])
    assert [row["event_id"] for row in all_expenses] == [str(inside_id), str(boundary_id)]
    assert all_expenses[0]["occurred_at_local"] == "2026-08-31T23:59:59+09:00"
    assert all_expenses[0]["merchant"] == "正規化店"
    assert all_expenses[0]["amount"] == "1200"
    assert all(row["event_id"] not in {str(pending_id), str(ignored_id)} for row in all_expenses)

    all_receipts = _csv_rows(contents["receipt_items.csv"])
    assert [row["expense_id"] for row in all_receipts] == [str(inside_expense_id)]
    assert all_receipts[0]["event_id"] == str(inside_id)
    assert all_receipts[0]["quantity"] == "2"
    assert all_receipts[0]["unit_price"] == "600"
    assert all_receipts[0]["amount"] == "1200"

    manifest = _manifest(contents["manifest.json"])
    assert manifest["schema_version"] == 1
    assert manifest["selected_month"] == "all"
    assert manifest["timezone"] == "Asia/Tokyo"
    assert str(manifest["generated_at_utc"]).endswith("Z")
    assert manifest["row_counts"] == {
        "expenses.csv": 2,
        "receipt_items.csv": 1,
        "accounts.csv": 3,
        "account_transactions.csv": 3,
    }
    assert manifest["semantics"]["confirmed_expenses_only"] is True

    month_result = service.create_export(month="2026-08")
    month_contents = _archive_contents(month_result.archive_path)
    month_expenses = _csv_rows(month_contents["expenses.csv"])
    assert [row["event_id"] for row in month_expenses] == [str(inside_id)]
    assert _csv_rows(month_contents["receipt_items.csv"])[0]["expense_id"] == str(
        inside_expense_id
    )
    month_manifest = _manifest(month_contents["manifest.json"])
    assert month_manifest["selected_month"] == "2026-08"
    assert month_manifest["filters"]["expenses"]["month_field"] == "Event.occurred_at"


def test_export_accounts_and_ledger_rows_include_current_balances(tmp_path: Path):
    database, database_path = _database(tmp_path)
    finance = FinanceService(database.session_factory)
    finance.ensure_default_accounts()

    expense_event_id, _ = _add_event(
        database,
        "cash-expense",
        _utc_from_local(2026, 8, 1),
        amount=100,
    )
    income_event_id, _ = _add_event(
        database,
        "salary",
        _utc_from_local(2026, 8, 2),
    )
    transfer_event_id, _ = _add_event(
        database,
        "transfer",
        _utc_from_local(2026, 8, 3),
    )
    old_income_event_id, _ = _add_event(
        database,
        "old-salary",
        _utc_from_local(2026, 7, 31),
    )
    finance.link_expense(expense_event_id, "現金")
    finance.record_income(income_event_id, "普通口座", 1_000, "給与")
    finance.record_transfer(transfer_event_id, "普通口座", "クレジットカード", 200, "カード支払")
    finance.record_income(old_income_event_id, "普通口座", 50, "先月給与")

    result = ExportService(
        database_url=f"sqlite:///{database_path}",
        data_dir=tmp_path,
        timezone="Asia/Tokyo",
    ).create_export(month="2026-08")
    contents = _archive_contents(result.archive_path)

    accounts = _csv_rows(contents["accounts.csv"])
    assert [row["name"] for row in accounts] == ["現金", "普通口座", "クレジットカード"]
    balances = {row["name"]: row["current_balance"] for row in accounts}
    assert balances == {"現金": "-100", "普通口座": "850", "クレジットカード": "-200"}
    assert all(row["active"] == "true" for row in accounts)

    transactions = _csv_rows(contents["account_transactions.csv"])
    assert [row["kind"] for row in transactions] == ["expense", "income", "transfer"]
    assert [row["event_id"] for row in transactions] == [
        str(expense_event_id),
        str(income_event_id),
        str(transfer_event_id),
    ]
    assert [row["amount"] for row in transactions] == ["100", "1000", "200"]
    assert transactions[0]["source_account"] == "現金"
    assert transactions[1]["destination_account"] == "普通口座"
    assert transactions[2]["note"] == "カード支払"
    assert transactions[0]["occurred_at_local"] == "2026-08-01T12:00:00+09:00"


def test_export_row_order_is_deterministic_and_receipts_follow_expense_order(tmp_path: Path):
    database, database_path = _database(tmp_path)
    newer_event_id, newer_expense_id = _add_event(
        database,
        "newer-created-first",
        _utc_from_local(2026, 8, 2),
        amount=200,
    )
    older_event_id, older_expense_id = _add_event(
        database,
        "older-created-second",
        _utc_from_local(2026, 8, 1),
        amount=100,
    )
    assert newer_expense_id is not None
    assert older_expense_id is not None
    _add_receipt_item(
        database,
        newer_expense_id,
        name="newer-second",
        quantity=1,
        unit_price=120,
        amount=120,
        order_index=1,
    )
    _add_receipt_item(
        database,
        newer_expense_id,
        name="newer-first",
        quantity=1,
        unit_price=80,
        amount=80,
        order_index=0,
    )
    _add_receipt_item(
        database,
        older_expense_id,
        name="older-only",
        quantity=1,
        unit_price=100,
        amount=100,
    )

    service = ExportService(
        database_url=f"sqlite:///{database_path}",
        data_dir=tmp_path,
        timezone="Asia/Tokyo",
    )
    first = _archive_contents(service.create_export(month="2026-08").archive_path)
    second = _archive_contents(service.create_export(month="2026-08").archive_path)
    assert first["expenses.csv"] == second["expenses.csv"]
    assert first["receipt_items.csv"] == second["receipt_items.csv"]
    assert [row["event_id"] for row in _csv_rows(first["expenses.csv"])] == [
        str(older_event_id),
        str(newer_event_id),
    ]
    assert [row["name"] for row in _csv_rows(first["receipt_items.csv"])] == [
        "older-only",
        "newer-first",
        "newer-second",
    ]


def test_export_neutralizes_formula_text_and_keeps_numeric_cells_numeric(tmp_path: Path):
    database, database_path = _database(tmp_path)
    with database.session() as session:
        account = FinancialAccount(
            name="@wallet",
            kind=AccountKind.CASH.value,
            opening_balance=-10,
            currency="JPY",
            active=True,
        )
        session.add(account)
        session.commit()

    _, expense_id = _add_event(
        database,
        "formula-event",
        _utc_from_local(2026, 8, 10),
        amount=25,
        merchant="+shop",
        category="@category",
        description="-description",
        raw_text="  \t=SUM(A1:A2)",
    )
    assert expense_id is not None
    _add_receipt_item(
        database,
        expense_id,
        name="=item",
        quantity=1,
        unit_price=25,
        amount=25,
    )

    result = ExportService(
        database_url=f"sqlite:///{database_path}",
        data_dir=tmp_path,
        timezone="Asia/Tokyo",
    ).create_export()
    contents = _archive_contents(result.archive_path)
    expense = _csv_rows(contents["expenses.csv"])[0]
    assert expense["merchant"] == "'+shop"
    assert expense["category"] == "'@category"
    assert expense["description"] == "'-description"
    assert expense["original_text"] == "'  \t=SUM(A1:A2)"
    assert expense["amount"] == "25"

    receipt = _csv_rows(contents["receipt_items.csv"])[0]
    assert receipt["name"] == "'=item"
    assert receipt["quantity"] == "1"
    account = next(row for row in _csv_rows(contents["accounts.csv"]) if row["name"] == "'@wallet")
    assert account["opening_balance"] == "-10"
    assert not account["opening_balance"].startswith("'")


def test_export_empty_database_has_bom_headers_and_zero_counts(tmp_path: Path):
    database, database_path = _database(tmp_path)
    result = ExportService(
        database_url=f"sqlite:///{database_path}",
        data_dir=tmp_path,
        timezone="Asia/Tokyo",
    ).create_export()
    contents = _archive_contents(result.archive_path)

    assert _csv_rows(contents["expenses.csv"]) == []
    assert _csv_rows(contents["receipt_items.csv"]) == []
    assert _csv_rows(contents["accounts.csv"]) == []
    assert _csv_rows(contents["account_transactions.csv"]) == []
    manifest = _manifest(contents["manifest.json"])
    assert manifest["row_counts"] == {
        "expenses.csv": 0,
        "receipt_items.csv": 0,
        "accounts.csv": 0,
        "account_transactions.csv": 0,
    }
    assert all(
        contents[name].startswith(codecs.BOM_UTF8)
        for name in (
            "expenses.csv",
            "receipt_items.csv",
            "accounts.csv",
            "account_transactions.csv",
        )
    )


def test_export_is_non_overwriting_private_and_cleans_temporary_archives(tmp_path: Path):
    database, database_path = _database(tmp_path)
    _add_event(database, "one", _utc_from_local(2026, 8, 1), amount=10)
    output_dir = tmp_path / "exports"
    service = ExportService(
        database_url=f"sqlite:///{database_path}",
        data_dir=tmp_path,
        output_dir=output_dir,
    )

    first = service.create_export()
    first_bytes = first.archive_path.read_bytes()
    second = service.create_export()
    assert first.archive_path != second.archive_path
    assert first.archive_path.read_bytes() == first_bytes
    assert sorted(path.name for path in output_dir.glob(".*")) == []
    assert set(_archive_contents(first.archive_path)) == {
        "expenses.csv",
        "receipt_items.csv",
        "accounts.csv",
        "account_transactions.csv",
        "manifest.json",
    }


def test_export_failure_removes_hidden_archive_temp(monkeypatch, tmp_path: Path):
    database, database_path = _database(tmp_path)
    output_dir = tmp_path / "exports"
    service = ExportService(
        database_url=f"sqlite:///{database_path}",
        data_dir=tmp_path,
        output_dir=output_dir,
    )

    def fail_commit(
        self: ExportService,
        output: Path,
        temporary: Path,
        generated_at: datetime,
    ) -> Path:
        raise ExportError("保存できません")

    monkeypatch.setattr(ExportService, "_commit_archive", fail_commit)
    with pytest.raises(ExportError, match="保存できません"):
        service.create_export()
    assert sorted(path.name for path in output_dir.glob(".*")) == []


def test_export_rejects_invalid_url_month_and_output_symlink(tmp_path: Path):
    missing_output = tmp_path / "invalid-output"
    with pytest.raises(ExportError, match="SQLite") as url_error:
        ExportService(
            database_url="postgresql://user:secret@example.invalid/life",
            data_dir=tmp_path,
            output_dir=missing_output,
        ).create_export()
    assert "secret" not in str(url_error.value)
    assert not missing_output.exists()

    database, database_path = _database(tmp_path)
    invalid_month_output = tmp_path / "invalid-month-output"
    with pytest.raises(ExportError, match="YYYY-MM"):
        ExportService(
            database_url=f"sqlite:///{database_path}",
            data_dir=tmp_path,
            output_dir=invalid_month_output,
        ).create_export(month="2026-8")
    assert not invalid_month_output.exists()

    if hasattr(Path, "symlink_to"):
        target = tmp_path / "real-output"
        target.mkdir()
        symlink = tmp_path / "output-link"
        try:
            symlink.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("symbolic links are not available")
        with pytest.raises(ExportError, match="シンボリックリンク"):
            ExportService(
                database_url=f"sqlite:///{database_path}",
                data_dir=tmp_path,
                output_dir=symlink,
            ).create_export()
        assert list(target.iterdir()) == []


def test_export_rejects_symlink_final_candidate_without_touching_target(tmp_path: Path):
    database, database_path = _database(tmp_path)
    output_dir = tmp_path / "exports"
    output_dir.mkdir()
    service = ExportService(
        database_url=f"sqlite:///{database_path}",
        data_dir=tmp_path,
        output_dir=output_dir,
    )
    generated_at = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    target = tmp_path / "protected.zip"
    target.write_bytes(b"protected")
    candidate = output_dir / "export-20260829T120000Z.zip"
    try:
        candidate.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are not available")
    temporary = output_dir / ".life-inbox-export-test.zip.tmp"
    temporary.write_bytes(b"temporary")

    with pytest.raises(ExportError, match="シンボリックリンク"):
        service._commit_archive(output_dir, temporary, generated_at)
    assert target.read_bytes() == b"protected"
    assert temporary.read_bytes() == b"temporary"
    temporary.unlink()


def test_export_cli_success_and_safe_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    database, database_path = _database(tmp_path)
    _add_event(database, "cli-event", _utc_from_local(2026, 8, 1), amount=100)
    output_dir = tmp_path / "cli-exports"
    parser_args = build_parser().parse_args(
        [
            "export-csv",
            "--no-env-file",
            "--database-url",
            f"sqlite:///{database_path}",
            "--data-dir",
            str(tmp_path),
            "--month",
            "2026-08",
            "--output-dir",
            str(output_dir),
        ]
    )
    assert parser_args.command == "export-csv"
    assert parser_args.month == "2026-08"
    assert parser_args.output_dir == str(output_dir)

    assert (
        main(
            [
                "export-csv",
                "--no-env-file",
                "--database-url",
                f"sqlite:///{database_path}",
                "--data-dir",
                str(tmp_path),
                "--month",
                "2026-08",
                "--output-dir",
                str(output_dir),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "CSV export archive:" in captured.out
    assert "1 expense row(s)" in captured.out
    assert captured.err == ""

    missing_database = tmp_path / "does-not-exist.db"
    assert (
        main(
            [
                "export-csv",
                "--no-env-file",
                "--database-url",
                f"sqlite:///{missing_database}",
                "--data-dir",
                str(tmp_path),
                "--output-dir",
                str(tmp_path / "failed-export"),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "エクスポートエラー" in captured.err
    assert "does-not-exist.db" not in captured.err
