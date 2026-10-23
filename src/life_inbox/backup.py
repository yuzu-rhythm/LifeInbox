"""Create safe, local backups of the SQLite database and attachments."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

from sqlalchemy.engine import make_url

from .archive_media import ArchiveMediaError, ArchiveMediaStore


class BackupError(RuntimeError):
    """Raised when a backup cannot be created safely."""


@dataclass(frozen=True)
class BackupResult:
    """The result of a successfully committed backup."""

    archive_path: Path
    manifest: dict[str, Any]

    @property
    def attachment_count(self) -> int:
        """Return the number of unique attachment files in the archive."""

        counts = self.manifest.get("counts", {})
        return int(counts.get("attachments_included", 0))

    @property
    def archive_media_count(self) -> int:
        """Return the number of verified archive media files in the backup."""

        counts = self.manifest.get("counts", {})
        return int(counts.get("archive_media_included", 0))


@dataclass(frozen=True)
class _SnapshotInspection:
    attachment_rows: tuple[sqlite3.Row, ...]
    archive_media_rows: tuple[sqlite3.Row, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _AttachmentPayload:
    data: bytes
    sha256: str


class BackupService:
    """Build an atomic ZIP backup from a local SQLite database.

    The service deliberately supports file-backed SQLite URLs only.  An
    in-memory SQLite database belongs to the connection that created it and
    cannot be safely discovered from a separate CLI process.
    """

    _SCHEMA_VERSION = 1
    _BACKUP_PAGES = 128
    _BACKUP_SLEEP_SECONDS = 0.05

    def __init__(
        self,
        database_url: str = "sqlite:///data/app.db",
        data_dir: str | Path = "data",
        output_dir: str | Path | None = None,
    ) -> None:
        self.database_url = database_url
        self.data_dir = Path(data_dir)
        self.output_dir = Path(output_dir) if output_dir is not None else self.data_dir / "backups"

    def create_backup(self) -> BackupResult:
        """Create and atomically commit one backup archive.

        All temporary files are created below the requested output directory.
        The final archive is only made visible after the database snapshot,
        integrity checks, attachment checks, and ZIP creation have succeeded.
        """

        created_at = datetime.now(UTC)
        source_path = self._sqlite_database_path()
        output_dir = self._prepare_output_dir()
        snapshot_path: Path | None = None
        archive_temp_path: Path | None = None

        try:
            snapshot_path = self._make_snapshot(output_dir, source_path)
            inspection = self._inspect_snapshot(snapshot_path)
            attachment_files, attachment_manifest, attachment_warnings = (
                self._collect_attachments(inspection.attachment_rows)
            )
            archive_media_files, archive_media_manifest = self._collect_archive_media(
                inspection.archive_media_rows
            )
            warnings = [*inspection.warnings, *attachment_warnings]
            manifest = self._build_manifest(
                created_at=created_at,
                attachment_rows=inspection.attachment_rows,
                attachment_manifest=attachment_manifest,
                unique_attachment_count=len(attachment_files),
                archive_media_manifest=archive_media_manifest,
                warnings=warnings,
            )
            archive_temp_path = self._write_archive(
                output_dir,
                snapshot_path,
                attachment_files,
                manifest,
                archive_media_files,
            )
            archive_path = self._commit_archive(
                output_dir,
                archive_temp_path,
                created_at,
            )
            archive_temp_path = None
            return BackupResult(archive_path=archive_path, manifest=manifest)
        except BackupError:
            raise
        except (OSError, sqlite3.Error, ValueError, zipfile.BadZipFile) as exc:
            raise BackupError("バックアップを作成できませんでした") from exc
        except Exception as exc:
            raise BackupError("バックアップを作成できませんでした") from exc
        finally:
            self._remove_temp_file(archive_temp_path)
            self._remove_temp_file(snapshot_path)

    def backup(self) -> BackupResult:
        """Compatibility alias for callers that prefer a shorter method name."""

        return self.create_backup()

    def _prepare_output_dir(self) -> Path:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            if not self.output_dir.is_dir():
                raise BackupError("バックアップ出力先がディレクトリではありません")
            return self.output_dir
        except BackupError:
            raise
        except OSError as exc:
            raise BackupError("バックアップ出力先を準備できませんでした") from exc

    def _sqlite_database_path(self) -> Path:
        try:
            url = make_url(self.database_url)
        except Exception as exc:
            raise BackupError("データベースURLを解釈できません") from exc

        if url.get_backend_name() != "sqlite":
            raise BackupError("バックアップはSQLite URLにのみ対応しています")
        if url.host or url.username or url.password or url.port:
            raise BackupError("ローカルSQLiteファイルURLのみ対応しています")
        if url.query:
            raise BackupError("SQLiteのURIオプション付きURLには対応していません")

        database = url.database
        if not database or database == ":memory:" or database.startswith("file:"):
            raise BackupError("ファイル型SQLiteデータベースを指定してください")

        try:
            path = Path(unquote(database))
            if not path.is_absolute():
                path = Path.cwd() / path
            return path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise BackupError("SQLiteデータベースファイルが見つかりません") from exc

    def _make_snapshot(self, output_dir: Path, source_path: Path) -> Path:
        if not source_path.is_file():
            raise BackupError("SQLiteデータベースファイルが見つかりません")

        file_descriptor, snapshot_name = tempfile.mkstemp(
            prefix=".life-inbox-snapshot-",
            suffix=".db",
            dir=output_dir,
        )
        os.close(file_descriptor)
        snapshot_path = Path(snapshot_name)

        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            source = sqlite3.connect(
                f"{source_path.as_uri()}?mode=ro",
                uri=True,
                timeout=30,
            )
            destination = sqlite3.connect(str(snapshot_path), timeout=30)
            source.backup(
                destination,
                pages=self._BACKUP_PAGES,
                sleep=self._BACKUP_SLEEP_SECONDS,
            )
            destination.commit()
            return snapshot_path
        except sqlite3.Error as exc:
            self._remove_temp_file(snapshot_path)
            raise BackupError("SQLiteの整合したスナップショットを作成できませんでした") from exc
        except Exception as exc:
            self._remove_temp_file(snapshot_path)
            raise BackupError("SQLiteの整合したスナップショットを作成できませんでした") from exc
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()

    def _inspect_snapshot(self, snapshot_path: Path) -> _SnapshotInspection:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(str(snapshot_path), timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")

            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
            integrity_ok = len(integrity_rows) == 1 and str(integrity_rows[0][0]).lower() == "ok"
            if not integrity_ok:
                raise BackupError("SQLiteスナップショットのintegrity_checkに失敗しました")
            if foreign_key_rows:
                raise BackupError("SQLiteスナップショットのforeign_key_checkに失敗しました")

            try:
                attachment_rows = tuple(
                    connection.execute(
                        """
                        SELECT id, event_id, original_filename, content_type, local_path, sha256
                        FROM attachments
                        ORDER BY id
                        """
                    ).fetchall()
                )
                warnings: tuple[str, ...] = ()
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise BackupError("SQLiteスナップショットの添付テーブルを読めません") from exc
                attachment_rows = ()
                warnings = ("attachmentsテーブルがないため、添付ファイルは含まれていません",)
            try:
                archive_media_rows = tuple(
                    connection.execute(
                        """
                        SELECT id, entry_id, sha256, byte_size,
                               private_storage_key, metadata_key,
                               web_derivative_key, thumbnail_key,
                               quarantine_reason, deleted_at
                        FROM archive_attachments
                        WHERE quarantine_reason IS NULL AND deleted_at IS NULL
                        ORDER BY id
                        """
                    ).fetchall()
                )
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise BackupError(
                        "SQLiteスナップショットのarchive添付テーブルを読めません"
                    ) from exc
                archive_media_rows = ()
            return _SnapshotInspection(
                attachment_rows=attachment_rows,
                archive_media_rows=archive_media_rows,
                warnings=warnings,
            )
        except BackupError:
            raise
        except sqlite3.Error as exc:
            raise BackupError("SQLiteスナップショットを検証できませんでした") from exc
        finally:
            if connection is not None:
                connection.close()

    def _collect_attachments(
        self,
        rows: tuple[sqlite3.Row, ...],
    ) -> tuple[dict[str, bytes], list[dict[str, Any]], list[str]]:
        root_candidate = self.data_dir.absolute() / "attachments"
        if self._check_relative_components(root_candidate, Path()) == "symlink":
            raise BackupError("DATA_DIR/attachmentsにシンボリックリンクがあります")
        # Resolve the configured data directory before doing containment
        # checks.  macOS commonly exposes /var as a symlink to /private/var;
        # that harmless ancestor must not make a valid DATA_DIR unsafe.  We
        # still inspect the attachments root and every component below it
        # without following links.
        root = root_candidate.resolve(strict=False)
        files: dict[str, bytes] = {}
        payload_cache: dict[Path, _AttachmentPayload] = {}
        manifest_entries: list[dict[str, Any]] = []
        warnings: list[str] = []
        duplicate_warning_added = False

        for row in rows:
            attachment_id = row["id"]
            path = self._resolve_attachment_path(row["local_path"], root, attachment_id)
            payload = payload_cache.get(path)
            if payload is None:
                data = self._read_attachment(path, attachment_id)
                payload = _AttachmentPayload(data=data, sha256=hashlib.sha256(data).hexdigest())
                payload_cache[path] = payload

            stored_sha256 = row["sha256"]
            if stored_sha256 is not None and str(stored_sha256).strip():
                if payload.sha256 != str(stored_sha256).strip().lower():
                    raise BackupError(f"添付ファイル {attachment_id} のSHA-256が一致しません")

            relative_path = path.relative_to(root)
            if any("\\" in part for part in relative_path.parts):
                raise BackupError(
                    f"添付ファイル {attachment_id} のアーカイブパスが不正です"
                )
            if any(part.casefold() in {".env", ".env.local"} for part in relative_path.parts):
                raise BackupError(
                    f"添付ファイル {attachment_id} は環境設定ファイルのため含められません"
                )
            archive_path = PurePosixPath("attachments", *relative_path.parts).as_posix()
            if archive_path in files:
                if not duplicate_warning_added:
                    warnings.append("同一の添付ファイル参照はアーカイブ内で1回だけ保存しました")
                    duplicate_warning_added = True
            else:
                files[archive_path] = payload.data

            manifest_entries.append(
                {
                    "id": attachment_id,
                    "event_id": row["event_id"],
                    "original_filename": row["original_filename"],
                    "content_type": row["content_type"],
                    "archive_path": archive_path,
                    "size_bytes": len(payload.data),
                    "sha256": payload.sha256,
                    "stored_sha256": (
                        str(stored_sha256).strip().lower()
                        if stored_sha256 is not None and str(stored_sha256).strip()
                        else None
                    ),
                }
            )

        return files, manifest_entries, warnings

    def _resolve_attachment_path(
        self,
        local_path: object,
        root: Path,
        attachment_id: object,
    ) -> Path:
        if not isinstance(local_path, str) or not local_path.strip():
            raise BackupError(f"添付ファイル {attachment_id} のパスが不正です")

        raw_path = Path(local_path)
        if ".." in raw_path.parts:
            raise BackupError(f"添付ファイル {attachment_id} のパスに親ディレクトリ指定があります")
        if "\\" in local_path:
            raise BackupError(f"添付ファイル {attachment_id} のパス区切りが不正です")

        data_dir = self.data_dir.resolve(strict=False)
        lexical_root = self.data_dir.absolute() / "attachments"
        candidates = [
            raw_path if raw_path.is_absolute() else Path.cwd() / raw_path,
        ]
        if not raw_path.is_absolute():
            candidates.append(data_dir / raw_path)

        contained_candidate_found = False
        missing_candidate_found = False
        for candidate in candidates:
            prechecked_components = False
            try:
                lexical_relative = candidate.absolute().relative_to(lexical_root)
            except ValueError:
                pass
            else:
                prechecked_components = True
                component_state = self._check_relative_components(
                    lexical_root,
                    lexical_relative,
                )
                if component_state == "symlink":
                    raise BackupError(
                        f"添付ファイル {attachment_id} のパスにシンボリックリンクがあります"
                    )
                if component_state == "missing":
                    contained_candidate_found = True
                    missing_candidate_found = True
                    continue
            try:
                resolved = candidate.resolve(strict=False)
            except (OSError, RuntimeError, ValueError) as exc:
                raise BackupError(f"添付ファイル {attachment_id} のパスを解決できません") from exc

            try:
                relative_path = resolved.relative_to(root)
            except ValueError:
                continue
            contained_candidate_found = True
            if not relative_path.parts:
                raise BackupError(f"添付ファイル {attachment_id} が添付ルート自体を指しています")

            if prechecked_components:
                component_state = "ok"
            else:
                component_root = root
                component_relative = relative_path
                component_state = self._check_relative_components(
                    component_root,
                    component_relative,
                )
            if component_state == "symlink":
                raise BackupError(
                    f"添付ファイル {attachment_id} のパスにシンボリックリンクがあります"
                )
            if component_state == "missing":
                missing_candidate_found = True
                continue

            try:
                mode = os.lstat(candidate).st_mode
            except FileNotFoundError:
                missing_candidate_found = True
                continue
            except OSError as exc:
                raise BackupError(f"添付ファイル {attachment_id} にアクセスできません") from exc
            if stat.S_ISLNK(mode):
                raise BackupError(
                    f"添付ファイル {attachment_id} のパスにシンボリックリンクがあります"
                )
            if not stat.S_ISREG(mode):
                raise BackupError(f"添付ファイル {attachment_id} が通常ファイルではありません")
            return resolved

        if contained_candidate_found and missing_candidate_found:
            raise BackupError(f"添付ファイル {attachment_id} が見つかりません")
        raise BackupError(f"添付ファイル {attachment_id} のパスがDATA_DIR/attachments外です")

    @staticmethod
    def _check_relative_components(root: Path, relative_path: Path) -> str:
        """Inspect ``root`` and descendants without rejecting symlinked ancestors.

        The caller has already established that the candidate resolves below
        the attachment root.  Starting at that root avoids treating macOS's
        system-level ``/var -> /private/var`` alias as an attachment symlink.
        """

        current = root
        for part in (None, *relative_path.parts):
            if part is not None:
                current /= part
            try:
                mode = os.lstat(current).st_mode
            except FileNotFoundError:
                return "missing"
            except OSError as exc:
                raise BackupError("添付ファイルのパスを検査できません") from exc
            if stat.S_ISLNK(mode):
                return "symlink"
        return "ok"

    @staticmethod
    def _read_attachment(path: Path, attachment_id: object) -> bytes:
        flags = os.O_RDONLY
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        file_descriptor: int | None = None
        try:
            file_descriptor = os.open(path, flags | no_follow)
            mode = os.fstat(file_descriptor).st_mode
            if not stat.S_ISREG(mode):
                raise BackupError(f"添付ファイル {attachment_id} が通常ファイルではありません")
            with os.fdopen(file_descriptor, "rb") as file_handle:
                file_descriptor = None
                return file_handle.read()
        except BackupError:
            raise
        except FileNotFoundError as exc:
            raise BackupError(f"添付ファイル {attachment_id} が見つかりません") from exc
        except OSError as exc:
            raise BackupError(f"添付ファイル {attachment_id} を読み取れません") from exc
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)

    def _collect_archive_media(
        self,
        rows: tuple[sqlite3.Row, ...],
    ) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
        """Collect only live archive originals and their referenced metadata.

        The archive media tree is intentionally separate from the legacy
        ``DATA_DIR/attachments`` tree.  A missing file or digest mismatch is a
        hard backup failure, because silently producing a partial archive would
        make the backup look healthier than the local source of truth.
        """

        if not rows:
            return {}, []

        store = ArchiveMediaStore(self.data_dir / "archive" / "media")
        files: dict[str, bytes] = {}
        manifest_entries: list[dict[str, Any]] = []
        for row in rows:
            attachment_id = row["id"]
            private_key = row["private_storage_key"]
            if not isinstance(private_key, str) or not private_key.startswith("originals/"):
                raise BackupError(f"archive media {attachment_id} のoriginal参照が不正です")
            try:
                original = store.verify_key(
                    private_key,
                    expected_sha256=str(row["sha256"] or ""),
                    expected_byte_size=int(row["byte_size"] or 0),
                )
                relative_key = PurePosixPath(private_key)
                archive_path = PurePosixPath(
                    "archive", "media", *relative_key.parts
                ).as_posix()
                if archive_path in files and files[archive_path] != original:
                    raise BackupError(f"archive media {attachment_id} の参照が衝突しています")
                files[archive_path] = original
            except (ArchiveMediaError, OSError, ValueError) as exc:
                raise BackupError(
                    f"archive media {attachment_id} のoriginalを検証できません"
                ) from exc
            manifest_entries.append(
                {
                    "id": attachment_id,
                    "entry_id": row["entry_id"],
                    "kind": "original",
                    "storage_key": private_key,
                    "archive_path": archive_path,
                    "size_bytes": len(original),
                    "sha256": hashlib.sha256(original).hexdigest(),
                }
            )

            for column, kind in (
                ("metadata_key", "metadata"),
                ("thumbnail_key", "thumbnail"),
                ("web_derivative_key", "web_derivative"),
            ):
                storage_key = row[column]
                if not storage_key:
                    continue
                if not isinstance(storage_key, str) or "/" not in storage_key:
                    raise BackupError(f"archive media {attachment_id} の参照が不正です")
                expected_root = {
                    "metadata": "metadata/",
                    "thumbnail": "thumbnails/",
                    "web_derivative": "web-derivatives/",
                }[kind]
                if not storage_key.startswith(expected_root):
                    raise BackupError(f"archive media {attachment_id} の参照先が不正です")
                try:
                    path = store.path_for_key(storage_key)
                    data = path.read_bytes()
                except (ArchiveMediaError, OSError) as exc:
                    raise BackupError(
                        f"archive media {attachment_id} の{kind}を読み取れません"
                    ) from exc
                archive_path = PurePosixPath(
                    "archive", "media", *PurePosixPath(storage_key).parts
                ).as_posix()
                if archive_path in files and files[archive_path] != data:
                    raise BackupError(f"archive media {attachment_id} の参照が衝突しています")
                files[archive_path] = data
                manifest_entries.append(
                    {
                        "id": attachment_id,
                        "entry_id": row["entry_id"],
                        "kind": kind,
                        "storage_key": storage_key,
                        "archive_path": archive_path,
                        "size_bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
        return files, manifest_entries

    def _build_manifest(
        self,
        *,
        created_at: datetime,
        attachment_rows: tuple[sqlite3.Row, ...],
        attachment_manifest: list[dict[str, Any]],
        unique_attachment_count: int,
        archive_media_manifest: list[dict[str, Any]] | None = None,
        warnings: list[str],
    ) -> dict[str, Any]:
        created_at_utc = created_at.astimezone(UTC).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
        manifest = {
            "schema_version": self._SCHEMA_VERSION,
            "created_at_utc": created_at_utc,
            "database": {
                "integrity_check": "ok",
                "foreign_key_check": "ok",
            },
            "counts": {
                "attachments_referenced": len(attachment_rows),
                "attachments_included": unique_attachment_count,
            },
            "attachments": attachment_manifest,
            "warnings": warnings,
        }
        if archive_media_manifest:
            manifest["counts"]["archive_media_included"] = len(archive_media_manifest)
            manifest["archive_media"] = archive_media_manifest
        return manifest

    @staticmethod
    def _write_archive(
        output_dir: Path,
        snapshot_path: Path,
        attachment_files: dict[str, bytes],
        manifest: dict[str, Any],
        archive_media_files: dict[str, bytes] | None = None,
    ) -> Path:
        file_descriptor, archive_name = tempfile.mkstemp(
            prefix=".life-inbox-backup-",
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
                archive.write(snapshot_path, "database/app.db")
                for member_name, data in attachment_files.items():
                    archive.writestr(member_name, data)
                for member_name, data in (archive_media_files or {}).items():
                    archive.writestr(member_name, data)
                manifest_data = json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ).encode("utf-8")
                archive.writestr("manifest.json", manifest_data)
            with archive_path.open("rb") as file_handle:
                os.fsync(file_handle.fileno())
            return archive_path
        except Exception:
            BackupService._remove_temp_file(archive_path)
            raise

    @staticmethod
    def _commit_archive(output_dir: Path, temp_path: Path, created_at: datetime) -> Path:
        timestamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        reserved_path: Path | None = None
        for disambiguation in range(10_000):
            suffix = "" if disambiguation == 0 else f"-{disambiguation:02d}"
            candidate = output_dir / f"backup-{timestamp}{suffix}.zip"
            try:
                # Linking first makes the candidate reservation non-overwriting
                # even if another backup process chooses the same timestamp.
                os.link(temp_path, candidate)
                reserved_path = candidate
                break
            except FileExistsError:
                continue
            except OSError as exc:
                raise BackupError("バックアップの保存先を確保できませんでした") from exc
        if reserved_path is None:
            raise BackupError("バックアップファイル名を確保できませんでした")

        try:
            # os.link publishes the already-complete archive atomically and
            # without overwriting another process's file.  POSIX rename/
            # os.replace is a no-op when source and destination are hard links
            # to the same inode, which would leave the hidden temp name behind;
            # explicitly remove that second name instead.
            temp_path.unlink()
            return reserved_path
        except OSError as exc:
            try:
                reserved_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise BackupError("バックアップを最終保存できませんでした") from exc

    @staticmethod
    def _remove_temp_file(path: Path | None) -> None:
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
