"""Safe, local CSV exports of the personal finance records.

The exporter intentionally uses a read-only SQLite connection instead of the
application write session.  This keeps the export path independent from the
Discord and AI integrations and makes it impossible for a successful export
to update an ORM object or an attachment row as a side effect.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import tempfile
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.engine import make_url


class ExportError(RuntimeError):
    """Raised when a local finance export cannot be created safely."""


@dataclass(frozen=True, slots=True)
class ExportResult:
    """The result of a successfully committed finance export."""

    archive_path: Path
    manifest: dict[str, Any]

    @property
    def row_counts(self) -> dict[str, int]:
        """Return the exported data row count for each CSV member."""

        return {
            str(name): int(count)
            for name, count in self.manifest.get("row_counts", {}).items()
        }


_MONTH_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}\Z")
_NUMERIC_PATTERN = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z"
)
_FORMULA_PREFIXES = frozenset("=+-@")
_NUMERIC_HEADERS = frozenset(
    {
        "expense_id",
        "event_id",
        "amount",
        "payment_account_id",
        "receipt_item_id",
        "quantity",
        "unit_price",
        "order_index",
        "account_id",
        "opening_balance",
        "current_balance",
        "transaction_id",
        "source_account_id",
        "destination_account_id",
        "confidence",
    }
)

_EXPENSE_HEADERS = (
    "expense_id",
    "event_id",
    "occurred_at_local",
    "amount",
    "merchant",
    "category",
    "subcategory",
    "description",
    "confidence",
    "payment_account_id",
    "payment_account",
    "original_text",
)
_RECEIPT_ITEM_HEADERS = (
    "receipt_item_id",
    "expense_id",
    "event_id",
    "name",
    "quantity",
    "unit_price",
    "amount",
    "order_index",
)
_ACCOUNT_HEADERS = (
    "account_id",
    "name",
    "kind",
    "opening_balance",
    "current_balance",
    "currency",
    "active",
)
_TRANSACTION_HEADERS = (
    "transaction_id",
    "event_id",
    "kind",
    "source_account_id",
    "source_account",
    "destination_account_id",
    "destination_account",
    "amount",
    "note",
    "occurred_at_local",
    "event_status",
)
_ARCHIVE_MEMBER_ORDER = (
    "expenses.csv",
    "receipt_items.csv",
    "accounts.csv",
    "account_transactions.csv",
    "manifest.json",
)


@dataclass(frozen=True, slots=True)
class _MonthWindow:
    month: str
    start_utc: datetime
    end_utc: datetime


class ExportService:
    """Create an atomic, non-overwriting finance ZIP from local SQLite data."""

    _SCHEMA_VERSION = 1
    _MAX_FILENAME_DISAMBIGUATION = 10_000

    def __init__(
        self,
        database_url: str = "sqlite:///data/app.db",
        data_dir: str | Path = "data",
        timezone: str = "Asia/Tokyo",
        output_dir: str | Path | None = None,
    ) -> None:
        self.database_url = database_url
        self.data_dir = Path(data_dir)
        self.output_dir = (
            Path(output_dir) if output_dir is not None else self.data_dir / "exports"
        )
        try:
            self.timezone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
            raise ExportError("タイムゾーンが不正です") from exc
        self.timezone_name = str(self.timezone)

    def create_export(self, month: str | None = None) -> ExportResult:
        """Create and atomically commit one finance export archive."""

        month_window = self._month_window(month)
        source_path = self._sqlite_database_path()
        output_dir = self._prepare_output_dir()
        generated_at = datetime.now(UTC)
        archive_temp_path: Path | None = None

        try:
            data = self._collect_data(source_path, month_window)
            manifest = self._build_manifest(
                generated_at=generated_at,
                month_window=month_window,
                row_counts={
                    "expenses.csv": len(data["expenses"]),
                    "receipt_items.csv": len(data["receipt_items"]),
                    "accounts.csv": len(data["accounts"]),
                    "account_transactions.csv": len(data["transactions"]),
                },
            )
            archive_files = self._build_archive_files(data, manifest)
            archive_temp_path = self._write_archive(output_dir, archive_files)
            archive_path = self._commit_archive(
                output_dir,
                archive_temp_path,
                generated_at,
            )
            archive_temp_path = None
            return ExportResult(archive_path=archive_path, manifest=manifest)
        except ExportError:
            raise
        except (OSError, sqlite3.Error, ValueError, zipfile.BadZipFile) as exc:
            raise ExportError("CSVエクスポートを作成できませんでした") from exc
        except Exception as exc:
            # Do not expose database paths, SQL, or any raw cell value in the
            # CLI error.  The exception remains chained for programmatic logs.
            raise ExportError("CSVエクスポートを作成できませんでした") from exc
        finally:
            self._remove_temp_file(archive_temp_path)

    def export_csv(self, month: str | None = None) -> ExportResult:
        """Compatibility alias for callers that name the output format."""

        return self.create_export(month=month)

    def export(self, month: str | None = None) -> ExportResult:
        """Compatibility alias for callers that prefer a shorter method name."""

        return self.create_export(month=month)

    @staticmethod
    def _validate_month(month: str | None) -> str | None:
        if month is None:
            return None
        if not isinstance(month, str) or _MONTH_PATTERN.fullmatch(month) is None:
            raise ExportError("月はYYYY-MM形式で指定してください")
        try:
            date.fromisoformat(f"{month}-01")
        except ValueError as exc:
            raise ExportError("月はYYYY-MM形式で指定してください") from exc
        return month

    def _month_window(self, month: str | None) -> _MonthWindow | None:
        normalized_month = self._validate_month(month)
        if normalized_month is None:
            return None

        year = int(normalized_month[:4])
        month_number = int(normalized_month[5:])
        month_index = year * 12 + month_number - 1
        next_year, next_month_index = divmod(month_index + 1, 12)
        next_month = next_month_index + 1
        try:
            local_start = datetime(year, month_number, 1, tzinfo=self.timezone)
            local_end = datetime(next_year, next_month, 1, tzinfo=self.timezone)
        except ValueError as exc:
            raise ExportError("月はYYYY-MM形式で指定してください") from exc
        return _MonthWindow(
            month=normalized_month,
            # SQLite DateTime values in this project are UTC-naive.  Keep the
            # calculated boundaries in that same representation after doing
            # the calendar arithmetic in the configured local timezone.
            start_utc=local_start.astimezone(UTC).replace(tzinfo=None),
            end_utc=local_end.astimezone(UTC).replace(tzinfo=None),
        )

    def _sqlite_database_path(self) -> Path:
        try:
            url = make_url(self.database_url)
        except Exception as exc:
            raise ExportError("データベースURLを解釈できません") from exc

        if url.get_backend_name() != "sqlite":
            raise ExportError("エクスポートはローカルSQLite URLにのみ対応しています")
        if url.host or url.username or url.password or url.port:
            raise ExportError("エクスポートはローカルSQLite URLにのみ対応しています")
        if url.query:
            raise ExportError("SQLiteのURIオプション付きURLには対応していません")

        database = unquote(url.database or "")
        if not database or database == ":memory:" or database.startswith("file:"):
            raise ExportError("ファイル型SQLiteデータベースを指定してください")

        try:
            path = Path(database)
            if not path.is_absolute():
                path = Path.cwd() / path
            resolved = path.resolve(strict=True)
            if not resolved.is_file():
                raise ExportError("SQLiteデータベースファイルが見つかりません")
            return resolved
        except ExportError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise ExportError("SQLiteデータベースファイルが見つかりません") from exc

    def _prepare_output_dir(self) -> Path:
        try:
            if self.output_dir.is_symlink():
                raise ExportError("エクスポート出力先にシンボリックリンクがあります")
            self.output_dir.mkdir(parents=True, exist_ok=True)
            if self.output_dir.is_symlink():
                raise ExportError("エクスポート出力先にシンボリックリンクがあります")
            if not self.output_dir.is_dir():
                raise ExportError("エクスポート出力先がディレクトリではありません")
            return self.output_dir
        except ExportError:
            raise
        except OSError as exc:
            raise ExportError("エクスポート出力先を準備できませんでした") from exc

    def _collect_data(
        self,
        source_path: Path,
        month_window: _MonthWindow | None,
    ) -> dict[str, list[sqlite3.Row]]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{source_path.as_uri()}?mode=ro",
                uri=True,
                timeout=30,
            )
            connection.row_factory = sqlite3.Row
            # query_only is a second guard in addition to mode=ro.  BEGIN
            # gives all five members one consistent database view.
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")

            accounts = list(
                self._select_rows(
                    connection,
                    """
                    SELECT id AS account_id, name, kind, opening_balance,
                           currency, active
                    FROM financial_accounts
                    ORDER BY id
                    """,
                )
            )
            all_transactions = list(
                self._select_rows(
                    connection,
                    """
                    SELECT
                        la.id AS transaction_id,
                        la.event_id AS event_id,
                        la.kind AS kind,
                        la.source_account_id AS source_account_id,
                        source.name AS source_account,
                        la.destination_account_id AS destination_account_id,
                        destination.name AS destination_account,
                        la.amount AS stored_amount,
                        expense.amount AS expense_amount,
                        la.description AS note,
                        la.occurred_at AS occurred_at,
                        event.status AS event_status
                    FROM ledger_activities AS la
                    LEFT JOIN events AS event ON event.id = la.event_id
                    LEFT JOIN expenses AS expense ON expense.event_id = la.event_id
                    LEFT JOIN financial_accounts AS source
                        ON source.id = la.source_account_id
                    LEFT JOIN financial_accounts AS destination
                        ON destination.id = la.destination_account_id
                    ORDER BY la.occurred_at, la.id
                    """,
                )
            )
            transactions = list(all_transactions)
            expenses = list(
                self._select_rows(
                    connection,
                    """
                    SELECT
                        expense.id AS expense_id,
                        event.id AS event_id,
                        event.occurred_at AS occurred_at,
                        expense.amount AS amount,
                        expense.merchant_normalized AS merchant_normalized,
                        expense.merchant_raw AS merchant_raw,
                        expense.category AS category,
                        expense.subcategory AS subcategory,
                        expense.description AS description,
                        expense.confidence AS confidence,
                        event.raw_text AS original_text
                    FROM expenses AS expense
                    INNER JOIN events AS event ON event.id = expense.event_id
                    WHERE event.status = 'confirmed'
                    ORDER BY event.occurred_at, event.id, expense.id
                    """,
                )
            )
            if month_window is not None:
                expenses = [
                    row
                    for row in expenses
                    if self._within_month(row["occurred_at"], month_window)
                ]
                transactions = [
                    row
                    for row in transactions
                    if self._within_month(row["occurred_at"], month_window)
                ]

            exported_expense_ids = {int(row["expense_id"]) for row in expenses}
            receipt_items = [
                row
                for row in self._select_rows(
                    connection,
                    """
                    SELECT id AS receipt_item_id, expense_id, name,
                           quantity, unit_price, amount, order_index
                    FROM receipt_items
                    ORDER BY expense_id, order_index, id
                    """,
                )
                if int(row["expense_id"]) in exported_expense_ids
            ]
            expense_event_ids = {
                int(row["expense_id"]): int(row["event_id"]) for row in expenses
            }
            expense_order = {
                expense_id: index for index, expense_id in enumerate(expense_event_ids)
            }
            receipt_items.sort(
                key=lambda row: (
                    expense_order[int(row["expense_id"])],
                    int(row["order_index"]),
                    int(row["receipt_item_id"]),
                )
            )
            return {
                "expenses": expenses,
                "receipt_items": receipt_items,
                "accounts": accounts,
                "transactions": transactions,
                "all_transactions": all_transactions,
            }
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                finally:
                    connection.close()

    @staticmethod
    def _select_rows(
        connection: sqlite3.Connection,
        statement: str,
        parameters: Sequence[object] = (),
    ) -> tuple[sqlite3.Row, ...]:
        try:
            return tuple(connection.execute(statement, parameters).fetchall())
        except sqlite3.OperationalError as exc:
            # A legacy database may predate one of the optional finance
            # tables.  An absent table is an empty dataset; malformed columns
            # and other SQLite errors remain failures.
            if "no such table" in str(exc).casefold():
                return ()
            raise

    def _within_month(self, value: object, window: _MonthWindow) -> bool:
        occurred_utc = self._utc_datetime(value).replace(tzinfo=None)
        return window.start_utc <= occurred_utc < window.end_utc

    @staticmethod
    def _utc_datetime(value: object) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            normalized = value.strip()
            if normalized.endswith("Z"):
                normalized = normalized[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError as exc:
                raise ExportError("データベースの日時を解釈できません") from exc
        else:
            raise ExportError("データベースの日時を解釈できません")

        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _local_datetime(self, value: object) -> str:
        local_value = self._utc_datetime(value).astimezone(self.timezone)
        timespec = "microseconds" if local_value.microsecond else "seconds"
        return local_value.isoformat(timespec=timespec)

    def _build_archive_files(
        self,
        data: dict[str, list[sqlite3.Row]],
        manifest: dict[str, Any],
    ) -> dict[str, bytes]:
        transactions = data["transactions"]
        all_transactions = data["all_transactions"]
        payment_accounts: dict[int, sqlite3.Row] = {}
        for row in all_transactions:
            if row["kind"] == "expense" and row["event_id"] is not None:
                payment_accounts.setdefault(int(row["event_id"]), row)

        expense_rows = [
            (
                row["expense_id"],
                row["event_id"],
                self._local_datetime(row["occurred_at"]),
                row["amount"],
                row["merchant_normalized"] or row["merchant_raw"],
                row["category"],
                row["subcategory"],
                row["description"],
                row["confidence"],
                (
                    payment_accounts[int(row["event_id"])]["source_account_id"]
                    if int(row["event_id"]) in payment_accounts
                    else None
                ),
                (
                    payment_accounts[int(row["event_id"])]["source_account"]
                    if int(row["event_id"]) in payment_accounts
                    else None
                ),
                row["original_text"],
            )
            for row in data["expenses"]
        ]

        expense_event_ids = {
            int(row["expense_id"]): int(row["event_id"]) for row in data["expenses"]
        }
        receipt_rows = [
            (
                row["receipt_item_id"],
                row["expense_id"],
                expense_event_ids[int(row["expense_id"])],
                row["name"],
                row["quantity"],
                row["unit_price"],
                row["amount"],
                row["order_index"],
            )
            for row in data["receipt_items"]
        ]

        account_rows = self._account_rows(data["accounts"], all_transactions)
        transaction_rows = [
            (
                row["transaction_id"],
                row["event_id"],
                row["kind"],
                row["source_account_id"],
                row["source_account"],
                row["destination_account_id"],
                row["destination_account"],
                self._effective_amount(row),
                row["note"],
                self._local_datetime(row["occurred_at"]),
                row["event_status"],
            )
            for row in transactions
        ]

        return {
            "expenses.csv": self._csv_bytes(_EXPENSE_HEADERS, expense_rows),
            "receipt_items.csv": self._csv_bytes(_RECEIPT_ITEM_HEADERS, receipt_rows),
            "accounts.csv": self._csv_bytes(_ACCOUNT_HEADERS, account_rows),
            "account_transactions.csv": self._csv_bytes(
                _TRANSACTION_HEADERS,
                transaction_rows,
            ),
            "manifest.json": json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8"),
        }

    @staticmethod
    def _effective_amount(row: sqlite3.Row) -> object:
        if row["kind"] == "expense":
            return row["expense_amount"]
        return row["stored_amount"]

    @staticmethod
    def _account_rows(
        accounts: list[sqlite3.Row],
        transactions: list[sqlite3.Row],
    ) -> list[tuple[object, ...]]:
        balances = {
            int(row["account_id"]): int(row["opening_balance"])
            for row in accounts
        }
        kinds = {int(row["account_id"]): row["kind"] for row in accounts}

        for row in transactions:
            if row["event_status"] != "confirmed":
                continue
            amount = ExportService._effective_amount(row)
            if amount is None:
                continue
            amount_int = int(amount)
            source_id = row["source_account_id"]
            destination_id = row["destination_account_id"]
            if row["kind"] in {"expense", "transfer"} and source_id is not None:
                account_id = int(source_id)
                if account_id in balances:
                    if kinds.get(account_id) == "card":
                        balances[account_id] += amount_int
                    else:
                        balances[account_id] -= amount_int
            if row["kind"] in {"income", "transfer"} and destination_id is not None:
                account_id = int(destination_id)
                if account_id in balances:
                    if kinds.get(account_id) == "card":
                        balances[account_id] -= amount_int
                    else:
                        balances[account_id] += amount_int

        return [
            (
                row["account_id"],
                row["name"],
                row["kind"],
                row["opening_balance"],
                balances[int(row["account_id"])],
                row["currency"],
                bool(row["active"]),
            )
            for row in accounts
        ]

    def _build_manifest(
        self,
        *,
        generated_at: datetime,
        month_window: _MonthWindow | None,
        row_counts: dict[str, int],
    ) -> dict[str, Any]:
        generated_at_utc = generated_at.astimezone(UTC).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
        selected_month = month_window.month if month_window is not None else "all"
        columns = {
            "expenses.csv": list(_EXPENSE_HEADERS),
            "receipt_items.csv": list(_RECEIPT_ITEM_HEADERS),
            "accounts.csv": list(_ACCOUNT_HEADERS),
            "account_transactions.csv": list(_TRANSACTION_HEADERS),
        }
        return {
            "schema_version": self._SCHEMA_VERSION,
            "generated_at_utc": generated_at_utc,
            "selected_month": selected_month,
            "timezone": self.timezone_name,
            "row_counts": dict(row_counts),
            # Keep a short alias consistent with the existing backup manifest.
            "counts": dict(row_counts),
            "files": {
                name: {"row_count": row_counts[name], "columns": columns[name]}
                for name in row_counts
            },
            "filters": {
                "month": selected_month,
                "expenses": {
                    "event_status": "confirmed",
                    "event_link_required": True,
                    "month_field": "Event.occurred_at",
                },
                "receipt_items": {
                    "linked_expenses_only": True,
                },
                "accounts": "all current FinancialAccount rows",
                "account_transactions": {
                    "rows": "all stored LedgerActivity rows",
                    "month_field": "LedgerActivity.occurred_at",
                },
            },
            "semantics": {
                "confirmed_expenses_only": True,
                "current_balance": (
                    "opening balance plus confirmed event-linked ledger activities; "
                    "expense amounts come from the linked Expense row"
                ),
                "original_text_privacy": (
                    "Event.raw_text is exported without redaction; keep this archive private"
                ),
                "account_transactions_note": (
                    "event_status is included so pending or ignored rows remain "
                    "auditable and do not affect current_balance"
                ),
            },
        }

    @staticmethod
    def _csv_bytes(
        headers: Sequence[str],
        rows: Iterable[Sequence[object]],
    ) -> bytes:
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(headers)
        for row in rows:
            writer.writerow(
                ExportService._render_cell(header, value)
                for header, value in zip(headers, row, strict=True)
            )
        return output.getvalue().encode("utf-8-sig")

    @staticmethod
    def _render_cell(header: str, value: object) -> str:
        if value is None:
            return ""
        if header in _NUMERIC_HEADERS:
            text = str(value)
            if _NUMERIC_PATTERN.fullmatch(text) is None:
                raise ExportError("数値データを読み取れません")
            return text
        if header == "active":
            return "true" if bool(value) else "false"
        return ExportService._safe_text(str(value))

    @staticmethod
    def _safe_text(value: str) -> str:
        first_non_whitespace = next(
            (character for character in value if not character.isspace()),
            None,
        )
        if first_non_whitespace in _FORMULA_PREFIXES:
            # A leading apostrophe is understood by spreadsheet software as a
            # literal-text marker while leaving the meaningful content intact.
            return "'" + value
        return value

    @staticmethod
    def _write_archive(output_dir: Path, archive_files: dict[str, bytes]) -> Path:
        file_descriptor, archive_name = tempfile.mkstemp(
            prefix=".life-inbox-export-",
            suffix=".zip.tmp",
            dir=output_dir,
        )
        os.close(file_descriptor)
        archive_path = Path(archive_name)
        try:
            with zipfile.ZipFile(
                archive_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                for member_name in _ARCHIVE_MEMBER_ORDER:
                    archive.writestr(member_name, archive_files[member_name])
            with archive_path.open("rb") as file_handle:
                os.fsync(file_handle.fileno())
            return archive_path
        except Exception:
            ExportService._remove_temp_file(archive_path)
            raise

    def _commit_archive(
        self,
        output_dir: Path,
        temp_path: Path,
        generated_at: datetime,
    ) -> Path:
        timestamp = generated_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        reserved_path: Path | None = None
        for disambiguation in range(self._MAX_FILENAME_DISAMBIGUATION):
            suffix = "" if disambiguation == 0 else f"-{disambiguation:02d}"
            candidate = output_dir / f"export-{timestamp}{suffix}.zip"
            if candidate.is_symlink():
                raise ExportError("エクスポート先の最終ファイルがシンボリックリンクです")
            try:
                # Linking first publishes a complete archive without
                # overwriting an existing regular file.
                os.link(temp_path, candidate)
                reserved_path = candidate
                break
            except FileExistsError:
                if candidate.is_symlink():
                    raise ExportError(
                        "エクスポート先の最終ファイルがシンボリックリンクです"
                    )
                continue
            except OSError as exc:
                raise ExportError("エクスポートの保存先を確保できませんでした") from exc

        if reserved_path is None:
            raise ExportError("エクスポートファイル名を確保できませんでした")

        try:
            temp_path.unlink()
            return reserved_path
        except OSError as exc:
            try:
                reserved_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise ExportError("エクスポートを最終保存できませんでした") from exc

    @staticmethod
    def _remove_temp_file(path: Path | None) -> None:
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
