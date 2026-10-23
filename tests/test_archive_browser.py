from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from life_inbox.config import Settings
from life_inbox.db import Database
from life_inbox.models import Attachment, Event, Expense, Memo
from life_inbox.notion_models import NotionSyncRecord
from life_inbox.web import create_web_app


def _event(
    message_id: str,
    *,
    text: str,
    kind: str,
    status: str = "confirmed",
    minute: int = 0,
) -> Event:
    occurred = datetime(2026, 8, 30, 10, minute, tzinfo=UTC).replace(tzinfo=None)
    return Event(
        discord_message_id=message_id,
        discord_channel_id="channel",
        discord_user_id="user",
        captured_at=occurred,
        occurred_at=occurred,
        raw_text=text,
        source="discord",
        status=status,
        event_type=kind,
        event_kinds=[kind],
    )


def _client(tmp_path: Path) -> tuple[TestClient, Database]:
    database = Database(f"sqlite:///{tmp_path / 'app.db'}")
    database.init()
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
        web_host="127.0.0.1",
        notion_hub_url="https://app.notion.com/p/life-archive",
    )
    return TestClient(create_web_app(settings, database=database)), database


def test_archive_searches_structured_local_events_and_shows_notion_state(tmp_path: Path):
    client, database = _client(tmp_path)
    with database.session() as session:
        expense_event = _event(
            "expense",
            text="680 マック",
            kind="expense",
            minute=1,
        )
        expense_event.expenses.append(
            Expense(
                amount=680,
                merchant_raw="マック",
                merchant_normalized="マクドナルド",
                category="食費",
                subcategory="外食",
                confidence=0.99,
            )
        )
        memo_event = _event(
            "memo",
            text="研究室で確認すること",
            kind="memo",
            minute=2,
        )
        memo_event.memo = Memo(body="研究室で確認すること")
        session.add_all([expense_event, memo_event])
        session.flush()
        session.add(
            NotionSyncRecord(
                event_id=expense_event.id,
                status="synced",
                content_fingerprint="a" * 64,
                notion_page_id="page-id",
            )
        )
        session.commit()

    with client:
        response = client.get("/archive", params={"q": "マクドナルド"})

    assert response.status_code == 200
    assert "マクドナルド" in response.text
    assert "¥680 / 食費 / 外食" in response.text
    assert "Notion同期済み" in response.text
    assert "研究室で確認すること" not in response.text
    assert "https://app.notion.com/p/life-archive" in response.text
    assert "1 / 1件" in response.text


def test_archive_filters_kind_status_and_image_without_losing_source_event(tmp_path: Path):
    client, database = _client(tmp_path)
    attachment_dir = tmp_path / "attachments"
    attachment_dir.mkdir(parents=True)
    image = attachment_dir / "memory.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    with database.session() as session:
        memory = _event(
            "memory",
            text="海で撮った思い出",
            kind="memories",
            status="pending",
            minute=3,
        )
        session.add(memory)
        session.flush()
        session.add(
            Attachment(
                event_id=memory.id,
                discord_attachment_id="memory-image",
                original_filename="memory.png",
                content_type="image/png",
                local_path=str(image),
                sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
            )
        )
        session.add(
            _event(
                "plain",
                text="画像なしメモ",
                kind="memo",
                status="confirmed",
                minute=4,
            )
        )
        session.commit()

    with client:
        response = client.get(
            "/archive",
            params={"kind": "memories", "status": "pending", "media": "with"},
        )

    assert response.status_code == 200
    assert "海で撮った思い出" in response.text
    assert "画像なしメモ" not in response.text
    assert "思い出" in response.text
    assert "確認待ち" in response.text
    assert "/attachments/" in response.text
    assert "Notion未昇格" in response.text


def test_archive_escapes_text_and_normalizes_invalid_filters(tmp_path: Path):
    client, database = _client(tmp_path)
    with database.session() as session:
        session.add(
            _event(
                "escaped",
                text="<script>alert('private')</script>",
                kind="memo",
            )
        )
        session.commit()

    with client:
        response = client.get(
            "/archive",
            params={"status": "not-a-status", "media": "anything", "limit": 999},
        )

    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "最大100件表示" in response.text
    assert "無限スクロールなし" in response.text
