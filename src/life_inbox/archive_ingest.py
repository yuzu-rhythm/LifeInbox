"""Fixture-only ingestion and local retry service for Life Archive P0."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .archive_media import ArchiveMediaStore, MediaStoreResult
from .archive_models import ArchiveAttachment, ArchiveDelivery, ArchiveEntry, ArchiveRelation
from .archive_schema import (
    ARCHIVE_SCHEMA_VERSION,
    SYNC_DELETED,
    SYNC_FAILED,
    SYNC_PENDING,
    SYNC_PROCESSING,
    SYNC_QUARANTINED,
    SYNC_RECONCILIATION_REQUIRED,
    SYNC_RETRYING,
    SYNCED,
    ArchiveAttachmentPayload,
    ArchiveEntryPayload,
    entry_payload_sha256,
    reject_location_fields,
    utc_naive,
)


class ArchiveIngestError(ValueError):
    """Base error for a rejected local archive payload."""


class ArchiveRevisionConflictError(ArchiveIngestError):
    """The same source revision arrived with different semantic content."""


class ArchiveStaleRevisionError(ArchiveIngestError):
    """An older source revision was received and intentionally ignored."""


@dataclass(frozen=True, slots=True)
class ArchiveIngestResult:
    """Outcome flags make retries and stale deliveries observable to callers."""

    archive_id: str
    entry_id: int
    delivery_id: str
    source: str
    revision: int
    status: str
    created: bool
    updated: bool
    idempotent: bool
    out_of_order: bool = False
    conflict: bool = False
    attachment_count: int = 0
    quarantined_attachment_count: int = 0
    download_failure_count: int = 0

    @property
    def stale(self) -> bool:
        return self.out_of_order


@dataclass(frozen=True, slots=True)
class FixtureIngestSummary:
    """Results from a deterministic JSON fixture import."""

    results: tuple[ArchiveIngestResult, ...]
    errors: tuple[str, ...] = ()

    @property
    def imported_count(self) -> int:
        return len(self.results)

    @property
    def failed_count(self) -> int:
        return len(self.errors)


def _now_naive(value: datetime | None = None) -> datetime:
    return utc_naive(value or datetime.now(UTC)) or datetime.now(UTC).replace(tzinfo=None)


def _payload_dict(payload: ArchiveEntryPayload) -> dict[str, Any]:
    """Serialize the validated payload for durable, local retry."""

    return payload.model_dump(mode="json", by_alias=False)


def _coerce_payload(payload: ArchiveEntryPayload | Mapping[str, object]) -> ArchiveEntryPayload:
    if isinstance(payload, ArchiveEntryPayload):
        return payload
    reject_location_fields(payload)
    return ArchiveEntryPayload.model_validate(payload)


class ArchiveIngestService:
    """Persist local archive deliveries without contacting any external service."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        media_store: ArchiveMediaStore | None = None,
        data_dir: str | Path = "data",
        max_media_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self.session_factory = session_factory
        self.media_store = media_store or ArchiveMediaStore.from_data_dir(
            data_dir,
            max_bytes=max_media_bytes,
        )

    def ingest(
        self,
        payload: ArchiveEntryPayload | Mapping[str, object],
        *,
        received_at: datetime | None = None,
        _retry_existing_delivery: bool = False,
    ) -> ArchiveIngestResult:
        """Ingest one already materialised payload.

        Delivery identity is checked before source revision.  This means a
        repeated delivery is a no-op, while a new delivery for the same source
        ID is compared against the durable revision and content hash.
        """

        normalized = _coerce_payload(payload)
        payload_hash = entry_payload_sha256(normalized)
        received_timestamp = _now_naive(received_at)
        session = self.session_factory()
        try:
            delivery = session.scalar(
                select(ArchiveDelivery).where(
                    ArchiveDelivery.source == normalized.source,
                    ArchiveDelivery.delivery_id == normalized.delivery_id,
                )
            )
            entry = session.scalar(
                select(ArchiveEntry)
                .options(selectinload(ArchiveEntry.attachments))
                .where(
                    ArchiveEntry.source == normalized.source,
                    ArchiveEntry.source_service_id == normalized.source_service_id,
                )
            )

            if delivery is not None and not (
                _retry_existing_delivery
                and delivery.status in {SYNC_FAILED, SYNC_RETRYING, SYNC_QUARANTINED}
            ):
                if delivery.payload_sha256 == payload_hash:
                    return self._result_from_existing_delivery(delivery, entry, idempotent=True)
                self._commit_conflict(
                    session,
                    delivery=delivery,
                    entry=entry,
                    received_at=received_timestamp,
                    detail="delivery_id was reused with different content",
                )
                raise ArchiveRevisionConflictError(
                    "the same archive delivery has different content"
                )

            if entry is not None:
                if normalized.archive_id is not None and normalized.archive_id != entry.archive_id:
                    self._commit_conflict(
                        session,
                        delivery=delivery,
                        entry=entry,
                        received_at=received_timestamp,
                        detail="archive_id changed for an existing source identity",
                        payload_hash=payload_hash,
                        normalized=normalized,
                    )
                    raise ArchiveRevisionConflictError(
                        "archive_id changed for an existing source identity"
                    )
                if normalized.revision < entry.source_revision:
                    delivery = self._ensure_delivery(
                        session,
                        delivery=delivery,
                        normalized=normalized,
                        payload_hash=payload_hash,
                        received_at=received_timestamp,
                    )
                    delivery.entry_id = entry.id
                    delivery.status = SYNCED
                    delivery.processed_at = received_timestamp
                    delivery.error_code = "stale_revision_ignored"
                    delivery.error_detail = "older revision did not change the archive entry"
                    delivery.attempt_count = max(1, delivery.attempt_count)
                    session.commit()
                    return ArchiveIngestResult(
                        archive_id=entry.archive_id,
                        entry_id=entry.id,
                        delivery_id=normalized.delivery_id or "",
                        source=normalized.source,
                        revision=normalized.revision,
                        status=entry.sync_status,
                        created=False,
                        updated=False,
                        idempotent=False,
                        out_of_order=True,
                        attachment_count=len(entry.attachments),
                    )
                retrying_same_revision = (
                    _retry_existing_delivery
                    and delivery is not None
                    and delivery.status == SYNC_RETRYING
                    and normalized.revision == entry.source_revision
                )
                if normalized.revision == entry.source_revision:
                    if entry.payload_sha256 != payload_hash:
                        self._commit_conflict(
                            session,
                            delivery=delivery,
                            entry=entry,
                            received_at=received_timestamp,
                            detail="same source revision has different content",
                            payload_hash=payload_hash,
                            normalized=normalized,
                        )
                        raise ArchiveRevisionConflictError(
                            "the same source revision has different content"
                        )
                    if not retrying_same_revision:
                        delivery = self._ensure_delivery(
                            session,
                            delivery=delivery,
                            normalized=normalized,
                            payload_hash=payload_hash,
                            received_at=received_timestamp,
                        )
                        delivery.entry_id = entry.id
                        delivery.status = SYNCED
                        delivery.processed_at = received_timestamp
                        delivery.error_code = None
                        delivery.error_detail = None
                        delivery.attempt_count = max(1, delivery.attempt_count)
                        session.commit()
                        return self._result_from_entry(
                            entry,
                            normalized.delivery_id or "",
                            idempotent=True,
                        )

                    # A retry deliberately re-runs the attachment/media
                    # validation even when the source revision is unchanged.
                    # The delivery was marked retrying and committed by
                    # ``retry`` before this call.

                # A newer revision is allowed to update the same stable
                # Archive ID. A tombstone is only protected from older input;
                # a newer explicit source revision may represent restoration.
                created = False
                updated = True
                was_newer_revision = normalized.revision > entry.source_revision
                self._apply_entry_values(entry, normalized, payload_hash, received_timestamp)
                if was_newer_revision:
                    entry.sync_attempt_count = max(1, entry.sync_attempt_count + 1)
            else:
                created = True
                updated = False
                entry = ArchiveEntry(
                    archive_id=normalized.archive_id or str(uuid4()),
                    schema_version=ARCHIVE_SCHEMA_VERSION,
                    source=normalized.source,
                    source_service_id=normalized.source_service_id,
                    source_revision=normalized.revision,
                    delivery_id=normalized.delivery_id or "",
                    original_text="" if normalized.tombstone else normalized.original_text,
                    provenance=normalized.provenance,
                    confidence=normalized.confidence,
                    visibility=normalized.visibility,
                    scheduled_at=normalized.scheduled_at,
                    occurred_at=normalized.occurred_at,
                    observed_at=normalized.observed_at,
                    captured_at=normalized.captured_at or received_timestamp,
                    reviewed_at=normalized.reviewed_at,
                    published_at=normalized.published_at,
                    occurred_at_estimated=normalized.occurred_at_estimated,
                    tombstone=normalized.tombstone,
                    payload_sha256=payload_hash,
                    sync_status=SYNC_PROCESSING,
                    sync_attempt_count=1,
                    last_synced_at=None,
                    created_at=received_timestamp,
                    updated_at=received_timestamp,
                )
                session.add(entry)
                session.flush()

            delivery = self._ensure_delivery(
                session,
                delivery=delivery,
                normalized=normalized,
                payload_hash=payload_hash,
                received_at=received_timestamp,
            )
            delivery.entry_id = entry.id
            delivery.status = SYNC_PROCESSING
            delivery.attempt_count = max(
                1,
                delivery.attempt_count
                + (0 if created or _retry_existing_delivery else 1),
            )
            delivery.processed_at = None
            delivery.error_code = None
            delivery.error_detail = None

            attachment_results = self._upsert_attachments(session, entry, normalized.attachments)
            self._upsert_relations(session, entry, normalized.relations)
            if normalized.tombstone:
                for attachment in entry.attachments:
                    attachment.deleted_at = received_timestamp

            quarantined = sum(
                1 for result in attachment_results if result.inspection.quarantine_reason
            )
            download_failures = sum(
                1
                for result in attachment_results
                if result.inspection.download_status == "failed"
            )
            if normalized.tombstone:
                final_status = SYNC_DELETED
            elif download_failures:
                final_status = SYNC_FAILED
            elif quarantined:
                final_status = SYNC_QUARANTINED
            else:
                final_status = SYNCED
            entry.sync_status = final_status
            entry.sync_attempt_count = max(1, entry.sync_attempt_count or 0)
            entry.last_sync_error = (
                "one or more attachments could not be downloaded"
                if download_failures
                else "one or more attachments were quarantined"
                if quarantined
                else None
            )
            entry.last_synced_at = received_timestamp
            entry.updated_at = received_timestamp
            delivery.status = final_status
            delivery.processed_at = received_timestamp
            delivery.error_code = (
                "download_failure"
                if download_failures
                else "media_quarantined"
                if quarantined
                else None
            )
            delivery.error_detail = entry.last_sync_error
            session.commit()
            return ArchiveIngestResult(
                archive_id=entry.archive_id,
                entry_id=entry.id,
                delivery_id=normalized.delivery_id or "",
                source=normalized.source,
                revision=normalized.revision,
                status=final_status,
                created=created,
                updated=updated,
                idempotent=False,
                attachment_count=len(attachment_results),
                quarantined_attachment_count=quarantined,
                download_failure_count=download_failures,
            )
        except ArchiveRevisionConflictError:
            raise
        except IntegrityError as exc:
            session.rollback()
            raise ArchiveIngestError("archive delivery conflicts with existing local data") from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def ingest_fixture_file(
        self,
        path: str | Path,
        *,
        continue_on_error: bool = False,
    ) -> FixtureIngestSummary:
        """Import one local JSON fixture; never interpret URLs or download them."""

        fixture_path = Path(path)
        if not fixture_path.is_file():
            raise ArchiveIngestError("fixture file was not found")
        try:
            document = json.loads(fixture_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArchiveIngestError("fixture JSON could not be read") from exc
        if isinstance(document, list):
            raw_entries = document
        elif isinstance(document, dict):
            reject_location_fields(document)
            raw_entries = document.get("entries", document.get("items"))
            if raw_entries is None:
                raw_entries = [document]
        else:
            raise ArchiveIngestError("fixture must contain an object or an entries list")
        if not isinstance(raw_entries, list):
            raise ArchiveIngestError("fixture entries must be a list")

        results: list[ArchiveIngestResult] = []
        errors: list[str] = []
        for index, raw_entry in enumerate(raw_entries):
            try:
                if not isinstance(raw_entry, Mapping):
                    raise ArchiveIngestError("fixture entry must be an object")
                results.append(self.ingest(raw_entry))
            except (ArchiveIngestError, ValueError, TypeError) as exc:
                if not continue_on_error:
                    raise
                errors.append(f"entry[{index}]: {type(exc).__name__}")
        return FixtureIngestSummary(results=tuple(results), errors=tuple(errors))

    # Discoverable compatibility aliases for CLI/adapters.
    import_fixture = ingest_fixture_file
    ingest_fixture = ingest_fixture_file

    def retry(self, identifier: int | str) -> ArchiveIngestResult:
        """Retry the latest failed/quarantined local delivery from its payload."""

        session = self.session_factory()
        try:
            if isinstance(identifier, int) and not isinstance(identifier, bool):
                entry = session.scalar(
                    select(ArchiveEntry).where(ArchiveEntry.id == identifier)
                )
            else:
                entry = session.scalar(
                    select(ArchiveEntry).where(ArchiveEntry.archive_id == str(identifier))
                )
            if entry is None:
                raise ArchiveIngestError("archive entry was not found")
            delivery = session.scalar(
                select(ArchiveDelivery)
                .where(ArchiveDelivery.entry_id == entry.id)
                .order_by(ArchiveDelivery.received_at.desc(), ArchiveDelivery.id.desc())
            )
            if delivery is None:
                raise ArchiveIngestError("archive delivery was not found")
            if delivery.status not in {SYNC_FAILED, SYNC_RETRYING, SYNC_QUARANTINED}:
                raise ArchiveIngestError("only failed or quarantined deliveries can be retried")
            try:
                raw_payload = json.loads(delivery.payload_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ArchiveIngestError("stored retry payload is invalid") from exc
            delivery.status = SYNC_RETRYING
            delivery.attempt_count = max(1, delivery.attempt_count + 1)
            entry.sync_status = SYNC_RETRYING
            entry.sync_attempt_count = max(1, entry.sync_attempt_count + 1)
            entry.updated_at = _now_naive()
            session.commit()
        finally:
            session.close()
        return self.ingest(raw_payload, _retry_existing_delivery=True)

    retry_entry = retry
    retry_delivery = retry

    def _result_from_existing_delivery(
        self,
        delivery: ArchiveDelivery,
        entry: ArchiveEntry | None,
        *,
        idempotent: bool,
    ) -> ArchiveIngestResult:
        if entry is None:
            raise ArchiveIngestError("archive delivery points to a missing entry")
        return ArchiveIngestResult(
            archive_id=entry.archive_id,
            entry_id=entry.id,
            delivery_id=delivery.delivery_id,
            source=delivery.source,
            revision=delivery.source_revision,
            status=entry.sync_status,
            created=False,
            updated=False,
            idempotent=idempotent,
            attachment_count=len(entry.attachments),
            quarantined_attachment_count=sum(
                1 for attachment in entry.attachments if attachment.quarantine_reason
            ),
            download_failure_count=sum(
                1 for attachment in entry.attachments if attachment.download_status == "failed"
            ),
        )

    def _result_from_entry(
        self,
        entry: ArchiveEntry,
        delivery_id: str,
        *,
        idempotent: bool,
    ) -> ArchiveIngestResult:
        return ArchiveIngestResult(
            archive_id=entry.archive_id,
            entry_id=entry.id,
            delivery_id=delivery_id,
            source=entry.source,
            revision=entry.source_revision,
            status=entry.sync_status,
            created=False,
            updated=False,
            idempotent=idempotent,
            attachment_count=len(entry.attachments),
            quarantined_attachment_count=sum(
                1 for attachment in entry.attachments if attachment.quarantine_reason
            ),
            download_failure_count=sum(
                1 for attachment in entry.attachments if attachment.download_status == "failed"
            ),
        )

    @staticmethod
    def _apply_entry_values(
        entry: ArchiveEntry,
        payload: ArchiveEntryPayload,
        payload_hash: str,
        timestamp: datetime,
    ) -> None:
        entry.source_revision = payload.revision
        entry.delivery_id = payload.delivery_id or entry.delivery_id
        entry.original_text = "" if payload.tombstone else payload.original_text
        entry.provenance = payload.provenance
        entry.confidence = payload.confidence
        entry.visibility = payload.visibility
        entry.scheduled_at = payload.scheduled_at
        entry.occurred_at = payload.occurred_at
        entry.observed_at = payload.observed_at
        entry.captured_at = payload.captured_at or entry.captured_at or timestamp
        entry.reviewed_at = payload.reviewed_at
        entry.published_at = payload.published_at
        entry.occurred_at_estimated = payload.occurred_at_estimated
        entry.tombstone = payload.tombstone
        entry.payload_sha256 = payload_hash
        entry.sync_status = SYNC_PROCESSING
        entry.last_sync_error = None
        entry.updated_at = timestamp

    @staticmethod
    def _ensure_delivery(
        session: Session,
        *,
        delivery: ArchiveDelivery | None,
        normalized: ArchiveEntryPayload,
        payload_hash: str,
        received_at: datetime,
    ) -> ArchiveDelivery:
        if delivery is None:
            delivery = ArchiveDelivery(
                source=normalized.source,
                delivery_id=normalized.delivery_id or "",
                source_service_id=normalized.source_service_id,
                source_revision=normalized.revision,
                payload_sha256=payload_hash,
                status=SYNC_PENDING,
                attempt_count=0,
                payload_json=json.dumps(
                    _payload_dict(normalized),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                received_at=received_at,
                updated_at=received_at,
            )
            session.add(delivery)
            session.flush()
        else:
            delivery.source_service_id = normalized.source_service_id
            delivery.source_revision = normalized.revision
            delivery.payload_sha256 = payload_hash
            delivery.payload_json = json.dumps(
                _payload_dict(normalized),
                ensure_ascii=False,
                sort_keys=True,
            )
            if delivery.received_at is None:
                delivery.received_at = received_at
        return delivery

    def _commit_conflict(
        self,
        session: Session,
        *,
        delivery: ArchiveDelivery | None,
        entry: ArchiveEntry | None,
        received_at: datetime,
        detail: str,
        payload_hash: str | None = None,
        normalized: ArchiveEntryPayload | None = None,
    ) -> None:
        if normalized is not None and payload_hash is not None:
            delivery = self._ensure_delivery(
                session,
                delivery=delivery,
                normalized=normalized,
                payload_hash=payload_hash,
                received_at=received_at,
            )
        if delivery is not None:
            delivery.status = SYNC_RECONCILIATION_REQUIRED
            delivery.error_code = "revision_conflict"
            delivery.error_detail = detail
            delivery.processed_at = received_at
            delivery.attempt_count = max(1, delivery.attempt_count + 1)
        if entry is not None:
            entry.sync_status = SYNC_RECONCILIATION_REQUIRED
            entry.last_sync_error = detail
            entry.updated_at = received_at
        session.commit()

    def _upsert_attachments(
        self,
        session: Session,
        entry: ArchiveEntry,
        payloads: tuple[ArchiveAttachmentPayload, ...],
    ) -> list[MediaStoreResult]:
        results: list[MediaStoreResult] = []
        existing_by_source = {
            attachment.source_attachment_id: attachment for attachment in entry.attachments
        }
        for payload in payloads:
            row = existing_by_source.get(payload.source_attachment_id)
            if row is None:
                row = ArchiveAttachment(
                    entry_id=entry.id,
                    source=entry.source,
                    source_attachment_id=payload.source_attachment_id,
                    original_filename=Path(payload.original_filename).name or "attachment",
                    mime_type=payload.mime_type,
                    captured_at=payload.captured_at,
                    imported_at=_now_naive(),
                    metadata_removed=False,
                    download_status=payload.download_status,
                )
                session.add(row)
                session.flush()
                entry.attachments.append(row)
            result = self.media_store.store(
                archive_id=entry.archive_id,
                attachment_id=row.id,
                payload=payload,
            )
            inspection = result.inspection
            row.source = entry.source
            row.original_filename = Path(payload.original_filename).name or "attachment"
            row.mime_type = payload.mime_type
            row.detected_mime_type = inspection.detected_mime_type
            row.byte_size = inspection.byte_size
            row.sha256 = inspection.sha256
            row.captured_at = payload.captured_at
            row.private_storage_key = result.private_storage_key
            row.metadata_key = result.metadata_key
            row.quarantine_storage_key = result.quarantine_storage_key
            row.metadata_removed = True
            row.quarantine_reason = inspection.quarantine_reason
            row.download_status = inspection.download_status
            row.download_error = inspection.download_error
            row.deleted_at = None
            results.append(result)
        return results

    @staticmethod
    def _upsert_relations(
        session: Session,
        entry: ArchiveEntry,
        relations: tuple[Any, ...],
    ) -> None:
        existing = {
            (relation.relation_type, relation.target_archive_id): relation
            for relation in entry.relations
        }
        for relation_payload in relations:
            key = (relation_payload.relation_type, relation_payload.target_archive_id)
            relation = existing.get(key)
            if relation is None:
                relation = ArchiveRelation(
                    entry_id=entry.id,
                    relation_type=relation_payload.relation_type,
                    target_archive_id=relation_payload.target_archive_id,
                    note=relation_payload.note,
                )
                session.add(relation)
            else:
                relation.note = relation_payload.note


def ingest_fixture(
    session_factory: Callable[[], Session],
    path: str | Path,
    *,
    data_dir: str | Path = "data",
    max_media_bytes: int = 10 * 1024 * 1024,
    continue_on_error: bool = False,
) -> FixtureIngestSummary:
    """Convenience function for local scripts and tests."""

    return ArchiveIngestService(
        session_factory,
        data_dir=data_dir,
        max_media_bytes=max_media_bytes,
    ).ingest_fixture_file(path, continue_on_error=continue_on_error)


__all__ = [
    "ArchiveIngestError",
    "ArchiveIngestResult",
    "ArchiveIngestService",
    "ArchiveRevisionConflictError",
    "ArchiveStaleRevisionError",
    "FixtureIngestSummary",
    "ingest_fixture",
]
