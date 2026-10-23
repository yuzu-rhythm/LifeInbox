"""Small safety and event helpers shared by rhythm-game collectors.

The provider adapters in this package deliberately keep credentials and raw
browser/provider objects at the edge.  The helpers here provide the narrow
redaction and runtime-file seams needed by the adapters without adding a
network, browser, or persistence dependency.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

REDACTED = "[redacted]"


class RuntimeSourceFailure(StrEnum):
    """Non-secret reasons for failing to load a runtime bookmarklet file."""

    MISSING_PATH = "missing_runtime_file"
    NOT_A_FILE = "runtime_path_not_a_file"
    READ_FAILURE = "runtime_file_read_failure"
    CREDENTIALS_PRESENT = "credentials_in_runtime_source"


def _secret_values(secrets: Sequence[str] | None) -> tuple[str, ...]:
    """Return non-empty secret strings in longest-first order."""

    if not secrets:
        return ()
    values = {secret for secret in secrets if isinstance(secret, str) and secret}
    return tuple(sorted(values, key=len, reverse=True))


def redact_text(value: str, secrets: Sequence[str] | None = None) -> str:
    """Redact supplied secrets, including their URL-encoded representation."""

    if not isinstance(value, str):
        value = str(value)
    redacted = value
    for secret in _secret_values(secrets):
        redacted = redacted.replace(secret, REDACTED)
        encoded = quote(secret, safe="")
        if encoded != secret:
            redacted = redacted.replace(encoded, REDACTED)
    # Defense in depth for provider/library messages whose concrete secret was
    # not supplied by the caller. Preserve the field name while removing its
    # value, including common URL-query and Authorization forms.
    redacted = re.sub(
        r"(?i)(\b(?:authorization|token|access[_-]?token|api[_-]?key|password|passwd|"
        r"client[_-]?secret|cookie|session)\s*[:=]\s*(?:bearer\s+)?)[^\s&,;]+",
        rf"\1{REDACTED}",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+",
        rf"\1{REDACTED}",
        redacted,
    )
    return redacted


def sanitize_for_output(value: Any, secrets: Sequence[str] | None = None) -> Any:
    """Make a JSON-like, recursively redacted copy of provider data.

    The result is intended for ``RatingSnapshot.raw_data`` and diagnostic
    structures.  Sensitive-looking mapping keys are replaced even when a
    caller forgot to pass the concrete token value.
    """

    secret_values = _secret_values(secrets)
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = redact_text(str(raw_key), secret_values)
            key_folded = key.casefold().replace("-", "_")
            if any(
                marker in key_folded
                for marker in (
                    "token",
                    "access_key",
                    "api_key",
                    "authorization",
                    "password",
                    "passwd",
                    "secret",
                    "credential",
                    "cookie",
                    "session",
                )
            ):
                sanitized[key] = REDACTED
            else:
                sanitized[key] = sanitize_for_output(raw_value, secret_values)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [sanitize_for_output(item, secret_values) for item in value]
    if isinstance(value, (set, frozenset)):
        values = [sanitize_for_output(item, secret_values) for item in value]
        return sorted(values, key=lambda item: repr(item))
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, str):
        return redact_text(value, secret_values)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value), secret_values)


def safe_message(message: str | None, secrets: Sequence[str] | None = None) -> str | None:
    """Return a short redacted message suitable for a public result."""

    if message is None:
        return None
    return redact_text(message, secrets)


def safe_browser_url(value: object) -> str | None:
    """Keep URL evidence useful without retaining query or fragment secrets."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    # Browser session identifiers and bearer-like values commonly arrive in a
    # query string.  Drop userinfo, query, and fragment before URL evidence is
    # stored in a result or an event log.
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


@dataclass(frozen=True, slots=True)
class RuntimeSourceResult:
    """Result of reading a caller-owned runtime bookmarklet source file.

    ``source`` is intentionally excluded from ``repr`` so a diagnostic result
    cannot accidentally print the runtime source.  The source is only held in
    memory long enough for an adapter to hand it to the browser.
    """

    success: bool
    source: str | None = field(default=None, repr=False)
    failure_reason: RuntimeSourceFailure | None = None


_CREDENTIAL_LITERAL_MARKERS = (
    "password",
    "passwd",
    "access_token",
    "access-token",
    "api_key",
    "api-key",
    "client_secret",
    "client-secret",
    "authorization",
    "cookie",
)


def _looks_like_embedded_credential(source: str) -> bool:
    """Reject obvious literal credential assignments in runtime source.

    This is intentionally conservative about source contents: a caller may
    use arbitrary bookmarklet code, but a literal value next to a credential
    key is not safe to execute through this foundation.
    """

    lowered = source.casefold()
    for marker in _CREDENTIAL_LITERAL_MARKERS:
        start = 0
        while True:
            position = lowered.find(marker, start)
            if position < 0:
                break
            suffix = source[position + len(marker) :]
            stripped = suffix.lstrip()
            if stripped.startswith(("=", ":")):
                value = stripped[1:].lstrip()
                if value.startswith(("'", '"', "`")):
                    return True
            start = position + len(marker)
    return False


def load_runtime_source(
    path: str | Path,
    *,
    secrets: Sequence[str] | None = None,
) -> RuntimeSourceResult:
    """Read a caller-supplied runtime file without copying it into the repo."""

    runtime_path = Path(path)
    try:
        if not runtime_path.exists():
            return RuntimeSourceResult(False, failure_reason=RuntimeSourceFailure.MISSING_PATH)
        if not runtime_path.is_file():
            return RuntimeSourceResult(False, failure_reason=RuntimeSourceFailure.NOT_A_FILE)
        source = runtime_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return RuntimeSourceResult(False, failure_reason=RuntimeSourceFailure.READ_FAILURE)
    if _looks_like_embedded_credential(source):
        return RuntimeSourceResult(
            False,
            failure_reason=RuntimeSourceFailure.CREDENTIALS_PRESENT,
        )
    return RuntimeSourceResult(True, source=redact_text(source, secrets))


class EvidenceKind(StrEnum):
    """Explicit browser evidence types accepted by event-driven adapters."""

    URL = "url"
    DOM = "dom"
    POPUP = "popup"
    DOWNLOAD = "download"
    BLOB = "blob"
    CANVAS = "canvas"
    IMAGE = "image"
    DATA = "data"


@dataclass(frozen=True, slots=True)
class BrowserEvidence:
    """A detached, non-secret description of one browser-side observation."""

    kind: EvidenceKind | str
    completed: bool = False
    url: str | None = None
    selector: str | None = None
    label: str | None = None
    path: str | Path | None = None
    generation_id: str | None = None
    payload: object | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            kind = (
                self.kind
                if isinstance(self.kind, EvidenceKind)
                else EvidenceKind(str(self.kind))
            )
        except ValueError as exc:
            raise ValueError("unsupported browser evidence kind") from exc
        if type(self.completed) is not bool:
            raise ValueError("completed must be a boolean")
        object.__setattr__(self, "kind", kind)
        if self.path is not None:
            object.__setattr__(self, "path", str(self.path))


__all__ = [
    "BrowserEvidence",
    "EvidenceKind",
    "REDACTED",
    "RuntimeSourceFailure",
    "RuntimeSourceResult",
    "load_runtime_source",
    "redact_text",
    "safe_browser_url",
    "safe_message",
    "sanitize_for_output",
]
