"""Durable task and daily-routine reminders.

The scheduler in this module is deliberately small: SQLite owns the
occurrence key and claim state, while Discord is only called after a claim has
been committed.  A failed send stays retryable and a process which dies while
sending leaves a lease that a later poll can recover.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Any, Protocol
from unicodedata import normalize as unicode_normalize
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import DailyRoutine, LifeTask, ReminderDelivery, RoutineCompletion

logger = logging.getLogger(__name__)

ROUTINE_TITLE_MAX_LENGTH = 255
ROUTINE_TIME_PATTERN = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


class ReminderPhase(StrEnum):
    TWENTY_FOUR_HOURS = "24h"
    ONE_HOUR = "1h"
    DUE = "due"
    OVERDUE = "overdue"
    ROUTINE = "routine"


_TASK_PHASE_RANK = {
    ReminderPhase.TWENTY_FOUR_HOURS.value: 1,
    ReminderPhase.ONE_HOUR.value: 2,
    ReminderPhase.DUE.value: 3,
    ReminderPhase.OVERDUE.value: 4,
}


class ReminderDeliveryStatus(StrEnum):
    PENDING = "pending"
    CLAIMING = "claiming"
    SENT = "sent"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TaskReminderCandidate:
    """The one task phase that is relevant at a particular polling instant."""

    task_id: int
    title: str
    phase: str
    occurrence_key: str
    due_at: datetime
    local_due_at: datetime
    local_date: date
    date_only: bool

    @property
    def is_overdue(self) -> bool:
        return self.phase == ReminderPhase.OVERDUE.value


@dataclass(frozen=True, slots=True)
class RoutineReminderCandidate:
    routine_id: int
    title: str
    reminder_time: str
    local_date: date
    phase: str = ReminderPhase.ROUTINE.value

    @property
    def occurrence_key(self) -> str:
        return self.local_date.isoformat()


@dataclass(frozen=True, slots=True)
class ReminderNotification:
    """A safe-to-send notification detached from an ORM session."""

    subject_type: str
    subject_id: int
    phase: str
    occurrence_key: str
    content: str
    title: str
    due_at: datetime | None = None
    local_date: date | None = None
    reminder_time: str | None = None

    @property
    def task_id(self) -> int | None:
        return self.subject_id if self.subject_type == "task" else None

    @property
    def routine_id(self) -> int | None:
        return self.subject_id if self.subject_type == "routine" else None

    @property
    def kind(self) -> str:
        """Compatibility alias for adapters that call the subject a kind."""

        return self.subject_type


@dataclass(frozen=True, slots=True)
class _Claim:
    delivery_id: int
    token: str


class ReminderSender(Protocol):
    def __call__(self, notification: ReminderNotification) -> Awaitable[Any] | Any: ...


def _zone_info(value: ZoneInfo | str) -> ZoneInfo:
    if isinstance(value, ZoneInfo):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timezone must be a valid IANA timezone")
    try:
        return ZoneInfo(value.strip())
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _utc_naive(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("clock must return a datetime")
    if value.tzinfo is None:
        # SQLite DateTime values in this application are UTC-naive.
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _utc_aware(value: datetime) -> datetime:
    return _utc_naive(value).replace(tzinfo=UTC)


def _local(value: datetime, timezone: ZoneInfo) -> datetime:
    return _utc_aware(value).astimezone(timezone)


def parse_hhmm(value: str) -> time:
    """Parse exactly one 24-hour ``HH:MM`` local time."""

    if not isinstance(value, str) or ROUTINE_TIME_PATTERN.fullmatch(value.strip()) is None:
        raise ValueError("reminder time must be HH:MM")
    hour_text, minute_text = value.split(":")
    return time(hour=int(hour_text), minute=int(minute_text))


def normalize_routine_title(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("routine title must be a string")
    title = " ".join(unicode_normalize("NFKC", value).split())
    if not title:
        raise ValueError("routine title must not be blank")
    if len(title) > ROUTINE_TITLE_MAX_LENGTH:
        raise ValueError("routine title is too long")
    return title


def _task_context(
    task: LifeTask | Any,
    timezone: ZoneInfo,
) -> tuple[datetime, datetime, bool] | None:
    raw_due_at = getattr(task, "due_at", None)
    if raw_due_at is None:
        return None
    due_at = _utc_naive(raw_due_at)
    local_due_at = _local(due_at, timezone)
    date_only = (
        local_due_at.hour == 0
        and local_due_at.minute == 0
        and local_due_at.second == 0
        and local_due_at.microsecond == 0
    )
    if date_only:
        target_local = datetime.combine(
            local_due_at.date(), time(hour=9), tzinfo=timezone
        )
        target_due_at = target_local.astimezone(UTC).replace(tzinfo=None)
    else:
        target_local = local_due_at
        target_due_at = due_at
    return target_due_at, target_local, date_only


def select_task_reminder_phase(
    task: LifeTask | Any,
    now: datetime,
    timezone: ZoneInfo | str = "Asia/Tokyo",
    *,
    due_grace_seconds: int = 300,
) -> TaskReminderCandidate | None:
    """Select one current phase, never all phases missed since the last poll.

    Date-only tasks are identified by the existing local-midnight storage
    convention and become due at 09:00 on their local due date.  They remain in
    the due phase for the rest of that date; daily overdue occurrences begin at
    09:00 on the following date.  A date without an explicit time must not be
    presented as overdue merely because the 09:00 polling minute was missed.
    """

    if getattr(task, "status", "open") != "open":
        return None
    if due_grace_seconds < 0:
        raise ValueError("due grace must not be negative")
    timezone = _zone_info(timezone)
    context = _task_context(task, timezone)
    if context is None:
        return None
    due_at, local_due_at, date_only = context
    current = _utc_aware(now)
    target = _utc_aware(due_at)
    phase: str | None = None
    occurrence_key: str | None = None

    if date_only:
        if current < target:
            return None
        current_local = current.astimezone(timezone)
        if current_local.date() == local_due_at.date():
            phase = ReminderPhase.DUE.value
            occurrence_key = local_due_at.date().isoformat()
        else:
            if current_local.time() >= time(9):
                phase = ReminderPhase.OVERDUE.value
                occurrence_key = current_local.date().isoformat()
            else:
                return None
    else:
        if current < target - timedelta(hours=24):
            return None
        if current < target - timedelta(hours=1):
            phase = ReminderPhase.TWENTY_FOUR_HOURS.value
        elif current < target:
            phase = ReminderPhase.ONE_HOUR.value
        elif current <= target + timedelta(seconds=due_grace_seconds):
            phase = ReminderPhase.DUE.value
        else:
            current_local = current.astimezone(timezone)
            if current_local.date() == local_due_at.date() and current_local.time() < time(9):
                phase = ReminderPhase.DUE.value
            elif current_local.time() >= time(9):
                phase = ReminderPhase.OVERDUE.value
                occurrence_key = current_local.date().isoformat()
            else:
                return None

        if occurrence_key is None:
            occurrence_key = target.isoformat(timespec="seconds")

    return TaskReminderCandidate(
        task_id=int(task.id),
        title=str(task.title),
        phase=phase,
        occurrence_key=occurrence_key,
        due_at=due_at,
        local_due_at=local_due_at,
        local_date=local_due_at.date(),
        date_only=date_only,
    )


# Short aliases keep the pure scheduling rule easy to discover in adapters and tests.
task_reminder_candidate = select_task_reminder_phase
select_reminder_phase = select_task_reminder_phase


def select_routine_reminder(
    routine: DailyRoutine | Any,
    now: datetime,
    timezone: ZoneInfo | str = "Asia/Tokyo",
) -> RoutineReminderCandidate | None:
    if not bool(getattr(routine, "enabled", False)):
        return None
    timezone = _zone_info(timezone)
    scheduled = parse_hhmm(str(routine.reminder_time))
    current_local = _local(_utc_naive(now), timezone)
    current_minute = time(
        hour=current_local.hour,
        minute=current_local.minute,
    )
    if current_minute < scheduled:
        return None
    return RoutineReminderCandidate(
        routine_id=int(routine.id),
        title=str(routine.title),
        reminder_time=str(routine.reminder_time),
        local_date=current_local.date(),
    )


routine_reminder_candidate = select_routine_reminder


@dataclass(frozen=True, slots=True)
class RoutineView:
    id: int
    title: str
    reminder_time: str
    enabled: bool
    created_at: datetime
    updated_at: datetime
    completed_today: bool = False


class RoutineService:
    """CRUD and local-date completion operations for daily routines."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        timezone: ZoneInfo | str = "Asia/Tokyo",
        *,
        timezone_name: str | None = None,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if now is not None and clock is not None:
            raise ValueError("provide only one routine clock")
        self.session_factory = session_factory
        self.timezone = _zone_info(timezone_name if timezone_name is not None else timezone)
        self._clock = clock or now or _utc_now

    def _now(self) -> datetime:
        return _utc_naive(self._clock())

    def local_date(self, moment: datetime | None = None) -> date:
        return _local(moment or self._now(), self.timezone).date()

    @staticmethod
    def _routine_id(routine_id: int) -> int:
        if isinstance(routine_id, bool) or not isinstance(routine_id, int) or routine_id <= 0:
            raise ValueError("routine ID must be a positive integer")
        return routine_id

    @staticmethod
    def _view(
        routine: DailyRoutine,
        *,
        completed_today: bool = False,
    ) -> RoutineView:
        return RoutineView(
            id=routine.id,
            title=routine.title,
            reminder_time=routine.reminder_time,
            enabled=bool(routine.enabled),
            created_at=routine.created_at,
            updated_at=routine.updated_at,
            completed_today=completed_today,
        )

    def create(self, title: str, reminder_time: str) -> RoutineView:
        normalized_title = normalize_routine_title(title)
        normalized_time = parse_hhmm(reminder_time)
        stored_time = normalized_time.strftime("%H:%M")
        now = self._now()
        session = self.session_factory()
        try:
            routine = DailyRoutine(
                title=normalized_title,
                reminder_time=stored_time,
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            session.add(routine)
            session.flush()
            result = self._view(routine)
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def add_routine(self, title: str, reminder_time: str) -> RoutineView:
        return self.create(title, reminder_time)

    def add(self, title: str, reminder_time: str) -> RoutineView:
        return self.create(title, reminder_time)

    def list_routines(
        self,
        *,
        include_disabled: bool = True,
        now: datetime | None = None,
    ) -> list[RoutineView]:
        timestamp = _utc_naive(now or self._now())
        today = _local(timestamp, self.timezone).date()
        session = self.session_factory()
        try:
            statement = select(DailyRoutine).order_by(
                DailyRoutine.enabled.desc(),
                DailyRoutine.reminder_time,
                DailyRoutine.id,
            )
            if not include_disabled:
                statement = statement.where(DailyRoutine.enabled.is_(True))
            routines = list(session.scalars(statement).all())
            completed_ids = {
                routine_id
                for routine_id in session.scalars(
                    select(RoutineCompletion.routine_id).where(
                        RoutineCompletion.local_date == today
                    )
                ).all()
            }
            return [
                self._view(routine, completed_today=routine.id in completed_ids)
                for routine in routines
            ]
        finally:
            session.close()

    def list(self, **kwargs: object) -> list[RoutineView]:
        return self.list_routines(**kwargs)  # type: ignore[arg-type]

    def get(self, routine_id: int) -> RoutineView | None:
        routine_id = self._routine_id(routine_id)
        session = self.session_factory()
        try:
            routine = session.get(DailyRoutine, routine_id)
            if routine is None:
                return None
            today = self.local_date()
            completed = session.scalar(
                select(RoutineCompletion.id).where(
                    RoutineCompletion.routine_id == routine_id,
                    RoutineCompletion.local_date == today,
                )
            )
            return self._view(routine, completed_today=completed is not None)
        finally:
            session.close()

    def complete(
        self,
        routine_id: int,
        *,
        local_date: date | str | None = None,
    ) -> RoutineView:
        routine_id = self._routine_id(routine_id)
        if local_date is None:
            target_date = self.local_date()
        elif isinstance(local_date, date) and not isinstance(local_date, datetime):
            target_date = local_date
        elif isinstance(local_date, str):
            try:
                target_date = date.fromisoformat(local_date)
            except ValueError:
                raise ValueError("local date must be YYYY-MM-DD") from None
            if target_date.isoformat() != local_date:
                raise ValueError("local date must be YYYY-MM-DD")
        else:
            raise ValueError("local date must be YYYY-MM-DD")

        now = self._now()
        session = self.session_factory()
        try:
            routine = session.get(DailyRoutine, routine_id)
            if routine is None:
                raise ValueError("routine not found")
            completion = RoutineCompletion(
                routine_id=routine_id,
                local_date=target_date,
                completed_at=now,
            )
            session.add(completion)
            try:
                session.commit()
            except IntegrityError:
                # The unique key makes a double click or two Bot instances
                # completing the same date an idempotent operation.
                session.rollback()
            return self._view(
                routine,
                completed_today=target_date == self.local_date(now),
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def complete_for_date(self, routine_id: int, local_date: date | str) -> RoutineView:
        return self.complete(routine_id, local_date=local_date)

    def is_completed(self, routine_id: int, local_date: date | str | None = None) -> bool:
        routine_id = self._routine_id(routine_id)
        target_date = self.local_date() if local_date is None else local_date
        if isinstance(target_date, str):
            try:
                target_date = date.fromisoformat(target_date)
            except ValueError:
                return False
        if not isinstance(target_date, date) or isinstance(target_date, datetime):
            return False
        session = self.session_factory()
        try:
            return (
                session.scalar(
                    select(RoutineCompletion.id).where(
                        RoutineCompletion.routine_id == routine_id,
                        RoutineCompletion.local_date == target_date,
                    )
                )
                is not None
            )
        finally:
            session.close()


class ReminderService:
    """Create, claim, send, and finalize durable reminder occurrences."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        timezone: ZoneInfo | str = "Asia/Tokyo",
        *,
        timezone_name: str | None = None,
        sender: ReminderSender | None = None,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], datetime] | None = None,
        claim_timeout_seconds: int = 300,
        due_grace_seconds: int = 300,
        owner_id: str | None = None,
    ) -> None:
        if now is not None and clock is not None:
            raise ValueError("provide only one reminder clock")
        if claim_timeout_seconds <= 0:
            raise ValueError("claim timeout must be greater than zero")
        if due_grace_seconds < 0:
            raise ValueError("due grace must not be negative")
        self.session_factory = session_factory
        self.timezone = _zone_info(timezone_name if timezone_name is not None else timezone)
        self.sender = sender
        self._clock = clock or now or _utc_now
        self.claim_timeout_seconds = claim_timeout_seconds
        self.due_grace_seconds = due_grace_seconds
        self.owner_id = owner_id or f"reminder-{uuid4()}"

    def _now(self) -> datetime:
        return _utc_naive(self._clock())

    def recover_stale_claims(self, *, now: datetime | None = None) -> int:
        timestamp = _utc_naive(now or self._now())
        session = self.session_factory()
        try:
            result = session.execute(
                update(ReminderDelivery)
                .where(
                    ReminderDelivery.status == ReminderDeliveryStatus.CLAIMING.value,
                    or_(
                        ReminderDelivery.claim_expires_at.is_(None),
                        ReminderDelivery.claim_expires_at <= timestamp,
                    ),
                )
                .values(
                    status=ReminderDeliveryStatus.PENDING.value,
                    claim_token=None,
                    claim_expires_at=None,
                    updated_at=timestamp,
                )
            )
            session.commit()
            return int(result.rowcount or 0)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _task_notification(
        self,
        task: LifeTask,
        candidate: TaskReminderCandidate,
    ) -> ReminderNotification:
        deadline = candidate.local_due_at.strftime("%Y-%m-%d %H:%M")
        if candidate.phase == ReminderPhase.TWENTY_FOUR_HOURS.value:
            phase_label = "24時間前"
        elif candidate.phase == ReminderPhase.ONE_HOUR.value:
            phase_label = "1時間前"
        elif candidate.phase == ReminderPhase.DUE.value:
            phase_label = "期限時刻"
        else:
            phase_label = f"期限超過（{candidate.occurrence_key}）"
        if candidate.date_only:
            deadline = f"{candidate.local_due_at.date().isoformat()} 09:00"
        content = (
            f"【期限リマインダー】\n"
            f"タスク #{task.id}: {task.title}\n"
            f"{phase_label} / 期限: {deadline}（{self.timezone.key}）"
        )
        return ReminderNotification(
            subject_type="task",
            subject_id=task.id,
            phase=candidate.phase,
            occurrence_key=candidate.occurrence_key,
            content=content,
            title=task.title,
            due_at=candidate.due_at,
            local_date=(
                date.fromisoformat(candidate.occurrence_key)
                if candidate.phase == ReminderPhase.OVERDUE.value
                else candidate.local_date
            ),
        )

    @staticmethod
    def _routine_notification(
        routine: DailyRoutine,
        candidate: RoutineReminderCandidate,
    ) -> ReminderNotification:
        content = (
            f"【日課リマインダー】\n"
            f"日課 #{routine.id}: {routine.title}\n"
            f"{candidate.local_date.isoformat()} {routine.reminder_time}\n"
            f"完了したら /日課完了 routine_id:{routine.id}"
        )
        return ReminderNotification(
            subject_type="routine",
            subject_id=routine.id,
            phase=candidate.phase,
            occurrence_key=candidate.occurrence_key,
            content=content,
            title=routine.title,
            local_date=candidate.local_date,
            reminder_time=routine.reminder_time,
        )

    def _task_candidate_from_delivery(
        self,
        task: LifeTask,
        delivery: ReminderDelivery,
        *,
        now: datetime,
    ) -> TaskReminderCandidate | None:
        """Rebuild one failed occurrence without replaying every old phase."""

        if delivery.phase not in _TASK_PHASE_RANK:
            return None
        context = _task_context(task, self.timezone)
        if context is None:
            return None
        due_at, local_due_at, date_only = context
        current_local = _local(now, self.timezone)
        if date_only:
            if (
                delivery.phase != ReminderPhase.DUE.value
                or delivery.occurrence_key != local_due_at.date().isoformat()
                or current_local.date() != local_due_at.date()
                or current_local.time() < time(9)
            ):
                return None
        elif delivery.phase != ReminderPhase.OVERDUE.value:
            if delivery.occurrence_key != _utc_aware(due_at).isoformat(timespec="seconds"):
                return None
            if current_local.time() < time(9) and current_local.date() > local_due_at.date():
                return None
        else:
            try:
                overdue_date = date.fromisoformat(delivery.occurrence_key)
            except ValueError:
                return None
            if (
                overdue_date > current_local.date()
                or current_local.time() < time(9)
            ):
                return None
        return TaskReminderCandidate(
            task_id=task.id,
            title=task.title,
            phase=delivery.phase,
            occurrence_key=delivery.occurrence_key,
            due_at=due_at,
            local_due_at=local_due_at,
            local_date=local_due_at.date(),
            date_only=date_only,
        )

    def _failed_task_notification(
        self,
        task: LifeTask,
        failed_deliveries: list[ReminderDelivery],
        *,
        now: datetime,
    ) -> ReminderNotification | None:
        for delivery in sorted(
            failed_deliveries,
            key=lambda item: (_TASK_PHASE_RANK.get(item.phase, 0), item.id),
            reverse=True,
        ):
            candidate = self._task_candidate_from_delivery(task, delivery, now=now)
            if candidate is not None:
                return self._task_notification(task, candidate)
        return None

    @staticmethod
    def _routine_candidate_from_delivery(
        routine: DailyRoutine,
        delivery: ReminderDelivery,
    ) -> RoutineReminderCandidate | None:
        if delivery.phase != ReminderPhase.ROUTINE.value:
            return None
        try:
            local_date = date.fromisoformat(delivery.occurrence_key)
        except ValueError:
            return None
        try:
            parse_hhmm(routine.reminder_time)
        except ValueError:
            return None
        return RoutineReminderCandidate(
            routine_id=routine.id,
            title=routine.title,
            reminder_time=routine.reminder_time,
            local_date=local_date,
        )

    def _failed_routine_notification(
        self,
        routine: DailyRoutine,
        failed_deliveries: list[ReminderDelivery],
        *,
        local_date: date,
    ) -> ReminderNotification | None:
        for delivery in sorted(failed_deliveries, key=lambda item: item.id, reverse=True):
            candidate = self._routine_candidate_from_delivery(routine, delivery)
            if candidate is not None and candidate.local_date == local_date:
                return self._routine_notification(routine, candidate)
        return None

    def _notifications(self, timestamp: datetime) -> list[ReminderNotification]:
        session = self.session_factory()
        try:
            notifications: list[ReminderNotification] = []
            deliveries = list(session.scalars(select(ReminderDelivery)).all())
            delivery_by_key = {
                delivery.dedupe_key: delivery for delivery in deliveries
            }
            tasks = session.scalars(
                select(LifeTask).where(
                    LifeTask.status == "open",
                    LifeTask.due_at.is_not(None),
                )
            ).all()
            for task in tasks:
                candidate = select_task_reminder_phase(
                    task,
                    timestamp,
                    self.timezone,
                    due_grace_seconds=self.due_grace_seconds,
                )
                if candidate is not None:
                    current = self._task_notification(task, candidate)
                    delivery = delivery_by_key.get(self._dedupe_key(current))
                    if delivery is None or delivery.status != ReminderDeliveryStatus.SENT.value:
                        notifications.append(current)

            current_local_date = _local(timestamp, self.timezone).date()
            completed_routine_ids = {
                routine_id
                for routine_id in session.scalars(
                    select(RoutineCompletion.routine_id).where(
                        RoutineCompletion.local_date == current_local_date
                    )
                ).all()
            }
            routines = session.scalars(
                select(DailyRoutine).where(DailyRoutine.enabled.is_(True))
            ).all()
            for routine in routines:
                if routine.id in completed_routine_ids:
                    continue
                candidate = select_routine_reminder(routine, timestamp, self.timezone)
                if candidate is not None:
                    current = self._routine_notification(routine, candidate)
                    delivery = delivery_by_key.get(self._dedupe_key(current))
                    if delivery is None or delivery.status != ReminderDeliveryStatus.SENT.value:
                        notifications.append(current)
            return notifications
        finally:
            session.close()

    @staticmethod
    def _dedupe_key(notification: ReminderNotification) -> str:
        return (
            f"{notification.subject_type}:{notification.subject_id}:"
            f"{notification.phase}:{notification.occurrence_key}"
        )

    def _claim(
        self,
        notification: ReminderNotification,
        *,
        now: datetime,
    ) -> _Claim | None:
        dedupe_key = self._dedupe_key(notification)
        timestamp = _utc_naive(now)
        token = uuid4().hex
        session = self.session_factory()
        try:
            bind = session.get_bind()
            if bind.dialect.name == "sqlite":
                session.execute(
                    sqlite_insert(ReminderDelivery)
                    .values(
                        subject_type=notification.subject_type,
                        subject_id=notification.subject_id,
                        phase=notification.phase,
                        occurrence_key=notification.occurrence_key,
                        dedupe_key=dedupe_key,
                        status=ReminderDeliveryStatus.PENDING.value,
                        attempt_count=0,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                    .on_conflict_do_nothing(index_elements=["dedupe_key"])
                )
            else:
                existing = session.scalar(
                    select(ReminderDelivery).where(
                        ReminderDelivery.dedupe_key == dedupe_key
                    )
                )
                if existing is None:
                    session.add(
                        ReminderDelivery(
                            subject_type=notification.subject_type,
                            subject_id=notification.subject_id,
                            phase=notification.phase,
                            occurrence_key=notification.occurrence_key,
                            dedupe_key=dedupe_key,
                            status=ReminderDeliveryStatus.PENDING.value,
                            attempt_count=0,
                            created_at=timestamp,
                            updated_at=timestamp,
                        )
                    )
            session.flush()
            delivery = session.scalar(
                select(ReminderDelivery).where(ReminderDelivery.dedupe_key == dedupe_key)
            )
            if delivery is None or delivery.status == ReminderDeliveryStatus.SENT.value:
                session.commit()
                return None

            claimable = or_(
                ReminderDelivery.status.in_(
                    (
                        ReminderDeliveryStatus.PENDING.value,
                        ReminderDeliveryStatus.FAILED.value,
                    )
                ),
                (
                    (ReminderDelivery.status == ReminderDeliveryStatus.CLAIMING.value)
                    & (
                        ReminderDelivery.claim_expires_at.is_(None)
                        | (ReminderDelivery.claim_expires_at <= timestamp)
                    )
                ),
            )
            result = session.execute(
                update(ReminderDelivery)
                .where(
                    ReminderDelivery.id == delivery.id,
                    claimable,
                )
                .values(
                    status=ReminderDeliveryStatus.CLAIMING.value,
                    claim_token=token,
                    claim_expires_at=timestamp
                    + timedelta(seconds=self.claim_timeout_seconds),
                    attempt_count=ReminderDelivery.attempt_count + 1,
                    updated_at=timestamp,
                )
            )
            session.commit()
            if result.rowcount != 1:
                return None
            return _Claim(delivery_id=delivery.id, token=token)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _still_eligible(self, notification: ReminderNotification, timestamp: datetime) -> bool:
        session = self.session_factory()
        try:
            if notification.subject_type == "task":
                task = session.get(LifeTask, notification.subject_id)
                if task is None or task.status != "open":
                    return False
                context = _task_context(task, self.timezone)
                if context is None or notification.phase not in _TASK_PHASE_RANK:
                    return False
                due_at, local_due_at, date_only = context
                if notification.phase == ReminderPhase.OVERDUE.value:
                    try:
                        overdue_date = date.fromisoformat(notification.occurrence_key)
                    except ValueError:
                        return False
                    current_local = _local(timestamp, self.timezone)
                    return (
                        not date_only
                        or overdue_date >= local_due_at.date()
                    ) and overdue_date <= current_local.date() and current_local.time() >= time(9)
                if date_only:
                    current_local = _local(timestamp, self.timezone)
                    return (
                        notification.phase == ReminderPhase.DUE.value
                        and notification.occurrence_key == local_due_at.date().isoformat()
                        and current_local.date() == local_due_at.date()
                        and current_local.time() >= time(9)
                    )
                return (
                    notification.occurrence_key
                    == _utc_aware(due_at).isoformat(timespec="seconds")
                )
            if notification.subject_type == "routine":
                routine = session.get(DailyRoutine, notification.subject_id)
                if routine is None or not routine.enabled:
                    return False
                if session.scalar(
                    select(RoutineCompletion.id).where(
                        RoutineCompletion.routine_id == routine.id,
                        RoutineCompletion.local_date == notification.local_date,
                    )
                ) is not None:
                    return False
                if notification.local_date is None:
                    return False
                current_local = _local(timestamp, self.timezone)
                if notification.local_date != current_local.date():
                    return False
                scheduled = parse_hhmm(routine.reminder_time)
                return not (
                    notification.local_date == current_local.date()
                    and time(current_local.hour, current_local.minute) < scheduled
                )
            return False
        finally:
            session.close()

    def _release_claim(self, claim: _Claim) -> None:
        session = self.session_factory()
        try:
            session.execute(
                delete(ReminderDelivery).where(
                    ReminderDelivery.id == claim.delivery_id,
                    ReminderDelivery.status == ReminderDeliveryStatus.CLAIMING.value,
                    ReminderDelivery.claim_token == claim.token,
                )
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _mark_sent(self, claim: _Claim, *, now: datetime) -> bool:
        timestamp = _utc_naive(now)
        session = self.session_factory()
        try:
            result = session.execute(
                update(ReminderDelivery)
                .where(
                    ReminderDelivery.id == claim.delivery_id,
                    ReminderDelivery.status == ReminderDeliveryStatus.CLAIMING.value,
                    ReminderDelivery.claim_token == claim.token,
                )
                .values(
                    status=ReminderDeliveryStatus.SENT.value,
                    claim_token=None,
                    claim_expires_at=None,
                    sent_at=timestamp,
                    last_error=None,
                    updated_at=timestamp,
                )
            )
            session.commit()
            return result.rowcount == 1
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _safe_error_code(exc: Exception) -> str:
        name = type(exc).__name__
        if re.fullmatch(r"[A-Za-z0-9_]+", name) is None:
            return "send_failed"
        return f"send_failed:{name[:96]}"

    def _mark_failed(self, claim: _Claim, *, now: datetime, error_code: str) -> bool:
        timestamp = _utc_naive(now)
        session = self.session_factory()
        try:
            result = session.execute(
                update(ReminderDelivery)
                .where(
                    ReminderDelivery.id == claim.delivery_id,
                    ReminderDelivery.status == ReminderDeliveryStatus.CLAIMING.value,
                    ReminderDelivery.claim_token == claim.token,
                )
                .values(
                    status=ReminderDeliveryStatus.FAILED.value,
                    claim_token=None,
                    claim_expires_at=None,
                    last_error=error_code,
                    updated_at=timestamp,
                )
            )
            session.commit()
            return result.rowcount == 1
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    async def _send(
        self,
        sender: ReminderSender,
        notification: ReminderNotification,
    ) -> None:
        callback: Any = sender
        if not callable(callback):
            callback = getattr(sender, "send", None)
        if not callable(callback):
            raise TypeError("reminder sender is not callable")
        result = callback(notification)
        if inspect.isawaitable(result):
            await result

    async def poll_once(
        self,
        sender: ReminderSender | None = None,
        *,
        now: datetime | None = None,
    ) -> list[ReminderNotification]:
        """Send at most one currently relevant phase per subject in one poll."""

        timestamp = _utc_naive(now or self._now())
        self.recover_stale_claims(now=timestamp)
        callback = sender or self.sender
        if callback is None:
            raise ValueError("a reminder sender is required")
        sent: list[ReminderNotification] = []
        for notification in self._notifications(timestamp):
            claim = self._claim(notification, now=timestamp)
            if claim is None:
                continue
            if not self._still_eligible(notification, timestamp):
                self._release_claim(claim)
                continue
            try:
                await self._send(callback, notification)
            except Exception as exc:
                error_code = self._safe_error_code(exc)
                try:
                    self._mark_failed(claim, now=timestamp, error_code=error_code)
                except Exception as state_exc:
                    logger.warning(
                        "reminder failure state update failed (%s)",
                        type(state_exc).__name__,
                    )
                logger.warning("reminder send failed (%s)", type(exc).__name__)
                continue
            try:
                finalized = self._mark_sent(claim, now=timestamp)
            except Exception as exc:
                logger.warning("reminder sent state update failed (%s)", type(exc).__name__)
                finalized = False
            if finalized:
                sent.append(notification)
            else:
                # Discord accepted the call, but another worker may have
                # recovered the lease.  Do not claim local success or create a
                # second message in this poll.
                logger.warning("reminder claim ownership was lost after send")
        return sent

    async def poll(self, *args: Any, **kwargs: Any) -> list[ReminderNotification]:
        return await self.poll_once(*args, **kwargs)

    def get_delivery(self, dedupe_key: str) -> ReminderDelivery | None:
        session = self.session_factory()
        try:
            return session.scalar(
                select(ReminderDelivery).where(ReminderDelivery.dedupe_key == dedupe_key)
            )
        finally:
            session.close()

    def list_deliveries(self) -> list[ReminderDelivery]:
        session = self.session_factory()
        try:
            return list(
                session.scalars(
                    select(ReminderDelivery).order_by(ReminderDelivery.id)
                ).all()
            )
        finally:
            session.close()


class ReminderScheduler:
    """Cleanly cancellable poll loop for a running Discord Bot."""

    def __init__(
        self,
        service: ReminderService,
        sender: ReminderSender,
        *,
        enabled: bool = False,
        poll_interval_seconds: int = 60,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        if poll_interval_seconds < 1 or poll_interval_seconds > 3_600:
            raise ValueError("reminder poll interval must be between 1 and 3600 seconds")
        self.service = service
        self.sender = sender
        self.enabled = enabled
        self.poll_interval_seconds = poll_interval_seconds
        self.now = now or _utc_now
        self.sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running or not self.enabled:
            return
        self._closed = False
        self.service.recover_stale_claims(now=self.now())
        self._task = asyncio.create_task(self._loop(), name="life-inbox-reminder-scheduler")

    async def _loop(self) -> None:
        while not self._closed:
            try:
                await self.service.poll_once(self.sender, now=self.now())
            except Exception as exc:
                logger.warning("reminder poll failed (%s)", type(exc).__name__)
            if self._closed:
                return
            # Sleep only controls when the next durable poll begins; waking is
            # never treated as a successful reminder delivery.
            await self.sleep(self.poll_interval_seconds)

    async def poll_once(self) -> list[ReminderNotification]:
        return await self.service.poll_once(self.sender, now=self.now())

    async def close(self) -> None:
        self._closed = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


__all__ = [
    "ReminderDeliveryStatus",
    "ReminderNotification",
    "ReminderPhase",
    "ReminderScheduler",
    "ReminderService",
    "RoutineReminderCandidate",
    "RoutineService",
    "RoutineView",
    "TaskReminderCandidate",
    "normalize_routine_title",
    "parse_hhmm",
    "routine_reminder_candidate",
    "select_reminder_phase",
    "select_routine_reminder",
    "select_task_reminder_phase",
    "task_reminder_candidate",
]
