from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from life_inbox.archive_ingest import ArchiveIngestService
from life_inbox.archive_models import ArchiveAttachment
from life_inbox.backup import BackupError, BackupService
from life_inbox.cli import build_parser, main
from life_inbox.db import Database
from life_inbox.models import Attachment, Event


def _database(tmp_path: Path) -> tuple[Database, Path]:
    database_path = tmp_path / "app.db"
    database = Database(f"sqlite:///{database_path}")
    database.init()
    return database, database_path


def _event(database: Database, message_id: str = "message-1") -> int:
    now = datetime.now(UTC).replace(tzinfo=None)
    with database.session() as session:
        event = Event(
            discord_message_id=message_id,
            discord_channel_id="channel-1",
            discord_user_id="user-1",
            captured_at=now,
            occurred_at=now,
            raw_text="テスト",
        )
        session.add(event)
        session.commit()
        return event.id


def _attachment(
    database: Database,
    data_dir: Path,
    event_id: int,
    *,
    relative_path: str = "2026/08/receipt.png",
    data: bytes = b"attachment-bytes",
    stored_sha256: str | None = None,
) -> Path:
    path = data_dir / "attachments" / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    with database.session() as session:
        session.add(
            Attachment(
                event_id=event_id,
                discord_attachment_id=f"attachment-{event_id}",
                original_filename="receipt.png",
                content_type="image/png",
                local_path=str(path),
                sha256=stored_sha256 or hashlib.sha256(data).hexdigest(),
            )
        )
        session.commit()
    return path


def _read_archive(result_path: Path) -> tuple[dict[str, object], zipfile.ZipFile]:
    archive = zipfile.ZipFile(result_path)
    manifest = json.loads(archive.read("manifest.json"))
    return manifest, archive


def test_backup_contains_snapshot_manifest_and_only_verified_attachments(tmp_path: Path):
    database, _ = _database(tmp_path)
    event_id = _event(database)
    attachment_path = _attachment(database, tmp_path, event_id)
    (tmp_path / ".env.local").write_text("DISCORD_BOT_TOKEN=not-for-archive\n", encoding="utf-8")
    (tmp_path / "unrelated.txt").write_text("not-for-archive", encoding="utf-8")
    old_backup = tmp_path / "backups" / "old.zip"
    old_backup.parent.mkdir(parents=True)
    old_backup.write_bytes(b"old-backup")

    result = BackupService(
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
    ).create_backup()

    assert result.archive_path.parent == tmp_path / "backups"
    assert result.archive_path.name.startswith("backup-")
    assert result.archive_path.suffix == ".zip"
    assert old_backup.read_bytes() == b"old-backup"
    manifest, archive = _read_archive(result.archive_path)
    try:
        assert set(archive.namelist()) == {
            "database/app.db",
            "attachments/2026/08/receipt.png",
            "manifest.json",
        }
        assert archive.read("attachments/2026/08/receipt.png") == attachment_path.read_bytes()
    finally:
        archive.close()

    assert manifest["schema_version"] == 1
    assert str(manifest["created_at_utc"]).endswith("Z")
    assert manifest["database"] == {
        "integrity_check": "ok",
        "foreign_key_check": "ok",
    }
    assert manifest["counts"] == {
        "attachments_referenced": 1,
        "attachments_included": 1,
    }
    assert manifest["warnings"] == []
    assert manifest["attachments"] == [
        {
            "id": 1,
            "event_id": event_id,
            "original_filename": "receipt.png",
            "content_type": "image/png",
            "archive_path": "attachments/2026/08/receipt.png",
            "size_bytes": len(b"attachment-bytes"),
            "sha256": hashlib.sha256(b"attachment-bytes").hexdigest(),
            "stored_sha256": hashlib.sha256(b"attachment-bytes").hexdigest(),
        }
    ]
    assert ".env.local" not in result.archive_path.name
    assert result.attachment_count == 1
    hidden_files = [
        path.name for path in result.archive_path.parent.iterdir() if path.name.startswith(".")
    ]
    assert hidden_files == []


def test_backup_uses_online_snapshot_while_live_transaction_is_open(tmp_path: Path):
    database, database_path = _database(tmp_path)
    _event(database, message_id="committed")
    live_connection = sqlite3.connect(database_path)
    try:
        live_connection.execute("BEGIN")
        live_connection.execute(
            """
            INSERT INTO events (
                discord_message_id, discord_channel_id, discord_user_id,
                captured_at, occurred_at, raw_text, source, status, event_type,
                event_kinds, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "uncommitted",
                "channel-1",
                "user-1",
                "2026-08-29 00:00:00",
                "2026-08-29 00:00:00",
                "live transaction",
                "discord",
                "pending",
                "unknown",
                "[]",
                "2026-08-29 00:00:00",
                "2026-08-29 00:00:00",
            ),
        )

        result = BackupService(
            database_url=f"sqlite:///{database_path}",
            data_dir=tmp_path,
        ).create_backup()
    finally:
        live_connection.rollback()
        live_connection.close()

    manifest, archive = _read_archive(result.archive_path)
    try:
        snapshot_path = tmp_path / "snapshot-from-archive.db"
        snapshot_path.write_bytes(archive.read("database/app.db"))
    finally:
        archive.close()
    snapshot = sqlite3.connect(snapshot_path)
    try:
        message_ids = [row[0] for row in snapshot.execute("SELECT discord_message_id FROM events")]
        assert message_ids == ["committed"]
        assert manifest["database"]["integrity_check"] == "ok"
        assert manifest["database"]["foreign_key_check"] == "ok"
    finally:
        snapshot.close()


def test_backup_accepts_a_data_dir_below_a_symlinked_system_ancestor(tmp_path: Path):
    """A benign ancestor alias such as macOS /var must not look like an attachment link."""

    database, _ = _database(tmp_path)

    result = BackupService(
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
    ).create_backup()

    assert result.archive_path.is_file()
    hidden_files = [
        path.name for path in result.archive_path.parent.iterdir() if path.name.startswith(".")
    ]
    assert hidden_files == []


@pytest.mark.parametrize(
    ("case", "expected_message"),
    [
        ("missing", "見つかりません"),
        ("hash", "SHA-256"),
        ("outside", "DATA_DIR/attachments外"),
    ],
)
def test_backup_fails_safely_for_invalid_attachment_paths_or_bytes(
    tmp_path: Path,
    case: str,
    expected_message: str,
):
    database, _ = _database(tmp_path)
    event_id = _event(database, message_id=f"message-{case}")
    if case == "missing":
        path = _attachment(database, tmp_path, event_id)
        path.unlink()
    elif case == "hash":
        _attachment(database, tmp_path, event_id, stored_sha256="0" * 64)
    else:
        path = tmp_path / "outside.bin"
        path.write_bytes(b"outside")
        with database.session() as session:
            session.add(
                Attachment(
                    event_id=event_id,
                    discord_attachment_id="attachment-outside",
                    original_filename="outside.bin",
                    content_type="application/octet-stream",
                    local_path=str(path),
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
            session.commit()

    with pytest.raises(BackupError, match=expected_message):
        BackupService(
            database_url=f"sqlite:///{tmp_path / 'app.db'}",
            data_dir=tmp_path,
        ).create_backup()

    assert list((tmp_path / "backups").glob("*.zip")) == []
    assert list((tmp_path / "backups").glob(".*")) == []


def test_backup_rejects_symlink_attachment(tmp_path: Path):
    if not hasattr(os, "symlink"):
        pytest.skip("symbolic links are not available")

    database, _ = _database(tmp_path)
    event_id = _event(database)
    target = tmp_path / "outside.bin"
    target.write_bytes(b"outside")
    link = tmp_path / "attachments" / "2026" / "08" / "linked.png"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    with database.session() as session:
        session.add(
            Attachment(
                event_id=event_id,
                discord_attachment_id="attachment-link",
                original_filename="linked.png",
                content_type="image/png",
                local_path=str(link),
                sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            )
        )
        session.commit()

    with pytest.raises(BackupError, match="シンボリックリンク"):
        BackupService(
            database_url=f"sqlite:///{tmp_path / 'app.db'}",
            data_dir=tmp_path,
        ).create_backup()


def test_backup_rejects_path_traversal_attachment(tmp_path: Path):
    database, _ = _database(tmp_path)
    event_id = _event(database)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    traversal_path = tmp_path / "attachments" / ".." / "outside.bin"
    with database.session() as session:
        session.add(
            Attachment(
                event_id=event_id,
                discord_attachment_id="attachment-traversal",
                original_filename="outside.bin",
                content_type="application/octet-stream",
                local_path=str(traversal_path),
                sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
            )
        )
        session.commit()

    with pytest.raises(BackupError, match="親ディレクトリ"):
        BackupService(
            database_url=f"sqlite:///{tmp_path / 'app.db'}",
            data_dir=tmp_path,
        ).create_backup()


def test_backup_rejects_foreign_key_failure_in_snapshot(tmp_path: Path):
    _, database_path = _database(tmp_path)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            INSERT INTO attachments (
                event_id, discord_attachment_id, original_filename,
                content_type, local_path, sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                999,
                "orphan",
                "orphan.png",
                "image/png",
                "orphan.png",
                "0" * 64,
                "2026-08-29 00:00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(BackupError, match="foreign_key_check"):
        BackupService(
            database_url=f"sqlite:///{database_path}",
            data_dir=tmp_path,
        ).create_backup()

    assert list((tmp_path / "backups").glob(".*")) == []


def test_backup_does_not_overwrite_an_existing_archive(tmp_path: Path):
    database, _ = _database(tmp_path)
    event_id = _event(database)
    _attachment(database, tmp_path, event_id)
    service = BackupService(
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
    )

    first = service.create_backup()
    first_bytes = first.archive_path.read_bytes()
    second = service.create_backup()

    assert first.archive_path != second.archive_path
    assert first.archive_path.read_bytes() == first_bytes
    assert first.archive_path.exists()
    assert second.archive_path.exists()


def test_backup_rejects_non_sqlite_url_without_echoing_url(tmp_path: Path):
    secret_url = "postgresql://backup-user:backup-password@example.invalid/life"

    with pytest.raises(BackupError, match="SQLite") as error:
        BackupService(database_url=secret_url, data_dir=tmp_path).create_backup()

    assert "backup-password" not in str(error.value)
    assert not (tmp_path / "backups").exists()


def test_backup_cli_parser_and_error_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    database_url = f"sqlite:///{tmp_path / 'missing.db'}"
    args = build_parser().parse_args(
        [
            "backup",
            "--no-env-file",
            "--database-url",
            database_url,
            "--data-dir",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "exports"),
        ]
    )
    assert args.command == "backup"
    assert args.no_env_file is True
    assert args.output_dir == str(tmp_path / "exports")

    assert (
        main(
            [
                "backup",
                "--no-env-file",
                "--database-url",
                database_url,
                "--data-dir",
                str(tmp_path),
                "--output-dir",
                str(tmp_path / "exports"),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "バックアップエラー" in captured.err
    assert "missing.db" not in captured.err


def test_backup_cli_prints_only_safe_path_and_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    database, _ = _database(tmp_path)
    event_id = _event(database)
    _attachment(database, tmp_path, event_id)
    secret_marker = "secret-marker-not-in-output"
    (tmp_path / ".env.local").write_text(f"OPENAI_API_KEY={secret_marker}\n", encoding="utf-8")

    assert (
        main(
            [
                "backup",
                "--no-env-file",
                "--database-url",
                f"sqlite:///{tmp_path / 'app.db'}",
                "--data-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Backup archive:" in captured.out
    assert "Summary: 1 attachment file(s); database checks passed" in captured.out
    assert secret_marker not in captured.out
    assert captured.err == ""


def test_backup_rejects_a_corrupted_live_archive_original(tmp_path: Path):
    database, _ = _database(tmp_path)
    archive_ingest = ArchiveIngestService(database.session_factory, data_dir=tmp_path)
    archive_ingest.ingest(
        {
            "source": "fixture",
            "source_service_id": "archive-backup-001",
            "revision": 1,
            "delivery_id": "archive-backup-delivery-001",
            "original_text": "archive backup",
            "attachments": [
                {
                    "source_attachment_id": "archive-attachment-001",
                    "original_filename": "archive.png",
                    "mime_type": "image/png",
                    "data_base64": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii"),
                }
            ],
        }
    )
    with database.session() as session:
        attachment = session.scalar(select(ArchiveAttachment))
        assert attachment is not None and attachment.private_storage_key is not None
        original = tmp_path / "archive" / "media" / attachment.private_storage_key
        original.write_bytes(b"corrupted archive original")

    with pytest.raises(BackupError, match="archive media"):
        BackupService(
            database_url=f"sqlite:///{tmp_path / 'app.db'}",
            data_dir=tmp_path,
        ).create_backup()

    assert list((tmp_path / "backups").glob("*.zip")) == []
    assert list((tmp_path / "backups").glob(".*")) == []
