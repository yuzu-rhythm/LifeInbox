from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import life_inbox.notion_http as notion_http
from life_inbox.notion_http import (
    DEFAULT_NOTION_API_BASE_URL,
    NOTION_API_VERSION,
    NotionHTTPTransport,
    NotionHttpTransport,
)
from life_inbox.notion_transport import (
    NotionErrorDisposition,
    NotionFileUploadRequest,
    NotionPageRequest,
    NotionTransportError,
    NotionValidationError,
    classify_notion_error,
)

SYNTHETIC_TOKEN = "synthetic-token-for-local-tests"


def _adapter(
    handler,
    *,
    parent_type: str = "page_id",
    base_url: str = "https://notion.test",
    timeout_seconds: float = 3,
    **kwargs,
) -> NotionHttpTransport:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return NotionHttpTransport(
        api_token=SYNTHETIC_TOKEN,
        parent_page_id="parent-page-id",
        parent_type=parent_type,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        http_client=client,
        **kwargs,
    )


def _page_request(
    *, title: str = "A local page", body: str = "# Heading\nBody"
) -> NotionPageRequest:
    return NotionPageRequest(
        event_id=42,
        title=title,
        body_markdown=body,
        properties=(
            ("Local Event ID", "42"),
            ("Source", "discord"),
        ),
    )


def _json_response(
    request: httpx.Request, payload: object, status_code: int = 200
) -> httpx.Response:
    return httpx.Response(status_code, json=payload, request=request)


def test_constants_and_default_base_url_are_explicit():
    assert DEFAULT_NOTION_API_BASE_URL == "https://api.notion.com"
    assert NOTION_API_VERSION == "2026-03-11"
    assert NotionHTTPTransport is NotionHttpTransport


def test_create_page_uses_current_headers_and_official_page_parent_shape():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        seen["body"] = json.loads(request.content)
        assert request.method == "POST"
        assert request.url == "https://notion.test/v1/pages"
        assert request.headers["authorization"] == f"Bearer {SYNTHETIC_TOKEN}"
        assert request.headers["notion-version"] == NOTION_API_VERSION
        assert request.headers["content-type"] == "application/json"
        assert request.extensions["timeout"]["connect"] == 3
        assert request.extensions["timeout"]["read"] == 3
        return _json_response(
            request,
            {
                "object": "page",
                "id": "created-page-id",
                "url": "https://www.notion.so/created-page-id?view=archive",
            },
        )

    adapter = _adapter(handler)
    result = adapter.create_page(_page_request())

    assert result.page_id == "created-page-id"
    assert result.url == "https://www.notion.so/created-page-id"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["parent"] == {"type": "page_id", "page_id": "parent-page-id"}
    assert body["properties"]["title"]["title"][0]["text"]["content"] == "A local page"
    assert body["children"][0]["type"] == "heading_1"
    assert body["children"][1]["type"] == "paragraph"
    assert SYNTHETIC_TOKEN not in request_body_text(body)


def test_data_source_parent_maps_string_properties_to_current_page_shapes():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _json_response(request, {"object": "page", "id": "data-source-page-id"})

    adapter = _adapter(handler, parent_type="data_source_id", title_property_name="Name")
    result = adapter.create_page(_page_request())

    assert result.page_id == "data-source-page-id"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["parent"] == {
        "type": "data_source_id",
        "data_source_id": "parent-page-id",
    }
    assert body["properties"]["Name"]["title"][0]["text"]["content"] == "A local page"
    assert body["properties"]["Local Event ID"]["rich_text"][0]["text"]["content"] == "42"
    assert body["properties"]["Source"] == {"select": {"name": "discord"}}


def test_update_page_replaces_content_using_update_then_append_block_children():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/v1/pages/existing-page-id":
            body = json.loads(request.content)
            assert request.method == "PATCH"
            assert body["erase_content"] is True
            assert body["properties"]["title"]["title"][0]["text"]["content"] == "Updated"
            return _json_response(
                request,
                {
                    "object": "page",
                    "id": "existing-page-id",
                    "url": "https://life.notion.site/existing-page-id",
                },
            )
        assert request.method == "PATCH"
        assert request.url.path == "/v1/blocks/existing-page-id/children"
        body = json.loads(request.content)
        assert body["children"][0]["type"] == "heading_1"
        return _json_response(request, {"object": "list", "results": []})

    adapter = _adapter(handler)
    result = adapter.update_page(
        "existing-page-id",
        _page_request(title="Updated", body="# New content"),
    )

    assert result.page_id == "existing-page-id"
    assert result.url == "https://life.notion.site/existing-page-id"
    assert [request.url.path for request in calls] == [
        "/v1/pages/existing-page-id",
        "/v1/blocks/existing-page-id/children",
    ]


def test_small_file_upload_uses_create_then_multipart_send_and_validates_uploaded_status(
    tmp_path: Path,
):
    upload = tmp_path / "photo.png"
    upload.write_bytes(b"PNG fixture bytes")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == f"Bearer {SYNTHETIC_TOKEN}"
        assert request.headers["notion-version"] == NOTION_API_VERSION
        if request.url.path == "/v1/file_uploads":
            assert request.method == "POST"
            assert json.loads(request.content) == {
                "mode": "single_part",
                "filename": "photo.png",
                "content_type": "image/png",
            }
            return _json_response(
                request,
                {
                    "object": "file_upload",
                    "id": "file-upload-id",
                    "status": "pending",
                    "upload_url": "file:///private/token",
                    "complete_url": "https://evil.test/complete",
                },
            )
        assert request.url.path == "/v1/file_uploads/file-upload-id/send"
        assert request.method == "POST"
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        assert b"photo.png" in request.content
        assert b"PNG fixture bytes" in request.content
        return _json_response(
            request,
            {"object": "file_upload", "id": "file-upload-id", "status": "uploaded"},
        )

    adapter = _adapter(handler)
    result = adapter.upload_file(
        NotionFileUploadRequest(upload, "photo.png", "image/png", upload.stat().st_size)
    )

    assert result.file_id == "file-upload-id"
    assert [request.url.path for request in calls] == [
        "/v1/file_uploads",
        "/v1/file_uploads/file-upload-id/send",
    ]


def test_large_file_upload_sends_parts_completes_and_requires_uploaded_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(notion_http, "NOTION_MAX_SINGLE_PART_BYTES", 4)
    monkeypatch.setattr(notion_http, "NOTION_MULTIPART_PART_BYTES", 3)
    upload = tmp_path / "larger.png"
    upload.write_bytes(b"1234567")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/v1/file_uploads":
            body = json.loads(request.content)
            assert body == {
                "mode": "multi_part",
                "filename": "larger.png",
                "content_type": "image/png",
                "number_of_parts": 3,
            }
            return _json_response(
                request,
                {"object": "file_upload", "id": "multipart-id", "status": "pending"},
            )
        if request.url.path.endswith("/send"):
            assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
            part_numbers = [
                number
                for number in (
                    b"part_number\"\r\n\r\n1",
                    b"part_number\"\r\n\r\n2",
                    b"part_number\"\r\n\r\n3",
                )
                if number in request.content
            ]
            assert len(part_numbers) == 1
            return _json_response(
                request,
                {"object": "file_upload", "id": "multipart-id", "status": "pending"},
            )
        assert request.url.path == "/v1/file_uploads/multipart-id/complete"
        assert request.method == "POST"
        assert request.content == b""
        return _json_response(
            request,
            {"object": "file_upload", "id": "multipart-id", "status": "uploaded"},
        )

    adapter = _adapter(handler)
    result = adapter.upload_file(
        NotionFileUploadRequest(upload, "larger.png", "image/png", upload.stat().st_size)
    )

    assert result.file_id == "multipart-id"
    assert [request.url.path for request in calls] == [
        "/v1/file_uploads",
        "/v1/file_uploads/multipart-id/send",
        "/v1/file_uploads/multipart-id/send",
        "/v1/file_uploads/multipart-id/send",
        "/v1/file_uploads/multipart-id/complete",
    ]


def test_uploaded_file_ids_are_attached_as_file_upload_image_blocks():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _json_response(request, {"object": "page", "id": "page-with-file"})

    adapter = _adapter(handler)
    adapter.create_page(
        NotionPageRequest(
            event_id=1,
            title="With image",
            body_markdown="Body",
            properties=(),
            file_ids=("uploaded-file-id",),
        )
    )

    body = seen["body"]
    assert isinstance(body, dict)
    file_block = body["children"][-1]
    assert file_block == {
        "object": "block",
        "type": "image",
        "image": {
            "type": "file_upload",
            "file_upload": {"id": "uploaded-file-id"},
            "caption": [],
        },
    }


def test_data_source_properties_match_life_archive_schema_types():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _json_response(request, {"object": "page", "id": "typed-page"})

    adapter = _adapter(handler, parent_type="data_source_id")
    adapter.create_page(
        NotionPageRequest(
            event_id=42,
            title="Typed record",
            body_markdown="Body",
            properties=(
                ("Local Event ID", "42"),
                ("Source", "discord"),
                ("Category", "Memo"),
                ("State", "Promoted"),
                ("Occurred At", "2026-08-31T00:15:00+09:00"),
                ("Summary", "短い要約"),
                ("Has Media", "false"),
            ),
            file_ids=("uploaded-file-id",),
        )
    )

    body = seen["body"]
    assert isinstance(body, dict)
    assert body["parent"] == {
        "type": "data_source_id",
        "data_source_id": "parent-page-id",
    }
    properties = body["properties"]
    assert properties["Name"]["title"][0]["text"]["content"] == "Typed record"
    assert properties["Local Event ID"]["rich_text"][0]["text"]["content"] == "42"
    assert properties["Source"] == {"select": {"name": "discord"}}
    assert properties["Category"] == {"select": {"name": "Memo"}}
    assert properties["State"] == {"select": {"name": "Promoted"}}
    assert properties["Occurred At"] == {
        "date": {"start": "2026-08-31T00:15:00+09:00"}
    }
    assert properties["Summary"]["rich_text"][0]["text"]["content"] == "短い要約"
    assert properties["Has Media"] == {"checkbox": True}
    assert properties["Media"] == {
        "files": [
            {
                "type": "file_upload",
                "file_upload": {"id": "uploaded-file-id"},
            }
        ]
    }


def test_create_page_appends_children_after_first_hundred_without_dropping_content():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/v1/pages":
            body = json.loads(request.content)
            assert len(body["children"]) == 100
            assert body["children"][0]["paragraph"]["rich_text"][0]["text"]["content"] == "line-0"
            assert body["children"][-1]["paragraph"]["rich_text"][0]["text"]["content"] == "line-99"
            return _json_response(request, {"object": "page", "id": "many-block-page"})
        assert request.url.path == "/v1/blocks/many-block-page/children"
        body = json.loads(request.content)
        assert len(body["children"]) == 5
        assert body["children"][0]["paragraph"]["rich_text"][0]["text"]["content"] == "line-100"
        assert body["children"][-1]["paragraph"]["rich_text"][0]["text"]["content"] == "line-104"
        return _json_response(request, {"object": "list", "results": []})

    adapter = _adapter(handler)
    body = "\n".join(f"line-{index}" for index in range(105))

    result = adapter.create_page(_page_request(body=body))

    assert result.page_id == "many-block-page"
    assert [request.url.path for request in calls] == [
        "/v1/pages",
        "/v1/blocks/many-block-page/children",
    ]


@pytest.mark.parametrize(
    ("exception_type", "expected_code"),
    [
        (httpx.ReadTimeout, "timeout"),
        (httpx.ConnectError, "network_error"),
    ],
)
def test_timeout_and_network_failures_are_typed_and_do_not_expose_exception_text(
    exception_type,
    expected_code: str,
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception_type(f"private detail {SYNTHETIC_TOKEN}")

    adapter = _adapter(handler)
    with pytest.raises(NotionTransportError) as caught:
        adapter.create_page(_page_request())

    error = caught.value
    assert error.code == expected_code
    assert error.status_code is None
    assert SYNTHETIC_TOKEN not in str(error)
    assert error.__cause__ is None
    assert classify_notion_error(error).disposition == NotionErrorDisposition.RETRYABLE


@pytest.mark.parametrize(
    ("status_code", "provider_code"),
    [(401, "unauthorized"), (403, "restricted_resource")],
)
def test_401_and_403_are_terminal_and_redact_provider_details(status_code: int, provider_code: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            request,
            {
                "object": "error",
                "status": status_code,
                "code": provider_code,
                "message": f"Authorization: Bearer {SYNTHETIC_TOKEN}",
            },
            status_code,
        )

    adapter = _adapter(handler)
    with pytest.raises(NotionTransportError) as caught:
        adapter.create_page(_page_request())

    error = caught.value
    classification = classify_notion_error(error)
    assert error.status_code == status_code
    assert SYNTHETIC_TOKEN not in str(error)
    assert classification.disposition == NotionErrorDisposition.TERMINAL
    assert classification.code == ("unauthorized" if status_code == 401 else "forbidden")


def test_429_is_retryable_and_preserves_retry_after_without_retaining_headers():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"object": "error", "code": "rate_limited", "message": "slow down"},
            headers={"Retry-After": "7"},
            request=request,
        )

    adapter = _adapter(handler)
    with pytest.raises(NotionTransportError) as caught:
        adapter.create_page(_page_request())

    error = caught.value
    classification = classify_notion_error(error)
    assert error.status_code == 429
    assert error.retry_after_seconds == 7
    assert classification.disposition == NotionErrorDisposition.RETRYABLE
    assert classification.code == "rate_limited"
    assert "Retry-After" not in str(error)


def test_5xx_is_retryable_and_has_sanitized_error_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            request,
            {"object": "error", "code": "service_unavailable", "message": "try later"},
            503,
        )

    adapter = _adapter(handler)
    with pytest.raises(NotionTransportError) as caught:
        adapter.create_page(_page_request())

    error = caught.value
    classification = classify_notion_error(error)
    assert error.status_code == 503
    assert classification.disposition == NotionErrorDisposition.RETRYABLE
    assert classification.code == "server_error"


@pytest.mark.parametrize("status_code", [200, 500])
def test_malformed_json_never_becomes_a_success_or_exposes_response_body(status_code: int):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            content=f"not json {SYNTHETIC_TOKEN}".encode(),
            headers={"content-type": "application/json"},
            request=request,
        )

    adapter = _adapter(handler)
    with pytest.raises((NotionTransportError, NotionValidationError)) as caught:
        adapter.create_page(_page_request())

    assert SYNTHETIC_TOKEN not in str(caught.value)
    if status_code == 200:
        assert isinstance(caught.value, NotionValidationError)
        assert caught.value.code == "malformed_json"
    else:
        assert isinstance(caught.value, NotionTransportError)
        assert caught.value.status_code == 500


def test_base_url_is_normalized_and_unsafe_schemes_credentials_queries_are_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(request, {"object": "page", "id": "page-id"})

    adapter = _adapter(handler, base_url="https://notion.test/v1/")
    assert adapter.base_url == "https://notion.test/v1"
    adapter.create_page(_page_request())

    for unsafe_url in (
        "http://notion.test",
        "file:///private/token",
        "https://user:password@notion.test",
        "https://notion.test/v1?token=secret",
        "https://notion.test/v1/../other",
    ):
        with pytest.raises(NotionValidationError):
            _adapter(handler, base_url=unsafe_url)


def test_redirects_are_not_followed_and_returned_upload_urls_are_not_trusted():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            302,
            headers={"location": "file:///private/secret"},
            request=request,
        )

    adapter = _adapter(handler)
    with pytest.raises(NotionTransportError) as caught:
        adapter.create_page(_page_request())

    assert len(calls) == 1
    assert caught.value.status_code == 302
    assert "private/secret" not in str(caught.value)


def request_body_text(body: object) -> str:
    return json.dumps(body, ensure_ascii=False)
