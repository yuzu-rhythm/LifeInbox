"""Read-only timeline and synchronization health views for Life Archive."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .archive_models import ArchiveAttachment, ArchiveEntry
from .archive_schema import (
    ARCHIVE_SCHEMA_VERSION,
    SYNC_STATUS_VALUES,
    isoformat_or_none,
)


def _format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "未確認"
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _title_for_entry(entry: ArchiveEntry) -> str:
    text = (entry.original_text or "").strip()
    if text:
        return text.splitlines()[0][:160]
    if entry.tombstone:
        return "削除済みエントリ"
    return f"{entry.source} / {entry.source_service_id}"


@dataclass(frozen=True, slots=True)
class ArchiveTimelineItem:
    archive_id: str
    source: str
    title: str
    original_text: str
    occurred_label: str
    observed_label: str
    captured_label: str
    provenance: str
    confidence_label: str
    revision: int
    sync_status: str
    visibility: str
    attachment_count: int
    quarantined_attachment_count: int


class ArchiveHealthService:
    """Expose only bounded local state; no health check contacts a provider."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        data_dir: str | Path = "data",
    ) -> None:
        self.session_factory = session_factory
        self.data_dir = Path(data_dir)

    def timeline(self, *, limit: int = 50) -> tuple[ArchiveTimelineItem, ...]:
        """Return a finite, private timeline with tombstones omitted."""

        bounded_limit = max(1, min(int(limit), 100))
        try:
            with self.session_factory() as session:
                entries = list(
                    session.scalars(
                        select(ArchiveEntry)
                        .where(
                            ArchiveEntry.tombstone.is_(False),
                            ArchiveEntry.visibility == "private",
                        )
                        .order_by(
                            ArchiveEntry.occurred_at.desc(),
                            ArchiveEntry.captured_at.desc(),
                            ArchiveEntry.id.desc(),
                        )
                        .limit(bounded_limit)
                    ).all()
                )
                # ``selectin`` is the model default, but keep this method safe
                # if a caller supplies a different mapper option in the future.
                attachment_counts = self._attachment_counts(
                    session,
                    [entry.id for entry in entries],
                )
        except SQLAlchemyError:
            return ()

        return tuple(
            ArchiveTimelineItem(
                archive_id=entry.archive_id,
                source=entry.source,
                title=_title_for_entry(entry),
                original_text=entry.original_text,
                occurred_label=_format_timestamp(entry.occurred_at),
                observed_label=_format_timestamp(entry.observed_at),
                captured_label=_format_timestamp(entry.captured_at),
                provenance=entry.provenance,
                confidence_label=(
                    f"{entry.confidence:.2f}" if entry.confidence is not None else "未確認"
                ),
                revision=entry.source_revision,
                sync_status=entry.sync_status,
                visibility=entry.visibility,
                attachment_count=attachment_counts.get(entry.id, (0, 0))[0],
                quarantined_attachment_count=attachment_counts.get(entry.id, (0, 0))[1],
            )
            for entry in entries
        )

    @staticmethod
    def _attachment_counts(
        session: Session,
        entry_ids: list[int],
    ) -> dict[int, tuple[int, int]]:
        if not entry_ids:
            return {}
        counts: dict[int, tuple[int, int]] = {}
        rows = session.execute(
            select(
                ArchiveAttachment.entry_id,
                func.count(ArchiveAttachment.id),
                func.sum(func.iif(ArchiveAttachment.quarantine_reason.is_not(None), 1, 0)),
            )
            .where(ArchiveAttachment.entry_id.in_(entry_ids))
            .group_by(ArchiveAttachment.entry_id)
        ).all()
        for entry_id, count, quarantined in rows:
            counts[int(entry_id)] = (int(count or 0), int(quarantined or 0))
        return counts

    def health(self) -> dict[str, Any]:
        """Return actual counts and explicit unknown states for unimplemented work."""

        status_counts = {status: 0 for status in sorted(SYNC_STATUS_VALUES)}
        report: dict[str, Any] = {
            "schema": {
                "state": "ok",
                "version": ARCHIVE_SCHEMA_VERSION,
                "tables": "確認済み",
            },
            "last_sync": "未確認",
            "last_sync_iso": None,
            "status_counts": status_counts,
            "sync_counts": status_counts,
            "attachments": {
                "total": 0,
                "quarantined": 0,
                "download_failed": 0,
            },
            "deletion_pending": 0,
            "publication_pending": 0,
            "backup": self._artifact_state("backups", "backup-*.zip"),
            "export": self._artifact_state("exports", "life-archive-*.zip"),
            "external_backup": "未確認（このローカル処理では外部バックアップを実行しない）",
        }
        try:
            with self.session_factory() as session:
                for status, count in session.execute(
                    select(ArchiveEntry.sync_status, func.count(ArchiveEntry.id)).group_by(
                        ArchiveEntry.sync_status
                    )
                ).all():
                    if status in status_counts:
                        status_counts[str(status)] = int(count)
                attachment_total = session.scalar(select(func.count(ArchiveAttachment.id))) or 0
                quarantine_total = session.scalar(
                    select(func.count(ArchiveAttachment.id)).where(
                        ArchiveAttachment.quarantine_reason.is_not(None)
                    )
                ) or 0
                download_failed = session.scalar(
                    select(func.count(ArchiveAttachment.id)).where(
                        ArchiveAttachment.download_status == "failed"
                    )
                ) or 0
                last_sync = session.scalar(
                    select(func.max(ArchiveEntry.last_synced_at))
                )
                report["attachments"] = {
                    "total": int(attachment_total),
                    "quarantined": int(quarantine_total),
                    "download_failed": int(download_failed),
                }
                report["deletion_pending"] = int(
                    status_counts.get("deleted", 0)
                )
                report["publication_pending"] = int(
                    session.scalar(
                        select(func.count(ArchiveEntry.id)).where(
                            ArchiveEntry.tombstone.is_(False),
                            ArchiveEntry.published_at.is_(None),
                        )
                    )
                    or 0
                )
                report["last_sync"] = _format_timestamp(last_sync)
                report["last_sync_iso"] = isoformat_or_none(last_sync)
        except SQLAlchemyError:
            report["schema"] = {
                "state": "未確認",
                "version": "未確認",
                "tables": "未確認（DB初期化待ち）",
            }
            report["last_sync"] = "未確認"
            report["last_sync_iso"] = None

        # Keep both keys pointing at independent dictionaries in the returned
        # object so template/API callers cannot accidentally mutate each other.
        report["sync_counts"] = dict(report["status_counts"])
        return report

    def _artifact_state(self, directory_name: str, pattern: str) -> dict[str, Any]:
        directory = self.data_dir / directory_name
        try:
            candidates = sorted(
                path for path in directory.glob(pattern) if path.is_file() and not path.is_symlink()
            )
        except (OSError, RuntimeError):
            return {"state": "未確認", "latest": None, "count": 0}
        if not candidates:
            return {"state": "未設定", "latest": None, "count": 0}
        return {
            "state": "ローカル生成物を確認",
            "latest": candidates[-1].name,
            "count": len(candidates),
        }

    def timeline_context(self, *, limit: int = 50) -> dict[str, Any]:
        bounded_limit = max(1, min(int(limit), 100))
        return {
            "page": "archive",
            "entries": self.timeline(limit=bounded_limit),
            "limit": bounded_limit,
            "finite": True,
        }

    def health_context(self) -> dict[str, Any]:
        return {"page": "archive_health", "health": self.health()}

    def health_dict(self) -> dict[str, Any]:
        """Compatibility alias for JSON/API callers."""

        return self.health()


__all__ = ["ArchiveHealthService", "ArchiveTimelineItem"]
