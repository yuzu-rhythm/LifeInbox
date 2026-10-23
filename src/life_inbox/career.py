"""Local-first career and job-hunting records.

The career domain is intentionally independent from Discord, Web, and network
clients.  ``CareerOpportunity`` stores a company, job listing, or event while
``CareerApplication`` stores the application stage for one opportunity.  A
career row can point at one ordinary manual ``LifeTask``; keeping that task
source as ``manual`` preserves compatibility with databases created before the
career domain existed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from unicodedata import normalize as unicode_normalize
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CareerApplication, CareerOpportunity, Event, LifeTask

if TYPE_CHECKING:
    from .schemas import InboxRouteResult


COMPANY_MAX_LENGTH = 255
TITLE_MAX_LENGTH = 255
LOCATION_MAX_LENGTH = 255
NOTES_MAX_LENGTH = 4_000
NEXT_ACTION_MAX_LENGTH = 255
SOURCE_MAX_LENGTH = 64
EXTERNAL_ID_MAX_LENGTH = 255
URL_MAX_LENGTH = 2_048
MIN_EXPLICIT_YEAR = 2000
MAX_EXPLICIT_YEAR = 2100

# Public aliases make the guardrails easy for command adapters and tests to
# discover without duplicating the actual values.
CAREER_COMPANY_MAX_LENGTH = COMPANY_MAX_LENGTH
CAREER_TITLE_MAX_LENGTH = TITLE_MAX_LENGTH
CAREER_NOTES_MAX_LENGTH = NOTES_MAX_LENGTH
CAREER_URL_MAX_LENGTH = URL_MAX_LENGTH


class CareerKind(StrEnum):
    COMPANY = "company"
    JOB = "job"
    EVENT = "event"
    APPLICATION = "application"


class CareerStatus(StrEnum):
    OPEN = "open"
    INTERESTED = "interested"
    APPLIED = "applied"
    IN_PROGRESS = "in_progress"
    ACCEPTED = "accepted"
    CLOSED = "closed"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    ARCHIVED = "archived"


class ApplicationStage(StrEnum):
    SAVED = "saved"
    INTERESTED = "interested"
    APPLIED = "applied"
    SCREENING = "screening"
    INTERVIEW = "interview"
    FINAL_INTERVIEW = "final_interview"
    OFFER = "offer"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    CLOSED = "closed"


# Descriptive aliases are useful to integrations that call the domain a job
# tracker rather than a career tracker.
CareerItemKind = CareerKind
CareerOpportunityKind = CareerKind
CareerApplicationStage = ApplicationStage
JobApplicationStage = ApplicationStage

SUPPORTED_CAREER_KINDS = tuple(kind.value for kind in CareerKind)
SUPPORTED_CAREER_STATUSES = tuple(status.value for status in CareerStatus)
SUPPORTED_APPLICATION_STAGES = tuple(stage.value for stage in ApplicationStage)

_TERMINAL_STATUSES = frozenset(
    {
        CareerStatus.ACCEPTED.value,
        CareerStatus.CLOSED.value,
        CareerStatus.REJECTED.value,
        CareerStatus.WITHDRAWN.value,
        CareerStatus.ARCHIVED.value,
    }
)
_TERMINAL_STAGES = frozenset(
    {
        ApplicationStage.ACCEPTED.value,
        ApplicationStage.CLOSED.value,
        ApplicationStage.REJECTED.value,
        ApplicationStage.WITHDRAWN.value,
    }
)
_UNSET = object()


@dataclass(frozen=True, slots=True)
class CareerRecord:
    """Detached, display-safe snapshot for either career table."""

    id: int
    record_type: str
    company: str
    title: str
    kind: str
    status: str
    stage: str | None
    location: str | None
    url: str | None
    notes: str | None
    source: str
    external_id: str | None
    source_event_id: int | None
    opportunity_id: int | None
    deadline_at: datetime | None
    next_action: str | None
    next_action_at: datetime | None
    linked_task_id: int | None
    created_at: datetime
    updated_at: datetime

    @property
    def role(self) -> str:
        return self.title

    @property
    def application_stage(self) -> str | None:
        return self.stage

    @property
    def deadline(self) -> datetime | None:
        return self.deadline_at

    @property
    def is_closed(self) -> bool:
        return self.status in _TERMINAL_STATUSES or self.stage in _TERMINAL_STAGES

    @property
    def has_reminder(self) -> bool:
        return (
            self.linked_task_id is not None
            and (self.deadline_at is not None or self.next_action_at is not None)
            and not self.is_closed
        )


CareerOpportunityView = CareerRecord
CareerApplicationView = CareerRecord
CareerItem = CareerRecord
JobOpportunity = CareerOpportunity
JobApplication = CareerApplication


def _zone_info(value: ZoneInfo | str) -> ZoneInfo:
    if isinstance(value, ZoneInfo):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timezone must be a valid IANA timezone")
    try:
        return ZoneInfo(value.strip())
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc


def _now_utc_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _positive_id(value: int, *, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _single_line(value: str, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    result = " ".join(unicode_normalize("NFKC", value).split())
    if not result:
        raise ValueError(f"{field_name} must not be blank")
    if len(result) > maximum:
        raise ValueError(f"{field_name} is too long")
    return result


def _optional_single_line(
    value: str | None,
    *,
    field_name: str,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    result = " ".join(unicode_normalize("NFKC", value).split())
    if not result:
        return None
    if len(result) > maximum:
        raise ValueError(f"{field_name} is too long")
    return result


def _optional_notes(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("notes must be a string")
    result = unicode_normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    result = result.strip()
    if not result:
        return None
    if any(ord(character) < 32 and character not in "\n\t" for character in result):
        raise ValueError("notes contains unsupported control characters")
    if len(result) > NOTES_MAX_LENGTH:
        raise ValueError("notes is too long")
    return result


def normalize_url(value: str | None) -> str | None:
    """Validate a URL without accepting credentials or non-web schemes."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("URL must be a string")
    url = value.strip()
    if not url:
        return None
    if len(url) > URL_MAX_LENGTH or any(character.isspace() for character in url):
        raise ValueError("URL must be a safe http or https URL")
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.casefold()
        # Accessing hostname also rejects malformed bracketed IPv6 values.
        hostname = parsed.hostname
        # Accessing port rejects malformed values such as ``:not-a-port``.
        parsed.port
        has_credentials = parsed.username is not None or parsed.password is not None
    except ValueError as exc:
        raise ValueError("URL must be a safe http or https URL") from exc
    if scheme not in {"http", "https"} or not parsed.netloc or not hostname or has_credentials:
        raise ValueError("URL must be a safe http or https URL")
    return url


def _source(value: str | None) -> str:
    return _single_line(
        "manual" if value is None else value,
        field_name="source",
        maximum=SOURCE_MAX_LENGTH,
    )


def _record_type(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("record_type must be opportunity or application")
    normalized = value.strip().casefold()
    if normalized not in {"opportunity", "application"}:
        raise ValueError("record_type must be opportunity or application")
    return normalized


def _external_id(value: str | None) -> str | None:
    return _optional_single_line(
        value,
        field_name="external_id",
        maximum=EXTERNAL_ID_MAX_LENGTH,
    )


def _kind(value: CareerKind | str) -> CareerKind:
    if isinstance(value, CareerKind):
        return value
    if not isinstance(value, str):
        raise ValueError("unsupported career kind")
    try:
        return CareerKind(value.strip().casefold())
    except ValueError as exc:
        raise ValueError("unsupported career kind") from exc


def _status(value: CareerStatus | str | None) -> CareerStatus:
    if value is None:
        return CareerStatus.OPEN
    if isinstance(value, CareerStatus):
        return value
    if not isinstance(value, str):
        raise ValueError("unsupported career status")
    try:
        return CareerStatus(value.strip().casefold())
    except ValueError as exc:
        raise ValueError("unsupported career status") from exc


def _stage(value: ApplicationStage | str | None) -> ApplicationStage:
    if value is None:
        return ApplicationStage.SAVED
    if isinstance(value, ApplicationStage):
        return value
    if not isinstance(value, str):
        raise ValueError("unsupported application stage")
    try:
        return ApplicationStage(value.strip().casefold())
    except ValueError as exc:
        raise ValueError("unsupported application stage") from exc


def _status_for_stage(stage: ApplicationStage) -> CareerStatus:
    return {
        ApplicationStage.SAVED: CareerStatus.OPEN,
        ApplicationStage.INTERESTED: CareerStatus.INTERESTED,
        ApplicationStage.APPLIED: CareerStatus.APPLIED,
        ApplicationStage.SCREENING: CareerStatus.IN_PROGRESS,
        ApplicationStage.INTERVIEW: CareerStatus.IN_PROGRESS,
        ApplicationStage.FINAL_INTERVIEW: CareerStatus.IN_PROGRESS,
        ApplicationStage.OFFER: CareerStatus.IN_PROGRESS,
        ApplicationStage.ACCEPTED: CareerStatus.ACCEPTED,
        ApplicationStage.REJECTED: CareerStatus.REJECTED,
        ApplicationStage.WITHDRAWN: CareerStatus.WITHDRAWN,
        ApplicationStage.CLOSED: CareerStatus.CLOSED,
    }[stage]


def _date_value(value: date | str | None, *, field_name: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime) or not isinstance(value, (date, str)):
        raise ValueError(f"{field_name} must be a YYYY-MM-DD date")
    if isinstance(value, date):
        parsed = value
    else:
        text = value.strip()
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            raise ValueError(f"{field_name} must be YYYY-MM-DD") from None
        if parsed.isoformat() != text:
            raise ValueError(f"{field_name} must be YYYY-MM-DD")
    if not MIN_EXPLICIT_YEAR <= parsed.year <= MAX_EXPLICIT_YEAR:
        raise ValueError(f"{field_name} is outside the accepted range")
    return parsed


def _time_value(value: time | str | None, *, field_name: str) -> time | None:
    if value is None:
        return None
    if isinstance(value, datetime) or not isinstance(value, (time, str)):
        raise ValueError(f"{field_name} must be an explicit local time")
    if isinstance(value, time):
        parsed = value
    else:
        text = value.strip()
        try:
            parsed = time.fromisoformat(text)
        except ValueError:
            raise ValueError(f"{field_name} must be HH:MM or HH:MM:SS") from None
    if parsed.tzinfo is not None:
        raise ValueError(f"{field_name} must not include a timezone offset")
    return parsed


def _utc_naive(value: datetime, timezone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone)
    return value.astimezone(UTC).replace(tzinfo=None)


def _when(
    value: object,
    *,
    date_value: object,
    time_value: object,
    field_name: str,
    timezone: ZoneInfo,
) -> datetime | None:
    """Normalize explicit date/time parts, never interpreting relative text."""

    has_parts = date_value is not _UNSET or time_value is not _UNSET
    if has_parts:
        if value is not _UNSET:
            raise ValueError(f"provide only {field_name} or its date/time parts")
        parsed_date = _date_value(
            None if date_value is _UNSET else date_value,
            field_name=f"{field_name}_date",
        )
        parsed_time = _time_value(
            None if time_value is _UNSET else time_value,
            field_name=f"{field_name}_time",
        )
        if parsed_time is not None and parsed_date is None:
            raise ValueError(f"{field_name}_time requires an explicit date")
        if parsed_date is None:
            return None
        return _utc_naive(
            datetime.combine(parsed_date, parsed_time or time.min),
            timezone,
        )

    if value is _UNSET or value is None:
        return None
    if isinstance(value, datetime):
        if not MIN_EXPLICIT_YEAR <= value.year <= MAX_EXPLICIT_YEAR:
            raise ValueError(f"{field_name} is outside the accepted range")
        return _utc_naive(value, timezone)
    if isinstance(value, date):
        parsed_date = _date_value(value, field_name=field_name)
        assert parsed_date is not None
        return _utc_naive(datetime.combine(parsed_date, time.min), timezone)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed_date = _date_value(text, field_name=field_name)
        except ValueError:
            parsed_date = None
        if parsed_date is not None:
            return _utc_naive(datetime.combine(parsed_date, time.min), timezone)
        try:
            parsed_datetime = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(
                f"{field_name} must be an explicit ISO datetime or YYYY-MM-DD"
            ) from None
        if not MIN_EXPLICIT_YEAR <= parsed_datetime.year <= MAX_EXPLICIT_YEAR:
            raise ValueError(f"{field_name} is outside the accepted range")
        return _utc_naive(parsed_datetime, timezone)
    raise ValueError(f"{field_name} must be an explicit date or datetime")


def _task_due(item: CareerOpportunity | CareerApplication) -> datetime | None:
    return item.next_action_at or item.deadline_at


def _task_title(item: CareerOpportunity | CareerApplication) -> str:
    suffix = "（応募）" if isinstance(item, CareerApplication) else ""
    company = item.company if isinstance(item, CareerOpportunity) else item.opportunity.company
    title = item.title if isinstance(item, CareerOpportunity) else item.opportunity.title
    return _single_line(
        f"[就活] {company} / {title} {suffix}".rstrip(),
        field_name="LifeTask title",
        maximum=255,
    )


def _task_description(item: CareerOpportunity | CareerApplication) -> str | None:
    company = item.company if isinstance(item, CareerOpportunity) else item.opportunity.company
    title = item.title if isinstance(item, CareerOpportunity) else item.opportunity.title
    parts: list[str] = []
    if item.next_action:
        parts.append(f"次の行動: {item.next_action}")
    if item.notes:
        parts.append(item.notes)
    if isinstance(item, CareerApplication):
        parts.insert(0, f"応募段階: {item.stage}")
    description = f"{company} / {title}"
    if parts:
        description += "\n" + "\n".join(parts)
    return description[:4_000]


def _is_terminal(item: CareerOpportunity | CareerApplication) -> bool:
    if isinstance(item, CareerApplication):
        return item.status in _TERMINAL_STATUSES or item.stage in _TERMINAL_STAGES
    return item.status in _TERMINAL_STATUSES


class CareerService:
    """CRUD, idempotent upsert, and reminder synchronization for career data."""

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
            raise ValueError("provide only one career clock")
        self.session_factory = session_factory
        self.timezone = _zone_info(timezone_name if timezone_name is not None else timezone)
        self._clock = clock or now or _now_utc_naive

    def _new_session(self) -> Session:
        return self.session_factory()

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise ValueError("career clock must return a datetime")
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value

    @staticmethod
    def _event(session: Session, event_id: int) -> Event:
        event = session.get(Event, _positive_id(event_id, field_name="event_id"))
        if event is None:
            raise ValueError("event not found")
        return event

    @staticmethod
    def _opportunity(session: Session, opportunity_id: int) -> CareerOpportunity:
        opportunity = session.get(
            CareerOpportunity,
            _positive_id(opportunity_id, field_name="opportunity_id"),
        )
        if opportunity is None:
            raise ValueError("career opportunity not found")
        return opportunity

    @staticmethod
    def _application(session: Session, application_id: int) -> CareerApplication:
        application = session.get(
            CareerApplication,
            _positive_id(application_id, field_name="application_id"),
        )
        if application is None:
            raise ValueError("career application not found")
        return application

    @staticmethod
    def _find_opportunity(
        session: Session,
        *,
        source_event_id: int | None,
        source: str,
        external_id: str | None,
    ) -> CareerOpportunity | None:
        if source_event_id is not None:
            found = session.scalar(
                select(CareerOpportunity).where(
                    CareerOpportunity.source_event_id == source_event_id
                )
            )
            if found is not None:
                return found
        if external_id is not None:
            return session.scalar(
                select(CareerOpportunity).where(
                    CareerOpportunity.source == source,
                    CareerOpportunity.external_id == external_id,
                )
            )
        return None

    @staticmethod
    def _find_application(
        session: Session,
        *,
        source_event_id: int | None,
        source: str,
        external_id: str | None,
    ) -> CareerApplication | None:
        if source_event_id is not None:
            found = session.scalar(
                select(CareerApplication).where(
                    CareerApplication.source_event_id == source_event_id
                )
            )
            if found is not None:
                return found
        if external_id is not None:
            return session.scalar(
                select(CareerApplication).where(
                    CareerApplication.source == source,
                    CareerApplication.external_id == external_id,
                )
            )
        return None

    @staticmethod
    def _mark_source_event(event: Event) -> None:
        if event.event_type not in {"unknown", "career"}:
            raise ValueError("event already has a different routed destination")
        event.status = "confirmed"
        event.event_type = "career"
        event.event_kinds = ["career"]

    def _sync_task(
        self,
        session: Session,
        item: CareerOpportunity | CareerApplication,
        *,
        now: datetime,
    ) -> None:
        """Create/update one manual task or complete it when no longer active."""

        task = session.get(LifeTask, item.linked_task_id) if item.linked_task_id else None
        if _is_terminal(item) or _task_due(item) is None:
            if task is not None and task.status != "completed":
                task.status = "completed"
                task.completed_at = now
                task.updated_at = now
            return

        if task is None:
            task = LifeTask(
                source="manual",
                title=_task_title(item),
                description=_task_description(item),
                due_at=_task_due(item),
                source_url=(
                    item.url
                    if isinstance(item, CareerOpportunity)
                    else item.opportunity.url
                ),
                status="open",
                completed_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(task)
            session.flush()
            item.linked_task_id = task.id
            return

        task.title = _task_title(item)
        task.description = _task_description(item)
        task.due_at = _task_due(item)
        task.source_url = (
            item.url if isinstance(item, CareerOpportunity) else item.opportunity.url
        )
        task.status = "open"
        task.completed_at = None
        task.updated_at = now

    def _opportunity_view(self, item: CareerOpportunity) -> CareerRecord:
        return CareerRecord(
            id=item.id,
            record_type="opportunity",
            company=item.company,
            title=item.title,
            kind=item.kind,
            status=item.status,
            stage=None,
            location=item.location,
            url=item.url,
            notes=item.notes,
            source=item.source,
            external_id=item.external_id,
            source_event_id=item.source_event_id,
            opportunity_id=item.id,
            deadline_at=item.deadline_at,
            next_action=item.next_action,
            next_action_at=item.next_action_at,
            linked_task_id=item.linked_task_id,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    def _application_view(self, item: CareerApplication) -> CareerRecord:
        return CareerRecord(
            id=item.id,
            record_type="application",
            company=item.opportunity.company,
            title=item.opportunity.title,
            kind=CareerKind.APPLICATION.value,
            status=item.status,
            stage=item.stage,
            location=item.opportunity.location,
            url=item.opportunity.url,
            notes=item.notes,
            source=item.source,
            external_id=item.external_id,
            source_event_id=item.source_event_id,
            opportunity_id=item.opportunity_id,
            deadline_at=item.deadline_at,
            next_action=item.next_action,
            next_action_at=item.next_action_at,
            linked_task_id=item.linked_task_id,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    def create_opportunity(
        self,
        company: str,
        title: str,
        *,
        kind: CareerKind | str = CareerKind.JOB,
        location: str | None = None,
        url: str | None = None,
        notes: str | None = None,
        status: CareerStatus | str | None = None,
        source: str | None = "manual",
        external_id: str | None = None,
        source_event_id: int | None = None,
        deadline: object = _UNSET,
        deadline_at: object = _UNSET,
        deadline_date: object = _UNSET,
        deadline_time: object = _UNSET,
        next_action: str | None = None,
        next_action_at: object = _UNSET,
        next_action_date: object = _UNSET,
        next_action_time: object = _UNSET,
    ) -> CareerRecord:
        normalized_company = _single_line(
            company, field_name="company", maximum=COMPANY_MAX_LENGTH
        )
        normalized_title = _single_line(title, field_name="title", maximum=TITLE_MAX_LENGTH)
        normalized_kind = _kind(kind)
        if normalized_kind == CareerKind.APPLICATION:
            raise ValueError("application kind must use create_application")
        normalized_location = _optional_single_line(
            location,
            field_name="location",
            maximum=LOCATION_MAX_LENGTH,
        )
        normalized_url = normalize_url(url)
        normalized_notes = _optional_notes(notes)
        normalized_status = _status(status)
        normalized_source = _source(source)
        normalized_external_id = _external_id(external_id)
        normalized_event_id = (
            None
            if source_event_id is None
            else _positive_id(source_event_id, field_name="source_event_id")
        )
        if normalized_event_id is not None:
            # Validate before opening the mutation sequence so a missing raw
            # event cannot leave a standalone career record behind.
            session = self._new_session()
            try:
                self._event(session, normalized_event_id)
            finally:
                session.close()
        normalized_deadline = _when(
            deadline_at if deadline_at is not _UNSET else deadline,
            date_value=deadline_date,
            time_value=deadline_time,
            field_name="deadline",
            timezone=self.timezone,
        )
        normalized_next_action_at = _when(
            next_action_at,
            date_value=next_action_date,
            time_value=next_action_time,
            field_name="next_action",
            timezone=self.timezone,
        )
        normalized_next_action = _optional_single_line(
            next_action,
            field_name="next_action",
            maximum=NEXT_ACTION_MAX_LENGTH,
        )
        now = self._now()
        session = self._new_session()
        try:
            item = self._find_opportunity(
                session,
                source_event_id=normalized_event_id,
                source=normalized_source,
                external_id=normalized_external_id,
            )
            if item is None:
                item = CareerOpportunity(
                    source_event_id=normalized_event_id,
                    company=normalized_company,
                    title=normalized_title,
                    kind=normalized_kind.value,
                    location=normalized_location,
                    url=normalized_url,
                    notes=normalized_notes,
                    status=normalized_status.value,
                    source=normalized_source,
                    external_id=normalized_external_id,
                    deadline_at=normalized_deadline,
                    next_action=normalized_next_action,
                    next_action_at=normalized_next_action_at,
                    created_at=now,
                    updated_at=now,
                )
                session.add(item)
                session.flush()
            else:
                item.company = normalized_company
                item.title = normalized_title
                item.kind = normalized_kind.value
                item.location = normalized_location
                item.url = normalized_url
                item.notes = normalized_notes
                item.status = normalized_status.value
                item.source = normalized_source
                item.external_id = normalized_external_id
                item.deadline_at = normalized_deadline
                item.next_action = normalized_next_action
                item.next_action_at = normalized_next_action_at
                item.updated_at = now
            self._sync_task(session, item, now=now)
            # ``linked_task_id`` may be assigned after the initial flush; keep
            # the career timestamp tied to the same deterministic clock tick.
            item.updated_at = now
            session.flush()
            if normalized_event_id is not None:
                event = self._event(session, normalized_event_id)
                self._mark_source_event(event)
                event.updated_at = now
            session.commit()
            return self._opportunity_view(item)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    add_opportunity = create_opportunity
    upsert_opportunity = create_opportunity

    def create_event(self, company: str, title: str, **kwargs: Any) -> CareerRecord:
        kwargs["kind"] = CareerKind.EVENT
        return self.create_opportunity(company, title, **kwargs)

    def _create_or_get_application_opportunity(
        self,
        session: Session,
        *,
        opportunity_id: int | None,
        company: str | None,
        title: str | None,
        source: str,
        source_event_id: int | None,
        now: datetime,
    ) -> CareerOpportunity:
        if opportunity_id is not None:
            opportunity = self._opportunity(session, opportunity_id)
            if company is not None and _single_line(
                company, field_name="company", maximum=COMPANY_MAX_LENGTH
            ) != opportunity.company:
                raise ValueError("application company does not match opportunity")
            if title is not None and _single_line(
                title, field_name="title", maximum=TITLE_MAX_LENGTH
            ) != opportunity.title:
                raise ValueError("application title does not match opportunity")
            return opportunity
        if company is None or title is None:
            raise ValueError("company and title are required when opportunity_id is omitted")
        normalized_company = _single_line(
            company, field_name="company", maximum=COMPANY_MAX_LENGTH
        )
        normalized_title = _single_line(title, field_name="title", maximum=TITLE_MAX_LENGTH)
        existing = session.scalar(
            select(CareerOpportunity).where(
                CareerOpportunity.company == normalized_company,
                CareerOpportunity.title == normalized_title,
                CareerOpportunity.kind == CareerKind.JOB.value,
            )
        )
        if existing is not None:
            return existing
        opportunity = CareerOpportunity(
            source_event_id=source_event_id,
            company=normalized_company,
            title=normalized_title,
            kind=CareerKind.JOB.value,
            status=CareerStatus.APPLIED.value,
            source=source,
            created_at=now,
            updated_at=now,
        )
        session.add(opportunity)
        session.flush()
        return opportunity

    def create_application(
        self,
        company: str | None = None,
        title: str | None = None,
        *,
        opportunity_id: int | None = None,
        stage: ApplicationStage | str | None = ApplicationStage.SAVED,
        status: CareerStatus | str | None = None,
        notes: str | None = None,
        source: str | None = "manual",
        external_id: str | None = None,
        source_event_id: int | None = None,
        deadline: object = _UNSET,
        deadline_at: object = _UNSET,
        deadline_date: object = _UNSET,
        deadline_time: object = _UNSET,
        next_action: str | None = None,
        next_action_at: object = _UNSET,
        next_action_date: object = _UNSET,
        next_action_time: object = _UNSET,
        location: str | None = None,
        url: str | None = None,
    ) -> CareerRecord:
        normalized_source = _source(source)
        normalized_external_id = _external_id(external_id)
        normalized_event_id = (
            None
            if source_event_id is None
            else _positive_id(source_event_id, field_name="source_event_id")
        )
        normalized_stage = _stage(stage)
        normalized_status = (
            _status(status) if status is not None else _status_for_stage(normalized_stage)
        )
        normalized_notes = _optional_notes(notes)
        normalized_deadline = _when(
            deadline_at if deadline_at is not _UNSET else deadline,
            date_value=deadline_date,
            time_value=deadline_time,
            field_name="deadline",
            timezone=self.timezone,
        )
        normalized_next_action_at = _when(
            next_action_at,
            date_value=next_action_date,
            time_value=next_action_time,
            field_name="next_action",
            timezone=self.timezone,
        )
        normalized_next_action = _optional_single_line(
            next_action,
            field_name="next_action",
            maximum=NEXT_ACTION_MAX_LENGTH,
        )
        normalized_location = _optional_single_line(
            location,
            field_name="location",
            maximum=LOCATION_MAX_LENGTH,
        )
        normalized_url = normalize_url(url)
        now = self._now()
        session = self._new_session()
        try:
            existing = self._find_application(
                session,
                source_event_id=normalized_event_id,
                source=normalized_source,
                external_id=normalized_external_id,
            )
            if existing is not None and opportunity_id is None:
                # An external/source-event upsert must keep the normalized
                # application attached to its existing opportunity.  This
                # avoids creating a second opportunity when a provider sends
                # an updated title or company for the same application.
                opportunity = existing.opportunity
                if company is not None:
                    opportunity.company = _single_line(
                        company,
                        field_name="company",
                        maximum=COMPANY_MAX_LENGTH,
                    )
                if title is not None:
                    opportunity.title = _single_line(
                        title,
                        field_name="title",
                        maximum=TITLE_MAX_LENGTH,
                    )
                opportunity.updated_at = now
            else:
                opportunity = self._create_or_get_application_opportunity(
                    session,
                    opportunity_id=opportunity_id,
                    company=company,
                    title=title,
                    source=normalized_source,
                    source_event_id=(
                        normalized_event_id if existing is None else None
                    ),
                    now=now,
                )
            # Optional application inputs can complete useful missing metadata
            # on an opportunistic record without replacing established values.
            if normalized_location is not None:
                opportunity.location = normalized_location
            if normalized_url is not None:
                opportunity.url = normalized_url
            if existing is None:
                existing = CareerApplication(
                    opportunity_id=opportunity.id,
                    source_event_id=normalized_event_id,
                    stage=normalized_stage.value,
                    status=normalized_status.value,
                    notes=normalized_notes,
                    source=normalized_source,
                    external_id=normalized_external_id,
                    deadline_at=normalized_deadline,
                    next_action=normalized_next_action,
                    next_action_at=normalized_next_action_at,
                    created_at=now,
                    updated_at=now,
                )
                session.add(existing)
                session.flush()
            else:
                existing.opportunity_id = opportunity.id
                existing.stage = normalized_stage.value
                existing.status = normalized_status.value
                existing.notes = normalized_notes
                existing.source = normalized_source
                existing.external_id = normalized_external_id
                existing.deadline_at = normalized_deadline
                existing.next_action = normalized_next_action
                existing.next_action_at = normalized_next_action_at
                existing.updated_at = now
            self._sync_task(session, existing, now=now)
            existing.updated_at = now
            session.flush()
            if normalized_event_id is not None:
                event = self._event(session, normalized_event_id)
                self._mark_source_event(event)
                event.updated_at = now
            session.commit()
            return self._application_view(existing)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    add_application = create_application
    upsert_application = create_application

    def create(
        self,
        company: str,
        title: str,
        *,
        kind: CareerKind | str = CareerKind.JOB,
        **kwargs: Any,
    ) -> CareerRecord:
        if _kind(kind) == CareerKind.APPLICATION:
            return self.create_application(company, title, **kwargs)
        return self.create_opportunity(company, title, kind=kind, **kwargs)

    def create_from_route(
        self,
        event_id: int,
        route: InboxRouteResult,
    ) -> CareerRecord:
        """Persist a validated analyzer route and its linked task atomically."""

        from .schemas import InboxRouteResult as InboxRouteSchema

        route = (
            route
            if isinstance(route, InboxRouteSchema)
            else InboxRouteSchema.model_validate(route)
        )
        if route.destination != "career":
            raise ValueError("route destination must be career")
        kind = route.career_kind or CareerKind.JOB.value
        source = route.career_source or "discord"
        external_id = route.career_external_id
        deadline_kwargs: dict[str, object]
        if route.career_deadline_at is not None:
            deadline_kwargs = {"deadline_at": route.career_deadline_at}
        else:
            deadline_kwargs = {
                "deadline_date": route.career_deadline_date,
                "deadline_time": route.career_deadline_time,
            }
        next_action_kwargs: dict[str, object]
        if route.career_next_action_at is not None:
            next_action_kwargs = {"next_action_at": route.career_next_action_at}
        else:
            next_action_kwargs = {
                "next_action_date": route.career_next_action_date,
                "next_action_time": route.career_next_action_time,
            }
        if kind == CareerKind.APPLICATION.value:
            return self.create_application(
                route.career_company,
                route.career_title,
                stage=route.career_stage or ApplicationStage.SAVED.value,
                status=route.career_status,
                notes=route.career_notes,
                source=source,
                external_id=external_id,
                source_event_id=event_id,
                next_action=route.career_next_action,
                location=route.career_location,
                url=route.career_url,
                **deadline_kwargs,
                **next_action_kwargs,
            )
        return self.create_opportunity(
            route.career_company,
            route.career_title,
            kind=kind,
            status=route.career_status,
            location=route.career_location,
            url=route.career_url,
            notes=route.career_notes,
            source=source,
            external_id=external_id,
            source_event_id=event_id,
            next_action=route.career_next_action,
            **deadline_kwargs,
            **next_action_kwargs,
        )

    def get_opportunity(self, opportunity_id: int) -> CareerRecord | None:
        session = self._new_session()
        try:
            item = session.get(CareerOpportunity, _positive_id(opportunity_id, field_name="id"))
            return self._opportunity_view(item) if item is not None else None
        finally:
            session.close()

    get_job = get_opportunity

    def get_application(self, application_id: int) -> CareerRecord | None:
        session = self._new_session()
        try:
            item = session.get(CareerApplication, _positive_id(application_id, field_name="id"))
            return self._application_view(item) if item is not None else None
        finally:
            session.close()

    def get(
        self,
        record_id: int,
        *,
        record_type: str = "opportunity",
    ) -> CareerRecord | None:
        return self.get_record(record_id, record_type=record_type)

    def list_opportunities(
        self,
        *,
        include_closed: bool = False,
        kind: CareerKind | str | None = None,
        status: CareerStatus | str | None = None,
    ) -> list[CareerRecord]:
        session = self._new_session()
        try:
            statement = select(CareerOpportunity)
            if kind is not None:
                normalized_kind = _kind(kind)
                if normalized_kind == CareerKind.APPLICATION:
                    raise ValueError("application kind must use list_applications")
                statement = statement.where(CareerOpportunity.kind == normalized_kind.value)
            if status is not None:
                statement = statement.where(CareerOpportunity.status == _status(status).value)
            elif not include_closed:
                statement = statement.where(~CareerOpportunity.status.in_(_TERMINAL_STATUSES))
            rows = [self._opportunity_view(item) for item in session.scalars(statement).all()]
            return sorted(rows, key=_list_sort_key)
        finally:
            session.close()

    list_jobs = list_opportunities

    def list_applications(
        self,
        *,
        include_closed: bool = False,
        stage: ApplicationStage | str | None = None,
        status: CareerStatus | str | None = None,
    ) -> list[CareerRecord]:
        session = self._new_session()
        try:
            statement = select(CareerApplication)
            if stage is not None:
                statement = statement.where(CareerApplication.stage == _stage(stage).value)
            if status is not None:
                statement = statement.where(CareerApplication.status == _status(status).value)
            elif not include_closed:
                statement = statement.where(
                    ~CareerApplication.status.in_(_TERMINAL_STATUSES),
                    ~CareerApplication.stage.in_(_TERMINAL_STAGES),
                )
            rows = [self._application_view(item) for item in session.scalars(statement).all()]
            return sorted(rows, key=_list_sort_key)
        finally:
            session.close()

    def list_records(
        self,
        *,
        include_closed: bool = False,
        kind: CareerKind | str | None = None,
    ) -> list[CareerRecord]:
        if kind is not None and _kind(kind) == CareerKind.APPLICATION:
            return self.list_applications(include_closed=include_closed)
        opportunities = self.list_opportunities(include_closed=include_closed, kind=kind)
        applications = self.list_applications(include_closed=include_closed)
        return sorted([*opportunities, *applications], key=_list_sort_key)

    list_items = list_records
    list = list_records

    def update_opportunity(
        self,
        opportunity_id: int,
        *,
        company: object = _UNSET,
        title: object = _UNSET,
        kind: object = _UNSET,
        location: object = _UNSET,
        url: object = _UNSET,
        notes: object = _UNSET,
        status: object = _UNSET,
        stage: object = _UNSET,
        deadline: object = _UNSET,
        deadline_at: object = _UNSET,
        deadline_date: object = _UNSET,
        deadline_time: object = _UNSET,
        next_action: object = _UNSET,
        next_action_at: object = _UNSET,
        next_action_date: object = _UNSET,
        next_action_time: object = _UNSET,
    ) -> CareerRecord:
        item_id = _positive_id(opportunity_id, field_name="opportunity_id")
        session = self._new_session()
        try:
            item = self._opportunity(session, item_id)
            now = self._now()
            if company is not _UNSET:
                item.company = _single_line(
                    company, field_name="company", maximum=COMPANY_MAX_LENGTH
                )
            if title is not _UNSET:
                item.title = _single_line(title, field_name="title", maximum=TITLE_MAX_LENGTH)
            if kind is not _UNSET:
                normalized_kind = _kind(kind)
                if normalized_kind == CareerKind.APPLICATION:
                    raise ValueError("application kind must use application records")
                item.kind = normalized_kind.value
            if location is not _UNSET:
                item.location = _optional_single_line(
                    location, field_name="location", maximum=LOCATION_MAX_LENGTH
                )
            if url is not _UNSET:
                item.url = normalize_url(url)
            if notes is not _UNSET:
                item.notes = _optional_notes(notes)
            if status is not _UNSET:
                item.status = _status(status).value
            elif stage is not _UNSET:
                item.status = _status(stage).value
            if any(
                value is not _UNSET
                for value in (deadline, deadline_at, deadline_date, deadline_time)
            ):
                item.deadline_at = _when(
                    deadline_at if deadline_at is not _UNSET else deadline,
                    date_value=deadline_date,
                    time_value=deadline_time,
                    field_name="deadline",
                    timezone=self.timezone,
                )
            if next_action is not _UNSET:
                item.next_action = _optional_single_line(
                    next_action, field_name="next_action", maximum=NEXT_ACTION_MAX_LENGTH
                )
            if any(
                value is not _UNSET
                for value in (next_action_at, next_action_date, next_action_time)
            ):
                item.next_action_at = _when(
                    next_action_at,
                    date_value=next_action_date,
                    time_value=next_action_time,
                    field_name="next_action",
                    timezone=self.timezone,
                )
            item.updated_at = now
            self._sync_task(session, item, now=now)
            item.updated_at = now
            session.flush()
            session.commit()
            return self._opportunity_view(item)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    update_job = update_opportunity

    def update_application(
        self,
        application_id: int,
        *,
        opportunity_id: object = _UNSET,
        stage: object = _UNSET,
        application_stage: object = _UNSET,
        status: object = _UNSET,
        notes: object = _UNSET,
        location: object = _UNSET,
        url: object = _UNSET,
        deadline: object = _UNSET,
        deadline_at: object = _UNSET,
        deadline_date: object = _UNSET,
        deadline_time: object = _UNSET,
        next_action: object = _UNSET,
        next_action_at: object = _UNSET,
        next_action_date: object = _UNSET,
        next_action_time: object = _UNSET,
    ) -> CareerRecord:
        item_id = _positive_id(application_id, field_name="application_id")
        session = self._new_session()
        try:
            item = self._application(session, item_id)
            now = self._now()
            if opportunity_id is not _UNSET:
                item.opportunity_id = _positive_id(
                    opportunity_id, field_name="opportunity_id"
                )
                self._opportunity(session, item.opportunity_id)
            if location is not _UNSET:
                item.opportunity.location = _optional_single_line(
                    location, field_name="location", maximum=LOCATION_MAX_LENGTH
                )
            if url is not _UNSET:
                item.opportunity.url = normalize_url(url)
            selected_stage = stage if stage is not _UNSET else application_stage
            if selected_stage is not _UNSET:
                normalized_stage = _stage(selected_stage)
                item.stage = normalized_stage.value
                if status is _UNSET:
                    item.status = _status_for_stage(normalized_stage).value
            if status is not _UNSET:
                item.status = _status(status).value
            if notes is not _UNSET:
                item.notes = _optional_notes(notes)
            if any(
                value is not _UNSET
                for value in (deadline, deadline_at, deadline_date, deadline_time)
            ):
                item.deadline_at = _when(
                    deadline_at if deadline_at is not _UNSET else deadline,
                    date_value=deadline_date,
                    time_value=deadline_time,
                    field_name="deadline",
                    timezone=self.timezone,
                )
            if next_action is not _UNSET:
                item.next_action = _optional_single_line(
                    next_action, field_name="next_action", maximum=NEXT_ACTION_MAX_LENGTH
                )
            if any(
                value is not _UNSET
                for value in (next_action_at, next_action_date, next_action_time)
            ):
                item.next_action_at = _when(
                    next_action_at,
                    date_value=next_action_date,
                    time_value=next_action_time,
                    field_name="next_action",
                    timezone=self.timezone,
                )
            item.updated_at = now
            self._sync_task(session, item, now=now)
            item.updated_at = now
            session.flush()
            session.commit()
            return self._application_view(item)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    update_application_stage = update_application

    def complete_opportunity(self, opportunity_id: int) -> CareerRecord:
        return self.update_opportunity(opportunity_id, status=CareerStatus.CLOSED)

    close_opportunity = complete_opportunity

    def complete_application(self, application_id: int) -> CareerRecord:
        return self.update_application(application_id, stage=ApplicationStage.CLOSED)

    close_application = complete_application

    def reject_opportunity(self, opportunity_id: int) -> CareerRecord:
        return self.update_opportunity(opportunity_id, status=CareerStatus.REJECTED)

    def reject_application(self, application_id: int) -> CareerRecord:
        return self.update_application(application_id, stage=ApplicationStage.REJECTED)

    def withdraw_application(self, application_id: int) -> CareerRecord:
        return self.update_application(application_id, stage=ApplicationStage.WITHDRAWN)

    def complete(self, record_id: int, *, record_type: str = "opportunity") -> CareerRecord:
        if _record_type(record_type) == "application":
            return self.complete_application(record_id)
        return self.complete_opportunity(record_id)

    def add(self, company: str, title: str, **kwargs: Any) -> CareerRecord:
        return self.create(company, title, **kwargs)

    def update(
        self,
        record_id: int,
        *,
        record_type: str = "opportunity",
        **changes: Any,
    ) -> CareerRecord:
        if _record_type(record_type) == "application":
            return self.update_application(record_id, **changes)
        return self.update_opportunity(record_id, **changes)

    def get_record(
        self,
        record_id: int,
        *,
        record_type: str = "opportunity",
    ) -> CareerRecord | None:
        if _record_type(record_type) == "application":
            return self.get_application(record_id)
        return self.get_opportunity(record_id)


def _list_sort_key(item: CareerRecord) -> tuple[bool, datetime, int, str]:
    timestamp = item.next_action_at or item.deadline_at or datetime.max
    return (item.is_closed, timestamp, item.id, item.record_type)


__all__ = [
    "ApplicationStage",
    "CAREER_COMPANY_MAX_LENGTH",
    "CAREER_NOTES_MAX_LENGTH",
    "CAREER_TITLE_MAX_LENGTH",
    "CAREER_URL_MAX_LENGTH",
    "CareerApplicationStage",
    "CareerApplicationView",
    "CareerItem",
    "CareerItemKind",
    "CareerKind",
    "CareerOpportunityKind",
    "CareerOpportunityView",
    "CareerRecord",
    "CareerService",
    "CareerStatus",
    "JobApplication",
    "JobApplicationStage",
    "JobOpportunity",
    "SUPPORTED_APPLICATION_STAGES",
    "SUPPORTED_CAREER_KINDS",
    "SUPPORTED_CAREER_STATUSES",
    "normalize_url",
]
