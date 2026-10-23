"""Inbox capture, analysis orchestration, and event state transitions."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .analyzer import AnalyzerProtocol, AnalyzerUnavailable
from .attachments import AttachmentError, AttachmentStore
from .career import CareerService
from .channel_parsers import (
    RhythmInstructionKind,
    parse_rhythm_instruction,
    split_account_prefix,
)
from .config import Settings
from .constants import EventKind, EventStatus, MealType, OutsideHome
from .models import AIRun, Attachment, Event, Expense, Meal, ReceiptItem
from .normalization import (
    category_for_merchant,
    normalize_merchant,
    validate_category,
    validate_subcategory,
)
from .parser import ParsedMessage, parse_message
from .repositories import AliasRepository, EventRepository, confirmed_expense_for_merchant
from .schemas import (
    InboxRouteResult,
    MealAnalysisResult,
    ReceiptAnalysisResult,
    TextAnalysisResult,
    validate_meal_result,
    validate_receipt_result,
    validate_text_result,
)

logger = logging.getLogger(__name__)

INBOX_ROUTE_INPUT_TYPE = "inbox_route"
INBOX_ROUTE_PROMPT_VERSION = "inbox-router-2"
_MEMO_ROUTE_RE = re.compile(
    r"^(?:memo|メモ|一言メモ)(?:\s*[:：]\s*|\s+)(?P<body>\S[\s\S]*)$",
    re.IGNORECASE,
)
_TASK_ROUTE_RE = re.compile(
    r"^(?:task|タスク|予定)(?:\s*[:：]\s*|\s+)(?P<title>\S[\s\S]*)$",
    re.IGNORECASE,
)
_CAREER_ROUTE_RE = re.compile(
    r"^(?P<prefix>career|就活|応募)(?:\s*[:：]\s*|\s+)(?P<body>\S[\s\S]*)$",
    re.IGNORECASE,
)
_CAREER_EXPLICIT_DEADLINE_RE = re.compile(
    r"\s+(?P<date>\d{4}-\d{2}-\d{2})(?:[ T](?P<time>\d{2}:\d{2}))?\s*$"
)
_RHYTHM_TODO_PREFIXES = ("todo", "課題", "やること")
_RHYTHM_RESULT_PREFIXES = ("result", "リザルト", "結果")
_EXPLICIT_FINANCE_ROUTE_RE = re.compile(
    r"(?:[￥¥]\s*\d|\d[\d,]*\s*円(?:\s|$)|^\s*\d[\d,]*(?:\s+\S|\s*$))"
)
_STRONG_MEAL_ROUTE_RE = re.compile(
    r"朝ご飯|朝飯|朝食|昼ご飯|昼飯|昼食|夕ご飯|夕飯|晩飯|夕食|夜ご飯|夜食|"
    r"おやつ|間食|食事|ごはん|ご飯|料理|ラーメン|食べた|食べる|弁当|定食"
)

# Categories whose meaning was explicitly selected in Discord.  These are
# archive/view destinations rather than domain analyzers, so they need one
# small Event transition before Notion can index them by category and time.
_REVIEWED_CLASSIFIER_DESTINATIONS = frozenset(
    {
        "self_talk",
        "dining_out",
        "finance_account",
        "finance_card",
        "memories",
        "social_draft",
        "shopping",
        "saved_link",
    }
)


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def utc_naive(value: datetime | None) -> datetime:
    if value is None:
        return utc_now()
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def local_datetime_to_utc_naive(value: datetime, timezone_name: str) -> datetime:
    """Interpret offset-free receipt times in the configured local timezone."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(timezone_name))
    return value.astimezone(UTC).replace(tzinfo=None)


@dataclass(slots=True)
class IncomingAttachment:
    discord_attachment_id: str | int
    original_filename: str
    content_type: str
    data: bytes


@dataclass(slots=True)
class IncomingMessage:
    discord_message_id: str | int
    discord_channel_id: str | int
    discord_user_id: str | int
    raw_text: str = ""
    captured_at: datetime | None = None
    attachments: list[IncomingAttachment] = field(default_factory=list)
    guild_id: str | int | None = None


@dataclass(slots=True)
class CaptureResult:
    event_id: int
    duplicate: bool = False
    attachment_errors: list[str] = field(default_factory=list)
    analysis_error: str | None = None


@dataclass(frozen=True, slots=True)
class InboxRouteOutcome:
    """Safe routing decision for an Event whose raw payload is already durable."""

    event_id: int
    route: InboxRouteResult
    source: str
    routing_failed: bool = False
    reused: bool = False


@dataclass(slots=True)
class _AnalysisState:
    expense: dict[str, Any] | None = None
    meal: dict[str, Any] | None = None
    receipt_items: list[dict[str, Any]] = field(default_factory=list)
    receipt_analyzed: bool = False
    occurred_at: datetime | None = None
    ai_used: bool = False
    ai_failed: bool = False


class InboxService:
    """Business logic independent from Discord objects and network clients."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        settings: Settings,
        analyzer: AnalyzerProtocol,
        *,
        attachment_store: AttachmentStore | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.analyzer = analyzer
        self.attachment_store = attachment_store or AttachmentStore(
            settings.data_dir, max_bytes=settings.attachment_max_bytes
        )
        self.career = CareerService(
            session_factory,
            timezone=settings.timezone_info(),
        )
        self.career_service = self.career

    def _new_session(self) -> Session:
        return self.session_factory()

    def _insert_raw_event(self, message: IncomingMessage) -> tuple[int, bool]:
        """Commit the raw Event before touching attachments or AI."""

        session = self._new_session()
        try:
            message_id = str(message.discord_message_id)
            existing = EventRepository(session).by_discord_message_id(message_id)
            if existing is not None:
                session.rollback()
                return existing.id, True
            captured_at = utc_naive(message.captured_at)
            event = Event(
                discord_message_id=message_id,
                discord_channel_id=str(message.discord_channel_id),
                discord_user_id=str(message.discord_user_id),
                captured_at=captured_at,
                occurred_at=captured_at,
                raw_text=message.raw_text or "",
                source="discord",
                status=EventStatus.PENDING,
                event_type="unknown",
                event_kinds=[],
            )
            session.add(event)
            session.flush()
            event_id = event.id
            session.commit()
            return event_id, False
        except IntegrityError:
            session.rollback()
            existing = EventRepository(session).by_discord_message_id(
                str(message.discord_message_id)
            )
            if existing is None:
                raise
            return existing.id, True
        finally:
            session.close()

    def _register_attachments(
        self, event_id: int, attachments: Iterable[IncomingAttachment]
    ) -> list[str]:
        errors: list[str] = []
        # Each attachment has an independent transaction. A bad later file
        # must never roll back rows already committed for this Event.
        for incoming in attachments:
            session = self._new_session()
            try:
                try:
                    attachment_id = str(incoming.discord_attachment_id)
                    existing_attachment = session.scalar(
                        select(Attachment).where(Attachment.discord_attachment_id == attachment_id)
                    )
                    if existing_attachment is not None:
                        if existing_attachment.event_id != event_id:
                            raise AttachmentError(
                                "Discord attachment ID is already linked to another Event"
                            )
                        continue
                    digest = self.attachment_store.sha256(incoming.data)
                    existing_hash = session.scalar(
                        select(Attachment)
                        .where(Attachment.sha256 == digest)
                        .order_by(Attachment.id)
                    )
                    if existing_hash is not None and Path(existing_hash.local_path).exists():
                        local_path = existing_hash.local_path
                    else:
                        local_path, digest = self.attachment_store.save(
                            event_id=event_id,
                            original_filename=incoming.original_filename,
                            content_type=incoming.content_type,
                            data=incoming.data,
                        )
                    session.add(
                        Attachment(
                            event_id=event_id,
                            discord_attachment_id=attachment_id,
                            original_filename=Path(incoming.original_filename).name or "attachment",
                            content_type=incoming.content_type,
                            local_path=local_path,
                            sha256=digest,
                        )
                    )
                    session.flush()
                    session.commit()
                except (AttachmentError, OSError, IntegrityError) as exc:
                    session.rollback()
                    # Continue registering later attachments. The raw event
                    # remains committed and the caller can retry it.
                    errors.append(type(exc).__name__)
            finally:
                session.close()
        return errors

    def capture(self, message: IncomingMessage) -> CaptureResult:
        captured = self.capture_raw(message)
        if captured.duplicate:
            return captured
        event_id = captured.event_id
        analysis_error: str | None = None
        try:
            analyzed = self.analyze_event(event_id)
            if getattr(analyzed, "_analysis_failed", False):
                analysis_error = "AIAnalysisFailed"
        except Exception as exc:  # defensive last line: raw event must survive
            logger.exception(
                "event analysis failed for event_id=%s (%s)", event_id, type(exc).__name__
            )
            analysis_error = type(exc).__name__
        return CaptureResult(
            event_id=event_id,
            attachment_errors=captured.attachment_errors,
            analysis_error=analysis_error,
        )

    def capture_raw(self, message: IncomingMessage) -> CaptureResult:
        """Persist an Event and its attachments without invoking any analyzer.

        Specialized deterministic channels (accounts, memos, and rhythm-game
        logs) use this entry point so their literal input is durable before the
        corresponding domain service validates or structures it.
        """

        event_id, duplicate = self._insert_raw_event(message)
        if duplicate:
            return CaptureResult(event_id=event_id, duplicate=True)
        return CaptureResult(
            event_id=event_id,
            attachment_errors=self._register_attachments(event_id, message.attachments),
        )

    async def capture_discord_message_raw(self, message: Any) -> CaptureResult:
        """Persist one discord.py message and its image bytes without AI."""

        event_id, duplicate = await asyncio.to_thread(
            self._insert_raw_event,
            IncomingMessage(
                discord_message_id=message.id,
                discord_channel_id=message.channel.id,
                discord_user_id=message.author.id,
                raw_text=message.content or "",
                captured_at=getattr(message, "created_at", None),
                guild_id=getattr(getattr(message, "guild", None), "id", None),
            ),
        )
        if duplicate:
            return CaptureResult(event_id=event_id, duplicate=True)

        incoming: list[IncomingAttachment] = []
        attachment_errors: list[str] = []
        for attachment in getattr(message, "attachments", []) or []:
            try:
                if getattr(attachment, "size", 0) > self.settings.attachment_max_bytes:
                    raise AttachmentError("attachment exceeds configured size limit")
                data = await attachment.read()
                incoming.append(
                    IncomingAttachment(
                        discord_attachment_id=attachment.id,
                        original_filename=attachment.filename,
                        content_type=attachment.content_type or "",
                        data=data,
                    )
                )
            except Exception as exc:
                logger.warning("attachment capture failed (%s)", type(exc).__name__)
                attachment_errors.append(type(exc).__name__)
        attachment_errors.extend(
            await asyncio.to_thread(self._register_attachments, event_id, incoming)
        )
        return CaptureResult(event_id=event_id, attachment_errors=attachment_errors)

    async def capture_discord_message(self, message: Any) -> CaptureResult:
        """Capture a discord.py message while preserving capture ordering."""

        captured = await self.capture_discord_message_raw(message)
        if captured.duplicate:
            return captured
        event_id = captured.event_id
        analysis_error: str | None = None
        try:
            analyzed = await asyncio.to_thread(self.analyze_event, event_id)
            if getattr(analyzed, "_analysis_failed", False):
                analysis_error = "AIAnalysisFailed"
        except Exception as exc:
            logger.exception(
                "event analysis failed for event_id=%s (%s)", event_id, type(exc).__name__
            )
            analysis_error = type(exc).__name__
        return CaptureResult(
            event_id=event_id,
            attachment_errors=captured.attachment_errors,
            analysis_error=analysis_error,
        )

    def _record_ai_run(
        self,
        session: Session,
        *,
        event_id: int,
        input_type: str,
        result: Any | None = None,
        error: BaseException | None = None,
    ) -> None:
        provider = getattr(self.analyzer, "provider", type(self.analyzer).__name__.lower())
        model = getattr(self.analyzer, "model", "mock")
        if error is not None:
            payload: dict[str, Any] = {
                "ok": False,
                "error_type": type(error).__name__,
            }
            confidence = None
        else:
            if hasattr(result, "model_dump"):
                payload = {"ok": True, "result": result.model_dump(mode="json")}
                confidence = getattr(result, "confidence", None)
            else:
                payload = {"ok": True, "result": result}
                confidence = getattr(result, "confidence", None) if result is not None else None
        session.add(
            AIRun(
                event_id=event_id,
                provider=str(provider),
                model=str(model),
                prompt_version=str(getattr(self.analyzer, "prompt_version", "mvp-1")),
                input_type=input_type,
                result_json=json.dumps(payload, ensure_ascii=False, default=str),
                confidence=confidence,
            )
        )

    @staticmethod
    def _starts_with_route_prefix(raw_text: str, prefixes: tuple[str, ...]) -> bool:
        lowered = raw_text.casefold()
        return any(
            lowered == prefix.casefold()
            or lowered.startswith(prefix.casefold() + " ")
            for prefix in prefixes
        )

    def _deterministic_inbox_route(self, event: Event) -> InboxRouteResult | None:
        """Return only high-confidence local routes; ambiguity goes to the analyzer."""

        raw_text = (event.raw_text or "").strip()
        memo_match = _MEMO_ROUTE_RE.fullmatch(raw_text)
        if memo_match is not None:
            return InboxRouteResult(
                destination="memo",
                content=memo_match.group("body"),
                confidence=1.0,
            )

        career_match = _CAREER_ROUTE_RE.fullmatch(raw_text)
        if career_match is not None:
            # A deterministic career route is intentionally narrow.  It only
            # accepts an explicit ``会社 | 職種`` shape; a bare ``就活: ...``
            # remains analyzer/unknown territory rather than inventing a
            # company or a deadline.
            parts = re.split(r"\s*[|｜]\s*", career_match.group("body"), maxsplit=1)
            if len(parts) == 2 and all(part.strip() for part in parts):
                prefix = career_match.group("prefix").casefold()
                company = parts[0].strip()
                title = parts[1].strip()
                deadline_match = _CAREER_EXPLICIT_DEADLINE_RE.search(title)
                route_kwargs: dict[str, Any] = {}
                if deadline_match is not None:
                    candidate_title = title[: deadline_match.start()].rstrip(" /,、")
                    if candidate_title:
                        try:
                            route_kwargs = {
                                "career_title": candidate_title,
                                "career_deadline_date": deadline_match.group("date"),
                                "career_deadline_time": deadline_match.group("time"),
                            }
                            InboxRouteResult(
                                destination="career",
                                career_company=company,
                                confidence=1.0,
                                **route_kwargs,
                            )
                        except ValueError:
                            route_kwargs = {}
                return InboxRouteResult(
                    destination="career",
                    career_company=company,
                    career_title=route_kwargs.pop("career_title", title),
                    career_kind=(
                        "application" if prefix == "応募" else "job"
                    ),
                    confidence=1.0,
                    **route_kwargs,
                )

        is_rhythm_todo = self._starts_with_route_prefix(
            raw_text, _RHYTHM_TODO_PREFIXES
        )
        is_rhythm_result = self._starts_with_route_prefix(
            raw_text, _RHYTHM_RESULT_PREFIXES
        )
        if is_rhythm_todo or is_rhythm_result:
            try:
                instruction = parse_rhythm_instruction(raw_text)
            except ValueError:
                instruction = None
            if instruction is not None and (
                instruction.kind == RhythmInstructionKind.RESULT
                or instruction.game is not None
            ):
                return InboxRouteResult(
                    destination=(
                        "rhythm_todo"
                        if instruction.kind == RhythmInstructionKind.TODO
                        else "rhythm_result"
                    ),
                    game=instruction.game,
                    content=instruction.text or None,
                    confidence=1.0,
                )

        task_match = _TASK_ROUTE_RE.fullmatch(raw_text)
        if task_match is not None:
            return InboxRouteResult(
                destination="life_task",
                task_title=task_match.group("title"),
                confidence=1.0,
            )

        if event.attachments and not raw_text:
            # Image-only input keeps the established receipt/meal path. Text
            # plus an image may still be a memo or rhythm result, but router
            # requests receive only the text and never the attachment bytes.
            return InboxRouteResult(destination="finance_or_meal", confidence=1.0)

        if not raw_text:
            return InboxRouteResult(destination="unknown", confidence=1.0)

        parsed = parse_message(raw_text, max_amount_yen=self.settings.max_expense_yen)
        if parsed.has_meal_hint and _STRONG_MEAL_ROUTE_RE.search(raw_text) is not None:
            return InboxRouteResult(destination="finance_or_meal", confidence=1.0)
        if parsed.amount is not None and _EXPLICIT_FINANCE_ROUTE_RE.search(raw_text) is not None:
            return InboxRouteResult(destination="finance_or_meal", confidence=0.99)
        return None

    @staticmethod
    def _ground_life_task_due(
        route: InboxRouteResult,
        raw_text: str,
    ) -> InboxRouteResult:
        """Keep only task dates/times explicitly present in the user's text."""

        due_date = route.task_due_date
        if route.destination != "life_task" or due_date is None:
            return route
        date_forms = {
            due_date.isoformat(),
            f"{due_date.year}/{due_date.month}/{due_date.day}",
            f"{due_date.year}/{due_date.month:02d}/{due_date.day:02d}",
            f"{due_date.year}年{due_date.month}月{due_date.day}日",
            f"{due_date.year}年{due_date.month:02d}月{due_date.day:02d}日",
        }
        if not any(value in raw_text for value in date_forms):
            return route.model_copy(
                update={"task_due_date": None, "task_due_time": None}
            )

        due_time = route.task_due_time
        if due_time is None:
            return route
        if any(
            marker in raw_text
            for marker in ("午前", "午後", "頃", "ごろ", "ぐらい", "くらい", "あたり", "未定")
        ):
            return route.model_copy(update={"task_due_time": None})
        time_forms = {
            f"{due_time.hour}:{due_time.minute:02d}",
            f"{due_time.hour:02d}:{due_time.minute:02d}",
            f"{due_time.hour}時{due_time.minute}分",
            f"{due_time.hour:02d}時{due_time.minute:02d}分",
        }
        if due_time.minute == 0:
            time_forms.update({f"{due_time.hour}時", f"{due_time.hour:02d}時"})
        if not any(value in raw_text for value in time_forms):
            return route.model_copy(update={"task_due_time": None})
        return route

    @staticmethod
    def _explicit_date_forms(value: date) -> set[str]:
        return {
            value.isoformat(),
            f"{value.year}/{value.month}/{value.day}",
            f"{value.year}/{value.month:02d}/{value.day:02d}",
            f"{value.year}年{value.month}月{value.day}日",
            f"{value.year}年{value.month:02d}月{value.day:02d}日",
        }

    @staticmethod
    def _explicit_time_in_text(value: time, raw_text: str) -> bool:
        if any(
            marker in raw_text
            for marker in ("午前", "午後", "頃", "ごろ", "ぐらい", "くらい", "あたり", "未定")
        ):
            return False
        forms = {
            f"{value.hour}:{value.minute:02d}",
            f"{value.hour:02d}:{value.minute:02d}",
            f"{value.hour}時{value.minute}分",
            f"{value.hour:02d}時{value.minute:02d}分",
        }
        if value.minute == 0:
            forms.update({f"{value.hour}時", f"{value.hour:02d}時"})
        return any(form in raw_text for form in forms)

    @classmethod
    def _ground_career_dates(
        cls,
        route: InboxRouteResult,
        raw_text: str,
    ) -> InboxRouteResult:
        """Keep only career dates and times visibly stated in the raw text."""

        if route.destination != "career":
            return route
        updates: dict[str, Any] = {}
        for date_field, time_field, at_field in (
            (
                "career_deadline_date",
                "career_deadline_time",
                "career_deadline_at",
            ),
            (
                "career_next_action_date",
                "career_next_action_time",
                "career_next_action_at",
            ),
        ):
            explicit_date = getattr(route, date_field)
            explicit_time = getattr(route, time_field)
            explicit_at = getattr(route, at_field)
            if explicit_date is not None:
                if not any(
                    form in raw_text for form in cls._explicit_date_forms(explicit_date)
                ):
                    updates[date_field] = None
                    updates[time_field] = None
                    continue
                if explicit_time is not None and not cls._explicit_time_in_text(
                    explicit_time, raw_text
                ):
                    updates[time_field] = None
            elif explicit_at is not None:
                if not any(
                    form in raw_text
                    for form in cls._explicit_date_forms(explicit_at.date())
                ):
                    updates[at_field] = None
                    continue
                if not cls._explicit_time_in_text(explicit_at.time(), raw_text):
                    updates[at_field] = datetime.combine(explicit_at.date(), time.min)
        if not updates:
            return route
        return route.model_copy(update=updates)

    def _stored_inbox_route(
        self,
        session: Session,
        *,
        event_id: int,
    ) -> InboxRouteOutcome | None:
        run = session.scalar(
            select(AIRun)
            .where(
                AIRun.event_id == event_id,
                AIRun.input_type == INBOX_ROUTE_INPUT_TYPE,
            )
            .order_by(AIRun.id.desc())
        )
        if run is None:
            return None
        try:
            payload = json.loads(run.result_json)
            route = InboxRouteResult.model_validate(payload["result"])
            source = payload["source"]
            if not isinstance(source, str) or not source or len(source) > 64:
                raise ValueError("invalid route source")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("stored inbox route is invalid for event_id=%s", event_id)
            return None
        return InboxRouteOutcome(
            event_id=event_id,
            route=route,
            source=source,
            routing_failed=payload.get("routing_failed") is True,
            reused=True,
        )

    def _record_inbox_route(
        self,
        session: Session,
        *,
        event_id: int,
        route: InboxRouteResult,
        source: str,
        routing_failed: bool = False,
        error: BaseException | None = None,
    ) -> None:
        local_route = source == "deterministic"
        provider = (
            "local"
            if local_route
            else getattr(self.analyzer, "provider", type(self.analyzer).__name__.lower())
        )
        model = (
            "inbox-rules-1"
            if local_route
            else getattr(self.analyzer, "model", "unconfigured")
        )
        payload: dict[str, Any] = {
            "ok": error is None,
            "source": source,
            "routing_failed": routing_failed,
            "result": route.model_dump(mode="json"),
        }
        if error is not None:
            # Never persist provider messages: they may contain request text,
            # URLs, headers, or credential fragments.
            payload["error_type"] = type(error).__name__
        session.add(
            AIRun(
                event_id=event_id,
                provider=str(provider),
                model=str(model),
                prompt_version=str(
                    getattr(
                        self.analyzer,
                        "routing_prompt_version",
                        INBOX_ROUTE_PROMPT_VERSION,
                    )
                ),
                input_type=INBOX_ROUTE_INPUT_TYPE,
                result_json=json.dumps(payload, ensure_ascii=False, default=str),
                confidence=route.confidence,
            )
        )

    def route_inbox_event(self, event_id: int) -> InboxRouteOutcome:
        """Route one durable Event exactly once without exposing provider errors."""

        session = self._new_session()
        try:
            event = session.get(Event, event_id)
            if event is None:
                raise ValueError(f"event not found: {event_id}")

            stored = self._stored_inbox_route(session, event_id=event.id)
            if stored is not None:
                return stored

            route = self._deterministic_inbox_route(event)
            source = "deterministic"
            routing_failed = False
            error: BaseException | None = None
            if route is None:
                route_method = getattr(self.analyzer, "route_inbox", None)
                try:
                    if not callable(route_method):
                        raise AnalyzerUnavailable("inbox router is not configured")
                    route = InboxRouteResult.model_validate(route_method(event.raw_text))
                    route = self._ground_life_task_due(route, event.raw_text)
                    route = self._ground_career_dates(route, event.raw_text)
                    if route.destination == "memo":
                        # The analyzer chooses the destination; the durable raw
                        # Event remains the source of truth for memo wording.
                        route = route.model_copy(update={"content": event.raw_text.strip()})
                    source = "analyzer"
                except AnalyzerUnavailable as exc:
                    # Preserve the pre-router behavior when OpenAI is absent:
                    # the existing parser/analyze_event path remains usable.
                    error = exc
                    source = "finance_fallback"
                    route = InboxRouteResult(destination="finance_or_meal")
                except Exception as exc:
                    error = exc
                    source = "routing_error"
                    routing_failed = True
                    route = InboxRouteResult(destination="unknown")
                    logger.warning(
                        "inbox routing failed for event_id=%s (%s)",
                        event.id,
                        type(exc).__name__,
                    )

            self._record_inbox_route(
                session,
                event_id=event.id,
                route=route,
                source=source,
                routing_failed=routing_failed,
                error=error,
            )
            session.commit()
            return InboxRouteOutcome(
                event_id=event.id,
                route=route,
                source=source,
                routing_failed=routing_failed,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mark_event_routed(self, event_id: int, destination: str) -> Event:
        """Mark non-Event-backed destinations after their own write succeeds."""

        if destination not in {"life_task", "career"}:
            raise ValueError("unsupported routed destination")
        session = self._new_session()
        try:
            event = session.get(Event, event_id)
            if event is None:
                raise ValueError(f"event not found: {event_id}")
            if event.event_type not in {"unknown", destination}:
                raise ValueError("event already has a different routed destination")
            event.status = EventStatus.CONFIRMED.value
            event.event_type = destination
            event.event_kinds = [destination]
            event.updated_at = utc_now()
            session.commit()
            return event
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mark_event_classified(self, event_id: int, destination: str) -> Event:
        """Persist one user-reviewed archive category without inventing data."""

        if destination not in _REVIEWED_CLASSIFIER_DESTINATIONS:
            raise ValueError("unsupported classified destination")
        session = self._new_session()
        try:
            event = session.get(Event, event_id)
            if event is None:
                raise ValueError(f"event not found: {event_id}")
            if event.event_type not in {"unknown", destination}:
                raise ValueError("event already has a different classified destination")
            event.status = EventStatus.CONFIRMED.value
            event.event_type = destination
            event.event_kinds = [destination]
            event.updated_at = utc_now()
            session.commit()
            return event
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _alias_mapping(self, session: Session) -> dict[str, str]:
        aliases = AliasRepository(session)
        aliases.seed_defaults()
        session.commit()
        return aliases.mapping()

    def _base_expense(
        self,
        parsed: ParsedMessage,
        *,
        merchant_raw: str | None,
        merchant_normalized: str | None,
        category: str,
        subcategory: str | None,
        description: str | None,
        confidence: float | None,
    ) -> dict[str, Any]:
        validated_category = self._validate_category(category)
        return {
            "amount": parsed.amount,
            "merchant_raw": merchant_raw,
            "merchant_normalized": merchant_normalized,
            "category": validated_category,
            "subcategory": self._validate_subcategory(validated_category, subcategory),
            "description": description,
            "confidence": confidence,
        }

    def _validate_category(self, category: str | None) -> str:
        return validate_category(category, self.settings.categories)

    def _validate_subcategory(self, category: str, subcategory: str | None) -> str | None:
        return validate_subcategory(
            category,
            subcategory,
            self.settings.food_subcategories,
        )

    def _should_analyze_text(
        self, parsed: ParsedMessage, merchant_normalized: str | None, category: str
    ) -> bool:
        if parsed.amount_invalid or not parsed.raw_text.strip():
            return False
        if parsed.has_meal_hint:
            return False
        if parsed.amount is None:
            # A known alias without an amount is not made into an invented
            # expense; an unknown phrase is an ambiguity worth sending.
            return merchant_normalized is None or category == "未分類"
        if parsed.merchant_raw is None:
            return True
        return category == "未分類"

    def _image_intents(
        self, parsed: ParsedMessage, merchant_category: str, raw_text: str
    ) -> list[str]:
        if not raw_text.strip():
            return ["receipt", "meal"]
        lower = raw_text.casefold()
        if any(word in lower for word in ("レシート", "領収書", "receipt", "会計")):
            return ["receipt"]
        if parsed.has_meal_hint or merchant_category == "食費":
            return ["meal"]
        return ["receipt"]

    @staticmethod
    def _coerce_model(value: Any, model_type: type) -> Any:
        if isinstance(value, model_type):
            return value
        return model_type.model_validate(value)

    def _upsert_children(
        self,
        session: Session,
        event: Event,
        state: _AnalysisState,
    ) -> None:
        if state.expense is not None:
            expense = session.scalar(select(Expense).where(Expense.event_id == event.id))
            if expense is None:
                expense = Expense(event_id=event.id, **state.expense)
                session.add(expense)
                session.flush()
            else:
                for key, value in state.expense.items():
                    setattr(expense, key, value)
                session.flush()
            if state.receipt_analyzed:
                expense.receipt_items.clear()
                for index, item in enumerate(state.receipt_items):
                    expense.receipt_items.append(ReceiptItem(order_index=index, **item))
        if state.meal is not None:
            meal = session.scalar(select(Meal).where(Meal.event_id == event.id))
            if meal is None:
                session.add(Meal(event_id=event.id, **state.meal))
            else:
                for key, value in state.meal.items():
                    setattr(meal, key, value)

    def analyze_event(self, event_id: int, *, meal_intent: bool = False) -> Event:
        """Analyze one Event, optionally treating the input as an explicit meal.

        ``meal_intent`` is an adapter-level hint for reviewed inputs such as a
        classifier's ``meals`` choice.  It is applied only to the local
        analysis text; the durable ``Event.raw_text`` remains the user's
        original body.
        """

        session = self._new_session()
        try:
            event = session.get(Event, event_id)
            if event is None:
                raise ValueError(f"event not found: {event_id}")
            analysis_text = event.raw_text
            is_card_channel = (
                self.settings.card_channel_id is not None
                and event.discord_channel_id == str(self.settings.card_channel_id)
            )
            is_meal_channel = (
                self.settings.meal_channel_id is not None
                and event.discord_channel_id == str(self.settings.meal_channel_id)
            ) or meal_intent
            if is_card_channel:
                _, analysis_text = split_account_prefix(analysis_text)
            if is_meal_channel:
                # Keep Event.raw_text untouched while giving the deterministic
                # parser an explicit meal intent for this specialized channel.
                analysis_text = f"食事 {analysis_text}".strip()
            parsed = parse_message(
                analysis_text, max_amount_yen=self.settings.max_expense_yen
            )
            aliases = self._alias_mapping(session)
            merchant_raw = parsed.merchant_raw
            merchant_normalized = normalize_merchant(merchant_raw, aliases)
            known = confirmed_expense_for_merchant(session, merchant_raw, merchant_normalized)
            if known is not None:
                category, subcategory = known.category, known.subcategory
            else:
                category, subcategory = category_for_merchant(merchant_normalized)

            state = _AnalysisState()
            if parsed.amount is not None:
                state.expense = self._base_expense(
                    parsed,
                    merchant_raw=merchant_raw,
                    merchant_normalized=merchant_normalized,
                    category=category,
                    subcategory=subcategory,
                    description=parsed.description,
                    confidence=0.98 if merchant_normalized and category != "未分類" else 0.9,
                )

            text_result: TextAnalysisResult | None = None
            if self._should_analyze_text(parsed, merchant_normalized, category):
                state.ai_used = True
                try:
                    text_result = validate_text_result(
                        self._coerce_model(
                            self.analyzer.analyze_text(analysis_text), TextAnalysisResult
                        ),
                        max_amount_yen=self.settings.max_expense_yen,
                        allowed_categories=self.settings.categories,
                        allowed_food_subcategories=self.settings.food_subcategories,
                    )
                    self._record_ai_run(
                        session, event_id=event.id, input_type="text", result=text_result
                    )
                except Exception as exc:
                    state.ai_failed = True
                    self._record_ai_run(session, event_id=event.id, input_type="text", error=exc)
                    logger.warning(
                        "text analysis failed for event_id=%s (%s)", event.id, type(exc).__name__
                    )

            if text_result is not None:
                ai_has_expense = (
                    "expense" in text_result.event_kinds or text_result.amount is not None
                )
                if state.expense is None and ai_has_expense and not parsed.amount_invalid:
                    ai_amount = text_result.amount
                    if ai_amount is not None:
                        ai_raw = merchant_raw or text_result.merchant
                        # Preserve the user's original remainder as raw, while
                        # allowing the validated AI merchant to supply the
                        # normalized display value.
                        ai_normalized = normalize_merchant(text_result.merchant or ai_raw, aliases)
                        ai_category = (
                            text_result.category or category_for_merchant(ai_normalized)[0]
                        )
                        ai_subcategory = (
                            text_result.subcategory or category_for_merchant(ai_normalized)[1]
                        )
                        state.expense = {
                            "amount": ai_amount,
                            "merchant_raw": ai_raw,
                            "merchant_normalized": ai_normalized,
                            "category": self._validate_category(ai_category),
                            "subcategory": self._validate_subcategory(
                                self._validate_category(ai_category), ai_subcategory
                            ),
                            "description": text_result.description,
                            "confidence": text_result.confidence,
                        }
                elif state.expense is not None:
                    if text_result.merchant:
                        if state.expense["merchant_raw"] is None:
                            state.expense["merchant_raw"] = text_result.merchant
                        state.expense["merchant_normalized"] = normalize_merchant(
                            text_result.merchant, aliases
                        )
                    if state.expense["category"] == "未分類" and text_result.category:
                        state.expense["category"] = self._validate_category(text_result.category)
                        state.expense["subcategory"] = self._validate_subcategory(
                            state.expense["category"], text_result.subcategory
                        )
                    if state.expense["description"] is None:
                        state.expense["description"] = text_result.description
                    if text_result.confidence is not None:
                        state.expense["confidence"] = text_result.confidence

                if "meal" in text_result.event_kinds:
                    meal_raw = parsed.merchant_raw or text_result.restaurant or text_result.merchant
                    state.meal = {
                        "meal_type": text_result.meal_type.value,
                        "restaurant_raw": meal_raw,
                        "restaurant_normalized": normalize_merchant(
                            text_result.restaurant or text_result.merchant or meal_raw,
                            aliases,
                        ),
                        "description": text_result.meal_description or text_result.description,
                        "outside_home": text_result.outside_home.value,
                        "confidence": text_result.confidence,
                    }

            if parsed.has_meal_hint and state.meal is None:
                state.meal = {
                    "meal_type": parsed.meal_type.value,
                    "restaurant_raw": parsed.merchant_raw,
                    "restaurant_normalized": normalize_merchant(parsed.merchant_raw, aliases),
                    "description": parsed.description,
                    "outside_home": OutsideHome.OUTSIDE.value
                    if parsed.merchant_raw
                    else OutsideHome.UNKNOWN.value,
                    "confidence": 0.95 if parsed.meal_type != MealType.UNKNOWN else 0.75,
                }

            attachments = list(event.attachments)
            intents = (
                (
                    ["meal"]
                    if is_meal_channel
                    else (
                        ["receipt"]
                        if is_card_channel
                        else self._image_intents(parsed, category, analysis_text)
                    )
                )
                if attachments
                else []
            )
            for attachment in attachments:
                for intent in intents:
                    state.ai_used = True
                    try:
                        if intent == "receipt":
                            result = validate_receipt_result(
                                self._coerce_model(
                                    self.analyzer.analyze_receipt(
                                        attachment.local_path,
                                        attachment.content_type,
                                        context_text=analysis_text,
                                    ),
                                    ReceiptAnalysisResult,
                                ),
                                max_amount_yen=self.settings.max_expense_yen,
                            )
                            self._record_ai_run(
                                session, event_id=event.id, input_type="receipt", result=result
                            )
                            state.receipt_analyzed = True
                            if result.purchase_datetime is not None:
                                state.occurred_at = local_datetime_to_utc_naive(
                                    result.purchase_datetime,
                                    self.settings.timezone,
                                )
                            receipt_raw = result.merchant or (
                                state.expense["merchant_raw"] if state.expense else None
                            )
                            receipt_normalized = normalize_merchant(receipt_raw, aliases)
                            receipt_category, receipt_subcategory = category_for_merchant(
                                receipt_normalized
                            )
                            receipt_category = self._validate_category(receipt_category)
                            receipt_subcategory = self._validate_subcategory(
                                receipt_category, receipt_subcategory
                            )
                            receipt_has_expense_signal = bool(
                                result.merchant
                                or result.total is not None
                                or result.subtotal is not None
                                or result.items
                            )
                            if state.expense is None and receipt_has_expense_signal:
                                state.expense = {
                                    "amount": result.total,
                                    "merchant_raw": receipt_raw,
                                    "merchant_normalized": receipt_normalized,
                                    "category": receipt_category,
                                    "subcategory": receipt_subcategory,
                                    "description": None,
                                    "confidence": result.confidence,
                                }
                            elif state.expense is not None:
                                if receipt_raw:
                                    if state.expense["merchant_raw"] is None:
                                        state.expense["merchant_raw"] = receipt_raw
                                    state.expense["merchant_normalized"] = receipt_normalized
                                    if state.expense["category"] == "未分類":
                                        state.expense["category"] = receipt_category
                                        state.expense["subcategory"] = receipt_subcategory
                                # Keep an explicit text amount if it disagrees
                                # with OCR; the discrepancy stays visible via
                                # the AI run and can be edited by the user.
                            state.receipt_items = [
                                {
                                    "name": item.name,
                                    "quantity": item.quantity,
                                    "unit_price": item.unit_price,
                                    "amount": item.amount,
                                }
                                for item in result.items
                            ]
                        else:
                            result = validate_meal_result(
                                self._coerce_model(
                                    self.analyzer.analyze_meal(
                                        attachment.local_path,
                                        attachment.content_type,
                                        context_text=analysis_text,
                                    ),
                                    MealAnalysisResult,
                                )
                            )
                            self._record_ai_run(
                                session, event_id=event.id, input_type="meal", result=result
                            )
                            meal_has_signal = bool(
                                result.meal_type != MealType.UNKNOWN
                                or result.restaurant
                                or result.description
                                or result.outside_home != OutsideHome.UNKNOWN
                            )
                            if meal_has_signal:
                                if state.meal is None:
                                    state.meal = {
                                        "meal_type": result.meal_type.value,
                                        "restaurant_raw": result.restaurant,
                                        "restaurant_normalized": normalize_merchant(
                                            result.restaurant, aliases
                                        ),
                                        "description": result.description,
                                        "outside_home": result.outside_home.value,
                                        "confidence": result.confidence,
                                    }
                                else:
                                    if result.meal_type != MealType.UNKNOWN:
                                        state.meal["meal_type"] = result.meal_type.value
                                    if result.restaurant:
                                        if state.meal["restaurant_raw"] is None:
                                            state.meal["restaurant_raw"] = result.restaurant
                                        state.meal["restaurant_normalized"] = normalize_merchant(
                                            result.restaurant, aliases
                                        )
                                    if result.description:
                                        state.meal["description"] = result.description
                                    if result.outside_home != OutsideHome.UNKNOWN:
                                        state.meal["outside_home"] = result.outside_home.value
                                    if result.confidence is not None:
                                        state.meal["confidence"] = result.confidence
                    except Exception as exc:
                        state.ai_failed = True
                        self._record_ai_run(
                            session, event_id=event.id, input_type=intent, error=exc
                        )
                        logger.warning(
                            "%s analysis failed for event_id=%s (%s)",
                            intent,
                            event.id,
                            type(exc).__name__,
                        )

            self._upsert_children(session, event, state)
            if state.occurred_at is not None:
                event.occurred_at = state.occurred_at
            kinds: list[str] = []
            if state.expense is not None:
                kinds.append(EventKind.EXPENSE.value)
            if state.meal is not None:
                kinds.append(EventKind.MEAL.value)
            event.event_kinds = kinds
            event.event_type = "+".join(kinds) if kinds else "unknown"
            event.status = (
                EventStatus.CONFIRMED
                if self.settings.auto_confirm
                and not state.ai_used
                and not state.ai_failed
                and kinds
                else EventStatus.PENDING
            )
            event.updated_at = utc_now()
            session.commit()
            # Capture callers use this transient flag to tell the user that
            # raw data is safe but one or more AI passes need /retry.
            event._analysis_failed = state.ai_failed
            return event
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_event(self, event_id: int) -> Event | None:
        session = self._new_session()
        try:
            return EventRepository(session).by_id(event_id)
        finally:
            session.close()

    def get_event_by_discord_message_id(
        self,
        message_id: str | int,
    ) -> Event | None:
        """Return the durable Event for a Discord source message.

        Candidate reactions can arrive after a Bot restart, when the in-memory
        candidate view no longer exists.  Keeping this lookup in the service
        avoids letting the Discord adapter reach into SQLAlchemy sessions.
        """

        session = self._new_session()
        try:
            return EventRepository(session).by_discord_message_id(message_id)
        finally:
            session.close()

    def list_pending(self) -> list[Event]:
        session = self._new_session()
        try:
            return EventRepository(session).pending()
        finally:
            session.close()

    def list_confirmed(self) -> list[Event]:
        session = self._new_session()
        try:
            return EventRepository(session).confirmed()
        finally:
            session.close()

    def list_uncopied(self) -> list[Event]:
        session = self._new_session()
        try:
            return EventRepository(session).uncopied_confirmed()
        finally:
            session.close()

    def confirm_event(self, event_id: int) -> Event:
        session = self._new_session()
        try:
            event = EventRepository(session).by_id(event_id)
            if event is None:
                raise ValueError("event not found")
            aliases = AliasRepository(session)
            for expense in event.expenses:
                if expense.merchant_raw and expense.merchant_normalized:
                    aliases.upsert(expense.merchant_raw, expense.merchant_normalized)
            for meal in event.meals:
                if meal.restaurant_raw and meal.restaurant_normalized:
                    aliases.upsert(meal.restaurant_raw, meal.restaurant_normalized)
            event.status = EventStatus.CONFIRMED
            event.updated_at = utc_now()
            session.commit()
            return event
        finally:
            session.close()

    def ignore_event(self, event_id: int) -> Event:
        return self._set_status(event_id, EventStatus.IGNORED)

    def _set_status(self, event_id: int, status: EventStatus) -> Event:
        session = self._new_session()
        try:
            event = EventRepository(session).by_id(event_id)
            if event is None:
                raise ValueError("event not found")
            event.status = status
            event.updated_at = utc_now()
            session.commit()
            return event
        finally:
            session.close()

    @staticmethod
    def _edit_amount(value: Any, max_amount_yen: int) -> int:
        if isinstance(value, bool):
            raise ValueError("amount must be a positive integer")
        if isinstance(value, int):
            amount = value
        else:
            text = str(value).strip().replace(",", "")
            if not re.fullmatch(r"\d+", text):
                raise ValueError("amount must be a positive integer")
            amount = int(text)
        if amount <= 0 or amount > max_amount_yen:
            raise ValueError("amount is outside the allowed range")
        return amount

    def edit_event(self, event_id: int, **changes: Any) -> Event:
        session = self._new_session()
        try:
            event = EventRepository(session).by_id(event_id)
            if event is None:
                raise ValueError("event not found")
            expense = event.expenses[0] if event.expenses else None
            meal = event.meals[0] if event.meals else None
            expense_keys = {"amount", "merchant", "category", "subcategory", "description"}
            meal_keys = {"meal_type", "restaurant", "meal_description"}
            if expense is None and any(
                key in changes and changes[key] not in (None, "") for key in expense_keys
            ):
                expense = Expense(event_id=event.id, category="未分類")
                session.add(expense)
                session.flush()
            if meal is None and any(
                key in changes and changes[key] not in (None, "") for key in meal_keys
            ):
                meal = Meal(event_id=event.id, meal_type=MealType.UNKNOWN.value)
                session.add(meal)
                session.flush()
            if expense is not None:
                if "amount" in changes:
                    expense.amount = (
                        None
                        if changes["amount"] in (None, "")
                        else self._edit_amount(changes["amount"], self.settings.max_expense_yen)
                    )
                if "merchant" in changes:
                    merchant = str(changes["merchant"] or "").strip()[:255]
                    expense.merchant_normalized = normalize_merchant(merchant)
                    if merchant and expense.merchant_raw is None:
                        expense.merchant_raw = merchant
                if "category" in changes:
                    expense.category = self._validate_category(
                        str(changes["category"] or "").strip()
                    )
                if "subcategory" in changes:
                    subcategory = str(changes["subcategory"] or "").strip() or None
                    expense.subcategory = self._validate_subcategory(expense.category, subcategory)
                if "description" in changes:
                    description = str(changes["description"] or "").strip()[:1_000]
                    expense.description = description or None
            if meal is not None:
                if "meal_type" in changes:
                    meal_type = str(changes["meal_type"] or "").strip().lower()
                    japanese_types = {
                        "朝": "breakfast",
                        "昼": "lunch",
                        "夜": "dinner",
                        "おやつ": "snack",
                    }
                    meal.meal_type = japanese_types.get(meal_type, meal_type)
                    if meal.meal_type not in {item.value for item in MealType}:
                        meal.meal_type = MealType.UNKNOWN.value
                if "restaurant" in changes:
                    restaurant = str(changes["restaurant"] or "").strip()[:255]
                    meal.restaurant_normalized = normalize_merchant(restaurant)
                    if restaurant and meal.restaurant_raw is None:
                        meal.restaurant_raw = restaurant
                if "meal_description" in changes:
                    description = str(changes["meal_description"] or "").strip()[:1_000]
                    meal.description = description or None
                elif "description" in changes:
                    description = str(changes["description"] or "").strip()[:1_000]
                    meal.description = description or None
            event.event_kinds = []
            if expense is not None:
                event.event_kinds.append(EventKind.EXPENSE.value)
            if meal is not None:
                event.event_kinds.append(EventKind.MEAL.value)
            event.event_type = "+".join(event.event_kinds) if event.event_kinds else "unknown"
            event.status = EventStatus.PENDING
            event.updated_at = utc_now()
            session.commit()
            return event
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mark_copied(self, event_ids: Iterable[int]) -> list[int]:
        ids = list(dict.fromkeys(int(event_id) for event_id in event_ids))
        if not ids:
            return []
        session = self._new_session()
        try:
            events = list(session.scalars(select(Event).where(Event.id.in_(ids))).all())
            marked: list[int] = []
            now = utc_now()
            for event in events:
                if event.status == EventStatus.CONFIRMED:
                    event.copied_to_planner_at = now
                    event.updated_at = now
                    marked.append(event.id)
            session.commit()
            return marked
        finally:
            session.close()

    def mark_all_uncopied(self) -> list[int]:
        return self.mark_copied(event.id for event in self.list_uncopied())

    def retry_event(self, event_id: int) -> Event:
        session = self._new_session()
        try:
            event = EventRepository(session).by_id(event_id)
            if event is None:
                raise ValueError("event not found")
            if event.status != EventStatus.PENDING:
                raise ValueError("only pending events can be retried")
            event.updated_at = utc_now()
            session.commit()
        finally:
            session.close()
        return self.analyze_event(event_id)
