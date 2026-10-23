from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from life_inbox.cli import main
from life_inbox.config import Settings
from life_inbox.db import Database
from life_inbox.operations import run_operations_check


def _settings(tmp_path: Path, *, web_port: int = 9) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
        web_port=web_port,
        discord_bot_token="test-token",
        discord_guild_id=1,
        allowed_user_id=2,
        inbox_channel_id=3,
    )


def test_operations_report_checks_db_backup_and_keeps_web_failure_nonfatal(tmp_path: Path):
    settings = _settings(tmp_path)
    Database(settings.database_url).init()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    with zipfile.ZipFile(backup_dir / "backup-20260830T000000Z.zip", "w") as archive:
        archive.writestr("manifest.json", "{}")

    report = run_operations_check(settings, working_directory=tmp_path)
    checks = {check.name: check for check in report.checks}

    assert report.healthy is True
    assert checks["discordConfiguration"].status == "ok"
    assert checks["database"].status == "ok"
    assert "quick_check" in checks["database"].message
    assert checks["backup"].status == "ok"
    assert checks["web"].status == "warning"
    serialized = json.dumps(report.as_dict(), ensure_ascii=False)
    assert "test-token" not in serialized


def test_operations_report_fails_when_database_or_bot_config_is_missing(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'missing.db'}",
        data_dir=tmp_path,
        web_port=9,
    )

    report = run_operations_check(settings, working_directory=tmp_path)
    checks = {check.name: check for check in report.checks}

    assert report.healthy is False
    assert checks["discordConfiguration"].status == "error"
    assert checks["database"].status == "error"
    assert all("sqlite:///" not in check.message for check in report.checks)


def test_doctor_cli_returns_machine_readable_secret_safe_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    settings = _settings(tmp_path)
    Database(settings.database_url).init()
    monkeypatch.setattr("life_inbox.cli._settings", lambda _args: settings)

    result = main(["doctor"])
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["healthy"] is True
    assert isinstance(payload["checks"], list)
    assert "test-token" not in json.dumps(payload)
