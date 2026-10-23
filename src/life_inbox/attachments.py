"""Safe local attachment storage."""

from __future__ import annotations

import hashlib
import mimetypes
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

SUPPORTED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})


class AttachmentError(ValueError):
    pass


class AttachmentStore:
    def __init__(self, data_dir: str | Path = "data", *, max_bytes: int = 10 * 1024 * 1024) -> None:
        self.data_dir = Path(data_dir)
        self.max_bytes = max_bytes

    def validate(self, content_type: str, data: bytes) -> None:
        normalized_type = (content_type or "").lower().split(";", 1)[0].strip()
        if normalized_type not in SUPPORTED_IMAGE_TYPES:
            raise AttachmentError(
                f"unsupported attachment MIME type: {normalized_type or 'unknown'}"
            )
        if not data:
            raise AttachmentError("attachment is empty")
        if len(data) > self.max_bytes:
            raise AttachmentError("attachment exceeds configured size limit")

    @staticmethod
    def sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def save(
        self,
        *,
        event_id: int,
        original_filename: str,
        content_type: str,
        data: bytes,
    ) -> tuple[str, str]:
        self.validate(content_type, data)
        now = datetime.now(UTC)
        directory = self.data_dir / "attachments" / f"{now.year:04d}" / f"{now.month:02d}"
        directory.mkdir(parents=True, exist_ok=True)
        suffix = Path(original_filename).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            suffix = mimetypes.guess_extension(content_type.split(";", 1)[0]) or ".bin"
        destination = directory / f"{uuid4().hex}{suffix}"
        destination.write_bytes(data)
        return str(destination), self.sha256(data)
