"""Private, checksum-verified media storage for Life Archive.

P0 accepts inline bytes only.  Originals are copied byte-for-byte into a
separate archive tree and are never exposed by the Web app.  A failed
validation is represented by a durable result so the database can retain the
source event and the reason for quarantine.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .archive_schema import ArchiveAttachmentPayload

SUPPORTED_ARCHIVE_MIME_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ArchiveMediaError(ValueError):
    """Raised for an invalid media storage root or unsafe storage key."""


@dataclass(frozen=True, slots=True)
class MediaInspection:
    """Deterministic validation result for one inline media payload."""

    data: bytes | None
    byte_size: int
    sha256: str
    detected_mime_type: str | None
    quarantine_reason: str | None
    download_status: str
    download_error: str | None

    @property
    def valid(self) -> bool:
        return self.data is not None and self.quarantine_reason is None


@dataclass(frozen=True, slots=True)
class MediaStoreResult:
    """Storage keys produced after a valid or quarantined import."""

    inspection: MediaInspection
    private_storage_key: str | None = None
    metadata_key: str | None = None
    quarantine_storage_key: str | None = None


def sha256_bytes(data: bytes) -> str:
    """Return a lowercase SHA-256 digest."""

    return hashlib.sha256(data).hexdigest()


def detect_mime_type(data: bytes) -> str | None:
    """Detect the small, dependency-free image format set supported by P0."""

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _normalise_mime(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().casefold()


def _safe_filename(value: str) -> str:
    candidate = Path(value).name.strip()
    if not candidate or candidate in {".", ".."}:
        return "attachment"
    # The filename is metadata, not a path. Keep it predictable and prevent
    # control characters from becoming archive members or log confusion.
    return "".join(character for character in candidate if ord(character) >= 32)[:255]


def _suffix_for_mime(mime_type: str, filename: str) -> str:
    suffix = Path(filename).suffix.casefold()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return suffix
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(mime_type, ".bin")


def _decode_base64(value: str) -> bytes:
    try:
        # validate=True rejects whitespace and non-base64 characters instead
        # of silently accepting a damaged fixture.
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ArchiveMediaError("invalid inline base64 payload") from exc


class ArchiveMediaStore:
    """Store originals below ``data/archive/media`` with no URL fetching."""

    def __init__(
        self,
        root: str | Path = "data/archive/media",
        *,
        max_bytes: int = 10 * 1024 * 1024,
    ):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.root = Path(root)
        self.max_bytes = max_bytes

    @classmethod
    def from_data_dir(cls, data_dir: str | Path, *, max_bytes: int) -> ArchiveMediaStore:
        return cls(Path(data_dir) / "archive" / "media", max_bytes=max_bytes)

    @property
    def originals_dir(self) -> Path:
        return self.root / "originals"

    @property
    def web_derivatives_dir(self) -> Path:
        return self.root / "web-derivatives"

    @property
    def thumbnails_dir(self) -> Path:
        return self.root / "thumbnails"

    @property
    def quarantined_dir(self) -> Path:
        return self.root / "quarantined"

    @property
    def metadata_dir(self) -> Path:
        return self.root / "metadata"

    def ensure_directories(self) -> None:
        for directory in (
            self.originals_dir,
            self.web_derivatives_dir,
            self.thumbnails_dir,
            self.quarantined_dir,
            self.metadata_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def inspect(
        self,
        payload: ArchiveAttachmentPayload,
    ) -> MediaInspection:
        """Validate an attachment without writing bytes."""

        if payload.download_status == "failed":
            return MediaInspection(
                data=None,
                byte_size=0,
                sha256="",
                detected_mime_type=None,
                quarantine_reason="download_failure",
                download_status="failed",
                download_error=payload.download_error or "download failed",
            )

        try:
            data = _decode_base64(payload.data_base64 or "")
        except ArchiveMediaError:
            return MediaInspection(
                data=None,
                byte_size=0,
                sha256="",
                detected_mime_type=None,
                quarantine_reason="invalid_payload",
                download_status="available",
                download_error="invalid inline base64 payload",
            )

        digest = sha256_bytes(data)
        detected = detect_mime_type(data)
        declared = _normalise_mime(payload.mime_type)
        reason: str | None = None
        if len(data) == 0:
            reason = "empty_payload"
        elif len(data) > self.max_bytes:
            reason = "size_exceeded"
        elif declared not in SUPPORTED_ARCHIVE_MIME_TYPES or detected is None:
            reason = "unsupported_format"
        elif declared != detected:
            reason = "mime_magic_mismatch"
        elif payload.sha256 is not None and payload.sha256.casefold() != digest:
            reason = "checksum_mismatch"

        return MediaInspection(
            data=data,
            byte_size=len(data),
            sha256=digest,
            detected_mime_type=detected,
            quarantine_reason=reason,
            download_status="available",
            download_error=None,
        )

    def store(
        self,
        *,
        archive_id: str,
        attachment_id: int,
        payload: ArchiveAttachmentPayload,
    ) -> MediaStoreResult:
        """Validate and persist one attachment, preserving invalid bytes safely."""

        self.ensure_directories()
        inspection = self.inspect(payload)
        metadata = {
            "schema_version": 1,
            "archive_id": archive_id,
            "attachment_id": attachment_id,
            "original_filename": _safe_filename(payload.original_filename),
            "declared_mime_type": _normalise_mime(payload.mime_type),
            "detected_mime_type": inspection.detected_mime_type,
            "byte_size": inspection.byte_size,
            "sha256": inspection.sha256,
            "quarantine_reason": inspection.quarantine_reason,
            "metadata_removed": True,
        }
        metadata_key = f"metadata/{archive_id}/{attachment_id}.json"
        self._write_text(
            metadata_key,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2),
        )

        if inspection.valid:
            suffix = _suffix_for_mime(
                _normalise_mime(payload.mime_type), payload.original_filename
            )
            private_key = f"originals/{archive_id}/{attachment_id}{suffix}"
            self._write_bytes(private_key, inspection.data or b"")
            return MediaStoreResult(
                inspection=inspection,
                private_storage_key=private_key,
                metadata_key=metadata_key,
            )

        quarantine_key: str | None = None
        if inspection.data is not None:
            reason = inspection.quarantine_reason or "invalid_payload"
            quarantine_key = f"quarantined/{archive_id}/{attachment_id}-{reason}.bin"
            self._write_bytes(quarantine_key, inspection.data)
        return MediaStoreResult(
            inspection=inspection,
            metadata_key=metadata_key,
            quarantine_storage_key=quarantine_key,
        )

    def verify_key(
        self,
        key: str,
        *,
        expected_sha256: str,
        expected_byte_size: int | None = None,
    ) -> bytes:
        """Read a stored private file only after path, size, and hash checks."""

        path = self.path_for_key(key)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ArchiveMediaError("stored archive media is not readable") from exc
        if expected_byte_size is not None and len(data) != expected_byte_size:
            raise ArchiveMediaError("stored archive media size does not match")
        if not _SHA256_PATTERN.fullmatch(expected_sha256.casefold()):
            raise ArchiveMediaError("stored archive media checksum is invalid")
        if sha256_bytes(data) != expected_sha256.casefold():
            raise ArchiveMediaError("stored archive media checksum does not match")
        return data

    def path_for_key(self, key: str) -> Path:
        """Resolve a key below the media root without following symlinks."""

        if not isinstance(key, str) or not key or "\\" in key:
            raise ArchiveMediaError("unsafe archive media key")
        relative = PurePosixPath(key)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ArchiveMediaError("unsafe archive media key")
        path = self.root.joinpath(*relative.parts)
        try:
            resolved_root = self.root.resolve(strict=False)
            resolved_path = path.resolve(strict=False)
            resolved_path.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ArchiveMediaError("unsafe archive media key") from exc
        if any(part in {".env", ".env.local"} for part in relative.parts):
            raise ArchiveMediaError("unsafe archive media key")
        current = self.root
        for part in (None, *relative.parts):
            if part is not None:
                current /= part
            try:
                mode = os.lstat(current).st_mode
            except FileNotFoundError:
                break
            except OSError as exc:
                raise ArchiveMediaError("archive media path cannot be inspected") from exc
            if stat.S_ISLNK(mode):
                raise ArchiveMediaError("archive media symlink is not allowed")
        return path

    def _write_bytes(self, key: str, data: bytes) -> None:
        destination = self.path_for_key(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".archive-media-", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as file_handle:
                file_handle.write(data)
                file_handle.flush()
                os.fsync(file_handle.fileno())
            os.replace(temporary, destination)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_text(self, key: str, value: str) -> None:
        self._write_bytes(key, value.encode("utf-8"))


__all__ = [
    "ArchiveMediaError",
    "ArchiveMediaStore",
    "MediaInspection",
    "MediaStoreResult",
    "SUPPORTED_ARCHIVE_MIME_TYPES",
    "detect_mime_type",
    "sha256_bytes",
]
