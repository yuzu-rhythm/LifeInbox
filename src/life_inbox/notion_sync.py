"""Durable Notion secondary-archive service.

``enqueue_event`` remains the explicit user-promotion boundary used by the
existing review button.  ``enqueue_event_if_eligible`` is the narrow
archive-gated automatic boundary used by the Discord #一般 flow; it evaluates
the same policy and creates no row for an ineligible Event.  Neither method
contacts Notion. ``process_pending`` only handles durable rows and never
discovers Events by category.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import SecretStr
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import Event
from .notion_models import (
    NotionAttachmentRecord,
    NotionSyncRecord,
    NotionSyncStatus,
    ensure_notion_attachment_schema,
)
from .notion_payload import (
    NotionAttachmentWarning,
    NotionEventPlan,
    build_notion_event_plan,
)
from .notion_policy import EligibilityDecision, NotionEligibilityPolicy
from .notion_transport import (
    NotionErrorClassification,
    NotionErrorDisposition,
    NotionFileUploadRequest,
    NotionTransport,
    NotionTransportError,
    classify_notion_error,
    sanitize_error_message,
)


def _utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class NotionSyncAction(StrEnum):
    """Observable result of one local promotion or remote page operation."""

    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


_NOTION_PAGE_HOSTS = frozenset({"notion.so", "www.notion.so", "app.notion.com"})


def _canonical_page_url(page_id: str | None) -> str | None:
    if not page_id:
        return None
    return f"https://www.notion.so/{quote(page_id, safe='')}"


def _safe_page_url(value: object, page_id: str | None) -> str | None:
    """Accept only a credential-free Notion page URL from an adapter result."""

    if isinstance(value, str) and value.strip():
        try:
            parsed = urlsplit(value.strip())
        except ValueError:
            parsed = None
        if parsed is not None:
            host = (parsed.hostname or "").casefold()
            if (
                parsed.scheme.casefold() == "https"
                and (host in _NOTION_PAGE_HOSTS or host.endswith(".notion.site"))
                and parsed.path
                and not parsed.username
                and not parsed.password
            ):
                return urlunsplit(("https", host, parsed.path, "", ""))
    return _canonical_page_url(page_id)


@dataclass(frozen=True, slots=True)
class NotionEnqueueResult:
    event_id: int
    record_id: int
    status: str
    eligible: bool
    reason_code: str
    content_fingerprint: str
    notion_page_id: str | None
    created: bool = False
    changed: bool = False
    action: str = NotionSyncAction.UNCHANGED.value
    notion_page_url: str | None = None

    @property
    def updated(self) -> bool:
        return self.action == NotionSyncAction.UPDATED.value or self.changed

    @property
    def unchanged(self) -> bool:
        return self.action == NotionSyncAction.UNCHANGED.value


@dataclass(frozen=True, slots=True)
class NotionSyncResult:
    event_id: int
    record_id: int
    status: str
    content_fingerprint: str
    notion_page_id: str | None = None
    attempts: int = 0
    warnings: tuple[NotionAttachmentWarning, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    action: str = NotionSyncAction.UNCHANGED.value
    notion_page_url: str | None = None

    @property
    def created(self) -> bool:
        return self.action == NotionSyncAction.CREATED.value

    @property
    def updated(self) -> bool:
        return self.action == NotionSyncAction.UPDATED.value

    @property
    def unchanged(self) -> bool:
        return self.action == NotionSyncAction.UNCHANGED.value


class NotionSyncService:
    """Process only durable rows from explicit or archive-gated promotion."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        settings: Any | None = None,
        *,
        transport: NotionTransport | None = None,
        data_dir: str | Path | None = None,
        policy: NotionEligibilityPolicy | None = None,
        now: Callable[[], datetime] | None = None,
        retry_base_seconds: int = 60,
        max_retry_seconds: int = 3_600,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.transport = transport
        self.data_dir = Path(data_dir or getattr(settings, "data_dir", "data"))
        self.policy = policy or NotionEligibilityPolicy()
        self.now = now or _utc_now_naive
        self.retry_base_seconds = max(1, retry_base_seconds)
        self.max_retry_seconds = max(self.retry_base_seconds, max_retry_seconds)

    def plan_event(self, event: Event) -> NotionEventPlan:
        """Build a preview without changing the database or calling Notion."""

        return build_notion_event_plan(event, self.data_dir, policy=self.policy)

    def enqueue_event(self, event_id: int) -> NotionEnqueueResult:
        """Record one explicit promotion in the local Notion outbox."""

        session = self.session_factory()
        try:
            event = session.get(Event, event_id)
            if event is None:
                raise ValueError(f"event not found: {event_id}")
            plan = self.plan_event(event)
            record = session.scalar(
                select(NotionSyncRecord).where(NotionSyncRecord.event_id == event_id)
            )
            created = record is None
            changed = False
            action = (
                NotionSyncAction.CREATED.value
                if created
                else NotionSyncAction.UNCHANGED.value
            )
            if record is None:
                record = NotionSyncRecord(
                    event_id=event_id,
                    status=(
                        NotionSyncStatus.PENDING.value
                        if plan.eligible
                        else NotionSyncStatus.SKIPPED.value
                    ),
                    content_fingerprint=plan.content_fingerprint,
                )
                if not plan.eligible:
                    self._store_policy_skip(record, plan.decision)
                session.add(record)
            else:
                changed = record.content_fingerprint != plan.content_fingerprint
                if changed:
                    action = NotionSyncAction.UPDATED.value
                    self._reset_record(record, plan)
                    if not plan.eligible:
                        self._store_policy_skip(record, plan.decision)
                elif not plan.eligible:
                    record.status = NotionSyncStatus.SKIPPED.value
                    self._store_policy_skip(record, plan.decision)
                elif record.status in {
                    NotionSyncStatus.FAILED.value,
                    NotionSyncStatus.SKIPPED.value,
                }:
                    # A repeat of the explicit promotion is an intentional
                    # request to make a terminal/previously skipped row pending.
                    record.status = NotionSyncStatus.PENDING.value
                    record.last_error_code = None
                    record.last_error_message = None
                    record.next_attempt_at = None
            session.flush()
            session.commit()
            return NotionEnqueueResult(
                event_id=event_id,
                record_id=record.id,
                status=str(record.status),
                eligible=plan.eligible,
                reason_code=plan.reason_code,
                content_fingerprint=plan.content_fingerprint,
                notion_page_id=record.notion_page_id,
                created=created,
                changed=changed,
                action=action,
                notion_page_url=_canonical_page_url(record.notion_page_id),
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def enqueue_event_if_eligible(self, event_id: int) -> NotionEnqueueResult | None:
        """Queue an eligible Event without creating policy-skip rows.

        The caller is responsible for establishing any workflow-specific
        prerequisite, such as the successful Discord archive copy.  This
        method only adds the existing policy gate to the automatic path; a
        policy-ineligible Event is a normal no-op and is not sent to Notion.
        The final ``enqueue_event`` call repeats the policy evaluation inside
        its own transaction so a concurrent Event change still fails closed.
        """

        session = self.session_factory()
        try:
            event = session.get(Event, event_id)
            if event is None:
                raise ValueError(f"event not found: {event_id}")
            if not self.plan_event(event).eligible:
                return None
        finally:
            session.close()
        return self.enqueue_event(event_id)

    def process_event(self, event_id: int) -> NotionSyncResult:
        """Process an already promoted row; never create a row implicitly."""

        session = self.session_factory()
        try:
            record = session.scalar(
                select(NotionSyncRecord).where(NotionSyncRecord.event_id == event_id)
            )
            if record is None:
                raise ValueError("event has not been explicitly promoted to the Notion archive")
            ensure_notion_attachment_schema(session.get_bind())
            event = record.event
            plan = self.plan_event(event)
            if record.content_fingerprint != plan.content_fingerprint:
                self._reset_record(record, plan)
            else:
                # ``plan_event`` may have used the local file bytes when an
                # older row only stored the attachment metadata hash.
                record.content_fingerprint = plan.content_fingerprint
            if not plan.eligible:
                self._store_policy_skip(record, plan.decision)
                session.commit()
                return self._result(record)
            if record.status in {
                NotionSyncStatus.SYNCED.value,
                NotionSyncStatus.SYNCING.value,
                NotionSyncStatus.SKIPPED.value,
                NotionSyncStatus.FAILED.value,
            }:
                session.commit()
                return self._result(record)
            current_time = self._now()
            if (
                record.status == NotionSyncStatus.RETRY_WAITING.value
                and record.next_attempt_at is not None
                and record.next_attempt_at > current_time
            ):
                session.commit()
                return self._result(record)

            record.status = NotionSyncStatus.SYNCING.value
            record.attempts += 1
            record.last_attempt_at = current_time
            record.next_attempt_at = None
            session.commit()
            warnings: tuple[NotionAttachmentWarning, ...] = ()
            try:
                transport = self._require_transport()
                max_upload_bytes = transport.get_workspace_max_upload_bytes()
                if max_upload_bytes is not None and max_upload_bytes <= 0:
                    raise NotionTransportError(
                        "workspace upload limit is invalid",
                        code="invalid_workspace_limit",
                    )
                refreshed_plan = build_notion_event_plan(
                    event,
                    self.data_dir,
                    max_upload_bytes=max_upload_bytes,
                    policy=self.policy,
                )
                record.content_fingerprint = refreshed_plan.content_fingerprint
                if not refreshed_plan.eligible or refreshed_plan.draft is None:
                    self._store_policy_skip(record, refreshed_plan.decision)
                    session.commit()
                    return self._result(record)
                draft = refreshed_plan.draft
                warnings = draft.warnings
                file_ids: list[str] = []
                for attachment in draft.attachments:
                    if not attachment.uploadable:
                        continue
                    file_id = self._stored_attachment_file_id(
                        session,
                        record_id=record.id,
                        fingerprint=attachment.sha256,
                    )
                    if file_id is None:
                        upload_result = transport.upload_file(
                            NotionFileUploadRequest(
                                path=attachment.path,
                                filename=attachment.filename,
                                content_type=attachment.content_type,
                                size_bytes=attachment.size_bytes,
                            )
                        )
                        file_id = self._remote_id(upload_result, "file_id")
                        if attachment.sha256:
                            session.add(
                                NotionAttachmentRecord(
                                    notion_sync_record_id=record.id,
                                    attachment_id=attachment.attachment_id,
                                    content_fingerprint=attachment.sha256,
                                    notion_file_id=file_id,
                                )
                            )
                            # Make a completed upload reusable if the
                            # following page request is interrupted/rejected.
                            session.flush()
                            session.commit()
                    file_ids.append(file_id)
                request = draft.request(file_ids)
                updating_page = bool(record.notion_page_id)
                if updating_page:
                    page_result = transport.update_page(record.notion_page_id, request)
                else:
                    page_result = transport.create_page(request)
                page_id = self._remote_id(page_result, "page_id")
                page_url = _safe_page_url(getattr(page_result, "url", None), page_id)
                record.notion_page_id = page_id
                record.status = NotionSyncStatus.SYNCED.value
                record.synced_at = self._now()
                record.next_attempt_at = None
                record.last_error_code = None
                record.last_error_message = None
                session.commit()
                return self._result(
                    record,
                    warnings=warnings,
                    action=(
                        NotionSyncAction.UPDATED.value
                        if updating_page
                        else NotionSyncAction.CREATED.value
                    ),
                    notion_page_url=page_url,
                )
            except Exception as error:
                classification = classify_notion_error(
                    error,
                    secrets=self._secret_values(),
                    now=current_time.replace(tzinfo=UTC),
                )
                self._store_failure(record, classification)
                session.commit()
                return self._result(record, warnings=warnings)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def process_pending(self, *, limit: int = 20) -> tuple[NotionSyncResult, ...]:
        """Process due rows already created by explicit user promotions."""

        if limit < 1:
            raise ValueError("limit must be positive")
        session = self.session_factory()
        try:
            current_time = self._now()
            event_ids = session.scalars(
                select(NotionSyncRecord.event_id)
                .where(
                    NotionSyncRecord.status.in_(
                        [
                            NotionSyncStatus.PENDING.value,
                            NotionSyncStatus.RETRY_WAITING.value,
                        ]
                    ),
                    or_(
                        NotionSyncRecord.next_attempt_at.is_(None),
                        NotionSyncRecord.next_attempt_at <= current_time,
                    ),
                )
                .order_by(NotionSyncRecord.created_at, NotionSyncRecord.id)
                .limit(limit)
            ).all()
        finally:
            session.close()
        return tuple(self.process_event(event_id) for event_id in event_ids)

    def _require_transport(self) -> NotionTransport:
        if self.transport is None:
            raise NotionTransportError(
                "Notion transport is not configured",
                code="transport_not_configured",
            )
        return self.transport

    @staticmethod
    def _remote_id(result: object, field_name: str) -> str:
        value = getattr(result, field_name, None)
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise NotionTransportError(
                f"Notion response did not contain a valid {field_name}",
                code="validation_error",
            )
        return value.strip()

    def _stored_attachment_file_id(
        self,
        session: Session,
        *,
        record_id: int,
        fingerprint: str | None,
    ) -> str | None:
        if not fingerprint:
            return None
        mapping = session.scalar(
            select(NotionAttachmentRecord).where(
                NotionAttachmentRecord.notion_sync_record_id == record_id,
                NotionAttachmentRecord.content_fingerprint == fingerprint,
            )
        )
        if mapping is None:
            return None
        return self._remote_id(mapping, "notion_file_id")

    @staticmethod
    def _store_policy_skip(record: NotionSyncRecord, decision: EligibilityDecision) -> None:
        record.status = NotionSyncStatus.SKIPPED.value
        record.last_error_code = decision.reason_code
        record.last_error_message = sanitize_error_message(decision.message)
        record.next_attempt_at = None

    @staticmethod
    def _reset_record(record: NotionSyncRecord, plan: NotionEventPlan) -> None:
        record.content_fingerprint = plan.content_fingerprint
        record.attempts = 0
        record.last_error_code = None
        record.last_error_message = None
        record.next_attempt_at = None
        record.synced_at = None
        record.status = (
            NotionSyncStatus.PENDING.value
            if plan.eligible
            else NotionSyncStatus.SKIPPED.value
        )

    def _store_failure(
        self,
        record: NotionSyncRecord,
        classification: NotionErrorClassification,
    ) -> None:
        record.last_error_code = classification.code
        record.last_error_message = sanitize_error_message(
            classification.message,
            secrets=self._secret_values(),
        )
        if classification.disposition == NotionErrorDisposition.RETRYABLE:
            delay = classification.retry_after_seconds
            if delay is None:
                exponent = min(max(record.attempts - 1, 0), 30)
                delay = min(self.max_retry_seconds, self.retry_base_seconds * (2**exponent))
            delay = min(self.max_retry_seconds, max(0, delay))
            record.status = NotionSyncStatus.RETRY_WAITING.value
            record.next_attempt_at = self._now() + timedelta(seconds=delay)
        else:
            record.status = NotionSyncStatus.FAILED.value
            record.next_attempt_at = None

    def _result(
        self,
        record: NotionSyncRecord,
        *,
        warnings: tuple[NotionAttachmentWarning, ...] = (),
        action: str = NotionSyncAction.UNCHANGED.value,
        notion_page_url: str | None = None,
    ) -> NotionSyncResult:
        return NotionSyncResult(
            event_id=record.event_id,
            record_id=record.id,
            status=str(record.status),
            content_fingerprint=record.content_fingerprint,
            notion_page_id=record.notion_page_id,
            attempts=record.attempts,
            warnings=warnings,
            error_code=record.last_error_code,
            error_message=record.last_error_message,
            action=action,
            notion_page_url=notion_page_url or _canonical_page_url(record.notion_page_id),
        )

    def _secret_values(self) -> tuple[str, ...]:
        token = getattr(self.settings, "notion_api_token", None)
        if isinstance(token, SecretStr):
            value = token.get_secret_value()
        else:
            value = str(token or "")
        return (value,) if value else ()

    def _now(self) -> datetime:
        value = self.now()
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value


__all__ = [
    "NotionEnqueueResult",
    "NotionSyncAction",
    "NotionSyncResult",
    "NotionSyncService",
]
