"""Privacy-safe, revision-aware Kiseki Moment persistence.

This module is deliberately an importable domain/persistence seam.  It does
not define an HTTP route, authenticate a device, call Kiseki, or publish to
Discord/Notion.  A caller must provide an already privacy-processed Moment;
the service stores only its bounded semantic content in the existing local
``Event``/``Memo`` shape and retains a hash for revision conflict detection.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .constants import EventKind, EventStatus
from .kiseki_models import KisekiMomentSyncRecord
from .models import Event, Memo

KISEKI_EVENT_SOURCE = "kiseki"
KISEKI_EVENT_CHANNEL_ID = "kiseki-sync"
KISEKI_EVENT_USER_ID = "kiseki"
KISEKI_EVENT_TYPE = "memo+kiseki_moment"
KISEKI_EVENT_KINDS = (EventKind.MEMO.value, "kiseki_moment")
_ORIGIN_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,31}$")


class KisekiMomentPayload(BaseModel):
    """The only active Moment shape accepted by the sync boundary.

    ``extra='forbid'`` is intentional: a caller cannot quietly add a route,
    coordinate, address, privacy-zone, or other location field to this
    boundary.  ``privacyProcessed`` is a literal rather than a loose boolean
    so false, missing, and truthy non-boolean values are all rejected.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    moment_id: StrictStr = Field(alias="momentId")
    revision: StrictInt = Field(ge=1)
    occurred_at: datetime = Field(alias="occurredAt")
    title: StrictStr = Field(min_length=1, max_length=200)
    note: StrictStr = Field(default="", max_length=10_000)
    mood: StrictInt | None = Field(default=None, ge=1, le=5)
    favorite: StrictBool = False
    origin: StrictStr = Field(default="iphone", min_length=1, max_length=32)
    privacy_processed: Literal[True] = Field(alias="privacyProcessed")

    @field_validator("moment_id", mode="before")
    @classmethod
    def _normalize_moment_id(cls, value: object) -> str:
        if isinstance(value, UUID):
            return str(value)
        if not isinstance(value, str):
            raise ValueError("momentId must be a UUID")
        try:
            return str(UUID(value.strip()))
        except (AttributeError, ValueError) as exc:
            raise ValueError("momentId must be a UUID") from exc

    @field_validator("occurred_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurredAt must include a timezone offset")
        return value.astimezone(UTC)

    @field_validator("title")
    @classmethod
    def _normalize_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("title must not be blank")
        if len(normalized) > 200:
            raise ValueError("title must be at most 200 characters")
        return normalized

    @field_validator("note")
    @classmethod
    def _normalize_note(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) > 10_000:
            raise ValueError("note must be at most 10000 characters")
        return normalized

    @field_validator("origin")
    @classmethod
    def _validate_origin(cls, value: str) -> str:
        normalized = value.strip()
        if not _ORIGIN_PATTERN.fullmatch(normalized):
            raise ValueError(
                "origin must start with an ASCII letter and contain at most 32 safe characters"
            )
        return normalized


class KisekiMomentDeletePayload(BaseModel):
    """The privacy-attested, content-free tombstone request shape."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    moment_id: StrictStr = Field(alias="momentId")
    revision: StrictInt = Field(ge=1)
    privacy_processed: Literal[True] = Field(alias="privacyProcessed")

    @field_validator("moment_id", mode="before")
    @classmethod
    def _normalize_moment_id(cls, value: object) -> str:
        if isinstance(value, UUID):
            return str(value)
        if not isinstance(value, str):
            raise ValueError("momentId must be a UUID")
        try:
            return str(UUID(value.strip()))
        except (AttributeError, ValueError) as exc:
            raise ValueError("momentId must be a UUID") from exc


# A concise alias is useful to route adapters while keeping the explicit name
# available for callers that distinguish active and delete payloads.
KisekiDeletePayload = KisekiMomentDeletePayload


class KisekiRevisionError(ValueError):
    """Base class for rejected Kiseki revisions."""


class StaleKisekiMomentRevisionError(KisekiRevisionError):
    """The incoming revision is older than the durable mapping."""


class KisekiMomentRevisionConflictError(KisekiRevisionError):
    """The incoming revision has different semantic content."""


# These shorter names make the error seam easy to discover without changing
# the more descriptive canonical class names above.
StaleKisekiRevisionError = StaleKisekiMomentRevisionError
KisekiRevisionConflictError = KisekiMomentRevisionConflictError


@dataclass(frozen=True, slots=True)
class KisekiMomentRecord:
    """Safe read model returned by the sync service."""

    id: int
    moment_id: str
    event_id: int
    revision: int
    semantic_fingerprint: str
    deleted_at: datetime | None
    synced_at: datetime
    created_at: datetime
    updated_at: datetime

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def content_sha256(self) -> str:
        return self.semantic_fingerprint


@dataclass(frozen=True, slots=True)
class KisekiSyncResult:
    """Result flags distinguish a first write, a no-op retry, and an update."""

    record: KisekiMomentRecord
    created: bool
    idempotent: bool

    @property
    def event_id(self) -> int:
        return self.record.event_id

    @property
    def deleted(self) -> bool:
        return self.record.deleted


def _utc_naive(value: datetime | None = None) -> datetime:
    """Convert timestamps to the repository's UTC-naive SQLite convention."""

    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        return resolved
    return resolved.astimezone(UTC).replace(tzinfo=None)


def _canonical_uuid(value: str | UUID) -> str:
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str):
        raise ValueError("momentId must be a UUID")
    try:
        return str(UUID(value.strip()))
    except (AttributeError, ValueError) as exc:
        raise ValueError("momentId must be a UUID") from exc


def _canonical_json_sha256(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def kiseki_content_sha256(payload: KisekiMomentPayload) -> str:
    """Hash semantic Moment content, deliberately excluding sync timestamps."""

    return _canonical_json_sha256(
        {
            "kind": "kiseki_moment",
            "momentId": payload.moment_id,
            "occurredAt": payload.occurred_at.isoformat(),
            "title": payload.title,
            "note": payload.note,
            "mood": payload.mood,
            "favorite": payload.favorite,
            "origin": payload.origin,
            "deleted": False,
        }
    )


def kiseki_delete_content_sha256(moment_id: str | UUID) -> str:
    """Hash a tombstone identity without including its arrival timestamp."""

    return _canonical_json_sha256(
        {
            "kind": "kiseki_moment",
            "momentId": _canonical_uuid(moment_id),
            "deleted": True,
        }
    )


# Existing code calls these values semantic fingerprints; retain both names
# for the domain seam without adding a second hash implementation.
kiseki_semantic_fingerprint = kiseki_content_sha256


def _memo_body(payload: KisekiMomentPayload) -> str:
    if payload.note:
        return f"{payload.title}\n{payload.note}"
    return payload.title


def _record(row: KisekiMomentSyncRecord) -> KisekiMomentRecord:
    return KisekiMomentRecord(
        id=row.id,
        moment_id=row.moment_id,
        event_id=row.event_id,
        revision=row.revision,
        semantic_fingerprint=row.semantic_fingerprint,
        deleted_at=row.deleted_at,
        synced_at=row.synced_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _coerce_payload(payload: KisekiMomentPayload | Mapping[str, object]) -> KisekiMomentPayload:
    if isinstance(payload, KisekiMomentPayload):
        return payload
    return KisekiMomentPayload.model_validate(payload)


def _coerce_delete_payload(
    payload: KisekiMomentDeletePayload | Mapping[str, object],
) -> KisekiMomentDeletePayload:
    if isinstance(payload, KisekiMomentDeletePayload):
        return payload
    return KisekiMomentDeletePayload.model_validate(payload)


class KisekiSyncService:
    """Persist selected privacy-processed Moments with revision semantics."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    @staticmethod
    def _new_event(*, occurred_at: datetime, synced_at: datetime, deleted: bool) -> Event:
        return Event(
            discord_message_id="",
            discord_channel_id=KISEKI_EVENT_CHANNEL_ID,
            discord_user_id=KISEKI_EVENT_USER_ID,
            captured_at=synced_at,
            occurred_at=occurred_at,
            raw_text="",
            source=KISEKI_EVENT_SOURCE,
            status=EventStatus.IGNORED.value if deleted else EventStatus.CONFIRMED.value,
            event_type=KISEKI_EVENT_TYPE,
            event_kinds=list(KISEKI_EVENT_KINDS),
            created_at=synced_at,
            updated_at=synced_at,
        )

    @staticmethod
    def _event_message_id(moment_id: str) -> str:
        return f"kiseki-moment:{moment_id}"

    @staticmethod
    def _set_active_event(
        session: Session,
        event: Event,
        payload: KisekiMomentPayload,
        synced_at: datetime,
    ) -> None:
        body = _memo_body(payload)
        event.discord_message_id = KisekiSyncService._event_message_id(payload.moment_id)
        event.discord_channel_id = KISEKI_EVENT_CHANNEL_ID
        event.discord_user_id = KISEKI_EVENT_USER_ID
        event.captured_at = event.captured_at or synced_at
        event.occurred_at = _utc_naive(payload.occurred_at)
        event.raw_text = body
        event.source = KISEKI_EVENT_SOURCE
        event.status = EventStatus.CONFIRMED.value
        event.event_type = KISEKI_EVENT_TYPE
        event.event_kinds = list(KISEKI_EVENT_KINDS)
        event.updated_at = synced_at
        if event.memo is None:
            event.memo = Memo(body=body, created_at=synced_at, updated_at=synced_at)
        else:
            event.memo.body = body
            event.memo.updated_at = synced_at
        session.flush()

    @staticmethod
    def _set_deleted_event(event: Event, synced_at: datetime) -> None:
        event.raw_text = ""
        if event.memo is not None:
            # Do not blank Memo.body: its database constraint requires a
            # non-empty body.  Removing the orphan clears the personal text.
            event.memo = None
        event.status = EventStatus.IGNORED.value
        event.updated_at = synced_at

    def upsert_moment(
        self,
        payload: KisekiMomentPayload | Mapping[str, object],
        *,
        synced_at: datetime | None = None,
    ) -> KisekiSyncResult:
        """Create or revision-update one active privacy-processed Moment."""

        normalized = _coerce_payload(payload)
        sync_timestamp = _utc_naive(synced_at)
        fingerprint = kiseki_content_sha256(normalized)
        session = self.session_factory()
        try:
            row = session.scalar(
                select(KisekiMomentSyncRecord).where(
                    KisekiMomentSyncRecord.moment_id == normalized.moment_id
                )
            )
            if row is None:
                event = self._new_event(
                    occurred_at=_utc_naive(normalized.occurred_at),
                    synced_at=sync_timestamp,
                    deleted=False,
                )
                event.discord_message_id = self._event_message_id(normalized.moment_id)
                session.add(event)
                session.flush()
                self._set_active_event(session, event, normalized, sync_timestamp)
                row = KisekiMomentSyncRecord(
                    moment_id=normalized.moment_id,
                    event_id=event.id,
                    revision=normalized.revision,
                    semantic_fingerprint=fingerprint,
                    deleted_at=None,
                    synced_at=sync_timestamp,
                    created_at=sync_timestamp,
                    updated_at=sync_timestamp,
                )
                session.add(row)
                session.flush()
                record = _record(row)
                session.commit()
                return KisekiSyncResult(record=record, created=True, idempotent=False)

            if normalized.revision < row.revision:
                raise StaleKisekiMomentRevisionError("an older Kiseki Moment revision was rejected")
            if normalized.revision == row.revision:
                if row.semantic_fingerprint != fingerprint:
                    raise KisekiMomentRevisionConflictError(
                        "the same Kiseki Moment revision has different content"
                    )
                return KisekiSyncResult(record=_record(row), created=False, idempotent=True)

            event = session.get(Event, row.event_id)
            if event is None:
                raise RuntimeError("Kiseki mapping points to a missing Event")
            self._set_active_event(session, event, normalized, sync_timestamp)
            row.revision = normalized.revision
            row.semantic_fingerprint = fingerprint
            row.deleted_at = None
            row.synced_at = sync_timestamp
            row.updated_at = sync_timestamp
            session.flush()
            record = _record(row)
            session.commit()
            return KisekiSyncResult(record=record, created=False, idempotent=False)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def sync_moment(
        self,
        payload: KisekiMomentPayload | Mapping[str, object],
        *,
        synced_at: datetime | None = None,
    ) -> KisekiSyncResult:
        """Explicitly named alias for route/import adapters."""

        return self.upsert_moment(payload, synced_at=synced_at)

    def upsert(
        self,
        payload: KisekiMomentPayload | Mapping[str, object],
        *,
        synced_at: datetime | None = None,
    ) -> KisekiSyncResult:
        return self.upsert_moment(payload, synced_at=synced_at)

    def delete_moment(
        self,
        moment_id: str | UUID | KisekiMomentDeletePayload | Mapping[str, object],
        revision: int | None = None,
        *,
        privacy_processed: bool = True,
        synced_at: datetime | None = None,
    ) -> KisekiSyncResult:
        """Apply a content-free tombstone and retain the same Event mapping."""

        if isinstance(moment_id, KisekiMomentDeletePayload) or isinstance(moment_id, Mapping):
            if revision is not None:
                raise ValueError("revision must be omitted when passing a delete payload")
            normalized_delete = _coerce_delete_payload(moment_id)
        else:
            if revision is None:
                raise ValueError("revision is required")
            normalized_delete = KisekiMomentDeletePayload(
                momentId=_canonical_uuid(moment_id),
                revision=revision,
                privacyProcessed=privacy_processed,
            )

        sync_timestamp = _utc_naive(synced_at)
        fingerprint = kiseki_delete_content_sha256(normalized_delete.moment_id)
        session = self.session_factory()
        try:
            row = session.scalar(
                select(KisekiMomentSyncRecord).where(
                    KisekiMomentSyncRecord.moment_id == normalized_delete.moment_id
                )
            )
            if row is None:
                event = self._new_event(
                    occurred_at=sync_timestamp,
                    synced_at=sync_timestamp,
                    deleted=True,
                )
                event.discord_message_id = self._event_message_id(normalized_delete.moment_id)
                session.add(event)
                session.flush()
                row = KisekiMomentSyncRecord(
                    moment_id=normalized_delete.moment_id,
                    event_id=event.id,
                    revision=normalized_delete.revision,
                    semantic_fingerprint=fingerprint,
                    deleted_at=sync_timestamp,
                    synced_at=sync_timestamp,
                    created_at=sync_timestamp,
                    updated_at=sync_timestamp,
                )
                session.add(row)
                session.flush()
                record = _record(row)
                session.commit()
                return KisekiSyncResult(record=record, created=True, idempotent=False)

            if normalized_delete.revision < row.revision:
                raise StaleKisekiMomentRevisionError(
                    "an older Kiseki deletion revision was rejected"
                )
            if normalized_delete.revision == row.revision:
                if row.semantic_fingerprint != fingerprint:
                    raise KisekiMomentRevisionConflictError(
                        "the same Kiseki Moment revision has different content"
                    )
                return KisekiSyncResult(record=_record(row), created=False, idempotent=True)

            event = session.get(Event, row.event_id)
            if event is None:
                raise RuntimeError("Kiseki mapping points to a missing Event")
            self._set_deleted_event(event, sync_timestamp)
            row.revision = normalized_delete.revision
            row.semantic_fingerprint = fingerprint
            row.deleted_at = sync_timestamp
            row.synced_at = sync_timestamp
            row.updated_at = sync_timestamp
            session.flush()
            record = _record(row)
            session.commit()
            return KisekiSyncResult(record=record, created=False, idempotent=False)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete(
        self,
        payload: KisekiMomentDeletePayload | Mapping[str, object],
        *,
        synced_at: datetime | None = None,
    ) -> KisekiSyncResult:
        return self.delete_moment(payload, synced_at=synced_at)

    def get_moment(self, moment_id: str | UUID) -> KisekiMomentRecord | None:
        """Read only the safe mapping metadata for one normalized Moment ID."""

        normalized_id = _canonical_uuid(moment_id)
        session = self.session_factory()
        try:
            row = session.scalar(
                select(KisekiMomentSyncRecord).where(
                    KisekiMomentSyncRecord.moment_id == normalized_id
                )
            )
            return _record(row) if row is not None else None
        finally:
            session.close()


# A service name that mirrors the table/domain terminology is useful for
# parent integration while retaining the concise primary name.
KisekiMomentSyncService = KisekiSyncService


__all__ = [
    "KISEKI_EVENT_CHANNEL_ID",
    "KISEKI_EVENT_KINDS",
    "KISEKI_EVENT_SOURCE",
    "KISEKI_EVENT_TYPE",
    "KISEKI_EVENT_USER_ID",
    "KisekiDeletePayload",
    "KisekiMomentDeletePayload",
    "KisekiMomentPayload",
    "KisekiMomentRecord",
    "KisekiMomentRevisionConflictError",
    "KisekiMomentSyncService",
    "KisekiRevisionConflictError",
    "KisekiRevisionError",
    "KisekiSyncResult",
    "KisekiSyncService",
    "StaleKisekiMomentRevisionError",
    "StaleKisekiRevisionError",
    "kiseki_content_sha256",
    "kiseki_delete_content_sha256",
    "kiseki_semantic_fingerprint",
]
