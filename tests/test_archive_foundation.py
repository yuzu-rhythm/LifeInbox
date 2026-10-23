from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from life_inbox.archive_export import ArchiveExportError, ArchiveExportService
from life_inbox.archive_ingest import (
    ArchiveIngestService,
    ArchiveRevisionConflictError,
)
from life_inbox.archive_models import ArchiveAttachment, ArchiveDelivery, ArchiveEntry
from life_inbox.backup import BackupService
from life_inbox.config import Settings
from life_inbox.db import Database
from life_inbox.models import Event
from life_inbox.web import create_web_app

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xda\x63d\xe8\x03\x00\x01\x05\x01\x01"
    b"\x27\x18\xe3\x66\x00\x00\x00\x00IEND\xaeB\x60\x82"
)
PNG_BASE64 = base64.b64encode(PNG_BYTES).decode("ascii")
SOURCE_ID = "fixture-entry-001"


def _payload(
    *,
    revision: int = 1,
    delivery_id: str | None = None,
    text: str = "最初の記録",
    tombstone: bool = False,
    attachments: list[dict[str, object]] | None = None,
    **changes: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "source": "fixture",
        "source_service_id": SOURCE_ID,
        "revision": revision,
        "delivery_id": delivery_id or f"fixture-delivery-{revision}",
        "provenance": "local_fixture",
        "confidence": 0.8,
        "visibility": "private",
        "scheduled_at": "2026-08-30T08:00:00+09:00",
        "occurred_at": "2026-08-30T12:00:00+09:00",
        "observed_at": "2026-08-30T12:01:00+09:00",
        "captured_at": "2026-08-30T12:02:00+09:00",
        "reviewed_at": None,
        "published_at": None,
        "original_text": text,
        "tombstone": tombstone,
        "attachments": attachments or [],
    }
    value.update(changes)
    return value


def _image_attachment(
    *,
    source_attachment_id: str = "attachment-001",
    mime_type: str = "image/png",
    data: bytes = PNG_BYTES,
    sha256: str | None = None,
) -> dict[str, object]:
    return {
        "source_attachment_id": source_attachment_id,
        "original_filename": "memory.png",
        "mime_type": mime_type,
        "data_base64": base64.b64encode(data).decode("ascii"),
        "sha256": sha256,
    }


def _service(tmp_path: Path) -> tuple[Database, ArchiveIngestService]:
    database = Database(f"sqlite:///{tmp_path / 'app.db'}")
    database.init()
    return database, ArchiveIngestService(database.session_factory, data_dir=tmp_path)


def test_fixture_ingest_uses_archive_tables_and_is_idempotent(tmp_path: Path):
    database, service = _service(tmp_path)
    fixture = Path(__file__).parent / "fixtures" / "life_archive" / "basic.json"

    summary = service.ingest_fixture_file(fixture)
    repeated = service.ingest_fixture_file(fixture)

    assert summary.imported_count == 1
    assert summary.results[0].status == "synced"
    assert repeated.results[0].idempotent is True
    with database.session() as session:
        entry = session.scalar(select(ArchiveEntry))
        assert entry is not None
        assert entry.archive_id
        assert entry.schema_version == 1
        assert entry.visibility == "private"
        assert entry.source_service_id == "fixture-event-001"
        assert entry.source_revision == 1
        assert entry.scheduled_at == datetime(2026, 8, 29, 23, 0)
        assert entry.occurred_at == datetime(2026, 8, 30, 3, 0)
        assert entry.observed_at == datetime(2026, 8, 30, 3, 1)
        assert entry.captured_at == datetime(2026, 8, 30, 3, 2)
        assert session.scalar(select(func.count(Event.id))) == 0
        attachment = session.scalar(select(ArchiveAttachment))
        assert attachment is not None
        assert attachment.private_storage_key is not None
        stored = tmp_path / "archive" / "media" / attachment.private_storage_key
        assert stored.read_bytes().startswith(b"\x89PNG")
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == attachment.sha256


def test_update_out_of_order_and_tombstone_do_not_restore_old_content(tmp_path: Path):
    database, service = _service(tmp_path)
    first = service.ingest(
        _payload(attachments=[_image_attachment()]),
    )
    updated = service.ingest(
        _payload(revision=2, text="新しい記録", attachments=[_image_attachment()]),
    )
    stale = service.ingest(
        _payload(revision=1, delivery_id="old-delivery", text="古い記録"),
    )
    deleted = service.ingest(
        _payload(revision=3, text="削除要求", tombstone=True),
    )
    stale_after_delete = service.ingest(
        _payload(revision=2, delivery_id="older-after-delete", text="復活しない"),
    )

    assert first.created is True
    assert updated.updated is True
    assert stale.out_of_order is True
    assert deleted.status == "deleted"
    assert stale_after_delete.out_of_order is True
    with database.session() as session:
        entry = session.scalar(select(ArchiveEntry))
        assert entry is not None
        assert entry.source_revision == 3
        assert entry.tombstone is True
        assert entry.original_text == ""
        assert session.scalar(select(func.count(ArchiveEntry.id))) == 1
        attachment = session.scalar(select(ArchiveAttachment))
        assert attachment is not None
        assert attachment.deleted_at is not None


def test_same_revision_different_content_is_a_durable_reconciliation_conflict(
    tmp_path: Path,
):
    database, service = _service(tmp_path)
    service.ingest(_payload(text="保持する内容"))

    with pytest.raises(ArchiveRevisionConflictError):
        service.ingest(
            _payload(
                delivery_id="conflicting-delivery",
                text="競合する内容",
            )
        )

    with database.session() as session:
        entry = session.scalar(select(ArchiveEntry))
        assert entry is not None
        assert entry.original_text == "保持する内容"
        assert entry.sync_status == "reconciliation_required"
        delivery = session.scalar(
            select(ArchiveDelivery).where(
                ArchiveDelivery.delivery_id == "conflicting-delivery"
            )
        )
        assert delivery is not None
        assert delivery.status == "reconciliation_required"


@pytest.mark.parametrize(
    ("attachment", "expected_reason", "expected_status"),
    [
        (_image_attachment(mime_type="image/jpeg"), "mime_magic_mismatch", "quarantined"),
        (
            _image_attachment(sha256="0" * 64),
            "checksum_mismatch",
            "quarantined",
        ),
        (
            {
                "source_attachment_id": "unsupported",
                "original_filename": "file.bin",
                "mime_type": "application/octet-stream",
                "data_base64": base64.b64encode(b"not-an-image").decode("ascii"),
            },
            "unsupported_format",
            "quarantined",
        ),
        (
            {
                "source_attachment_id": "download-failure",
                "original_filename": "remote.png",
                "mime_type": "image/png",
                "download_status": "failed",
                "download_error": "fixture download failure",
            },
            "download_failure",
            "failed",
        ),
    ],
)
def test_media_validation_persists_quarantine_and_download_failure(
    tmp_path: Path,
    attachment: dict[str, object],
    expected_reason: str,
    expected_status: str,
):
    database, service = _service(tmp_path)
    result = service.ingest(
        _payload(
            delivery_id=f"media-{expected_reason}",
            attachments=[attachment],
        )
    )

    assert result.status == expected_status
    with database.session() as session:
        row = session.scalar(select(ArchiveAttachment))
        assert row is not None
        assert row.quarantine_reason == expected_reason
        assert row.private_storage_key is None
        assert row.metadata_key is not None
        assert row.download_status == (
            "failed" if expected_reason == "download_failure" else "available"
        )


def test_failed_download_has_a_local_retry_path(tmp_path: Path):
    database, service = _service(tmp_path)
    result = service.ingest(
        _payload(
            attachments=[
                {
                    "source_attachment_id": "retryable",
                    "original_filename": "retry.png",
                    "mime_type": "image/png",
                    "download_status": "failed",
                }
            ]
        )
    )

    retried = service.retry(result.archive_id)

    assert retried.status == "failed"
    with database.session() as session:
        entry = session.scalar(select(ArchiveEntry))
        delivery = session.scalar(select(ArchiveDelivery))
        assert entry is not None and entry.sync_attempt_count >= 2
        assert delivery is not None and delivery.attempt_count >= 2


def test_complete_export_contains_required_members_and_verified_hashes(tmp_path: Path):
    database, service = _service(tmp_path)
    service.ingest(_payload(attachments=[_image_attachment()]))
    output_dir = tmp_path / "exports"

    result = ArchiveExportService(
        database.session_factory,
        data_dir=tmp_path,
        output_dir=output_dir,
    ).create_export()

    assert result.archive_path.name.startswith("life-archive-")
    assert result.entry_count == 1
    with zipfile.ZipFile(result.archive_path) as archive:
        names = set(archive.namelist())
        required = {
            "manifest.json",
            "checksums.json",
            "README.md",
            "entries.jsonl",
            "relations.jsonl",
            "sync-state.jsonl",
            "discord/messages.jsonl",
            "notion/pages/.keep",
            "calendar/events.ics",
            "reminders/reminders.json",
            "media/originals/.keep",
            "media/thumbnails/.keep",
            "schemas/schema-v1.json",
        }
        assert required <= names
        manifest = json.loads(archive.read("manifest.json"))
        checksums = json.loads(archive.read("checksums.json"))
        assert manifest["counts"]["entries"] == 1
        assert manifest["external_backup_success"] is False
        assert any(name.startswith("media/originals/") for name in names)
        for name, digest in checksums["files"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
    ArchiveExportService(
        database.session_factory,
        data_dir=tmp_path,
        output_dir=output_dir,
    ).verify_export(result.archive_path)


def test_export_rejects_a_corrupted_archive_original(tmp_path: Path):
    database, service = _service(tmp_path)
    service.ingest(_payload(attachments=[_image_attachment()]))
    with database.session() as session:
        attachment = session.scalar(select(ArchiveAttachment))
        assert attachment is not None and attachment.private_storage_key is not None
        original = tmp_path / "archive" / "media" / attachment.private_storage_key
        original.write_bytes(b"corrupted")

    with pytest.raises(ArchiveExportError):
        ArchiveExportService(database.session_factory, data_dir=tmp_path).create_export()
    assert not list((tmp_path / "exports").glob("life-archive-*.zip"))


def test_web_archive_is_finite_private_and_has_no_original_route(tmp_path: Path):
    database, service = _service(tmp_path)
    service.ingest(_payload(text="Webに表示する記録"))
    service.ingest(
        _payload(
            revision=2,
            delivery_id="deleted",
            source_service_id="web-deleted",
            text="消える記録",
            tombstone=True,
        )
    )
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
        web_host="127.0.0.1",
    )
    app = create_web_app(settings, database=database)

    with TestClient(app) as client:
        timeline = client.get("/archive?limit=1")
        health = client.get("/archive/health")
        original_route = client.get("/archive/media/originals/anything")

    assert timeline.status_code == 200
    assert "Webに表示する記録" in timeline.text
    assert "消える記録" not in timeline.text
    assert "無限スクロールなし" in timeline.text
    assert timeline.headers["cache-control"] == "no-store"
    assert timeline.headers["x-robots-tag"] == "noindex, nofollow"
    assert health.status_code == 200
    assert "synced" in health.text
    assert "外部backup" in health.text
    assert original_route.status_code == 404


def test_existing_safe_backup_includes_verified_archive_media_additively(tmp_path: Path):
    database, service = _service(tmp_path)
    service.ingest(_payload(attachments=[_image_attachment()]))

    result = BackupService(
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
    ).create_backup()

    with zipfile.ZipFile(result.archive_path) as archive:
        names = set(archive.namelist())
        assert any(name.startswith("archive/media/originals/") for name in names)
        assert any(name.startswith("archive/media/metadata/") for name in names)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["counts"]["archive_media_included"] >= 2
        for item in manifest["archive_media"]:
            assert hashlib.sha256(archive.read(item["archive_path"])).hexdigest() == item["sha256"]
