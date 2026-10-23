"""Schemas and invariants for the local Life Archive P0 slice.

This module is intentionally independent from the existing Discord/Event
schema.  The archive accepts already materialised, local payloads only.  It
does not know how to fetch a URL, authenticate a provider, or turn a location
sample into an archive entry.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

ARCHIVE_SCHEMA_VERSION = 1
DEFAULT_VISIBILITY = "private"
VISIBILITY_VALUES = frozenset({"private", "unlisted", "public"})

SYNC_PENDING = "pending"
SYNC_PROCESSING = "processing"
SYNCED = "synced"
SYNC_RETRYING = "retrying"
SYNC_FAILED = "failed"
SYNC_QUARANTINED = "quarantined"
SYNC_DELETED = "deleted"
SYNC_RECONCILIATION_REQUIRED = "reconciliation_required"
SYNC_STATUS_VALUES = frozenset(
    {
        SYNC_PENDING,
        SYNC_PROCESSING,
        SYNCED,
        SYNC_RETRYING,
        SYNC_FAILED,
        SYNC_QUARANTINED,
        SYNC_DELETED,
        SYNC_RECONCILIATION_REQUIRED,
    }
)

_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_SOURCE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,63}$")
_FORBIDDEN_LOCATION_MARKERS = frozenset(
    {
        "latitude",
        "longitude",
        "lat",
        "lng",
        "accuracy",
        "horizontalaccuracy",
        "verticalaccuracy",
        "altitude",
        "speed",
        "course",
        "coordinate",
        "coordinates",
        "location",
        "route",
        "polyline",
        "track",
        "trackchunk",
        "visit",
        "journey",
        "place",
        "privacyzone",
        "privacy_zone",
    }
)


def utc_naive(value: datetime | None) -> datetime | None:
    """Normalise an input timestamp to the repository's UTC-naive storage.

    A timezone-less fixture timestamp is treated as UTC.  Production adapters
    should provide an explicit offset; accepting a naive value here keeps the
    local fixture boundary useful without silently applying the machine's
    timezone.
    """

    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def isoformat_or_none(value: datetime | None) -> str | None:
    """Return a stable JSON representation for a database timestamp."""

    normalized = utc_naive(value)
    if normalized is None:
        return None
    return normalized.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


class ArchiveRelationPayload(BaseModel):
    """A deliberately small, provider-neutral relation edge."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    relation_type: StrictStr = Field(
        min_length=1,
        max_length=64,
        validation_alias=AliasChoices("relation_type", "relationType", "type"),
    )
    target_archive_id: StrictStr = Field(
        min_length=1,
        max_length=64,
        validation_alias=AliasChoices(
            "target_archive_id", "targetArchiveId", "target_id", "targetId"
        ),
    )
    note: StrictStr = Field(default="", max_length=1_000)

    @field_validator("relation_type", "target_archive_id", "note")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()


class ArchiveAttachmentPayload(BaseModel):
    """An attachment that is already available as local inline bytes.

    ``data_base64`` is the only supported transfer shape in P0.  URL fields
    intentionally are not accepted, so fixture ingestion cannot accidentally
    become a network downloader.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    source_attachment_id: StrictStr = Field(
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices(
            "source_attachment_id",
            "sourceAttachmentId",
            "attachment_id",
            "attachmentId",
        ),
    )
    original_filename: StrictStr = Field(
        default="attachment",
        max_length=255,
        validation_alias=AliasChoices("original_filename", "originalFilename", "filename"),
    )
    mime_type: StrictStr = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("mime_type", "mimeType", "content_type", "contentType"),
    )
    sha256: StrictStr | None = Field(
        default=None,
        validation_alias=AliasChoices("sha256", "checksum", "expected_sha256", "expectedSha256"),
    )
    data_base64: StrictStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "data_base64", "dataBase64", "content_base64", "contentBase64"
        ),
    )
    download_status: Literal["available", "failed"] = Field(
        default="available",
        validation_alias=AliasChoices("download_status", "downloadStatus"),
    )
    download_error: StrictStr | None = Field(
        default=None,
        max_length=255,
        validation_alias=AliasChoices("download_error", "downloadError"),
    )
    captured_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("captured_at", "capturedAt"),
    )

    @field_validator("source_attachment_id", "original_filename", "mime_type")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("attachment text must not be blank")
        return normalized

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().lower()
        if _SHA256_PATTERN.fullmatch(normalized) is None:
            raise ValueError("sha256 must be a 64-character hexadecimal digest")
        return normalized

    @field_validator("captured_at")
    @classmethod
    def _normalise_captured_at(cls, value: datetime | None) -> datetime | None:
        return utc_naive(value)

    @model_validator(mode="after")
    def _validate_transfer_shape(self) -> ArchiveAttachmentPayload:
        if self.download_status == "failed":
            return self
        if self.data_base64 is None or not self.data_base64.strip():
            raise ValueError("available attachment must contain inline base64 data")
        return self


class ArchiveEntryPayload(BaseModel):
    """Provider-neutral, location-free input for one archive entry."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    archive_id: StrictStr | None = Field(
        default=None,
        validation_alias=AliasChoices("archive_id", "archiveId"),
    )
    schema_version: StrictInt = Field(default=ARCHIVE_SCHEMA_VERSION, ge=1)
    source: StrictStr = Field(min_length=1, max_length=64)
    provenance: StrictStr = Field(default="fixture", min_length=1, max_length=512)
    confidence: StrictFloat | None = Field(default=None, ge=0, le=1)
    visibility: Literal["private", "unlisted", "public"] = DEFAULT_VISIBILITY

    scheduled_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("scheduled_at", "scheduledAt"),
    )
    occurred_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("occurred_at", "occurredAt"),
    )
    observed_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("observed_at", "observedAt"),
    )
    captured_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("captured_at", "capturedAt"),
    )
    reviewed_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("reviewed_at", "reviewedAt"),
    )
    published_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("published_at", "publishedAt"),
    )
    occurred_at_estimated: StrictBool = Field(
        default=False,
        validation_alias=AliasChoices("occurred_at_estimated", "occurredAtEstimated", "estimated"),
    )

    source_service_id: StrictStr = Field(
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices(
            "source_service_id",
            "sourceServiceId",
            "source_id",
            "sourceId",
            "original_service_id",
            "originalServiceId",
        ),
    )
    revision: StrictInt = Field(
        default=1,
        ge=1,
        validation_alias=AliasChoices("revision", "source_revision", "sourceRevision"),
    )
    delivery_id: StrictStr | None = Field(
        default=None,
        max_length=255,
        validation_alias=AliasChoices(
            "delivery_id", "deliveryId", "event_id", "eventId", "delivery_key", "deliveryKey"
        ),
    )
    original_text: StrictStr = Field(
        default="",
        max_length=100_000,
        validation_alias=AliasChoices("original_text", "originalText", "raw_text", "rawText"),
    )
    tombstone: StrictBool = False
    attachments: tuple[ArchiveAttachmentPayload, ...] = ()
    relations: tuple[ArchiveRelationPayload, ...] = ()

    @field_validator("source", "provenance", "source_service_id", "original_text")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip() if value != "" else value

    @field_validator("source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        if _SAFE_SOURCE_PATTERN.fullmatch(value) is None:
            raise ValueError("source contains unsupported characters")
        return value

    @field_validator("archive_id")
    @classmethod
    def _validate_archive_id(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        try:
            return str(UUID(value.strip()))
        except (AttributeError, ValueError) as exc:
            raise ValueError("archive_id must be a UUID") from exc

    @field_validator(
        "scheduled_at",
        "occurred_at",
        "observed_at",
        "captured_at",
        "reviewed_at",
        "published_at",
    )
    @classmethod
    def _normalise_timestamp(cls, value: datetime | None) -> datetime | None:
        return utc_naive(value)

    @field_validator("delivery_id")
    @classmethod
    def _normalise_delivery_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def _validate_meaning(self) -> ArchiveEntryPayload:
        if self.schema_version != ARCHIVE_SCHEMA_VERSION:
            raise ValueError(f"unsupported archive schema_version: {self.schema_version}")
        if self.tombstone and self.attachments:
            raise ValueError("tombstone payload must not contain attachments")
        if self.tombstone and self.relations:
            raise ValueError("tombstone payload must not contain relations")
        if self.delivery_id is None:
            object.__setattr__(
                self,
                "delivery_id",
                f"{self.source}:{self.source_service_id}:r{self.revision}",
            )
        return self

    @property
    def original_service_id(self) -> str:
        """Compatibility spelling for callers using the requirement wording."""

        return self.source_service_id

    @property
    def source_revision(self) -> int:
        return self.revision


def reject_location_fields(value: object) -> None:
    """Reject location-shaped keys before arbitrary fixture JSON is used.

    The typed models already use ``extra='forbid'``.  This recursive guard is
    an additional safety net for the fixture envelope and for future adapter
    code that may inspect a generic mapping before model validation.
    """

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                normalized = str(key).replace("-", "_").casefold()
                compact = normalized.replace("_", "")
                if normalized in _FORBIDDEN_LOCATION_MARKERS or compact in {
                    marker.replace("_", "") for marker in _FORBIDDEN_LOCATION_MARKERS
                }:
                    raise ValueError("location fields are not accepted by Life Archive")
                visit(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child)

    visit(value)


def _json_timestamp(value: datetime | None) -> str | None:
    return isoformat_or_none(value)


def canonical_entry_payload(payload: ArchiveEntryPayload) -> dict[str, Any]:
    """Return semantic content used for idempotency and revision conflict checks."""

    return {
        "schema_version": payload.schema_version,
        "source": payload.source,
        "source_service_id": payload.source_service_id,
        "revision": payload.revision,
        "scheduled_at": _json_timestamp(payload.scheduled_at),
        "occurred_at": _json_timestamp(payload.occurred_at),
        "observed_at": _json_timestamp(payload.observed_at),
        "captured_at": _json_timestamp(payload.captured_at),
        "reviewed_at": _json_timestamp(payload.reviewed_at),
        "published_at": _json_timestamp(payload.published_at),
        "occurred_at_estimated": payload.occurred_at_estimated,
        "provenance": payload.provenance,
        "confidence": payload.confidence,
        "visibility": payload.visibility,
        "original_text": "" if payload.tombstone else payload.original_text,
        "tombstone": payload.tombstone,
        "attachments": [
            {
                "source_attachment_id": attachment.source_attachment_id,
                "original_filename": attachment.original_filename,
                "mime_type": attachment.mime_type,
                "sha256": attachment.sha256,
                "data_base64": attachment.data_base64,
                "download_status": attachment.download_status,
                "download_error": attachment.download_error,
                "captured_at": _json_timestamp(attachment.captured_at),
            }
            for attachment in payload.attachments
        ],
        "relations": [relation.model_dump(mode="json") for relation in payload.relations],
    }


def entry_payload_sha256(payload: ArchiveEntryPayload) -> str:
    """Hash semantic payload content while excluding delivery arrival time/id."""

    encoded = json.dumps(
        canonical_entry_payload(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def archive_schema_document() -> dict[str, Any]:
    """Return a small exportable schema description for schema-v1.json."""

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "life-archive/schema-v1",
        "title": "Life Archive P0 entry",
        "type": "object",
        "required": ["schema_version", "source", "source_service_id", "revision"],
        "properties": {
            "archive_id": {"type": ["string", "null"], "format": "uuid"},
            "schema_version": {"const": ARCHIVE_SCHEMA_VERSION},
            "source": {"type": "string", "pattern": _SAFE_SOURCE_PATTERN.pattern},
            "provenance": {"type": "string"},
            "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
            "visibility": {"enum": sorted(VISIBILITY_VALUES)},
            "scheduled_at": {"type": ["string", "null"], "format": "date-time"},
            "occurred_at": {"type": ["string", "null"], "format": "date-time"},
            "observed_at": {"type": ["string", "null"], "format": "date-time"},
            "captured_at": {"type": ["string", "null"], "format": "date-time"},
            "reviewed_at": {"type": ["string", "null"], "format": "date-time"},
            "published_at": {"type": ["string", "null"], "format": "date-time"},
            "occurred_at_estimated": {"type": "boolean"},
            "source_service_id": {"type": "string"},
            "revision": {"type": "integer", "minimum": 1},
            "delivery_id": {"type": "string"},
            "original_text": {"type": "string"},
            "tombstone": {"type": "boolean"},
        },
        "additionalProperties": False,
        "x-location-policy": "No raw GPS, coordinates, tracks, visits, journeys, or places.",
    }


__all__ = [
    "ARCHIVE_SCHEMA_VERSION",
    "DEFAULT_VISIBILITY",
    "SYNC_DELETED",
    "SYNC_FAILED",
    "SYNC_PENDING",
    "SYNC_PROCESSING",
    "SYNC_QUARANTINED",
    "SYNC_RECONCILIATION_REQUIRED",
    "SYNC_RETRYING",
    "SYNC_STATUS_VALUES",
    "SYNCED",
    "ArchiveAttachmentPayload",
    "ArchiveEntryPayload",
    "ArchiveRelationPayload",
    "archive_schema_document",
    "canonical_entry_payload",
    "entry_payload_sha256",
    "isoformat_or_none",
    "reject_location_fields",
    "utc_naive",
]
