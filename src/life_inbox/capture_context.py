"""Durable context and outbox helpers for Discord/Shortcut capture flows."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .inbox_classifier import InboxPlannerItem
from .models import (
    Event,
    EventCaptureContext,
    PlannerImageItem,
    ShortcutInboxDelivery,
)


def _utc_naive(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        return current
    return current.astimezone(UTC).replace(tzinfo=None)


def location_map_url(latitude: float, longitude: float) -> str:
    """Return a non-secret Apple Maps link for one explicitly shared point."""

    return "https://maps.apple.com/?" + urlencode(
        {"ll": f"{latitude:.6f},{longitude:.6f}", "q": "記録した現在地"}
    )


def format_capture_location(context: EventCaptureContext | None) -> str:
    """Render one capture point without implying continuous tracking."""

    if context is None or context.latitude is None or context.longitude is None:
        return ""
    label = (context.location_label or "現在地").strip()[:255]
    accuracy = (
        f"（精度 約{context.horizontal_accuracy_meters:.0f}m）"
        if context.horizontal_accuracy_meters is not None
        else ""
    )
    return (
        f"📍 {label}{accuracy}\n"
        f"{location_map_url(context.latitude, context.longitude)}"
    )


@dataclass(frozen=True, slots=True)
class ShortcutDeliveryClaim:
    event_id: int
    attempt_count: int


class CaptureContextService:
    """Small transactional service shared by the API and Discord adapter."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def upsert_shortcut_context(
        self,
        *,
        event_id: int,
        capture_type: str,
        source_label: str,
        latitude: float | None = None,
        longitude: float | None = None,
        horizontal_accuracy_meters: float | None = None,
        location_label: str | None = None,
        location_shared_at: datetime | None = None,
    ) -> EventCaptureContext:
        session: Session = self.session_factory()
        try:
            context = session.get(EventCaptureContext, event_id)
            if context is None:
                context = EventCaptureContext(event_id=event_id)
                session.add(context)
            context.capture_type = capture_type
            context.source_label = source_label
            context.latitude = latitude
            context.longitude = longitude
            context.horizontal_accuracy_meters = horizontal_accuracy_meters
            context.location_label = location_label
            context.location_shared_at = (
                _utc_naive(location_shared_at)
                if latitude is not None and longitude is not None
                else None
            )
            context.updated_at = _utc_naive()
            session.commit()
            return context
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record_route(
        self,
        *,
        event_id: int,
        category: str,
        destination_channel_id: object,
        destination_message_id: object,
    ) -> None:
        session: Session = self.session_factory()
        try:
            context = session.get(EventCaptureContext, event_id)
            if context is None:
                context = EventCaptureContext(event_id=event_id)
                session.add(context)
            context.route_category = category
            context.destination_channel_id = str(destination_channel_id)[:64]
            context.destination_message_id = str(destination_message_id)[:64]
            context.updated_at = _utc_naive()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def ensure_shortcut_delivery(self, event_id: int) -> ShortcutInboxDelivery:
        session: Session = self.session_factory()
        try:
            delivery = session.get(ShortcutInboxDelivery, event_id)
            if delivery is None:
                delivery = ShortcutInboxDelivery(event_id=event_id, status="pending")
                session.add(delivery)
                session.commit()
            return delivery
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def claim_shortcut_deliveries(
        self,
        *,
        now: datetime | None = None,
        limit: int = 10,
        lease_seconds: int = 300,
    ) -> list[ShortcutDeliveryClaim]:
        checked_at = _utc_naive(now)
        stale_before = checked_at - timedelta(seconds=lease_seconds)
        session: Session = self.session_factory()
        try:
            rows = list(
                session.scalars(
                    select(ShortcutInboxDelivery)
                    .join(Event, Event.id == ShortcutInboxDelivery.event_id)
                    .where(
                        Event.status == "pending",
                        or_(
                            ShortcutInboxDelivery.status == "pending",
                            (
                                (ShortcutInboxDelivery.status == "retryWaiting")
                                & or_(
                                    ShortcutInboxDelivery.next_retry_at.is_(None),
                                    ShortcutInboxDelivery.next_retry_at <= checked_at,
                                )
                            ),
                            (
                                (ShortcutInboxDelivery.status == "sending")
                                & (ShortcutInboxDelivery.claimed_at <= stale_before)
                            ),
                        ),
                    )
                    .order_by(ShortcutInboxDelivery.created_at, ShortcutInboxDelivery.event_id)
                    .limit(limit)
                )
            )
            claims: list[ShortcutDeliveryClaim] = []
            for row in rows:
                previous_status = row.status
                previous_attempts = row.attempt_count
                result = session.execute(
                    update(ShortcutInboxDelivery)
                    .where(
                        ShortcutInboxDelivery.event_id == row.event_id,
                        ShortcutInboxDelivery.status == previous_status,
                        ShortcutInboxDelivery.attempt_count == previous_attempts,
                    )
                    .values(
                        status="sending",
                        attempt_count=previous_attempts + 1,
                        claimed_at=checked_at,
                        next_retry_at=None,
                        last_error_code=None,
                        updated_at=checked_at,
                    )
                )
                if result.rowcount == 1:
                    claims.append(
                        ShortcutDeliveryClaim(row.event_id, previous_attempts + 1)
                    )
            session.commit()
            return claims
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mark_shortcut_posted(
        self,
        *,
        event_id: int,
        attempt_count: int,
        source_channel_id: object,
        source_message_id: object,
        candidate_message_id: object | None = None,
        now: datetime | None = None,
    ) -> None:
        timestamp = _utc_naive(now)
        session: Session = self.session_factory()
        try:
            result = session.execute(
                update(ShortcutInboxDelivery)
                .where(
                    ShortcutInboxDelivery.event_id == event_id,
                    ShortcutInboxDelivery.status == "sending",
                    ShortcutInboxDelivery.attempt_count == attempt_count,
                )
                .values(
                    status="posted",
                    source_channel_id=str(source_channel_id)[:64],
                    source_message_id=str(source_message_id)[:64],
                    candidate_message_id=(
                        str(candidate_message_id)[:64]
                        if candidate_message_id is not None
                        else None
                    ),
                    claimed_at=None,
                    next_retry_at=None,
                    posted_at=timestamp,
                    updated_at=timestamp,
                )
            )
            if result.rowcount != 1:
                raise RuntimeError("shortcut delivery claim changed")
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record_shortcut_source(
        self,
        *,
        event_id: int,
        attempt_count: int,
        source_channel_id: object,
        source_message_id: object,
    ) -> None:
        """Persist the source card before archive/classification side effects."""

        timestamp = _utc_naive()
        session: Session = self.session_factory()
        try:
            result = session.execute(
                update(ShortcutInboxDelivery)
                .where(
                    ShortcutInboxDelivery.event_id == event_id,
                    ShortcutInboxDelivery.status == "sending",
                    ShortcutInboxDelivery.attempt_count == attempt_count,
                )
                .values(
                    source_channel_id=str(source_channel_id)[:64],
                    source_message_id=str(source_message_id)[:64],
                    updated_at=timestamp,
                )
            )
            if result.rowcount != 1:
                raise RuntimeError("shortcut delivery claim changed")
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mark_shortcut_failed(
        self,
        *,
        event_id: int,
        attempt_count: int,
        error_code: str,
        retryable: bool,
        now: datetime | None = None,
    ) -> None:
        timestamp = _utc_naive(now)
        status = "retryWaiting" if retryable and attempt_count < 5 else "failed"
        next_retry_at = (
            timestamp + timedelta(seconds=min(300, 2 ** min(attempt_count, 8)))
            if status == "retryWaiting"
            else None
        )
        session: Session = self.session_factory()
        try:
            session.execute(
                update(ShortcutInboxDelivery)
                .where(
                    ShortcutInboxDelivery.event_id == event_id,
                    ShortcutInboxDelivery.status == "sending",
                    ShortcutInboxDelivery.attempt_count == attempt_count,
                )
                .values(
                    status=status,
                    last_error_code=error_code[:64],
                    claimed_at=None,
                    next_retry_at=next_retry_at,
                    updated_at=timestamp,
                )
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_shortcut_delivery(self, event_id: int) -> ShortcutInboxDelivery | None:
        session: Session = self.session_factory()
        try:
            return session.get(ShortcutInboxDelivery, event_id)
        finally:
            session.close()

    def shortcut_event_for_message(self, message_id: object) -> Event | None:
        session: Session = self.session_factory()
        try:
            return session.scalar(
                select(Event)
                .join(ShortcutInboxDelivery)
                .where(ShortcutInboxDelivery.source_message_id == str(message_id))
            )
        finally:
            session.close()

    def upsert_planner_items(
        self,
        event_id: int,
        items: Iterable[InboxPlannerItem],
    ) -> list[PlannerImageItem]:
        session: Session = self.session_factory()
        try:
            existing = {
                item.item_index: item
                for item in session.scalars(
                    select(PlannerImageItem).where(PlannerImageItem.event_id == event_id)
                )
            }
            for index, item in enumerate(items):
                primary = item.candidates[0]
                row = existing.get(index)
                if row is None:
                    row = PlannerImageItem(
                        event_id=event_id,
                        item_index=index,
                        raw_text=item.text,
                        category=primary.category.value,
                        confidence=primary.confidence,
                        candidate_categories=[
                            candidate.category.value for candidate in item.candidates
                        ],
                        status=(
                            "unknown"
                            if primary.category.value == "unknown"
                            else "pending"
                        ),
                    )
                    session.add(row)
                elif row.status in {"pending", "unknown"}:
                    # Keep the first durable interpretation stable across an
                    # API retry; a model re-run must not silently rewrite an
                    # item after the user has seen it.
                    continue
            session.commit()
            return list(
                session.scalars(
                    select(PlannerImageItem)
                    .where(PlannerImageItem.event_id == event_id)
                    .order_by(PlannerImageItem.item_index)
                )
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mark_planner_item_posted(
        self,
        *,
        item_id: int,
        channel_id: object,
        message_id: object,
    ) -> None:
        session: Session = self.session_factory()
        try:
            row = session.get(PlannerImageItem, item_id)
            if row is None:
                raise ValueError("planner item not found")
            row.status = "posted"
            row.destination_channel_id = str(channel_id)[:64]
            row.destination_message_id = str(message_id)[:64]
            row.last_error_code = None
            row.updated_at = _utc_naive()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mark_planner_item_failed(self, *, item_id: int, error_code: str) -> None:
        session: Session = self.session_factory()
        try:
            row = session.get(PlannerImageItem, item_id)
            if row is None:
                raise ValueError("planner item not found")
            row.status = "failed"
            row.last_error_code = error_code[:64]
            row.updated_at = _utc_naive()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


__all__ = [
    "CaptureContextService",
    "ShortcutDeliveryClaim",
    "format_capture_location",
    "location_map_url",
]
