"""Credential-free, provider-neutral life task persistence.

The service in this module owns only local SQLite reads and writes.  Future
Google Classroom and Notion readers can translate their read-only results into
``upsert_external`` calls without making those providers part of the local
task or Discord layers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum
from unicodedata import normalize as unicode_normalize
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import LifeTask

TITLE_MAX_LENGTH = 255
DESCRIPTION_MAX_LENGTH = 4_000
EXTERNAL_ID_MAX_LENGTH = 255
SOURCE_URL_MAX_LENGTH = 2_048

# More explicit aliases make the bounds discoverable to callers and tests.
TASK_TITLE_MAX_LENGTH = TITLE_MAX_LENGTH
TASK_DESCRIPTION_MAX_LENGTH = DESCRIPTION_MAX_LENGTH
TASK_EXTERNAL_ID_MAX_LENGTH = EXTERNAL_ID_MAX_LENGTH
TASK_SOURCE_URL_MAX_LENGTH = SOURCE_URL_MAX_LENGTH


class TaskSource(StrEnum):
    MANUAL = "manual"
    GOOGLE_CLASSROOM = "google_classroom"
    NOTION = "notion"


class TaskStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"


SUPPORTED_TASK_SOURCES = tuple(source.value for source in TaskSource)
TASK_SOURCE_LABELS = {
    TaskSource.MANUAL.value: "手動",
    TaskSource.GOOGLE_CLASSROOM.value: "Google Classroom",
    TaskSource.NOTION.value: "Notion",
}


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _zone_info(value: ZoneInfo | str) -> ZoneInfo:
    if isinstance(value, ZoneInfo):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timezone must be a valid IANA timezone")
    try:
        return ZoneInfo(value.strip())
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc


def _normalize_source(value: TaskSource | str) -> TaskSource:
    if isinstance(value, TaskSource):
        return value
    if not isinstance(value, str):
        raise ValueError("task source is unsupported")
    try:
        return TaskSource(value.strip().casefold())
    except ValueError as exc:
        raise ValueError("task source is unsupported") from exc


def _normalize_title(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("task title must be a string")
    title = " ".join(unicode_normalize("NFKC", value).split())
    if not title:
        raise ValueError("task title must not be blank")
    if len(title) > TITLE_MAX_LENGTH:
        raise ValueError("task title is too long")
    return title


def _normalize_description(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("task description must be a string")
    description = unicode_normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    description = description.strip()
    if not description:
        return None
    if len(description) > DESCRIPTION_MAX_LENGTH:
        raise ValueError("task description is too long")
    return description


def _normalize_external_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("external task ID must be a string")
    external_id = unicode_normalize("NFKC", value).strip()
    if not external_id:
        raise ValueError("external task ID must not be blank")
    if len(external_id) > EXTERNAL_ID_MAX_LENGTH:
        raise ValueError("external task ID is too long")
    return external_id


def _normalize_source_url(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("task source URL must be a string")
    source_url = value.strip()
    if not source_url:
        return None
    if len(source_url) > SOURCE_URL_MAX_LENGTH:
        raise ValueError("task source URL is too long")
    parsed = urlparse(source_url)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or any(character.isspace() for character in source_url)
    ):
        raise ValueError("task source URL must use http or https")
    return source_url


def _as_utc_naive(value: datetime, timezone: ZoneInfo) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("task due_at must be a datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone)
    return value.astimezone(UTC).replace(tzinfo=None)


def _parse_due_date(value: date | str) -> date:
    if isinstance(value, datetime) or not isinstance(value, (date, str)):
        raise ValueError("due_date must be a date or YYYY-MM-DD string")
    if isinstance(value, date):
        return value
    due_date_text = value.strip()
    try:
        parsed = date.fromisoformat(due_date_text)
    except ValueError:
        raise ValueError("due_date must be YYYY-MM-DD") from None
    if parsed.isoformat() != due_date_text:
        raise ValueError("due_date must be YYYY-MM-DD")
    return parsed


def _normalize_due_at(value: datetime | date | str | None, timezone: ZoneInfo) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc_naive(value, timezone)
    if isinstance(value, date):
        return _as_utc_naive(datetime.combine(value, time.min), timezone)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            parsed_datetime = datetime.fromisoformat(value)
        except ValueError:
            try:
                parsed_date = _parse_due_date(value)
            except ValueError:
                raise ValueError("due_at must be an ISO datetime or YYYY-MM-DD") from None
            return _as_utc_naive(datetime.combine(parsed_date, time.min), timezone)
        return _as_utc_naive(parsed_datetime, timezone)
    raise ValueError("task due_at must be a datetime, date, or ISO value")


@dataclass(frozen=True, slots=True)
class TaskView:
    """Detached task snapshot used by Discord and the read-only web page."""

    id: int
    source: str
    external_id: str | None
    title: str
    description: str | None
    due_at: datetime | None
    source_url: str | None
    status: str
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    due_date: date | None = None
    completed_date: date | None = None
    is_overdue: bool = False

    @property
    def provider(self) -> str:
        """Compatibility alias for consumers that call a source a provider."""

        return self.source

    @property
    def source_label(self) -> str:
        return TASK_SOURCE_LABELS.get(self.source, self.source)

    @property
    def due_label(self) -> str:
        return self.due_date.isoformat() if self.due_date is not None else "期限なし"

    @property
    def completed_label(self) -> str:
        return self.completed_date.isoformat() if self.completed_date is not None else "完了済み"

    @property
    def overdue(self) -> bool:
        return self.is_overdue

    @property
    def is_completed(self) -> bool:
        return self.status == TaskStatus.COMPLETED.value


# Descriptive aliases keep the result boundary convenient for adapters.
LifeTaskView = TaskView
TaskResult = TaskView
LifeTaskResult = TaskView


class TaskService:
    """Persist manual and provider-neutral tasks through a session factory."""

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
            raise ValueError("provide only one task clock")
        self.session_factory = session_factory
        self.timezone = _zone_info(timezone_name if timezone_name is not None else timezone)
        self._clock = clock or now or _utc_now

    def _new_session(self) -> Session:
        return self.session_factory()

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise ValueError("task clock must return a datetime")
        if value.tzinfo is None:
            # A naive clock value follows the repository's UTC-naive convention.
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def _local_date(self, value: datetime | None) -> date | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC).astimezone(self.timezone).date()

    def _view(self, task: LifeTask, *, now: datetime) -> TaskView:
        due_date = self._local_date(task.due_at)
        completed_date = self._local_date(task.completed_at)
        local_today = self._local_date(now)
        return TaskView(
            id=task.id,
            source=task.source,
            external_id=task.external_id,
            title=task.title,
            description=task.description,
            due_at=task.due_at,
            source_url=task.source_url,
            status=task.status,
            completed_at=task.completed_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
            due_date=due_date,
            completed_date=completed_date,
            is_overdue=(
                task.status == TaskStatus.OPEN.value
                and due_date is not None
                and local_today is not None
                and due_date < local_today
            ),
        )

    @staticmethod
    def _task_id(task_id: int) -> int:
        if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
            raise ValueError("task ID must be a positive integer")
        return task_id

    @staticmethod
    def _status(value: TaskStatus | str) -> TaskStatus:
        if isinstance(value, TaskStatus):
            return value
        if not isinstance(value, str):
            raise ValueError("task status is unsupported")
        try:
            return TaskStatus(value.strip().casefold())
        except ValueError as exc:
            raise ValueError("task status is unsupported") from exc

    def create_manual(
        self,
        title: str,
        description: str | None = None,
        due_date: date | str | None = None,
        due_at: datetime | date | str | None = None,
    ) -> TaskView:
        """Create a local task, interpreting offset-free times in ``self.timezone``."""

        normalized_title = _normalize_title(title)
        normalized_description = _normalize_description(description)
        if due_date is not None and due_at is not None:
            raise ValueError("provide either due_date or due_at, not both")
        normalized_due_at = None
        if due_date is not None:
            parsed_due_date = _parse_due_date(due_date)
            normalized_due_at = _as_utc_naive(
                datetime.combine(parsed_due_date, time.min), self.timezone
            )
        elif due_at is not None:
            normalized_due_at = _normalize_due_at(due_at, self.timezone)

        now = self._now()
        session = self._new_session()
        try:
            task = LifeTask(
                source=TaskSource.MANUAL.value,
                title=normalized_title,
                description=normalized_description,
                due_at=normalized_due_at,
                status=TaskStatus.OPEN.value,
                created_at=now,
                updated_at=now,
            )
            session.add(task)
            session.flush()
            result = self._view(task, now=now)
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def upsert_external(
        self,
        source: TaskSource | str,
        external_id: str,
        title: str,
        description: str | None = None,
        due_at: datetime | date | str | None = None,
        source_url: str | None = None,
    ) -> TaskView:
        """Insert or refresh one external task without any network access.

        Validation happens before opening a write transaction.  A caller that
        has no usable provider result therefore raises without deleting or
        changing an existing task.
        """

        normalized_source = _normalize_source(source)
        if normalized_source == TaskSource.MANUAL:
            raise ValueError("manual tasks must be created with create_manual")
        normalized_external_id = _normalize_external_id(external_id)
        normalized_title = _normalize_title(title)
        normalized_description = _normalize_description(description)
        normalized_due_at = _normalize_due_at(due_at, self.timezone)
        normalized_source_url = _normalize_source_url(source_url)
        now = self._now()

        session = self._new_session()
        try:
            task = session.scalar(
                select(LifeTask).where(
                    LifeTask.source == normalized_source.value,
                    LifeTask.external_id == normalized_external_id,
                )
            )
            if task is None:
                task = LifeTask(
                    source=normalized_source.value,
                    external_id=normalized_external_id,
                    title=normalized_title,
                    description=normalized_description,
                    due_at=normalized_due_at,
                    source_url=normalized_source_url,
                    status=TaskStatus.OPEN.value,
                    created_at=now,
                    updated_at=now,
                )
                session.add(task)
                try:
                    session.flush()
                except IntegrityError as exc:
                    # A concurrent reader may have inserted the same provider
                    # record. Re-read it and apply the same refresh safely.
                    session.rollback()
                    task = session.scalar(
                        select(LifeTask).where(
                            LifeTask.source == normalized_source.value,
                            LifeTask.external_id == normalized_external_id,
                        )
                    )
                    if task is None:
                        raise ValueError("could not upsert external task") from exc

            changed = (
                task.title != normalized_title
                or task.description != normalized_description
                or task.due_at != normalized_due_at
                or task.source_url != normalized_source_url
            )
            if changed:
                task.title = normalized_title
                task.description = normalized_description
                task.due_at = normalized_due_at
                task.source_url = normalized_source_url
                task.updated_at = now
            result = self._view(task, now=now)
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_tasks(
        self,
        *,
        include_completed: bool = True,
        status: TaskStatus | str | None = None,
        now: datetime | None = None,
        as_of: datetime | None = None,
    ) -> list[TaskView]:
        """Return open tasks first, followed by completed tasks newest first."""

        if now is not None and as_of is not None:
            raise ValueError("provide only one task list timestamp")
        normalized_status = self._status(status) if status is not None else None
        if normalized_status == TaskStatus.COMPLETED and not include_completed:
            return []
        timestamp = self._now() if now is None and as_of is None else (now or as_of)
        if timestamp is None:
            raise ValueError("task list timestamp is invalid")
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(UTC).replace(tzinfo=None)

        session = self._new_session()
        try:
            statement = select(LifeTask)
            if normalized_status is not None:
                statement = statement.where(LifeTask.status == normalized_status.value)
            elif not include_completed:
                statement = statement.where(LifeTask.status == TaskStatus.OPEN.value)
            tasks = list(session.scalars(statement).all())
            views = [self._view(task, now=timestamp) for task in tasks]
            open_views = sorted(
                (view for view in views if view.status == TaskStatus.OPEN.value),
                key=lambda view: (
                    view.due_at is None,
                    view.due_at if view.due_at is not None else datetime.max,
                    view.id,
                ),
            )
            completed_views = sorted(
                (view for view in views if view.status == TaskStatus.COMPLETED.value),
                key=lambda view: (
                    view.completed_at or datetime.min,
                    view.updated_at,
                    view.id,
                ),
                reverse=True,
            )
            return [*open_views, *completed_views]
        finally:
            session.close()

    def list_open_tasks(self, *, now: datetime | None = None) -> list[TaskView]:
        return self.list_tasks(include_completed=False, now=now)

    def list_completed_tasks(self, *, now: datetime | None = None) -> list[TaskView]:
        return self.list_tasks(status=TaskStatus.COMPLETED, now=now)

    def list(self, **kwargs: object) -> list[TaskView]:
        """Short alias for adapters that expose a generic task list method."""

        return self.list_tasks(**kwargs)  # type: ignore[arg-type]

    def get(self, task_id: int, *, now: datetime | None = None) -> TaskView | None:
        task_id = self._task_id(task_id)
        timestamp = self._now() if now is None else now
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(UTC).replace(tzinfo=None)
        session = self._new_session()
        try:
            task = session.get(LifeTask, task_id)
            return self._view(task, now=timestamp) if task is not None else None
        finally:
            session.close()

    def get_task(self, task_id: int, *, now: datetime | None = None) -> TaskView | None:
        return self.get(task_id, now=now)

    def complete(self, task_id: int) -> TaskView:
        """Complete one task; repeating the operation preserves its timestamp."""

        task_id = self._task_id(task_id)
        now = self._now()
        session = self._new_session()
        try:
            task = session.get(LifeTask, task_id)
            if task is None:
                raise ValueError("task not found")
            if task.status != TaskStatus.COMPLETED.value:
                task.status = TaskStatus.COMPLETED.value
                task.completed_at = now
                task.updated_at = now
                session.flush()
            elif task.completed_at is None:
                # Keep a legacy completed row internally consistent without
                # changing a valid idempotent completion timestamp.
                task.completed_at = now
                task.updated_at = now
                session.flush()
            result = self._view(task, now=now)
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
