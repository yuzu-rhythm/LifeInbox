from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from itertools import count
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from life_inbox.config import Settings
from life_inbox.constants import EventKind, EventStatus
from life_inbox.models import (
    Attachment,
    Event,
    EventCaptureContext,
    Expense,
    PlannerImageItem,
    ReceiptItem,
)
from life_inbox.notion_models import (
    NotionAttachmentRecord,
    NotionSyncRecord,
    NotionSyncStatus,
    ensure_notion_attachment_schema,
)
from life_inbox.notion_payload import resolve_safe_attachment_path
from life_inbox.notion_policy import NotionEligibilityPolicy
from life_inbox.notion_sync import NotionSyncService
from life_inbox.notion_transport import (
    NotionErrorDisposition,
    NotionFileResult,
    NotionPageRequest,
    NotionPageResult,
    NotionTransportError,
    NotionValidationError,
    classify_notion_error,
    sanitize_error_message,
)

_message_numbers = count(1)


def _event(
    session,
    *,
    event_type: str = EventKind.MEMO.value,
    event_kinds: list[str] | None = None,
    status: str = EventStatus.CONFIRMED.value,
    raw_text: str = "保存しておくメモ",
) -> Event:
    captured_at = datetime(2026, 8, 30, 12, 0)
    event = Event(
        discord_message_id=f"message-{next(_message_numbers)}",
        discord_channel_id="channel",
        discord_user_id="user",
        captured_at=captured_at,
        occurred_at=captured_at,
        raw_text=raw_text,
        source="discord",
        status=status,
        event_type=event_type,
        event_kinds=event_kinds if event_kinds is not None else [event_type],
    )
    session.add(event)
    session.commit()
    return event


def _transient_event(
    *,
    event_type: str,
    event_kinds: list[str] | None = None,
    status: str = EventStatus.CONFIRMED.value,
    raw_text: str = "内容",
) -> Event:
    event = Event(
        id=1,
        discord_message_id="transient-message",
        discord_channel_id="channel",
        discord_user_id="user",
        captured_at=datetime(2026, 8, 30, 12, 0),
        occurred_at=datetime(2026, 8, 30, 12, 0),
        raw_text=raw_text,
        source="discord",
        status=status,
        event_type=event_type,
        event_kinds=event_kinds if event_kinds is not None else [event_type],
    )
    return event


class FakeTransport:
    def __init__(self, *, max_upload_bytes: int | None = None) -> None:
        self.max_upload_bytes = max_upload_bytes
        self.upload_requests = []
        self.create_requests: list[NotionPageRequest] = []
        self.update_requests: list[tuple[str, NotionPageRequest]] = []
        self.create_error: Exception | None = None
        self.update_error: Exception | None = None
        self.page_number = 0
        self.page_url: str | None = None

    def get_workspace_max_upload_bytes(self) -> int | None:
        return self.max_upload_bytes

    def upload_file(self, request):
        self.upload_requests.append(request)
        return NotionFileResult(f"file-{len(self.upload_requests)}")

    def create_page(self, request: NotionPageRequest) -> NotionPageResult:
        if self.create_error is not None:
            raise self.create_error
        self.create_requests.append(request)
        self.page_number += 1
        return NotionPageResult(f"page-{self.page_number}", self.page_url)

    def update_page(self, page_id: str, request: NotionPageRequest) -> NotionPageResult:
        if self.update_error is not None:
            raise self.update_error
        self.update_requests.append((page_id, request))
        return NotionPageResult(page_id, self.page_url)


def test_additive_notion_schema_is_registered(db):
    ensure_notion_attachment_schema(db.engine)
    tables = set(inspect(db.engine).get_table_names())
    assert "notion_sync_records" in tables
    assert "notion_sync_attachments" in tables
    columns = {column["name"] for column in inspect(db.engine).get_columns("notion_sync_records")}
    assert {
        "event_id",
        "status",
        "attempts",
        "last_error_code",
        "last_error_message",
        "next_attempt_at",
        "notion_page_id",
        "content_fingerprint",
        "last_attempt_at",
        "synced_at",
    }.issubset(columns)
    attachment_columns = {
        column["name"] for column in inspect(db.engine).get_columns("notion_sync_attachments")
    }
    assert {
        "notion_sync_record_id",
        "attachment_id",
        "content_fingerprint",
        "notion_file_id",
    }.issubset(attachment_columns)


def test_notion_settings_are_optional_and_safe_summary_never_contains_token():
    sentinel = "LOCAL_TEST_SENTINEL"
    settings = Settings(
        _env_file=None,
        notion_api_token=sentinel,
        notion_parent_page_id="parent-id",
    )
    summary = settings.safe_summary()
    assert summary["notion_configured"] is True
    assert sentinel not in repr(summary)

    disabled = Settings(_env_file=None)
    assert disabled.notion_sync_enabled is False
    assert disabled.safe_summary()["notion_configured"] is False


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        (EventKind.MEMO.value, True),
        (EventKind.MEAL.value, True),
        (EventKind.RHYTHM_RESULT.value, True),
        ("career", True),
        ("dining_out", True),
        ("memories", True),
        ("saved_link", True),
        ("expense", True),
        ("finance_account", False),
        ("card", False),
        ("balance", False),
        ("monologue", True),
        ("unknown", False),
        (EventKind.RHYTHM_TODO.value, True),
    ],
)
def test_policy_allowlist_and_exclusions(event_type: str, expected: bool):
    decision = NotionEligibilityPolicy().evaluate(_transient_event(event_type=event_type))
    assert decision.eligible is expected


def test_policy_requires_confirmation_and_always_excludes_kiseki_location_data():
    policy = NotionEligibilityPolicy()
    pending = policy.evaluate(
        _transient_event(event_type=EventKind.MEMO.value, status=EventStatus.PENDING.value)
    )
    kiseki = policy.evaluate(
        _transient_event(
            event_type=EventKind.MEMO.value,
            raw_text="Kiseki raw GPS full archive: latitude and longitude",
        )
    )
    coordinates = policy.evaluate(
        _transient_event(event_type=EventKind.MEMO.value, raw_text="35.681236, 139.767125")
    )
    raw_kiseki_archive = _transient_event(event_type=EventKind.MEMO.value)
    raw_kiseki_archive.source = "kiseki"
    safe_kiseki_moment = _transient_event(event_type="memo+kiseki_moment")
    safe_kiseki_moment.source = "kiseki"
    raw_kiseki_decision = policy.evaluate(raw_kiseki_archive)
    safe_kiseki_decision = policy.evaluate(safe_kiseki_moment)
    assert pending.reason_code == "not_confirmed"
    assert kiseki.reason_code == "sensitive_kiseki_data"
    assert kiseki.eligible is False
    assert coordinates.reason_code == "sensitive_kiseki_data"
    assert raw_kiseki_decision.reason_code == "sensitive_kiseki_data"
    assert safe_kiseki_decision.eligible is True


def test_process_pending_does_not_auto_enqueue_by_category(db, tmp_path: Path):
    with db.session() as session:
        event = _event(session, event_type=EventKind.MEMO.value)
        event_id = event.id
    service = NotionSyncService(
        db.session_factory,
        data_dir=tmp_path,
        transport=FakeTransport(),
    )

    assert service.process_pending() == ()
    with pytest.raises(ValueError, match="explicitly promoted"):
        service.process_event(event_id)
    with db.session() as session:
        assert session.scalars(select(NotionSyncRecord)).all() == []


def test_enqueue_is_idempotent_for_same_fingerprint(db, tmp_path: Path):
    with db.session() as session:
        event_id = _event(session).id
    service = NotionSyncService(db.session_factory, data_dir=tmp_path, transport=FakeTransport())

    first = service.enqueue_event(event_id)
    second = service.enqueue_event(event_id)
    assert first.created is True
    assert first.action == "created"
    assert second.created is False
    assert second.changed is False
    assert second.action == "unchanged"
    assert second.unchanged is True
    with db.session() as session:
        records = session.scalars(select(NotionSyncRecord)).all()
        assert len(records) == 1
        assert records[0].status == NotionSyncStatus.PENDING.value


def test_page_draft_contains_life_archive_database_metadata(db, tmp_path: Path):
    with db.session() as session:
        event_id = _event(session, raw_text="あとで読み返すメモ").id
    service = NotionSyncService(db.session_factory, data_dir=tmp_path)

    with db.session() as session:
        event = session.get(Event, event_id)
        plan = service.plan_event(event)

    assert plan.draft is not None
    properties = dict(plan.draft.properties)
    assert properties["Local Event ID"] == str(event_id)
    assert properties["Source"] == "discord"
    assert properties["Category"] == "Memo"
    assert properties["State"] == "Promoted"
    assert properties["Occurred At"].startswith("2026-08-30T12:00:00")
    assert properties["Summary"] == "あとで読み返すメモ"
    assert properties["Has Media"] == "false"


def test_notion_draft_contains_discord_time_route_planner_and_explicit_location(
    db,
    tmp_path: Path,
):
    with db.session() as session:
        event = _event(
            session,
            event_type="memories",
            raw_text="友人と豊橋で過ごした",
        )
        event.capture_context = EventCaptureContext(
            capture_type="normal",
            source_label="actionButton",
            latitude=34.7692,
            longitude=137.3915,
            horizontal_accuracy_meters=15.0,
            location_label="豊橋駅付近",
            location_shared_at=datetime(2026, 8, 30, 12, 0),
            route_category="memories",
            destination_channel_id="40",
            destination_message_id="4001",
        )
        event.planner_items.append(
            PlannerImageItem(
                item_index=0,
                raw_text="友人との思い出を残す",
                category="memories",
                confidence=0.96,
                candidate_categories=["memories", "memo"],
                status="posted",
                destination_channel_id="40",
                destination_message_id="4001",
            )
        )
        session.commit()
        event_id = event.id

    service = NotionSyncService(db.session_factory, data_dir=tmp_path)
    with db.session() as session:
        plan = service.plan_event(session.get(Event, event_id))

    assert plan.draft is not None
    properties = dict(plan.draft.properties)
    assert properties["Category"] == "Memory"
    assert properties["Occurred At"] == "2026-08-30T12:00:00"
    assert "### Discord Capture" in plan.draft.body_markdown
    assert '"captured_at": "2026-08-30T12:00:00"' in plan.draft.body_markdown
    assert '"destination_channel_id": "40"' in plan.draft.body_markdown
    assert "### Explicit Capture Location" in plan.draft.body_markdown
    assert "豊橋駅付近" in plan.draft.body_markdown
    assert "### Planner Items" in plan.draft.body_markdown
    assert "continuous tracking" in plan.draft.body_markdown


def test_notion_receipt_backup_contains_amount_and_items(db, tmp_path: Path):
    with db.session() as session:
        event = _event(
            session,
            event_type=EventKind.EXPENSE.value,
            raw_text="レシートを保存",
        )
        expense = Expense(
            event_id=event.id,
            amount=680,
            merchant_raw="マクドナルド",
            merchant_normalized="マクドナルド",
            category="食費",
            subcategory="外食",
            confidence=0.98,
        )
        expense.receipt_items.append(
            ReceiptItem(name="セット", quantity=1, unit_price=680, amount=680)
        )
        session.add(expense)
        session.commit()
        event_id = event.id

    service = NotionSyncService(db.session_factory, data_dir=tmp_path)
    with db.session() as session:
        plan = service.plan_event(session.get(Event, event_id))

    assert plan.eligible is True
    assert plan.draft is not None
    assert dict(plan.draft.properties)["Category"] == "Expenses"
    assert '"amount_yen": 680' in plan.draft.body_markdown
    assert '"name": "セット"' in plan.draft.body_markdown


def test_changed_fingerprint_updates_the_same_page_mapping(db, tmp_path: Path):
    transport = FakeTransport()
    transport.page_url = "https://www.notion.so/life-archive-page"
    service = NotionSyncService(db.session_factory, data_dir=tmp_path, transport=transport)
    with db.session() as session:
        event_id = _event(session, raw_text="最初の本文").id

    service.enqueue_event(event_id)
    first = service.process_event(event_id)
    with db.session() as session:
        event = session.get(Event, event_id)
        event.raw_text = "更新された本文"
        session.commit()

    second = service.process_event(event_id)
    assert first.notion_page_id == "page-1"
    assert first.action == "created"
    assert first.notion_page_url == "https://www.notion.so/life-archive-page"
    assert second.notion_page_id == "page-1"
    assert second.action == "updated"
    assert second.notion_page_url == "https://www.notion.so/life-archive-page"
    assert len(transport.create_requests) == 1
    assert [page_id for page_id, _request in transport.update_requests] == ["page-1"]
    with db.session() as session:
        record = session.scalar(
            select(NotionSyncRecord).where(NotionSyncRecord.event_id == event_id)
        )
        assert record.notion_page_id == "page-1"
        assert record.content_fingerprint == second.content_fingerprint


def test_repeating_synced_event_is_unchanged_without_remote_calls(db, tmp_path: Path):
    transport = FakeTransport()
    service = NotionSyncService(db.session_factory, data_dir=tmp_path, transport=transport)
    with db.session() as session:
        event_id = _event(session).id

    service.enqueue_event(event_id)
    first = service.process_event(event_id)
    second = service.process_event(event_id)

    assert first.action == "created"
    assert second.action == "unchanged"
    assert second.unchanged is True
    assert len(transport.create_requests) == 1
    assert transport.update_requests == []
    assert transport.upload_requests == []


def test_attachment_fingerprint_reuses_upload_for_text_change_and_reuploads_changed_bytes(
    db,
    tmp_path: Path,
):
    attachments_dir = tmp_path / "attachments"
    attachments_dir.mkdir()
    original = attachments_dir / "memory.png"
    original.write_bytes(b"image-v1")
    original_digest = hashlib.sha256(original.read_bytes()).hexdigest()
    transport = FakeTransport()
    service = NotionSyncService(db.session_factory, data_dir=tmp_path, transport=transport)
    with db.session() as session:
        event = _event(session, raw_text="本文 v1")
        event.attachments.append(
            Attachment(
                discord_attachment_id="attachment-fingerprint",
                original_filename="memory.png",
                content_type="image/png",
                local_path=str(original),
                sha256=original_digest,
            )
        )
        session.commit()
        event_id = event.id

    service.enqueue_event(event_id)
    first = service.process_event(event_id)
    with db.session() as session:
        event = session.get(Event, event_id)
        event.raw_text = "本文 v2"
        session.commit()
    text_changed = service.process_event(event_id)

    assert first.action == "created"
    assert text_changed.action == "updated"
    assert len(transport.upload_requests) == 1
    assert transport.create_requests[0].file_ids == ("file-1",)
    assert transport.update_requests[0][1].file_ids == ("file-1",)

    original.write_bytes(b"image-v2")
    bytes_changed = service.process_event(event_id)

    assert bytes_changed.action == "updated"
    assert len(transport.upload_requests) == 2
    assert transport.update_requests[-1][1].file_ids == ("file-2",)
    assert bytes_changed.content_fingerprint != text_changed.content_fingerprint
    with db.session() as session:
        mappings = session.scalars(select(NotionAttachmentRecord)).all()
        event = session.get(Event, event_id)
        assert len(mappings) == 2
        assert event.attachments[0].sha256 == original_digest
        assert original.read_bytes() == b"image-v2"


@pytest.mark.parametrize(
    ("error", "disposition", "code"),
    [
        (
            NotionTransportError("rate limited", status_code=429, headers={"Retry-After": "7"}),
            NotionErrorDisposition.RETRYABLE,
            "rate_limited",
        ),
        (
            NotionTransportError("server unavailable", status_code=503),
            NotionErrorDisposition.RETRYABLE,
            "server_error",
        ),
        (
            NotionTransportError("unauthorized", status_code=401),
            NotionErrorDisposition.TERMINAL,
            "unauthorized",
        ),
        (
            NotionTransportError("forbidden", status_code=403),
            NotionErrorDisposition.TERMINAL,
            "forbidden",
        ),
        (
            NotionValidationError("invalid page properties"),
            NotionErrorDisposition.TERMINAL,
            "validation_error",
        ),
    ],
)
def test_notion_error_classification(error, disposition: str, code: str):
    classification = classify_notion_error(error)
    assert classification.disposition == disposition
    assert classification.code == code
    if code == "rate_limited":
        assert classification.retry_after_seconds == 7


def test_transport_errors_are_redacted_before_result_storage(db, tmp_path: Path):
    sentinel = "LOCAL_TEST_SENTINEL"
    transport = FakeTransport()
    transport.create_error = NotionTransportError(
        f"Authorization: Bearer {sentinel}",
        status_code=503,
    )
    service = NotionSyncService(db.session_factory, data_dir=tmp_path, transport=transport)
    with db.session() as session:
        event_id = _event(session).id
    service.enqueue_event(event_id)

    result = service.process_event(event_id)
    assert result.status == NotionSyncStatus.RETRY_WAITING.value
    assert sentinel not in (result.error_message or "")
    assert sentinel not in sanitize_error_message(f'{{"token": "{sentinel}"}}')
    with db.session() as session:
        record = session.scalar(
            select(NotionSyncRecord).where(NotionSyncRecord.event_id == event_id)
        )
        assert sentinel not in (record.last_error_message or "")


def test_attachment_path_must_stay_under_data_dir_attachments(tmp_path: Path):
    attachments_dir = tmp_path / "attachments"
    attachments_dir.mkdir()
    allowed = attachments_dir / "allowed.png"
    allowed.write_bytes(b"image")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"private")

    assert resolve_safe_attachment_path(tmp_path, allowed) == allowed.resolve()
    assert resolve_safe_attachment_path(tmp_path, outside) is None
    symlink = attachments_dir / "outside-link.png"
    try:
        symlink.symlink_to(outside)
    except OSError:
        symlink = None
    if symlink is not None:
        assert resolve_safe_attachment_path(tmp_path, symlink) is None


def test_oversized_attachment_is_skipped_without_losing_local_original(db, tmp_path: Path):
    attachments_dir = tmp_path / "attachments"
    attachments_dir.mkdir()
    original = attachments_dir / "receipt.png"
    original.write_bytes(b"1234")
    transport = FakeTransport(max_upload_bytes=3)
    service = NotionSyncService(db.session_factory, data_dir=tmp_path, transport=transport)
    with db.session() as session:
        event = _event(session)
        event.attachments.append(
            Attachment(
                discord_attachment_id="attachment-1",
                original_filename="receipt.png",
                content_type="image/png",
                local_path=str(original),
                sha256=hashlib.sha256(original.read_bytes()).hexdigest(),
            )
        )
        session.commit()
        event_id = event.id
    service.enqueue_event(event_id)

    result = service.process_event(event_id)
    assert result.status == NotionSyncStatus.SYNCED.value
    assert [warning.reason_code for warning in result.warnings] == ["attachment_oversized"]
    assert transport.upload_requests == []
    assert transport.create_requests[0].file_ids == ()
    assert original.read_bytes() == b"1234"


def test_sync_failure_keeps_local_event_and_uses_retry_after(db, tmp_path: Path):
    clock = [datetime(2026, 8, 30, 12, 0)]
    attachments_dir = tmp_path / "attachments"
    attachments_dir.mkdir()
    original = attachments_dir / "retry.png"
    original.write_bytes(b"retry-image")
    transport = FakeTransport()
    transport.create_error = NotionTransportError(
        "provider unavailable",
        status_code=503,
        headers={"Retry-After": "5"},
    )
    service = NotionSyncService(
        db.session_factory,
        data_dir=tmp_path,
        transport=transport,
        now=lambda: clock[0],
    )
    with db.session() as session:
        event = _event(session, raw_text="canonical local text")
        event.attachments.append(
            Attachment(
                discord_attachment_id="attachment-retry",
                original_filename="retry.png",
                content_type="image/png",
                local_path=str(original),
                sha256=hashlib.sha256(original.read_bytes()).hexdigest(),
            )
        )
        session.commit()
        event_id = event.id
        before = (
            event.raw_text,
            event.status,
            event.event_type,
            tuple(event.event_kinds),
            original.read_bytes(),
        )
    service.enqueue_event(event_id)

    result = service.process_event(event_id)
    assert result.status == NotionSyncStatus.RETRY_WAITING.value
    assert result.action == "unchanged"
    assert len(transport.upload_requests) == 1
    assert transport.create_requests == []
    with db.session() as session:
        event = session.get(Event, event_id)
        record = session.scalar(
            select(NotionSyncRecord).where(NotionSyncRecord.event_id == event_id)
        )
        mappings = session.scalars(select(NotionAttachmentRecord)).all()
        after = (
            event.raw_text,
            event.status,
            event.event_type,
            tuple(event.event_kinds),
            original.read_bytes(),
        )
        assert after == before
        assert record.attempts == 1
        assert record.next_attempt_at == clock[0] + timedelta(seconds=5)
        assert len(mappings) == 1

    transport.create_error = None
    clock[0] += timedelta(seconds=5)
    retried = service.process_event(event_id)

    assert retried.status == NotionSyncStatus.SYNCED.value
    assert retried.action == "created"
    assert retried.notion_page_url == "https://www.notion.so/page-1"
    assert len(transport.upload_requests) == 1
    assert len(transport.create_requests) == 1
    assert transport.create_requests[0].file_ids == ("file-1",)
    assert original.read_bytes() == b"retry-image"
