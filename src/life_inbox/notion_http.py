"""Synchronous HTTP transport for the Notion secondary archive.

The durable sync service deliberately depends on the small
``NotionTransport`` protocol in :mod:`life_inbox.notion_transport`.  This
module is the opt-in boundary that turns those local request objects into
official Notion REST API calls.  It does not create a client from global
configuration and it never logs credentials.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .notion_transport import (
    NotionFileResult,
    NotionFileUploadRequest,
    NotionPageRequest,
    NotionPageResult,
    NotionTransportError,
    NotionValidationError,
    sanitize_error_message,
)

# Notion's current API documentation (as of 2026-08-30) lists this as the
# latest version.  Keeping it explicit avoids the API's implicit-version
# behavior and makes a future breaking-version upgrade reviewable.
NOTION_API_VERSION = "2026-03-11"
NOTION_VERSION = NOTION_API_VERSION
DEFAULT_NOTION_API_BASE_URL = "https://api.notion.com"
NOTION_MAX_SINGLE_PART_BYTES = 20 * 1024 * 1024
NOTION_MULTIPART_PART_BYTES = 10 * 1024 * 1024
NOTION_MAX_FILE_BYTES = 5 * 1024 * 1024 * 1024
_MAX_FILENAME_BYTES = 900
_MAX_RICH_TEXT_CHARS = 2_000
_MAX_CHILDREN_PER_REQUEST = 100
_SAFE_PARENT_TYPES = {"page_id", "data_source_id"}
_SELECT_PROPERTY_NAMES = frozenset({"source", "category", "state"})
_DATE_PROPERTY_NAMES = frozenset({"occurred at"})
_CHECKBOX_PROPERTY_NAMES = frozenset({"has media", "favorite"})
_NOTION_PAGE_HOSTS = frozenset({"notion.so", "www.notion.so", "app.notion.com"})
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_LANGUAGE_MAP = {
    "json": "json",
    "javascript": "javascript",
    "js": "javascript",
    "python": "python",
    "py": "python",
    "bash": "bash",
    "shell": "bash",
    "sh": "bash",
    "css": "css",
    "html": "html",
    "markdown": "markdown",
    "md": "markdown",
    "sql": "sql",
    "typescript": "typescript",
    "ts": "typescript",
    "yaml": "yaml",
    "yml": "yaml",
}


class _HttpClient(Protocol):
    """The small synchronous subset used from an injected httpx client."""

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        ...


def _secret_value(value: object | None) -> str:
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        value = getter()
    return str(value or "").strip()


def _clean_identifier(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise NotionValidationError(f"{field_name} is required", code="invalid_configuration")
    if len(text) > 256 or _CONTROL_RE.search(text):
        raise NotionValidationError(f"{field_name} is invalid", code="invalid_configuration")
    return text


def _clean_filename(value: object) -> str:
    text = str(value or "").replace("\x00", "").replace("\\", "/").rsplit("/", 1)[-1]
    text = text.replace("\r", "").replace("\n", "").strip()
    if not text:
        text = "upload.bin"
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_FILENAME_BYTES:
        text = encoded[:_MAX_FILENAME_BYTES].decode("utf-8", errors="ignore")
    return text or "upload.bin"


def _clean_content_type(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 255 or _CONTROL_RE.search(text) or ";" in text:
        raise NotionValidationError("content type is invalid", code="invalid_request")
    return text


def _page_url(payload: Mapping[str, Any]) -> str | None:
    """Return a credential-free canonical URL from a Notion page response."""

    value = payload.get("url")
    if not isinstance(value, str) or not value.strip() or _CONTROL_RE.search(value):
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold()
    if not (
        parsed.scheme.casefold() == "https"
        and (host in _NOTION_PAGE_HOSTS or host.endswith(".notion.site"))
        and parsed.path
        and not parsed.username
        and not parsed.password
    ):
        return None
    return urlunsplit(("https", host, parsed.path, "", ""))


def _split_text(text: str, maximum: int = _MAX_RICH_TEXT_CHARS) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": {"content": text[index : index + maximum]}}
        for index in range(0, len(text), maximum)
    ] or [{"type": "text", "text": {"content": ""}}]


def _text_block(block_type: str, text: str) -> dict[str, Any]:
    return {"object": "block", "type": block_type, block_type: {"rich_text": _split_text(text)}}


def _markdown_blocks(markdown: str) -> list[dict[str, Any]]:
    """Convert the bounded local Markdown draft into supported Notion blocks.

    The local planner intentionally emits a small Markdown vocabulary.  This
    parser handles that vocabulary without pretending to be a full Markdown
    renderer, so arbitrary user text remains plain text and cannot become a
    remote URL or executable content.
    """

    blocks: list[dict[str, Any]] = []
    code_lines: list[str] | None = None
    code_language = "plain text"

    def flush_code() -> None:
        nonlocal code_lines
        if code_lines is None:
            return
        content = "\n".join(code_lines)
        blocks.append(
            {
                "object": "block",
                "type": "code",
                "code": {
                    "rich_text": _split_text(content),
                    "language": code_language,
                },
            }
        )
        code_lines = None

    for raw_line in str(markdown or "").splitlines():
        line = raw_line.rstrip("\r")
        if code_lines is not None:
            if line.startswith("```"):
                flush_code()
            else:
                code_lines.append(line)
            continue
        if line.startswith("```"):
            code_lines = []
            language = line[3:].strip().casefold()
            code_language = _LANGUAGE_MAP.get(language, "plain text")
            continue
        if not line.strip():
            continue
        if line == "---":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            continue
        if line.startswith("### "):
            blocks.append(_text_block("heading_3", line[4:]))
            continue
        if line.startswith("## "):
            blocks.append(_text_block("heading_2", line[3:]))
            continue
        if line.startswith("# "):
            blocks.append(_text_block("heading_1", line[2:]))
            continue
        if line.startswith("> "):
            blocks.append(_text_block("quote", line[2:]))
            continue
        if line.startswith("- ") or line.startswith("* "):
            blocks.append(_text_block("bulleted_list_item", line[2:]))
            continue
        if re.match(r"^\d+\. ", line):
            blocks.append(_text_block("numbered_list_item", re.split(r"\. ", line, 1)[1]))
            continue
        blocks.append(_text_block("paragraph", line))

    flush_code()
    return blocks


def _file_block(file_id: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "image",
        "image": {
            "type": "file_upload",
            "file_upload": {"id": file_id},
            "caption": [],
        },
    }


def _chunked(values: Sequence[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)]


class NotionHttpTransport:
    """Synchronous, credential-safe implementation of ``NotionTransport``.

    ``settings`` may be the existing Settings object, or callers may provide
    the token and parent explicitly.  An ``http_client`` or low-level
    ``transport`` can be injected for deterministic tests; an injected client
    is never closed by this adapter.
    """

    def __init__(
        self,
        token: object | None = None,
        parent_id: str | None = None,
        *,
        settings: Any | None = None,
        api_token: object | None = None,
        parent_page_id: str | None = None,
        parent_database_id: str | None = None,
        parent_type: str | None = None,
        title_property_name: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        http_client: _HttpClient | None = None,
        client: _HttpClient | None = None,
        transport: httpx.BaseTransport | None = None,
        workspace_max_upload_bytes: int | None = None,
    ) -> None:
        # A positional Settings object is convenient for the eventual wiring,
        # while a positional token remains the simple explicit API.
        if settings is None and token is not None and not isinstance(token, (str, bytes)):
            if hasattr(token, "notion_api_token") or hasattr(token, "notion_parent_page_id"):
                settings = token
                token = None

        configured_token = api_token
        if configured_token is None:
            configured_token = getattr(settings, "notion_api_token", None)
        if configured_token is None:
            configured_token = token
        self._token = _secret_value(configured_token)
        if not self._token:
            raise NotionTransportError(
                "Notion API token is not configured",
                code="transport_not_configured",
            )

        configured_parent = parent_page_id or parent_database_id or parent_id
        if configured_parent is None:
            configured_parent = getattr(settings, "notion_parent_page_id", None)
        self.parent_id = _clean_identifier(configured_parent, "Notion parent ID")
        if self._token in self.parent_id:
            raise NotionValidationError(
                "Notion parent ID is invalid",
                code="invalid_configuration",
            )

        configured_parent_type = parent_type or getattr(settings, "notion_parent_type", None)
        if configured_parent_type is None and parent_database_id is not None:
            configured_parent_type = "data_source_id"
        self.parent_type = self._normalize_parent_type(configured_parent_type or "page_id")
        configured_title_name = title_property_name
        if configured_title_name is None:
            configured_title_name = getattr(settings, "notion_title_property_name", None)
        self.title_property_name = self._normalize_title_property_name(
            configured_title_name or "Name"
        )

        configured_base_url = base_url
        if configured_base_url is None:
            configured_base_url = getattr(settings, "notion_api_base_url", None)
        self.base_url = self._normalize_base_url(
            configured_base_url or DEFAULT_NOTION_API_BASE_URL
        )

        configured_timeout = timeout_seconds
        if configured_timeout is None:
            configured_timeout = getattr(settings, "notion_request_timeout_seconds", 20)
        try:
            timeout = float(configured_timeout)
        except (TypeError, ValueError):
            timeout = math.nan
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 120:
            raise NotionValidationError(
                "Notion request timeout must be between 0 and 120 seconds",
                code="invalid_configuration",
            )
        self.timeout_seconds = timeout

        if http_client is not None and client is not None:
            raise NotionValidationError(
                "provide only one injected HTTP client",
                code="invalid_configuration",
            )
        injected_client = http_client or client
        if injected_client is not None and transport is not None:
            raise NotionValidationError(
                "provide either an HTTP client or transport",
                code="invalid_configuration",
            )
        self._owns_client = injected_client is None
        if injected_client is not None:
            self._client = injected_client
        else:
            self._client = cast(
                _HttpClient,
                httpx.Client(
                    timeout=timeout,
                    follow_redirects=False,
                    transport=transport,
                ),
            )

        if workspace_max_upload_bytes is not None:
            if (
                isinstance(workspace_max_upload_bytes, bool)
                or not isinstance(workspace_max_upload_bytes, int)
                or workspace_max_upload_bytes <= 0
            ):
                raise NotionValidationError(
                    "workspace upload limit is invalid",
                    code="invalid_workspace_limit",
                )
        self._workspace_max_upload_bytes = workspace_max_upload_bytes

    @staticmethod
    def _normalize_parent_type(value: object) -> str:
        normalized = str(value or "").strip().casefold()
        aliases = {"page": "page_id", "data_source": "data_source_id"}
        normalized = aliases.get(normalized, normalized)
        if normalized not in _SAFE_PARENT_TYPES:
            raise NotionValidationError(
                "Notion parent type must be page_id or data_source_id",
                code="invalid_configuration",
            )
        return normalized

    @staticmethod
    def _normalize_title_property_name(value: object) -> str:
        text = str(value or "").strip()
        if not text or len(text) > 100 or _CONTROL_RE.search(text):
            raise NotionValidationError(
                "Notion title property name is invalid",
                code="invalid_configuration",
            )
        return text

    @staticmethod
    def _normalize_base_url(value: object) -> str:
        text = str(value or "").strip()
        if not text or any(character.isspace() for character in text) or "\\" in text:
            raise NotionValidationError(
                "Notion API base URL is invalid",
                code="invalid_configuration",
            )
        parsed = urlsplit(text)
        if parsed.scheme.casefold() != "https" or not parsed.hostname:
            raise NotionValidationError(
                "Notion API base URL must use HTTPS",
                code="invalid_configuration",
            )
        if parsed.username is not None or parsed.password is not None:
            raise NotionValidationError(
                "Notion API base URL must not contain credentials",
                code="invalid_configuration",
            )
        try:
            parsed.port
        except ValueError:
            raise NotionValidationError(
                "Notion API base URL has an invalid port",
                code="invalid_configuration",
            ) from None
        if parsed.query or parsed.fragment:
            raise NotionValidationError(
                "Notion API base URL must not contain a query or fragment",
                code="invalid_configuration",
            )
        path = parsed.path.rstrip("/")
        if not path:
            path = "/v1"
        elif not path.casefold().endswith("/v1") and path.casefold() != "/v1":
            path = f"{path}/v1"
        if any(part in {".", ".."} for part in path.split("/")):
            raise NotionValidationError(
                "Notion API base URL path is invalid",
                code="invalid_configuration",
            )
        return urlunsplit((parsed.scheme.casefold(), parsed.netloc, path, "", ""))

    def get_workspace_max_upload_bytes(self) -> int | None:
        """Return a caller-supplied limit; Notion exposes no limit endpoint."""

        return self._workspace_max_upload_bytes

    def upload_file(self, request: NotionFileUploadRequest) -> NotionFileResult:
        """Create, send, and (for multipart files) complete one file upload."""

        path = Path(request.path)
        filename = _clean_filename(request.filename)
        content_type = _clean_content_type(request.content_type)
        try:
            actual_size = path.stat().st_size
        except (FileNotFoundError, OSError):
            raise NotionValidationError(
                "attachment file is not readable", code="invalid_request"
            ) from None
        if not path.is_file() or actual_size <= 0:
            raise NotionValidationError("attachment file is not readable", code="invalid_request")
        if actual_size > NOTION_MAX_FILE_BYTES:
            raise NotionValidationError(
                "attachment exceeds Notion file limit", code="invalid_request"
            )
        declared_size = request.size_bytes
        if (
            isinstance(declared_size, bool)
            or not isinstance(declared_size, int)
            or declared_size <= 0
            or declared_size != actual_size
        ):
            raise NotionValidationError(
                "attachment size changed before upload", code="invalid_request"
            )

        if actual_size <= NOTION_MAX_SINGLE_PART_BYTES:
            created = self._create_file_upload(
                filename=filename,
                content_type=content_type,
                mode="single_part",
            )
            file_id = self._file_upload_id(created)
            sent = self._send_single_part(file_id, path, filename, content_type)
            self._require_uploaded_status(sent)
            return NotionFileResult(file_id)

        part_count = math.ceil(actual_size / NOTION_MULTIPART_PART_BYTES)
        created = self._create_file_upload(
            filename=filename,
            content_type=content_type,
            mode="multi_part",
            number_of_parts=part_count,
        )
        file_id = self._file_upload_id(created)
        try:
            with path.open("rb") as source:
                for part_number in range(1, part_count + 1):
                    part = source.read(NOTION_MULTIPART_PART_BYTES)
                    if not part:
                        raise NotionValidationError(
                            "attachment ended before all parts were read",
                            code="invalid_request",
                        )
                    sent = self._send_part(
                        file_id,
                        part,
                        part_number,
                        filename,
                        content_type,
                    )
                    status = self._file_upload_status(sent)
                    if status in {"failed", "expired"}:
                        raise NotionValidationError(
                            f"Notion file upload failed with status {status}",
                            code="file_upload_failed",
                        )
        except OSError:
            raise NotionValidationError(
                "attachment file is not readable", code="invalid_request"
            ) from None
        completed = self._complete_file_upload(file_id)
        self._require_uploaded_status(completed)
        return NotionFileResult(file_id)

    def create_page(self, request: NotionPageRequest) -> NotionPageResult:
        """Create a page and include its initial content in official block form."""

        body = self._page_body(request)
        children = body.get("children", [])
        body["children"] = children[:_MAX_CHILDREN_PER_REQUEST]
        response = self._request_json("POST", "/pages", json_body=body)
        page_id = self._page_id(response)
        self._append_remaining_children(page_id, children[_MAX_CHILDREN_PER_REQUEST:])
        return NotionPageResult(page_id, _page_url(response))

    def update_page(self, page_id: str, request: NotionPageRequest) -> NotionPageResult:
        """Update page properties and replace its content through block append."""

        normalized_page_id = self._clean_remote_id(page_id, "Notion page ID")
        body = {"properties": self._page_properties(request)}
        children = self._page_children(request)
        if children:
            # Update page cannot accept children.  erase_content is the
            # documented 2026-03-11 replacement mechanism before appending the
            # new deterministic draft below.
            body["erase_content"] = True
        response = self._request_json(
            "PATCH",
            f"/pages/{quote(normalized_page_id, safe='')}",
            json_body=body,
        )
        returned_page_id = self._page_id(response)
        self._append_remaining_children(returned_page_id, children)
        return NotionPageResult(returned_page_id, _page_url(response))

    def _page_body(self, request: NotionPageRequest) -> dict[str, Any]:
        return {
            "parent": self._parent_body(),
            "properties": self._page_properties(request),
            # Keep the complete list here. ``create_page`` sends the first API
            # batch and appends the remainder after Notion returns the page ID.
            # Slicing at this layer would silently drop block 101 onward.
            "children": self._page_children(request),
        }

    def _parent_body(self) -> dict[str, str]:
        return {"type": self.parent_type, self.parent_type: self.parent_id}

    def _page_properties(self, request: NotionPageRequest) -> dict[str, Any]:
        title_property = "title" if self.parent_type == "page_id" else self.title_property_name
        properties: dict[str, Any] = {
            title_property: {"title": _split_text(request.title or "(untitled)")}
        }
        if self.parent_type == "page_id":
            # A page parent legally accepts only the title property.  The
            # planner's metadata remains in the page children instead.
            return properties
        for key, value in request.properties:
            property_name = str(key or "").strip()
            if not property_name or _CONTROL_RE.search(property_name):
                raise NotionValidationError("page property name is invalid", code="invalid_request")
            if property_name.casefold() in {"title", title_property.casefold()}:
                properties[title_property] = {"title": _split_text(str(value or request.title))}
            elif property_name.casefold() in _SELECT_PROPERTY_NAMES:
                text = str(value or "").strip()
                properties[property_name] = {"select": {"name": text} if text else None}
            elif property_name.casefold() in _DATE_PROPERTY_NAMES:
                text = str(value or "").strip()
                properties[property_name] = {"date": {"start": text} if text else None}
            elif property_name.casefold() in _CHECKBOX_PROPERTY_NAMES:
                checked = str(value or "").strip().casefold() in {"1", "true", "yes", "on"}
                properties[property_name] = {"checkbox": checked}
            else:
                properties[property_name] = {"rich_text": _split_text(str(value or ""))}
        # The configured Life Archive data source has a Files property named
        # ``Media`` and a matching ``Has Media`` checkbox.  Reuse the official
        # File Upload IDs here as well as in page children so gallery views can
        # render the uploaded image without a public intermediary URL.
        if request.file_ids:
            properties["Media"] = {
                "files": [
                    {
                        "type": "file_upload",
                        "file_upload": {
                            "id": self._clean_remote_id(file_id, "Notion file ID")
                        },
                    }
                    for file_id in request.file_ids
                ]
            }
            properties["Has Media"] = {"checkbox": True}
        return properties

    def _page_children(self, request: NotionPageRequest) -> list[dict[str, Any]]:
        children = _markdown_blocks(request.body_markdown)
        for file_id in request.file_ids:
            children.append(_file_block(self._clean_remote_id(file_id, "Notion file ID")))
        return children

    def _append_remaining_children(
        self,
        page_id: str,
        children: Sequence[dict[str, Any]],
    ) -> None:
        normalized_page_id = self._clean_remote_id(page_id, "Notion page ID")
        for chunk in _chunked(children, _MAX_CHILDREN_PER_REQUEST):
            self._request_json(
                "PATCH",
                f"/blocks/{quote(normalized_page_id, safe='')}/children",
                json_body={"children": chunk},
            )

    def _create_file_upload(
        self,
        *,
        filename: str,
        content_type: str,
        mode: str,
        number_of_parts: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "mode": mode,
            "filename": filename,
            "content_type": content_type,
        }
        if number_of_parts is not None:
            body["number_of_parts"] = number_of_parts
        return self._request_json("POST", "/file_uploads", json_body=body)

    def _send_single_part(
        self,
        file_id: str,
        path: Path,
        filename: str,
        content_type: str,
    ) -> dict[str, Any]:
        try:
            with path.open("rb") as source:
                return self._request_json(
                    "POST",
                    f"/file_uploads/{quote(file_id, safe='')}/send",
                    files={"file": (filename, source, content_type)},
                )
        except OSError:
            raise NotionValidationError(
                "attachment file is not readable", code="invalid_request"
            ) from None

    def _send_part(
        self,
        file_id: str,
        content: bytes,
        part_number: int,
        filename: str,
        content_type: str,
    ) -> dict[str, Any]:
        return self._request_json(
            "POST",
            f"/file_uploads/{quote(file_id, safe='')}/send",
            data={"part_number": str(part_number)},
            files={"file": (filename, content, content_type)},
        )

    def _complete_file_upload(self, file_id: str) -> dict[str, Any]:
        return self._request_json(
            "POST",
            f"/file_uploads/{quote(file_id, safe='')}/complete",
        )

    def _file_upload_id(self, payload: Mapping[str, Any]) -> str:
        file_id = payload.get("id")
        if not isinstance(file_id, str) or not file_id.strip():
            raise NotionValidationError(
                "Notion file upload response did not contain an ID",
                code="malformed_json",
            )
        return self._clean_remote_id(file_id, "Notion file ID")

    @staticmethod
    def _file_upload_status(payload: Mapping[str, Any]) -> str:
        status = payload.get("status")
        if not isinstance(status, str) or not status.strip():
            raise NotionValidationError(
                "Notion file upload response did not contain a status",
                code="malformed_json",
            )
        normalized = status.strip().casefold()
        if normalized not in {"pending", "uploaded", "failed", "expired"}:
            raise NotionValidationError(
                "Notion file upload response contained an unknown status",
                code="malformed_json",
            )
        return normalized

    def _require_uploaded_status(self, payload: Mapping[str, Any]) -> None:
        status = self._file_upload_status(payload)
        if status == "uploaded":
            return
        if status in {"failed", "expired"}:
            raise NotionValidationError(
                f"Notion file upload failed with status {status}",
                code="file_upload_failed",
            )
        raise NotionTransportError(
            f"Notion file upload is not ready: {status}",
            code="file_upload_pending",
        )

    def _page_id(self, payload: Mapping[str, Any]) -> str:
        page_id = payload.get("id")
        if not isinstance(page_id, str) or not page_id.strip():
            raise NotionValidationError(
                "Notion page response did not contain an ID",
                code="malformed_json",
            )
        return self._clean_remote_id(page_id, "Notion page ID")

    def _clean_remote_id(self, value: object, field_name: str) -> str:
        identifier = _clean_identifier(value, field_name)
        if self._token in identifier:
            raise NotionValidationError(f"{field_name} is invalid", code="malformed_json")
        return identifier

    def _headers(self, *, json_body: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_API_VERSION,
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        data: Mapping[str, str] | None = None,
        files: Any | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            response = self._client.request(
                method,
                url,
                headers=self._headers(json_body=json_body is not None),
                json=json_body,
                data=data,
                files=files,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise NotionTransportError(
                "Notion request timed out",
                code="timeout",
            ) from None
        except (httpx.NetworkError, httpx.RequestError, TimeoutError, OSError):
            raise NotionTransportError(
                "Notion request failed before receiving a response",
                code="network_error",
            ) from None

        history = getattr(response, "history", ())
        if history:
            raise NotionTransportError(
                "Notion request redirected unexpectedly",
                code="redirect_not_allowed",
            )

        status_code = getattr(response, "status_code", None)
        if not isinstance(status_code, int):
            raise NotionTransportError("Notion response status is invalid", code="transport_error")
        if not 200 <= status_code < 300:
            self._raise_http_error(response, status_code)
        try:
            payload = response.json()
        except (TypeError, ValueError):
            raise NotionValidationError(
                "Notion response contained malformed JSON",
                code="malformed_json",
            ) from None
        if not isinstance(payload, dict):
            raise NotionValidationError(
                "Notion response JSON object is invalid",
                code="malformed_json",
            )
        return cast(dict[str, Any], payload)

    def _raise_http_error(self, response: Any, status_code: int) -> None:
        detail: object = "Notion API request failed"
        provider_code: object = None
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            detail = payload.get("message") or detail
            provider_code = payload.get("code")
        detail_text = sanitize_error_message(detail, secrets=(self._token,))
        code_text = sanitize_error_message(provider_code, secrets=(self._token,))
        if not code_text:
            if status_code == 429:
                code_text = "rate_limited"
            elif status_code == 401:
                code_text = "unauthorized"
            elif status_code == 403:
                code_text = "forbidden"
            elif 500 <= status_code <= 599:
                code_text = "server_error"
            else:
                code_text = "http_error"
        raise NotionTransportError(
            detail_text,
            status_code=status_code,
            code=code_text,
            headers=getattr(response, "headers", None),
        )

    def close(self) -> None:
        if self._owns_client:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> NotionHttpTransport:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()


# Both spellings are useful at a boundary where callers commonly use either
# HTTP or Http in class names.  They refer to the same implementation.
NotionHTTPTransport = NotionHttpTransport


__all__ = [
    "DEFAULT_NOTION_API_BASE_URL",
    "NOTION_API_VERSION",
    "NOTION_MAX_FILE_BYTES",
    "NOTION_MAX_SINGLE_PART_BYTES",
    "NOTION_MULTIPART_PART_BYTES",
    "NOTION_VERSION",
    "NotionHTTPTransport",
    "NotionHttpTransport",
]
