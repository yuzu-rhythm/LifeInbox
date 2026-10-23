"""Finite read model for browsing canonical local Life Inbox events.

The provider-neutral ``archive_entries`` projection is intentionally separate
from the existing Discord/Web ``events`` table.  This module makes the local
source-of-truth events searchable without pretending that they have already
been projected, published, or synchronized to Notion.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from .attachments import SUPPORTED_IMAGE_TYPES
from .constants import EventStatus
from .models import Event, Expense
from .notion_models import NotionSyncRecord

_MAX_SCAN: Final = 1_000

KIND_LABELS: Final[dict[str, str]] = {
    "expense": "支出",
    "meal": "食事",
    "income": "収入",
    "transfer": "振替",
    "memo": "メモ",
    "life_task": "タスク",
    "rhythm_todo": "音ゲーTODO",
    "rhythm_result": "音ゲー進捗",
    "career": "就活",
    "dining_out": "外食",
    "memories": "思い出",
    "saved_link": "神リンク",
    "social_draft": "SNS下書き",
    "monologue": "独り言",
    "kiseki_moment": "Kiseki Moment",
    "unknown": "未分類",
}

STATUS_LABELS: Final[dict[str, str]] = {
    EventStatus.PENDING.value: "確認待ち",
    EventStatus.CONFIRMED.value: "確定",
    EventStatus.IGNORED.value: "除外",
}

NOTION_STATUS_LABELS: Final[dict[str, str]] = {
    "pending": "Notion送信待ち",
    "syncing": "Notion同期中",
    "synced": "Notion同期済み",
    "retry_waiting": "Notion再試行待ち",
    "failed": "Notion同期失敗",
    "skipped": "Notion対象外",
}


def _normalize_filter(value: object, maximum: int = 80) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:maximum]


def _format_datetime(value: datetime, timezone: ZoneInfo) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(timezone).strftime("%Y/%m/%d %H:%M")


def _kind_keys(event: Event) -> tuple[str, ...]:
    values: list[str] = []
    for raw in [*(event.event_kinds or []), event.event_type]:
        value = _normalize_filter(raw, 64).casefold()
        if value and value not in values:
            values.append(value)
    return tuple(values or ["unknown"])


def _kind_label(key: str) -> str:
    return KIND_LABELS.get(key, key.replace("_", " "))


def _status_tone(status: str) -> str:
    if status == EventStatus.CONFIRMED.value:
        return "success"
    if status == EventStatus.PENDING.value:
        return "warning"
    return "neutral"


def _notion_tone(status: str | None) -> str:
    if status == "synced":
        return "success"
    if status in {"pending", "syncing", "retry_waiting"}:
        return "warning"
    if status == "failed":
        return "danger"
    return "neutral"


def _expense(event: Event) -> Expense | None:
    return event.expenses[0] if event.expenses else None


def _title(event: Event) -> str:
    expense = _expense(event)
    if expense is not None:
        value = expense.merchant_normalized or expense.merchant_raw or expense.description
        if value:
            return _normalize_filter(value, 160)
    if event.meals:
        meal = event.meals[0]
        value = meal.restaurant_normalized or meal.restaurant_raw or meal.description
        if value:
            return _normalize_filter(value, 160)
    if event.memo is not None and event.memo.body:
        return _normalize_filter(event.memo.body.splitlines()[0], 160)
    if event.rhythm_todo is not None:
        return _normalize_filter(event.rhythm_todo.title, 160)
    if event.rhythm_result is not None:
        note = _normalize_filter(event.rhythm_result.note, 140)
        game = _normalize_filter(event.rhythm_result.game, 32)
        return f"{game} · {note}" if note else f"{game}の進捗"
    if event.career_opportunity is not None:
        opportunity = event.career_opportunity
        company = _normalize_filter(opportunity.company, 80)
        title = _normalize_filter(opportunity.title, 100)
        return " · ".join(value for value in (company, title) if value)
    raw = _normalize_filter((event.raw_text or "").splitlines()[0], 160)
    return raw or f"記録 #{event.id}"


def _detail(event: Event) -> str:
    expense = _expense(event)
    if expense is not None:
        parts = []
        if expense.amount is not None:
            parts.append(f"¥{expense.amount:,}")
        if expense.category:
            parts.append(str(expense.category))
        if expense.subcategory:
            parts.append(str(expense.subcategory))
        return " / ".join(parts)
    if event.meals:
        meal = event.meals[0]
        values = [meal.meal_type, meal.outside_home]
        return " / ".join(_normalize_filter(value, 32) for value in values if value)
    if event.memo is not None:
        return "メモ"
    return " / ".join(_kind_label(key) for key in _kind_keys(event))


def _search_text(event: Event) -> str:
    values = [event.raw_text, event.source, event.event_type, *_kind_keys(event)]
    expense = _expense(event)
    if expense is not None:
        values.extend(
            [
                expense.merchant_raw,
                expense.merchant_normalized,
                expense.category,
                expense.subcategory,
                expense.description,
                expense.amount,
            ]
        )
    if event.meals:
        meal = event.meals[0]
        values.extend(
            [
                meal.restaurant_raw,
                meal.restaurant_normalized,
                meal.description,
                meal.meal_type,
                meal.outside_home,
            ]
        )
    if event.memo is not None:
        values.append(event.memo.body)
    if event.rhythm_todo is not None:
        values.extend([event.rhythm_todo.title, event.rhythm_todo.game])
    if event.rhythm_result is not None:
        values.extend([event.rhythm_result.note, event.rhythm_result.game])
    if event.career_opportunity is not None:
        opportunity = event.career_opportunity
        values.extend(
            [
                opportunity.company,
                opportunity.title,
                opportunity.location,
                opportunity.notes,
            ]
        )
    return " ".join(str(value) for value in values if value is not None).casefold()


@dataclass(frozen=True, slots=True)
class LocalArchiveItem:
    event_id: int
    title: str
    raw_text: str
    occurred_label: str
    source: str
    status: str
    status_label: str
    status_tone: str
    kind_keys: tuple[str, ...]
    kind_labels: tuple[str, ...]
    detail: str
    attachment_id: int | None
    attachment_count: int
    notion_status: str | None
    notion_status_label: str
    notion_tone: str


class LocalArchiveBrowser:
    """Search a bounded local Event set without provider or network access."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        timezone: ZoneInfo,
    ) -> None:
        self.session_factory = session_factory
        self.timezone = timezone

    def context(
        self,
        *,
        query: str = "",
        status: str = "all",
        kind: str = "all",
        media: str = "all",
        limit: int = 50,
    ) -> dict[str, object]:
        normalized_query = _normalize_filter(query)
        normalized_status = _normalize_filter(status, 24).casefold()
        if normalized_status not in {"all", *STATUS_LABELS}:
            normalized_status = "all"
        normalized_kind = _normalize_filter(kind, 64).casefold() or "all"
        normalized_media = _normalize_filter(media, 16).casefold()
        if normalized_media not in {"all", "with"}:
            normalized_media = "all"
        bounded_limit = max(1, min(int(limit), 100))

        try:
            with self.session_factory() as session:
                events = list(
                    session.scalars(
                        select(Event)
                        .options(
                            selectinload(Event.expenses),
                            selectinload(Event.meals),
                            selectinload(Event.attachments),
                            selectinload(Event.career_opportunity),
                            selectinload(Event.career_application),
                        )
                        .order_by(Event.occurred_at.desc(), Event.id.desc())
                        .limit(_MAX_SCAN)
                    ).all()
                )
                notion_rows = {
                    row.event_id: row
                    for row in session.scalars(select(NotionSyncRecord)).all()
                }
        except SQLAlchemyError:
            events = []
            notion_rows = {}

        all_kinds = sorted(
            {key for event in events for key in _kind_keys(event)},
            key=lambda key: (_kind_label(key), key),
        )
        needle = normalized_query.casefold()
        matched: list[Event] = []
        for event in events:
            keys = _kind_keys(event)
            image_attachments = [
                attachment
                for attachment in event.attachments
                if str(attachment.content_type or "").split(";", 1)[0].casefold()
                in SUPPORTED_IMAGE_TYPES
            ]
            if normalized_status != "all" and str(event.status) != normalized_status:
                continue
            if normalized_kind != "all" and normalized_kind not in keys:
                continue
            if normalized_media == "with" and not image_attachments:
                continue
            if needle and needle not in _search_text(event):
                continue
            matched.append(event)

        items: list[LocalArchiveItem] = []
        for event in matched[:bounded_limit]:
            image_attachments = sorted(
                (
                    attachment
                    for attachment in event.attachments
                    if str(attachment.content_type or "").split(";", 1)[0].casefold()
                    in SUPPORTED_IMAGE_TYPES
                ),
                key=lambda attachment: attachment.id,
            )
            notion = notion_rows.get(event.id)
            notion_status = str(notion.status) if notion is not None else None
            keys = _kind_keys(event)
            items.append(
                LocalArchiveItem(
                    event_id=event.id,
                    title=_title(event),
                    raw_text=(event.raw_text or "")[:2_000],
                    occurred_label=_format_datetime(event.occurred_at, self.timezone),
                    source=_normalize_filter(event.source, 64) or "unknown",
                    status=str(event.status),
                    status_label=STATUS_LABELS.get(str(event.status), str(event.status)),
                    status_tone=_status_tone(str(event.status)),
                    kind_keys=keys,
                    kind_labels=tuple(_kind_label(key) for key in keys),
                    detail=_detail(event),
                    attachment_id=image_attachments[0].id if image_attachments else None,
                    attachment_count=len(image_attachments),
                    notion_status=notion_status,
                    notion_status_label=(
                        NOTION_STATUS_LABELS.get(notion_status, notion_status)
                        if notion_status is not None
                        else "Notion未昇格"
                    ),
                    notion_tone=_notion_tone(notion_status),
                )
            )

        return {
            "local_entries": tuple(items),
            "local_total": len(matched),
            "local_displayed": len(items),
            "local_media_total": sum(
                1
                for event in matched
                if any(
                    str(attachment.content_type or "").split(";", 1)[0].casefold()
                    in SUPPORTED_IMAGE_TYPES
                    for attachment in event.attachments
                )
            ),
            "local_pending_total": sum(
                str(event.status) == EventStatus.PENDING.value for event in matched
            ),
            "local_notion_synced_total": sum(
                notion_rows.get(event.id) is not None
                and str(notion_rows[event.id].status) == "synced"
                for event in matched
            ),
            "archive_query": normalized_query,
            "archive_status": normalized_status,
            "archive_kind": normalized_kind,
            "archive_media": normalized_media,
            "archive_kind_options": tuple(
                (key, _kind_label(key)) for key in all_kinds
            ),
            "archive_status_options": tuple(STATUS_LABELS.items()),
            "archive_limit": bounded_limit,
        }


__all__ = ["LocalArchiveBrowser", "LocalArchiveItem"]
