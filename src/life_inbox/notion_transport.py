"""Mockable transport boundary for the Notion secondary archive.

This module contains request data and error classification only.  A future
authenticated adapter can implement :class:`NotionTransport`; the foundation
does not perform HTTP requests by itself.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Protocol

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)((?:authorization|bearer|token|secret|password|api[ _-]?key|access[ _-]?token)"
    r"\s*[:=]\s*)(?:bearer\s+)?([^\s,;]+)"
)
_QUOTED_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)((?:[\"']?)(?:authorization|bearer|token|secret|password|api[ _-]?key|"
    r"access[ _-]?token)(?:[\"']?\s*:\s*[\"']))([^\"']+)([\"'])"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_NOTION_TOKEN_RE = re.compile(r"(?i)\b(?:secret|ntn)_[A-Za-z0-9._~-]+\b")
_SAFE_CODE_RE = re.compile(r"[^a-z0-9_.-]+")


def sanitize_error_message(message: object, *, secrets: Sequence[str] = ()) -> str:
    """Return a bounded provider error safe for local diagnostics/storage."""

    text = re.sub(r"\s+", " ", str(message or "").replace("\x00", " ")).strip()
    for secret in sorted((str(value) for value in secrets if value), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = _SECRET_ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)
    text = _QUOTED_SECRET_ASSIGNMENT_RE.sub(r"\1[REDACTED]\3", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _NOTION_TOKEN_RE.sub("[REDACTED]", text)
    return text[:500]


def _safe_error_code(code: object, default: str = "transport_error") -> str:
    normalized = _SAFE_CODE_RE.sub("_", str(code or "").casefold()).strip("_.-")
    return (normalized or default)[:64]


def _header_value(headers: Mapping[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted:
            return str(value).strip()
    return None


def parse_retry_after(
    value: object,
    *,
    now: datetime | None = None,
) -> int | None:
    """Parse numeric or HTTP-date ``Retry-After`` without retaining headers."""

    if value is None:
        return None
    text = str(value).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        base = now or datetime.now(UTC)
        if base.tzinfo is None:
            base = base.replace(tzinfo=UTC)
        seconds = (retry_at - base).total_seconds()
    if seconds < 0:
        return 0
    return int(seconds + 0.999)


class NotionTransportError(RuntimeError):
    """Provider/adapter error with only sanitized diagnostic metadata."""

    def __init__(
        self,
        message: object,
        *,
        status_code: int | None = None,
        code: str | None = None,
        retry_after_seconds: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.message = sanitize_error_message(message)
        self.status_code = status_code
        self.code = _safe_error_code(code)
        retry_after = retry_after_seconds
        if retry_after is None:
            retry_after = parse_retry_after(_header_value(headers, "Retry-After"))
        self.retry_after_seconds = max(0, int(retry_after)) if retry_after is not None else None
        super().__init__(self.message)


class NotionValidationError(NotionTransportError):
    """A request or response that cannot be retried without changing data."""

    def __init__(self, message: object, *, code: str = "validation_error") -> None:
        super().__init__(message, code=code)


class NotionErrorDisposition:
    RETRYABLE = "retryable"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class NotionErrorClassification:
    disposition: str
    code: str
    message: str
    retry_after_seconds: int | None = None

    @property
    def retryable(self) -> bool:
        return self.disposition == NotionErrorDisposition.RETRYABLE


def classify_notion_error(
    error: BaseException,
    *,
    secrets: Sequence[str] = (),
    now: datetime | None = None,
) -> NotionErrorClassification:
    """Map adapter failures to durable retry or terminal states."""

    status_code = getattr(error, "status_code", None)
    code = _safe_error_code(getattr(error, "code", None))
    message = sanitize_error_message(error, secrets=secrets)
    retry_after = getattr(error, "retry_after_seconds", None)

    if isinstance(error, NotionValidationError) or code in {
        "validation_error",
        "invalid_request",
        "invalid_workspace_limit",
    }:
        return NotionErrorClassification(
            NotionErrorDisposition.TERMINAL,
            code,
            message,
        )
    if code in {"sync_disabled", "transport_not_configured"}:
        return NotionErrorClassification(
            NotionErrorDisposition.TERMINAL,
            "configuration_error",
            message,
        )
    if status_code == 429:
        if retry_after is None:
            retry_after = parse_retry_after(getattr(error, "retry_after", None), now=now)
        return NotionErrorClassification(
            NotionErrorDisposition.RETRYABLE,
            "rate_limited",
            message,
            retry_after,
        )
    if isinstance(status_code, int) and 500 <= status_code <= 599:
        return NotionErrorClassification(
            NotionErrorDisposition.RETRYABLE,
            "server_error",
            message,
            retry_after,
        )
    if status_code == 401:
        return NotionErrorClassification(NotionErrorDisposition.TERMINAL, "unauthorized", message)
    if status_code == 403:
        return NotionErrorClassification(NotionErrorDisposition.TERMINAL, "forbidden", message)
    if isinstance(status_code, int) and 400 <= status_code <= 499:
        return NotionErrorClassification(NotionErrorDisposition.TERMINAL, "http_error", message)
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return NotionErrorClassification(
            NotionErrorDisposition.RETRYABLE,
            "transport_error",
            message,
            retry_after,
        )
    if isinstance(error, NotionTransportError):
        return NotionErrorClassification(
            NotionErrorDisposition.RETRYABLE,
            "transport_error",
            message,
            retry_after,
        )
    if isinstance(error, ValueError) and re.search(r"invalid|validation", message, re.IGNORECASE):
        return NotionErrorClassification(
            NotionErrorDisposition.TERMINAL,
            "validation_error",
            message,
        )
    return NotionErrorClassification(
        NotionErrorDisposition.TERMINAL,
        "unexpected_error",
        message,
    )


@dataclass(frozen=True, slots=True)
class NotionFileUploadRequest:
    """Local file metadata passed to a File Upload API adapter."""

    path: Path
    filename: str
    content_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class NotionFileResult:
    file_id: str


@dataclass(frozen=True, slots=True)
class NotionPageRequest:
    event_id: int
    title: str
    body_markdown: str
    properties: tuple[tuple[str, str], ...]
    file_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NotionPageResult:
    """Validated remote page identity and an optional provider page URL."""

    page_id: str
    url: str | None = None


class NotionTransport(Protocol):
    """The only boundary a real Notion adapter needs to implement."""

    def get_workspace_max_upload_bytes(self) -> int | None:
        """Return the current workspace limit, or None if it is unknown."""

    def upload_file(self, request: NotionFileUploadRequest) -> NotionFileResult:
        """Create, send, and complete one Notion File Upload API upload."""

    def create_page(self, request: NotionPageRequest) -> NotionPageResult:
        """Create one page below the configured parent."""

    def update_page(self, page_id: str, request: NotionPageRequest) -> NotionPageResult:
        """Update the existing page for the same local Event."""


__all__ = [
    "NotionErrorClassification",
    "NotionErrorDisposition",
    "NotionFileResult",
    "NotionFileUploadRequest",
    "NotionPageRequest",
    "NotionPageResult",
    "NotionTransport",
    "NotionTransportError",
    "NotionValidationError",
    "classify_notion_error",
    "parse_retry_after",
    "sanitize_error_message",
]
