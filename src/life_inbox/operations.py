"""Secret-safe local operational diagnostics for the life-inbox runtime."""

from __future__ import annotations

import http.client
import os
import sqlite3
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy.engine import make_url

from .config import Settings

CheckStatus = Literal["ok", "warning", "error"]


@dataclass(frozen=True, slots=True)
class OperationCheck:
    name: str
    status: CheckStatus
    message: str


@dataclass(frozen=True, slots=True)
class OperationsReport:
    checked_at: datetime
    checks: tuple[OperationCheck, ...]

    @property
    def healthy(self) -> bool:
        return all(check.status != "error" for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "healthy": self.healthy,
            "checkedAt": self.checked_at.isoformat().replace("+00:00", "Z"),
            "checks": [asdict(check) for check in self.checks],
        }


def _sqlite_path(database_url: str, *, working_directory: Path) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or url.database in {None, "", ":memory:"}:
        return None
    path = Path(url.database)
    if not path.is_absolute():
        path = working_directory / path
    return path.resolve()


def _check_database(settings: Settings, *, working_directory: Path) -> OperationCheck:
    try:
        path = _sqlite_path(settings.database_url, working_directory=working_directory)
    except Exception as exc:  # noqa: BLE001 - never reflect a possibly secret URL
        return OperationCheck("database", "error", f"DB設定を解釈できません: {type(exc).__name__}")
    if path is None:
        return OperationCheck(
            "database",
            "warning",
            "SQLite以外またはメモリDBのため、この診断では整合性を確認していません。",
        )
    if not path.is_file():
        return OperationCheck("database", "error", "SQLiteファイルが見つかりません。")
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            foreign_key_error = connection.execute("PRAGMA foreign_key_check").fetchone()
            table_count = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        return OperationCheck(
            "database",
            "error",
            f"SQLiteを読み取り確認できません: {type(exc).__name__}",
        )
    if quick_check != ("ok",) or foreign_key_error is not None:
        return OperationCheck("database", "error", "SQLite整合性チェックに失敗しました。")
    count = int(table_count[0]) if table_count is not None else 0
    return OperationCheck(
        "database",
        "ok",
        f"SQLite quick_check・外部キー整合性OK（テーブル {count}件）。",
    )


def _check_data_directory(settings: Settings, *, working_directory: Path) -> OperationCheck:
    path = settings.data_dir
    if not path.is_absolute():
        path = working_directory / path
    path = path.resolve()
    if not path.is_dir():
        return OperationCheck("dataDirectory", "error", "DATA_DIRが見つかりません。")
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        return OperationCheck(
            "dataDirectory",
            "error",
            "DATA_DIRを読み書きできません。権限を確認してください。",
        )
    return OperationCheck("dataDirectory", "ok", "DATA_DIRを読み書きできます。")


def _check_latest_backup(settings: Settings, *, working_directory: Path) -> OperationCheck:
    data_dir = settings.data_dir
    if not data_dir.is_absolute():
        data_dir = working_directory / data_dir
    backup_dir = data_dir.resolve() / "backups"
    try:
        candidates = sorted(
            (path for path in backup_dir.glob("backup-*.zip") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError as exc:
        return OperationCheck(
            "backup",
            "error",
            f"バックアップ一覧を確認できません: {type(exc).__name__}",
        )
    if not candidates:
        return OperationCheck(
            "backup",
            "warning",
            "検証対象のバックアップがまだありません。",
        )
    latest = candidates[0]
    try:
        with zipfile.ZipFile(latest) as archive:
            bad_member = archive.testzip()
        modified = datetime.fromtimestamp(latest.stat().st_mtime, tz=UTC)
    except (OSError, zipfile.BadZipFile) as exc:
        return OperationCheck(
            "backup",
            "error",
            f"最新バックアップを検証できません: {type(exc).__name__}",
        )
    if bad_member is not None:
        return OperationCheck("backup", "error", "最新バックアップZIPが破損しています。")
    age_hours = max(0, int((datetime.now(UTC) - modified).total_seconds() // 3_600))
    status: CheckStatus = "ok" if age_hours <= 36 else "warning"
    return OperationCheck(
        "backup",
        status,
        f"最新バックアップZIPは読取可能です（約{age_hours}時間前）。",
    )


def _check_web(settings: Settings) -> OperationCheck:
    connection = http.client.HTTPConnection(
        settings.web_host,
        settings.web_port,
        timeout=1.5,
    )
    try:
        connection.request("GET", "/healthz", headers={"Accept": "application/json"})
        response = connection.getresponse()
        payload = response.read(256)
    except (OSError, http.client.HTTPException):
        return OperationCheck(
            "web",
            "warning",
            "ローカルWebの/healthzへ接続できません（Web停止中の可能性）。",
        )
    finally:
        connection.close()
    if response.status != 200 or b'"status":"ok"' not in payload.replace(b" ", b""):
        return OperationCheck(
            "web",
            "warning",
            f"ローカルWebのhealth応答が想定外です（HTTP {response.status}）。",
        )
    return OperationCheck("web", "ok", "ローカルWebの/healthzは正常です。")


def _check_configuration(settings: Settings) -> tuple[OperationCheck, ...]:
    checks: list[OperationCheck] = []
    try:
        settings.validate_for_bot()
    except ValueError:
        checks.append(
            OperationCheck(
                "discordConfiguration",
                "error",
                "Bot必須設定が不足しています。"
                "`python -m life_inbox check`で項目を確認してください。",
            )
        )
    else:
        checks.append(
            OperationCheck(
                "discordConfiguration",
                "ok",
                "Discord Botの必須設定は揃っています（値は表示しません）。",
            )
        )
    summary = settings.safe_summary()
    checks.append(
        OperationCheck(
            "deviceApi",
            "ok" if summary["rhythm_api_configured"] else "warning",
            (
                "端末同期APIのトークンハッシュは設定済みです。"
                if summary["rhythm_api_configured"]
                else "端末同期APIは未設定です。"
            ),
        )
    )
    if settings.rhythm_enabled:
        provider_states = {
            "CHUNITHM": bool(summary["chunirec_configured"]),
            "maimai": bool(summary["maimai_browser_configured"]),
            "オンゲキ": bool(summary["ongeki_browser_configured"]),
        }
        configured = [name for name, ready in provider_states.items() if ready]
        incomplete = [name for name, ready in provider_states.items() if not ready]
        provider_count = len(configured)
        provider_message = f"設定済み: {', '.join(configured) or 'なし'}。"
        if incomplete:
            provider_message += f" 未完了: {', '.join(incomplete)}。"
        checks.append(
            OperationCheck(
                "rhythmProviders",
                "ok" if provider_count == 3 else "warning",
                f"音ゲー取得元 {provider_count}/3。{provider_message}秘密値は表示しません。",
            )
        )
    return tuple(checks)


def run_operations_check(
    settings: Settings,
    *,
    working_directory: Path | None = None,
) -> OperationsReport:
    """Run local read-only checks without contacting third-party services."""

    cwd = (working_directory or Path.cwd()).resolve()
    checks = (
        *_check_configuration(settings),
        _check_data_directory(settings, working_directory=cwd),
        _check_database(settings, working_directory=cwd),
        _check_latest_backup(settings, working_directory=cwd),
        _check_web(settings),
    )
    return OperationsReport(checked_at=datetime.now(UTC), checks=checks)
