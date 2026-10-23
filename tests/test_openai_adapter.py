from __future__ import annotations

import json
from types import SimpleNamespace

import httpx2
import pytest
from openai import OpenAI

from life_inbox.analyzer import AnalyzerError, OpenAIAnalyzer
from life_inbox.schemas import (
    InboxRouteResult,
    ReceiptAnalysisResult,
    ReceiptItemResult,
    TextAnalysisResult,
)


class FakeResponses:
    def __init__(self, output_parsed=None):
        self.calls = []
        self.output_parsed = output_parsed or TextAnalysisResult(
            event_kinds=["expense"],
            amount=680,
            merchant="マック",
            category="食費",
            subcategory="外食",
            confidence=0.9,
        )

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.output_parsed)


class FakeClient:
    def __init__(self, output_parsed=None):
        self.responses = FakeResponses(output_parsed)


def test_openai_adapter_uses_responses_structured_outputs_without_network():
    client = FakeClient()
    analyzer = OpenAIAnalyzer("test-key", client=client)
    result = analyzer.analyze_text("680 マック")
    assert isinstance(result, TextAnalysisResult)
    assert result.amount == 680
    call = client.responses.calls[0]
    assert call["store"] is False
    assert call["text_format"] is TextAnalysisResult
    assert call["input"][1]["content"][0]["type"] == "input_text"


def test_openai_adapter_routes_inbox_with_strict_schema_without_network():
    parsed = InboxRouteResult(
        destination="life_task",
        task_title="研究計画書を提出",
        task_due_date="2026-09-05",
        task_due_time="15:00",
        confidence=0.91,
    )
    client = FakeClient(parsed)
    analyzer = OpenAIAnalyzer("test-key", client=client)

    result = analyzer.route_inbox("2026年9月5日15時に研究計画書を提出")

    assert result == parsed
    call = client.responses.calls[0]
    assert call["store"] is False
    assert call["text_format"] is InboxRouteResult
    assert call["input"][1]["content"] == [
        {"type": "input_text", "text": "2026年9月5日15時に研究計画書を提出"}
    ]
    assert all(block["type"] != "input_image" for block in call["input"][1]["content"])
    system_text = call["input"][0]["content"][0]["text"]
    assert "相対的・曖昧な日時" in system_text
    assert "推測せずnull" in system_text


def test_openai_adapter_sends_receipt_image_and_requests_item_schema_without_network(tmp_path):
    parsed = ReceiptAnalysisResult(
        merchant="テストスーパー",
        total=398,
        items=[ReceiptItemResult(name="牛乳", quantity=1, amount=198)],
        confidence=0.88,
    )
    client = FakeClient(parsed)
    analyzer = OpenAIAnalyzer("test-key", client=client)
    image_path = tmp_path / "receipt.png"
    image_path.write_bytes(b"fake-image-bytes")

    result = analyzer.analyze_receipt(image_path, "image/png", context_text="レシート")

    assert result.total == 398
    assert result.items[0].name == "牛乳"
    call = client.responses.calls[0]
    assert call["store"] is False
    assert call["text_format"] is ReceiptAnalysisResult
    user_content = call["input"][1]["content"]
    assert user_content[0] == {"type": "input_text", "text": "レシート"}
    assert user_content[1]["type"] == "input_image"
    assert user_content[1]["image_url"].startswith("data:image/png;base64,")
    system_text = call["input"][0]["content"][0]["text"]
    assert "itemsには購入した商品・サービスの明細" in system_text


def test_current_openai_sdk_serializes_pydantic_schema_without_network():
    seen: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            400,
            json={
                "error": {
                    "message": "mock failure",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": None,
                }
            },
        )

    with OpenAI(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    ) as client:
        analyzer = OpenAIAnalyzer("unused", client=client)

        with pytest.raises(AnalyzerError) as caught:
            analyzer.analyze_text("680 マック")

    body = seen["body"]
    assert isinstance(body, dict)
    assert body["store"] is False
    assert body["text"]["format"]["type"] == "json_schema"
    schema = body["text"]["format"]["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert "mock failure" not in str(caught.value)
    assert "680 マック" not in str(caught.value)


def test_current_openai_sdk_serializes_inbox_route_schema_without_network():
    seen: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            400,
            json={
                "error": {
                    "message": "private mock response",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": None,
                }
            },
        )

    with OpenAI(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    ) as client:
        analyzer = OpenAIAnalyzer("unused", client=client)

        with pytest.raises(AnalyzerError) as caught:
            analyzer.route_inbox("2026-09-05 15:00に研究計画書を提出")

    body = seen["body"]
    assert isinstance(body, dict)
    assert body["store"] is False
    schema = body["text"]["format"]["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert {
        "destination",
        "game",
        "content",
        "task_title",
        "task_due_date",
        "task_due_time",
        "career_company",
        "career_title",
        "career_kind",
        "career_location",
        "career_stage",
        "career_status",
        "career_deadline_date",
        "career_deadline_time",
        "career_deadline_at",
        "career_next_action",
        "career_next_action_date",
        "career_next_action_time",
        "career_next_action_at",
        "career_url",
        "career_notes",
        "career_source",
        "career_external_id",
        "confidence",
    } == set(schema["properties"])
    assert "private mock response" not in str(caught.value)
    assert "研究計画書" not in str(caught.value)
