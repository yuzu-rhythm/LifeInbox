"""Revision-safe persistence for iPhone rhythm-spending events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from .rhythm_collectors.common import safe_message
from .rhythm_spending_models import RhythmSpendingEventRow

SPENDING_GAMES = frozenset({"chunithm", "maimai", "ongeki"})
SPENDING_SOURCES = frozenset(
    {"app", "widget", "shortcut", "siri", "calendar", "import"}
)
ACTIVE_DELIVERY_STATES = frozenset({"sending", "editing", "deleting"})
QUEUED_DELIVERY_STATES = frozenset({"pending", "retryWaiting"})


def utc_naive(value: datetime | None = None) -> datetime:
    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        return resolved
    return resolved.astimezone(UTC).replace(tzinfo=None)


class SpendingEventRevisionError(ValueError):
    """Base class for a rejected event revision."""


class StaleSpendingEventRevisionError(SpendingEventRevisionError):
    pass


class SpendingEventRevisionConflictError(SpendingEventRevisionError):
    pass


class SpendingEventDeliveryConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SpendingEventRecord:
    id: int
    event_id: str
    rhythm_user_id: int
    source_user_id: str
    game: str
    amount_yen: int
    occurred_at: datetime
    local_date: date
    time_zone: str
    revision: int
    source: str
    deleted_at: datetime | None
    discord_channel_id: str | None
    discord_message_id: str | None
    discord_status: str
    discord_last_error: str | None
    discord_attempt_count: int
    discord_next_retry_at: datetime | None
    synced_at: datetime
    created_at: datetime
    updated_at: datetime

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None


@dataclass(frozen=True, slots=True)
class SpendingEventSyncResult:
    record: SpendingEventRecord
    created: bool
    idempotent: bool


@dataclass(frozen=True, slots=True)
class SpendingDayTotals:
    rhythm_user_id: int
    local_date: date
    time_zone: str
    chunithm_amount_yen: int
    maimai_amount_yen: int
    ongeki_amount_yen: int
    total_amount_yen: int
    active_event_count: int
    has_events: bool
    latest_revision: int
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class SpendingEventDeliveryClaim:
    record: SpendingEventRecord
    operation: str
    claim_revision: int
    attempt_count: int


def _record(row: RhythmSpendingEventRow) -> SpendingEventRecord:
    return SpendingEventRecord(
        id=row.id,
        event_id=row.event_id,
        rhythm_user_id=row.rhythm_user_id,
        source_user_id=row.source_user_id,
        game=row.game,
        amount_yen=row.amount_yen,
        occurred_at=row.occurred_at,
        local_date=row.local_date,
        time_zone=row.time_zone,
        revision=row.revision,
        source=row.source,
        deleted_at=row.deleted_at,
        discord_channel_id=row.discord_channel_id,
        discord_message_id=row.discord_message_id,
        discord_status=row.discord_status,
        discord_last_error=row.discord_last_error,
        discord_attempt_count=row.discord_attempt_count,
        discord_next_retry_at=row.discord_next_retry_at,
        synced_at=row.synced_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _bounded_error(value: str | None) -> str | None:
    cleaned = safe_message(value)
    return cleaned[:1_000] if cleaned else None


class RhythmSpendingEventPersistence:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    @staticmethod
    def local_date_for(occurred_at: datetime, time_zone: str) -> date:
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurredAt must include a timezone")
        try:
            timezone = ZoneInfo(time_zone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timeZone must be a valid IANA timezone") from exc
        local = occurred_at.astimezone(timezone)
        if occurred_at.utcoffset() != local.utcoffset():
            raise ValueError("occurredAt offset does not match timeZone")
        return local.date()

    def sync_event(
        self,
        *,
        rhythm_user_id: int,
        event_id: str,
        source_user_id: str,
        game: str,
        amount_yen: int,
        occurred_at: datetime,
        time_zone: str,
        revision: int,
        source: str,
        deleted: bool,
        synced_at: datetime | None = None,
        _retry_count: int = 0,
    ) -> SpendingEventSyncResult:
        if game not in SPENDING_GAMES:
            raise ValueError("unsupported rhythm spending game")
        if source not in SPENDING_SOURCES:
            raise ValueError("unsupported rhythm spending source")
        if type(amount_yen) is not int or not 0 <= amount_yen <= 10_000_000:
            raise ValueError("amountYen must be a non-negative yen integer")
        if type(revision) is not int or revision < 1:
            raise ValueError("revision must be a positive integer")
        normalized_user_id = source_user_id.strip()
        if not normalized_user_id or len(normalized_user_id) > 100:
            raise ValueError("userId is invalid")
        normalized_synced_at = utc_naive(synced_at)
        normalized_occurred_at = utc_naive(occurred_at)
        local_date = self.local_date_for(occurred_at, time_zone)
        deleted_at = normalized_synced_at if deleted else None

        session = self.session_factory()
        try:
            row = session.scalar(
                select(RhythmSpendingEventRow).where(
                    RhythmSpendingEventRow.event_id == event_id
                )
            )
            if row is None:
                row = RhythmSpendingEventRow(
                    event_id=event_id,
                    rhythm_user_id=rhythm_user_id,
                    source_user_id=normalized_user_id,
                    game=game,
                    amount_yen=amount_yen,
                    occurred_at=normalized_occurred_at,
                    local_date=local_date,
                    time_zone=time_zone,
                    revision=revision,
                    source=source,
                    deleted_at=deleted_at,
                    discord_status="posted" if deleted else "pending",
                    synced_at=normalized_synced_at,
                    updated_at=normalized_synced_at,
                )
                session.add(row)
                session.commit()
                return SpendingEventSyncResult(_record(row), True, False)

            incoming = (
                rhythm_user_id,
                normalized_user_id,
                game,
                amount_yen,
                normalized_occurred_at,
                local_date,
                time_zone,
                source,
                deleted,
            )
            existing = (
                row.rhythm_user_id,
                row.source_user_id,
                row.game,
                row.amount_yen,
                row.occurred_at,
                row.local_date,
                row.time_zone,
                row.source,
                row.deleted_at is not None,
            )
            if revision < row.revision:
                raise StaleSpendingEventRevisionError(
                    "an older spending event revision was rejected"
                )
            if revision == row.revision:
                if incoming != existing:
                    raise SpendingEventRevisionConflictError(
                        "the same event revision has different content"
                    )
                return SpendingEventSyncResult(_record(row), False, True)

            # Never make a second worker claim while an older revision is in
            # flight.  Its completion records the message ID and then queues
            # this newer revision for edit/delete as needed.
            if row.discord_status in ACTIVE_DELIVERY_STATES:
                next_status = row.discord_status
            elif deleted and row.discord_message_id is None:
                next_status = "posted"
            else:
                next_status = "pending"
            expected_revision = row.revision
            result = session.execute(
                update(RhythmSpendingEventRow)
                .where(
                    RhythmSpendingEventRow.id == row.id,
                    RhythmSpendingEventRow.revision == expected_revision,
                )
                .values(
                    rhythm_user_id=rhythm_user_id,
                    source_user_id=normalized_user_id,
                    game=game,
                    amount_yen=amount_yen,
                    occurred_at=normalized_occurred_at,
                    local_date=local_date,
                    time_zone=time_zone,
                    revision=revision,
                    source=source,
                    deleted_at=deleted_at,
                    discord_status=next_status,
                    discord_last_error=None,
                    discord_next_retry_at=None,
                    synced_at=normalized_synced_at,
                    updated_at=normalized_synced_at,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                if _retry_count >= 4:
                    raise SpendingEventRevisionConflictError(
                        "spending event changed concurrently"
                    )
                return self.sync_event(
                    rhythm_user_id=rhythm_user_id,
                    event_id=event_id,
                    source_user_id=normalized_user_id,
                    game=game,
                    amount_yen=amount_yen,
                    occurred_at=occurred_at,
                    time_zone=time_zone,
                    revision=revision,
                    source=source,
                    deleted=deleted,
                    synced_at=normalized_synced_at,
                    _retry_count=_retry_count + 1,
                )
            session.commit()
            saved = session.get(RhythmSpendingEventRow, row.id)
            if saved is None:
                raise RuntimeError("spending event disappeared after update")
            return SpendingEventSyncResult(_record(saved), False, False)
        except IntegrityError:
            session.rollback()
            if _retry_count >= 4:
                raise
            return self.sync_event(
                rhythm_user_id=rhythm_user_id,
                event_id=event_id,
                source_user_id=normalized_user_id,
                game=game,
                amount_yen=amount_yen,
                occurred_at=occurred_at,
                time_zone=time_zone,
                revision=revision,
                source=source,
                deleted=deleted,
                synced_at=normalized_synced_at,
                _retry_count=_retry_count + 1,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_event(
        self, *, rhythm_user_id: int, event_id: str
    ) -> SpendingEventRecord | None:
        session = self.session_factory()
        try:
            row = session.scalar(
                select(RhythmSpendingEventRow).where(
                    RhythmSpendingEventRow.rhythm_user_id == rhythm_user_id,
                    RhythmSpendingEventRow.event_id == event_id,
                )
            )
            return _record(row) if row is not None else None
        finally:
            session.close()

    def daily_totals(
        self, *, rhythm_user_id: int, local_date: date, time_zone: str
    ) -> SpendingDayTotals:
        session = self.session_factory()
        try:
            rows = list(
                session.scalars(
                    select(RhythmSpendingEventRow).where(
                        RhythmSpendingEventRow.rhythm_user_id == rhythm_user_id,
                        RhythmSpendingEventRow.local_date == local_date,
                        RhythmSpendingEventRow.time_zone == time_zone,
                    )
                )
            )
            amounts = {"chunithm": 0, "maimai": 0, "ongeki": 0}
            active_count = 0
            latest_revision = 0
            updated_at: datetime | None = None
            for row in rows:
                latest_revision = max(latest_revision, row.revision)
                updated_at = max(updated_at, row.updated_at) if updated_at else row.updated_at
                if row.deleted_at is None:
                    amounts[row.game] += row.amount_yen
                    active_count += 1
            total = sum(amounts.values())
            return SpendingDayTotals(
                rhythm_user_id=rhythm_user_id,
                local_date=local_date,
                time_zone=time_zone,
                chunithm_amount_yen=amounts["chunithm"],
                maimai_amount_yen=amounts["maimai"],
                ongeki_amount_yen=amounts["ongeki"],
                total_amount_yen=total,
                active_event_count=active_count,
                has_events=bool(rows),
                latest_revision=latest_revision,
                updated_at=updated_at,
            )
        finally:
            session.close()

    def pending_events(
        self,
        *,
        rhythm_user_id: int,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[SpendingEventRecord]:
        checked_at = utc_naive(now)
        session = self.session_factory()
        try:
            rows = session.scalars(
                select(RhythmSpendingEventRow)
                .where(
                    RhythmSpendingEventRow.rhythm_user_id == rhythm_user_id,
                    or_(
                        RhythmSpendingEventRow.discord_status == "pending",
                        (
                            RhythmSpendingEventRow.discord_status == "retryWaiting"
                        )
                        & or_(
                            RhythmSpendingEventRow.discord_next_retry_at.is_(None),
                            RhythmSpendingEventRow.discord_next_retry_at <= checked_at,
                        ),
                    ),
                )
                .order_by(
                    RhythmSpendingEventRow.synced_at,
                    RhythmSpendingEventRow.id,
                )
                .limit(limit)
            ).all()
            return [_record(row) for row in rows]
        finally:
            session.close()

    def claim_event(
        self,
        *,
        rhythm_user_id: int,
        event_id: str,
        channel_id: int,
        now: datetime | None = None,
    ) -> SpendingEventDeliveryClaim | None:
        claimed_at = utc_naive(now)
        session = self.session_factory()
        try:
            row = session.scalar(
                select(RhythmSpendingEventRow).where(
                    RhythmSpendingEventRow.rhythm_user_id == rhythm_user_id,
                    RhythmSpendingEventRow.event_id == event_id,
                )
            )
            if row is None or row.discord_status not in QUEUED_DELIVERY_STATES:
                return None
            if (
                row.discord_status == "retryWaiting"
                and row.discord_next_retry_at is not None
                and row.discord_next_retry_at > claimed_at
            ):
                return None
            if row.deleted_at is not None and row.discord_message_id is None:
                row.discord_status = "posted"
                row.discord_channel_id = str(channel_id)
                row.discord_last_error = None
                row.discord_next_retry_at = None
                row.updated_at = claimed_at
                session.commit()
                return None
            if row.deleted_at is not None:
                operation = "deleting"
            elif row.discord_message_id is not None:
                operation = "editing"
            else:
                operation = "sending"
            previous_status = row.discord_status
            previous_attempt = row.discord_attempt_count
            result = session.execute(
                update(RhythmSpendingEventRow)
                .where(
                    RhythmSpendingEventRow.id == row.id,
                    RhythmSpendingEventRow.revision == row.revision,
                    RhythmSpendingEventRow.discord_status == previous_status,
                    RhythmSpendingEventRow.discord_attempt_count == previous_attempt,
                )
                .values(
                    discord_channel_id=str(channel_id),
                    discord_status=operation,
                    discord_attempt_count=previous_attempt + 1,
                    discord_claimed_at=claimed_at,
                    discord_next_retry_at=None,
                    discord_last_error=None,
                    updated_at=claimed_at,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                return None
            session.commit()
            saved = session.get(RhythmSpendingEventRow, row.id)
            if saved is None:
                raise RuntimeError("claimed spending event disappeared")
            record = _record(saved)
            return SpendingEventDeliveryClaim(
                record=record,
                operation=operation,
                claim_revision=record.revision,
                attempt_count=record.discord_attempt_count,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def finish_delivery(
        self,
        *,
        event_id: str,
        claim_revision: int,
        attempt_count: int,
        operation: str,
        channel_id: int,
        message_id: str | None,
        now: datetime | None = None,
    ) -> SpendingEventRecord:
        finished_at = utc_naive(now)
        session = self.session_factory()
        try:
            row = session.scalar(
                select(RhythmSpendingEventRow).where(
                    RhythmSpendingEventRow.event_id == event_id
                )
            )
            if row is None or row.discord_attempt_count != attempt_count:
                raise SpendingEventDeliveryConflictError("stale spending delivery claim")
            if row.discord_status != operation:
                raise SpendingEventDeliveryConflictError("delivery operation changed")

            if operation in {"sending", "editing"} and message_id is not None:
                row.discord_message_id = str(message_id)
            if operation == "deleting":
                # The retained ID is useful audit evidence; a later undelete
                # will try an edit and safely fall back to a new message on 404.
                pass
            row.discord_channel_id = str(channel_id)
            if row.revision == claim_revision:
                row.discord_status = "posted"
            elif operation == "deleting" and row.deleted_at is not None:
                row.discord_status = "posted"
            else:
                row.discord_status = "pending"
            row.discord_last_error = None
            row.discord_next_retry_at = None
            row.discord_claimed_at = None
            row.updated_at = finished_at
            session.commit()
            return _record(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def fail_delivery(
        self,
        *,
        event_id: str,
        claim_revision: int,
        attempt_count: int,
        operation: str,
        error_message: str,
        retryable: bool,
        retry_after_seconds: float | None,
        now: datetime | None = None,
    ) -> SpendingEventRecord:
        failed_at = utc_naive(now)
        session = self.session_factory()
        try:
            row = session.scalar(
                select(RhythmSpendingEventRow).where(
                    RhythmSpendingEventRow.event_id == event_id
                )
            )
            if row is None or row.discord_attempt_count != attempt_count:
                raise SpendingEventDeliveryConflictError("stale spending delivery claim")
            if row.discord_status != operation:
                raise SpendingEventDeliveryConflictError("delivery operation changed")
            if row.revision != claim_revision:
                row.discord_status = "pending"
                row.discord_next_retry_at = None
            elif retryable:
                delay = max(1.0, retry_after_seconds or 5.0)
                row.discord_status = "retryWaiting"
                row.discord_next_retry_at = failed_at + timedelta(seconds=delay)
            else:
                row.discord_status = "failed"
                row.discord_next_retry_at = None
            row.discord_last_error = _bounded_error(error_message)
            row.discord_claimed_at = None
            row.updated_at = failed_at
            session.commit()
            return _record(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def recover_stale_deliveries(
        self,
        *,
        now: datetime | None = None,
        claim_timeout_seconds: int = 300,
    ) -> int:
        checked_at = utc_naive(now)
        cutoff = checked_at - timedelta(seconds=claim_timeout_seconds)
        session = self.session_factory()
        try:
            rows = list(
                session.scalars(
                    select(RhythmSpendingEventRow).where(
                        RhythmSpendingEventRow.discord_status.in_(
                            ACTIVE_DELIVERY_STATES
                        ),
                        RhythmSpendingEventRow.discord_claimed_at <= cutoff,
                    )
                )
            )
            for row in rows:
                if row.discord_status == "sending":
                    row.discord_status = "failed"
                    row.discord_last_error = (
                        "前回のDiscord新規投稿結果を確認できません。"
                        "重複防止のため自動再送しません。"
                    )
                    row.discord_next_retry_at = None
                else:
                    row.discord_status = "retryWaiting"
                    row.discord_last_error = "中断された編集または削除を再試行します。"
                    row.discord_next_retry_at = checked_at
                row.discord_claimed_at = None
                row.updated_at = checked_at
            session.commit()
            return len(rows)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mark_day_delivery(
        self,
        *,
        rhythm_user_id: int,
        local_date: date,
        channel_id: int,
        message_id: str | None,
        now: datetime | None = None,
    ) -> int:
        changed_at = utc_naive(now)
        session = self.session_factory()
        try:
            result = session.execute(
                update(RhythmSpendingEventRow)
                .where(
                    RhythmSpendingEventRow.rhythm_user_id == rhythm_user_id,
                    RhythmSpendingEventRow.local_date == local_date,
                    ~RhythmSpendingEventRow.discord_status.in_(
                        ACTIVE_DELIVERY_STATES
                    ),
                )
                .values(
                    discord_channel_id=str(channel_id),
                    discord_message_id=str(message_id) if message_id is not None else None,
                    discord_status="posted",
                    discord_last_error=None,
                    discord_next_retry_at=None,
                    discord_claimed_at=None,
                    updated_at=changed_at,
                )
            )
            session.commit()
            return int(result.rowcount)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dates_with_events(
        self, *, rhythm_user_id: int, limit: int = 31
    ) -> list[date]:
        session = self.session_factory()
        try:
            return list(
                session.scalars(
                    select(RhythmSpendingEventRow.local_date)
                    .where(RhythmSpendingEventRow.rhythm_user_id == rhythm_user_id)
                    .group_by(RhythmSpendingEventRow.local_date)
                    .order_by(func.max(RhythmSpendingEventRow.updated_at).desc())
                    .limit(limit)
                )
            )
        finally:
            session.close()
