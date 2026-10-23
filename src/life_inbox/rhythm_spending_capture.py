"""Turn a reviewed Discord message into one rhythm-spending event.

The free-form classifier only proposes this route.  This module performs a
second deterministic parse after the user selects the candidate, then links
the revision-safe yen record back to the already-durable Life Archive Event.
It never asks a model to invent a game or amount.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .constants import EventStatus
from .models import Event, EventCaptureContext
from .rhythm_persistence import RhythmPersistence
from .rhythm_spending import (
    RhythmSpendingEventPersistence,
    SpendingEventRecord,
    utc_naive,
)
from .rhythm_spending_models import RhythmSpendingEventLink

_GAME_PATTERNS: dict[str, re.Pattern[str]] = {
    "chunithm": re.compile(r"chunithm|チュウニズム|チュニズム|ウニ", re.IGNORECASE),
    "maimai": re.compile(r"maimai|マイマイ|マイ(?!ナンバー)", re.IGNORECASE),
    "ongeki": re.compile(r"ongeki|オンゲキ|ゲキ", re.IGNORECASE),
}
_YEN_AMOUNT_RE = re.compile(
    r"(?:[￥¥]\s*(?P<prefix>\d[\d,]*)|(?P<suffix>\d[\d,]*)\s*円)",
    re.IGNORECASE,
)
_BARE_AMOUNT_RE = re.compile(r"(?<!\d)(\d{1,7})(?!\d)")
_BARE_ALLOWED_RE = re.compile(
    r"[\s:：,，。.!！?？/／\-ー_]*(?:使った|利用|プレイ|遊んだ|円)?"
    r"[\s:：,，。.!！?？/／\-ー_]*",
    re.IGNORECASE,
)
_MAX_AMOUNT_YEN = 10_000_000


@dataclass(frozen=True, slots=True)
class ParsedRhythmSpending:
    game: str
    amount_yen: int


@dataclass(frozen=True, slots=True)
class RhythmSpendingCaptureResult:
    archive_event_id: int
    spending: SpendingEventRecord
    created: bool
    idempotent: bool


def parse_rhythm_spending(text: str) -> ParsedRhythmSpending | None:
    """Parse one explicit game and one yen amount, otherwise fail closed."""

    raw = (text or "").strip()
    if not raw:
        return None
    games = [game for game, pattern in _GAME_PATTERNS.items() if pattern.search(raw)]
    if len(games) != 1:
        return None

    amount_matches = list(_YEN_AMOUNT_RE.finditer(raw))
    if len(amount_matches) == 1:
        value = amount_matches[0].group("prefix") or amount_matches[0].group("suffix")
    elif amount_matches:
        return None
    else:
        # The short form used from Discord/Action Button is intentionally
        # supported (for example, "マイ 600").  Accept it only when the text
        # contains one number and nothing meaningful besides the game cue and
        # a small allowlist of spending words.
        number_matches = list(_BARE_AMOUNT_RE.finditer(raw))
        if len(number_matches) != 1:
            return None
        value = number_matches[0].group(1)
        remainder = _GAME_PATTERNS[games[0]].sub("", raw)
        remainder = _BARE_AMOUNT_RE.sub("", remainder, count=1)
        if not _BARE_ALLOWED_RE.fullmatch(remainder):
            return None

    amount = int(value.replace(",", ""))
    if not 0 < amount <= _MAX_AMOUNT_YEN:
        return None
    return ParsedRhythmSpending(game=games[0], amount_yen=amount)


def spending_event_uuid(discord_message_id: str) -> str:
    """Return a stable UUID without exposing the Discord snowflake as an API key."""

    normalized = str(discord_message_id).strip()
    if not normalized:
        raise ValueError("Discord message ID is required")
    return str(uuid5(NAMESPACE_URL, f"life-inbox:rhythm-spending:{normalized}"))


class RhythmSpendingCaptureService:
    """Idempotently persist one user-reviewed Discord spending capture."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory
        self.rhythm = RhythmPersistence(session_factory)
        self.spending = RhythmSpendingEventPersistence(session_factory)

    @staticmethod
    def _client_source(event: Event) -> str:
        label = (
            event.capture_context.source_label.casefold()
            if event.capture_context is not None
            and event.capture_context.source_label
            else ""
        )
        if "widget" in label:
            return "widget"
        if "siri" in label:
            return "siri"
        if "calendar" in label:
            return "calendar"
        # The existing public API contract predates direct Discord capture and
        # its SQLite CHECK allows app/widget/shortcut/siri/calendar/import.
        # Keep that stable schema and use the linked Event.source="discord" as
        # the authoritative ingress metadata for a direct Discord message.
        return "shortcut"

    def record_event(
        self,
        event_id: int,
        *,
        display_name: str,
        time_zone: str,
        allowed_discord_user_id: str | int | None,
    ) -> RhythmSpendingCaptureResult:
        session = self.session_factory()
        try:
            event = session.get(Event, event_id)
            if event is None:
                raise ValueError("inbox event was not found")
            parsed = parse_rhythm_spending(event.raw_text)
            if parsed is None:
                raise ValueError("one rhythm game and one yen amount are required")
            discord_message_id = event.discord_message_id
            event_user_id = event.discord_user_id
            occurred_at = event.occurred_at
            client_source = self._client_source(event)
            existing_link = session.get(RhythmSpendingEventLink, event.id)
            if existing_link is not None:
                linked_id = existing_link.spending_event.event_id
                record = self.spending.get_event(
                    rhythm_user_id=existing_link.spending_event.rhythm_user_id,
                    event_id=linked_id,
                )
                if record is None:
                    raise RuntimeError("linked rhythm spending event is unavailable")
                if record.game != parsed.game or record.amount_yen != parsed.amount_yen:
                    raise ValueError("Event is already linked to different rhythm spending")
                return RhythmSpendingCaptureResult(event.id, record, False, True)
        finally:
            session.close()

        rhythm_user_id = self.rhythm.ensure_user(
            display_name=display_name,
            time_zone=time_zone,
            discord_user_id=allowed_discord_user_id,
        )
        aware_occurred_at = occurred_at.replace(tzinfo=UTC).astimezone(ZoneInfo(time_zone))
        stable_event_id = spending_event_uuid(discord_message_id)
        existing = self.spending.get_event(
            rhythm_user_id=rhythm_user_id,
            event_id=stable_event_id,
        )
        revision = 1
        if existing is not None:
            same = (
                existing.source_user_id == str(event_user_id)[:100]
                and existing.game == parsed.game
                and existing.amount_yen == parsed.amount_yen
                and existing.occurred_at == utc_naive(aware_occurred_at)
                and existing.time_zone == time_zone
                and not existing.deleted
            )
            revision = existing.revision if same else existing.revision + 1
        synced = self.spending.sync_event(
            rhythm_user_id=rhythm_user_id,
            event_id=stable_event_id,
            source_user_id=str(event_user_id)[:100],
            game=parsed.game,
            amount_yen=parsed.amount_yen,
            occurred_at=aware_occurred_at,
            time_zone=time_zone,
            revision=revision,
            source=client_source,
            deleted=False,
        )

        session = self.session_factory()
        try:
            event = session.get(Event, event_id)
            if event is None:
                raise ValueError("inbox event was not found after spending save")
            if event.event_type not in {"unknown", "rhythm_spending"}:
                raise ValueError("Event is already routed to another destination")
            occupied = session.scalar(
                select(RhythmSpendingEventLink).where(
                    RhythmSpendingEventLink.spending_event_id == synced.record.id
                )
            )
            if occupied is not None and occupied.event_id != event.id:
                raise ValueError("rhythm spending event is linked to another Event")
            link = session.get(RhythmSpendingEventLink, event.id)
            if link is None:
                event.rhythm_spending_link = RhythmSpendingEventLink(
                    spending_event_id=synced.record.id
                )
            elif link.spending_event_id != synced.record.id:
                raise ValueError("Event is linked to another rhythm spending event")
            event.status = EventStatus.CONFIRMED.value
            event.event_type = "rhythm_spending"
            event.event_kinds = [
                "rhythm_spending",
                f"rhythm_game_{parsed.game}",
            ]
            if event.capture_context is None:
                event.capture_context = EventCaptureContext(
                    capture_type="rhythm",
                    source_label=("discord" if event.source == "discord" else event.source),
                    route_category="rhythm_spending",
                )
            else:
                event.capture_context.route_category = "rhythm_spending"
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        return RhythmSpendingCaptureResult(
            archive_event_id=event_id,
            spending=synced.record,
            created=synced.created,
            idempotent=synced.idempotent,
        )


__all__ = [
    "ParsedRhythmSpending",
    "RhythmSpendingCaptureResult",
    "RhythmSpendingCaptureService",
    "parse_rhythm_spending",
    "spending_event_uuid",
]
