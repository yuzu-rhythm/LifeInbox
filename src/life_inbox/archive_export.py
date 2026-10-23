"""Complete, local Life Archive export with post-generation verification."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from .archive_health import _title_for_entry
from .archive_media import ArchiveMediaError, ArchiveMediaStore
from .archive_models import ArchiveDelivery, ArchiveEntry, ArchiveRelation
from .archive_schema import (
    ARCHIVE_SCHEMA_VERSION,
    archive_schema_document,
    isoformat_or_none,
)


class ArchiveExportError(RuntimeError):
    """Raised when a complete archive export cannot be verified safely."""


@dataclass(frozen=True, slots=True)
class ArchiveExportResult:
    """A verified, atomically committed Life Archive ZIP."""

    archive_path: Path
    manifest: dict[str, Any]

    @property
    def entry_count(self) -> int:
        return int(self.manifest.get("counts", {}).get("entries", 0))

    @property
    def original_count(self) -> int:
        return int(self.manifest.get("counts", {}).get("media_originals", 0))


_REQUIRED_FILE_MEMBERS = (
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
)
_MEDIA_DIRECTORY_MARKERS = (
    "media/originals/.keep",
    "media/thumbnails/.keep",
    "media/metadata/.keep",
    "media/web-derivatives/.keep",
)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b""
    return b"".join(
        (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        for row in rows
    )


def _iso(value: datetime | None) -> str | None:
    return isoformat_or_none(value)


def _safe_key(value: str, *, prefix: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ArchiveExportError("archive media key has an unexpected prefix")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise ArchiveExportError("archive media key is unsafe")
    return value


def _escape_ics(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_timestamp(value: datetime | None) -> str:
    if value is None:
        value = datetime.now(UTC).replace(tzinfo=None)
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


class ArchiveExportService:
    """Export all local archive state and reject partial output."""

    _MAX_FILENAME_DISAMBIGUATION = 10_000

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        data_dir: str | Path = "data",
        output_dir: str | Path | None = None,
        media_store: ArchiveMediaStore | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.data_dir = Path(data_dir)
        self.output_dir = Path(output_dir) if output_dir is not None else self.data_dir / "exports"
        self.media_store = media_store or ArchiveMediaStore(self.data_dir / "archive" / "media")

    def create_export(self) -> ArchiveExportResult:
        """Build, validate, ZIP, revalidate, and atomically publish the export."""

        generated_at = datetime.now(UTC)
        output_dir = self._prepare_output_dir()
        temporary: Path | None = None
        try:
            files, manifest = self._collect_files(generated_at)
            files["manifest.json"] = _json_bytes(manifest)
            checksums = {
                name: hashlib.sha256(data).hexdigest()
                for name, data in sorted(files.items())
                if name != "checksums.json"
            }
            files["checksums.json"] = _json_bytes(
                {
                    "algorithm": "sha256",
                    "files": checksums,
                    "verified": True,
                }
            )
            self._verify_files(files, manifest)
            temporary = self._write_zip(output_dir, files)
            self._verify_zip(temporary, manifest)
            archive_path = self._commit_archive(output_dir, temporary, generated_at)
            temporary = None
            return ArchiveExportResult(archive_path=archive_path, manifest=manifest)
        except ArchiveExportError:
            raise
        except (OSError, SQLAlchemyError, ValueError, zipfile.BadZipFile) as exc:
            raise ArchiveExportError("Life Archiveエクスポートを作成できませんでした") from exc
        except Exception as exc:
            raise ArchiveExportError("Life Archiveエクスポートを作成できませんでした") from exc
        finally:
            self._remove_temp_file(temporary)

    export = create_export

    def verify_export(self, archive_path: str | Path) -> dict[str, Any]:
        """Revalidate an already-created ZIP and return its manifest."""

        path = Path(archive_path)
        try:
            with zipfile.ZipFile(path, "r") as archive:
                manifest = json.loads(archive.read("manifest.json"))
                self._verify_zip(path, manifest)
                return manifest
        except ArchiveExportError:
            raise
        except (OSError, KeyError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            raise ArchiveExportError("Life Archiveエクスポートを検証できませんでした") from exc

    validate_export = verify_export

    def _collect_files(self, generated_at: datetime) -> tuple[dict[str, bytes], dict[str, Any]]:
        try:
            with self.session_factory() as session:
                entries = list(
                    session.scalars(
                        select(ArchiveEntry)
                        .options(selectinload(ArchiveEntry.attachments))
                        .order_by(ArchiveEntry.id)
                    ).all()
                )
                relations = list(
                    session.scalars(select(ArchiveRelation).order_by(ArchiveRelation.id)).all()
                )
                deliveries = list(
                    session.scalars(select(ArchiveDelivery).order_by(ArchiveDelivery.id)).all()
                )
                # All values used below are copied while the session is open;
                # export verification never relies on lazy ORM objects.
                entry_rows = [self._entry_row(entry) for entry in entries]
                relation_rows = [self._relation_row(relation, entries) for relation in relations]
                entry_archive_ids = {entry.id: entry.archive_id for entry in entries}
                delivery_rows = [
                    self._delivery_row(delivery, entry_archive_ids) for delivery in deliveries
                ]
                attachment_rows = [
                    (entry_rows[index], list(entry.attachments))
                    for index, entry in enumerate(entries)
                ]
        except SQLAlchemyError as exc:
            raise ArchiveExportError("archive tables could not be read") from exc

        files: dict[str, bytes] = {
            "README.md": self._readme_bytes(),
            "entries.jsonl": _jsonl_bytes(entry_rows),
            "relations.jsonl": _jsonl_bytes(relation_rows),
            "sync-state.jsonl": _jsonl_bytes(delivery_rows),
            "discord/messages.jsonl": _jsonl_bytes(
                [row for row in entry_rows if row["source"] == "discord"]
            ),
            "calendar/events.ics": self._calendar_bytes(entry_rows, generated_at),
            "reminders/reminders.json": _json_bytes(
                [row for row in entry_rows if row["source"] == "reminders"]
            ),
            "schemas/schema-v1.json": _json_bytes(archive_schema_document()),
        }
        for marker in _MEDIA_DIRECTORY_MARKERS:
            files[marker] = b""
        files["notion/pages/.keep"] = b""

        notion_rows = [row for row in entry_rows if row["source"] == "notion"]
        for row in notion_rows:
            files[f"notion/pages/{row['archive_id']}.json"] = _json_bytes(row)

        media_originals = 0
        media_thumbnails = 0
        media_metadata = 0
        media_web_derivatives = 0
        excluded_quarantine = 0
        copied_keys: set[str] = set()
        for entry_row, attachments in attachment_rows:
            for attachment in attachments:
                if attachment.quarantine_reason:
                    excluded_quarantine += 1
                    continue
                if attachment.deleted_at is not None or entry_row["tombstone"]:
                    continue
                private_key = attachment.private_storage_key
                if not private_key:
                    raise ArchiveExportError(
                        f"valid archive attachment {attachment.id} has no private original"
                    )
                private_key = _safe_key(private_key, prefix="originals/")
                try:
                    data = self.media_store.verify_key(
                        private_key,
                        expected_sha256=attachment.sha256,
                        expected_byte_size=attachment.byte_size,
                    )
                except ArchiveMediaError as exc:
                    raise ArchiveExportError(
                        f"archive attachment {attachment.id} original could not be verified"
                    ) from exc
                export_key = f"media/{private_key}"
                if export_key in copied_keys and files[export_key] != data:
                    raise ArchiveExportError(
                        f"archive attachment {attachment.id} original reference collides"
                    )
                if export_key not in copied_keys:
                    files[export_key] = data
                    copied_keys.add(export_key)
                    media_originals += 1

                for source_key, export_prefix, counter_name in (
                    (attachment.metadata_key, "media/metadata/", "metadata"),
                    (attachment.thumbnail_key, "media/thumbnails/", "thumbnail"),
                    (attachment.web_derivative_key, "media/web-derivatives/", "derivative"),
                ):
                    if not source_key:
                        continue
                    source_key = _safe_key(source_key, prefix=source_key.split("/", 1)[0] + "/")
                    if counter_name == "metadata":
                        expected_prefix = "metadata/"
                    elif counter_name == "thumbnail":
                        expected_prefix = "thumbnails/"
                    else:
                        expected_prefix = "web-derivatives/"
                    source_key = _safe_key(source_key, prefix=expected_prefix)
                    try:
                        derivative = self.media_store.path_for_key(source_key).read_bytes()
                    except (ArchiveMediaError, OSError) as exc:
                        raise ArchiveExportError(
                            f"archive attachment {attachment.id} derivative is not readable"
                        ) from exc
                    destination = f"media/{source_key}"
                    if destination in copied_keys and files[destination] != derivative:
                        raise ArchiveExportError(
                            f"archive attachment {attachment.id} derivative reference collides"
                        )
                    if destination not in copied_keys:
                        files[destination] = derivative
                        copied_keys.add(destination)
                        if counter_name == "metadata":
                            media_metadata += 1
                        elif counter_name == "thumbnail":
                            media_thumbnails += 1
                        else:
                            media_web_derivatives += 1

        observed_values = [
            datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))
            for row in entry_rows
            if row["observed_at"]
        ]
        occurred_values = [
            datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00"))
            for row in entry_rows
            if row["occurred_at"]
        ]
        source_counts = Counter(row["source"] for row in entry_rows)
        status_counts = Counter(row["sync_status"] for row in entry_rows)
        estimated_count = sum(1 for row in entry_rows if row["occurred_at_estimated"])
        observed_count = len(entry_rows) - estimated_count
        manifest: dict[str, Any] = {
            "format": "life-archive",
            "format_version": 1,
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "generated_at": _iso(generated_at),
            "database": "local SQLite archive tables",
            "counts": {
                "entries": len(entry_rows),
                "relations": len(relation_rows),
                "sync_state": len(delivery_rows),
                "discord_messages": sum(1 for row in entry_rows if row["source"] == "discord"),
                "notion_pages": len(notion_rows),
                "media_originals": media_originals,
                "media_thumbnails": media_thumbnails,
                "media_metadata": media_metadata,
                "media_web_derivatives": media_web_derivatives,
                "quarantined_media_excluded": excluded_quarantine,
            },
            "source_counts": dict(sorted(source_counts.items())),
            "sync_status_counts": dict(sorted(status_counts.items())),
            "period": {
                "observed_from": _iso(min(observed_values).replace(tzinfo=None))
                if observed_values
                else None,
                "observed_to": _iso(max(observed_values).replace(tzinfo=None))
                if observed_values
                else None,
                "occurred_from": _iso(min(occurred_values).replace(tzinfo=None))
                if occurred_values
                else None,
                "occurred_to": _iso(max(occurred_values).replace(tzinfo=None))
                if occurred_values
                else None,
            },
            "time_semantics": {
                "estimated_occurred_at_entries": estimated_count,
                "observed_or_non_estimated_entries": observed_count,
                "fields_are_kept_separate": True,
            },
            "deletion_policy": {
                "tombstones_are_exported_as_state": True,
                "deleted_entries_are_not_timeline_media": True,
                "quarantined_media_is_not_exported_as_original": True,
            },
            "required_members": list(_REQUIRED_FILE_MEMBERS),
            "external_backup_success": False,
            "external_backup_note": "生成成功は外部バックアップ成功を意味しません。",
            "verification": {
                "checksums_algorithm": "sha256",
                "checksums_verified": True,
                "manifest_counts_verified": True,
                "required_members_verified": True,
            },
        }
        return files, manifest

    @staticmethod
    def _entry_row(entry: ArchiveEntry) -> dict[str, Any]:
        return {
            "archive_id": entry.archive_id,
            "schema_version": entry.schema_version,
            "source": entry.source,
            "source_service_id": entry.source_service_id,
            "revision": entry.source_revision,
            "delivery_id": entry.delivery_id,
            "original_text": entry.original_text,
            "title": _title_for_entry(entry),
            "provenance": entry.provenance,
            "confidence": entry.confidence,
            "visibility": entry.visibility,
            "scheduled_at": _iso(entry.scheduled_at),
            "occurred_at": _iso(entry.occurred_at),
            "observed_at": _iso(entry.observed_at),
            "captured_at": _iso(entry.captured_at),
            "reviewed_at": _iso(entry.reviewed_at),
            "published_at": _iso(entry.published_at),
            "occurred_at_estimated": bool(entry.occurred_at_estimated),
            "tombstone": bool(entry.tombstone),
            "sync_status": entry.sync_status,
            "sync_attempt_count": entry.sync_attempt_count,
            "last_sync_error": entry.last_sync_error,
            "last_synced_at": _iso(entry.last_synced_at),
            "created_at": _iso(entry.created_at),
            "updated_at": _iso(entry.updated_at),
        }

    @staticmethod
    def _relation_row(relation: ArchiveRelation, entries: list[ArchiveEntry]) -> dict[str, Any]:
        entry_map = {entry.id: entry.archive_id for entry in entries}
        return {
            "entry_archive_id": entry_map.get(relation.entry_id),
            "relation_type": relation.relation_type,
            "target_archive_id": relation.target_archive_id,
            "note": relation.note,
        }

    @staticmethod
    def _delivery_row(
        delivery: ArchiveDelivery,
        entry_archive_ids: dict[int, str],
    ) -> dict[str, Any]:
        return {
            "source": delivery.source,
            "delivery_id": delivery.delivery_id,
            "entry_id": delivery.entry_id,
            "entry_archive_id": entry_archive_ids.get(delivery.entry_id),
            "source_service_id": delivery.source_service_id,
            "revision": delivery.source_revision,
            "payload_sha256": delivery.payload_sha256,
            "status": delivery.status,
            "attempt_count": delivery.attempt_count,
            "error_code": delivery.error_code,
            "error_detail": delivery.error_detail,
            "received_at": _iso(delivery.received_at),
            "processed_at": _iso(delivery.processed_at),
            "updated_at": _iso(delivery.updated_at),
        }

    @staticmethod
    def _calendar_bytes(rows: list[dict[str, Any]], generated_at: datetime) -> bytes:
        events = [row for row in rows if row["source"] == "calendar" and not row["tombstone"]]
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Life Archive P0//LOCAL//"]
        for row in events:
            created_at = (
                datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                if row["created_at"]
                else generated_at
            )
            start_at = next(
                (
                    datetime.fromisoformat(row[field].replace("Z", "+00:00"))
                    for field in ("occurred_at", "scheduled_at", "captured_at")
                    if row[field]
                ),
                generated_at,
            )
            lines.extend(
                [
                    "BEGIN:VEVENT",
                    f"UID:{_escape_ics(row['archive_id'])}",
                    f"DTSTAMP:{_ics_timestamp(created_at)}",
                    f"DTSTART:{_ics_timestamp(start_at)}",
                    f"SUMMARY:{_escape_ics(row['title'])}",
                    "END:VEVENT",
                ]
            )
        lines.append("END:VCALENDAR")
        return ("\r\n".join(lines) + "\r\n").encode("utf-8")

    @staticmethod
    def _readme_bytes() -> bytes:
        return (
            "# Life Archive export\n\n"
            "このZIPはローカルSQLiteから生成したLife Archive P0の完全エクスポートです。\n"
            "Archive ID、元サービスID、revision、schema version、時刻の意味、"
            "削除状態を保持します。\n\n"
            "- `entries.jsonl` がエントリ正本の機械可読表です。\n"
            "- `discord/messages.jsonl`、`notion/pages/`、`calendar/events.ics`、"
            "`reminders/reminders.json` はサービス別の読みやすい補助表です。\n"
            "- `media/originals/` はDBから有効に参照され、SHA-256とサイズが一致した原本だけです。"
            "quarantinedは原本として混ぜていません。\n"
            "- `checksums.json` とmanifest件数は生成後に検証済みです。\n"
            "- この生成成功は外部バックアップ成功を意味しません。"
            "外部通信や外部サービス書き込みは行っていません。\n"
        ).encode()

    def _verify_files(self, files: dict[str, bytes], manifest: dict[str, Any]) -> None:
        if not isinstance(manifest, dict):
            raise ArchiveExportError("manifest.json is not an object")
        for required in _REQUIRED_FILE_MEMBERS:
            if required not in files:
                raise ArchiveExportError(f"required export member is missing: {required}")
        try:
            checksums = json.loads(files["checksums.json"])
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise ArchiveExportError("checksums.json is invalid") from exc
        if not isinstance(checksums, dict):
            raise ArchiveExportError("checksums.json is not an object")
        if checksums.get("algorithm") != "sha256":
            raise ArchiveExportError("checksums.json uses an unsupported algorithm")
        expected = checksums.get("files")
        if not isinstance(expected, dict):
            raise ArchiveExportError("checksums.json has no file map")
        actual_names = set(files) - {"checksums.json"}
        if set(expected) != actual_names:
            raise ArchiveExportError("checksums.json does not cover every export member")
        for name, digest in expected.items():
            if (
                not isinstance(name, str)
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ArchiveExportError("checksums.json references an invalid member")
            if hashlib.sha256(files[name]).hexdigest() != digest:
                raise ArchiveExportError("export checksum verification failed")
        jsonl_paths = (
            "entries.jsonl",
            "relations.jsonl",
            "sync-state.jsonl",
            "discord/messages.jsonl",
        )
        for path in jsonl_paths:
            self._jsonl_rows(files[path], path)
        counts = manifest.get("counts", {})
        if not isinstance(counts, dict):
            raise ArchiveExportError("manifest counts are invalid")
        if counts.get("entries") != self._line_count(files["entries.jsonl"]):
            raise ArchiveExportError("export entry count does not match manifest")
        if counts.get("relations") != self._line_count(files["relations.jsonl"]):
            raise ArchiveExportError("export relation count does not match manifest")
        if counts.get("sync_state") != self._line_count(files["sync-state.jsonl"]):
            raise ArchiveExportError("export sync-state count does not match manifest")
        if counts.get("discord_messages") != self._line_count(files["discord/messages.jsonl"]):
            raise ArchiveExportError("export Discord count does not match manifest")
        if counts.get("notion_pages") != sum(
            1 for name in files if name.startswith("notion/pages/") and name.endswith(".json")
        ):
            raise ArchiveExportError("export Notion count does not match manifest")
        if counts.get("media_originals") != sum(
            1
            for name in files
            if name.startswith("media/originals/") and not name.endswith(".keep")
        ):
            raise ArchiveExportError("export original count does not match manifest")
        for key, prefix in (
            ("media_thumbnails", "media/thumbnails/"),
            ("media_metadata", "media/metadata/"),
            ("media_web_derivatives", "media/web-derivatives/"),
        ):
            if counts.get(key) != sum(
                1
                for name in files
                if name.startswith(prefix) and not name.endswith(".keep")
            ):
                raise ArchiveExportError(f"export {key} does not match manifest")
        if manifest.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
            raise ArchiveExportError("export schema version is unsupported")

    @staticmethod
    def _jsonl_rows(data: bytes, path: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (ValueError, json.JSONDecodeError) as exc:
                raise ArchiveExportError(f"{path} contains invalid JSONL") from exc
            if not isinstance(row, dict):
                raise ArchiveExportError(f"{path} contains a non-object row")
            rows.append(row)
        return rows

    @staticmethod
    def _line_count(data: bytes) -> int:
        return sum(1 for line in data.splitlines() if line.strip())

    def _write_zip(self, output_dir: Path, files: dict[str, bytes]) -> Path:
        fd, name = tempfile.mkstemp(prefix=".life-archive-", suffix=".zip.tmp", dir=output_dir)
        os.close(fd)
        path = Path(name)
        try:
            with zipfile.ZipFile(
                path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                for member_name in sorted(files):
                    archive.writestr(member_name, files[member_name])
            with path.open("rb") as file_handle:
                os.fsync(file_handle.fileno())
            return path
        except Exception:
            self._remove_temp_file(path)
            raise

    def _verify_zip(self, path: Path, manifest: dict[str, Any]) -> None:
        try:
            with zipfile.ZipFile(path, "r") as archive:
                names = archive.namelist()
                if len(names) != len(set(names)):
                    raise ArchiveExportError("export ZIP contains duplicate members")
                for required in _REQUIRED_FILE_MEMBERS:
                    if required not in names:
                        raise ArchiveExportError(f"export ZIP is missing {required}")
                files = {
                    name: archive.read(name)
                    for name in names
                    if name != "checksums.json"
                }
                files["checksums.json"] = archive.read("checksums.json")
                self._verify_files(files, manifest)
        except ArchiveExportError:
            raise
        except (OSError, KeyError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            raise ArchiveExportError("export ZIP could not be verified") from exc

    def _prepare_output_dir(self) -> Path:
        try:
            if self.output_dir.is_symlink():
                raise ArchiveExportError("export output directory is a symlink")
            self.output_dir.mkdir(parents=True, exist_ok=True)
            if self.output_dir.is_symlink() or not self.output_dir.is_dir():
                raise ArchiveExportError("export output directory is invalid")
            return self.output_dir
        except ArchiveExportError:
            raise
        except OSError as exc:
            raise ArchiveExportError("export output directory could not be prepared") from exc

    def _commit_archive(self, output_dir: Path, temporary: Path, generated_at: datetime) -> Path:
        date_text = generated_at.astimezone(UTC).strftime("%Y-%m-%d")
        for disambiguation in range(self._MAX_FILENAME_DISAMBIGUATION):
            suffix = "" if disambiguation == 0 else f"-{disambiguation:02d}"
            candidate = output_dir / f"life-archive-{date_text}{suffix}.zip"
            try:
                os.link(temporary, candidate)
            except FileExistsError:
                continue
            except OSError as exc:
                raise ArchiveExportError("export archive could not be reserved") from exc
            try:
                temporary.unlink()
            except OSError as exc:
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
                raise ArchiveExportError("export archive could not be finalized") from exc
            return candidate
        raise ArchiveExportError("export archive filename could not be reserved")

    @staticmethod
    def _remove_temp_file(path: Path | None) -> None:
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = ["ArchiveExportError", "ArchiveExportResult", "ArchiveExportService"]
