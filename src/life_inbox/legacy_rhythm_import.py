"""Non-destructive migration from the retired ArcadeRhythmLog JSON store.

The old app remains untouched and serves only as a migration source.  Credit
UUIDs become the revision-safe spending event IDs, while a small additive link
connects each spending row to a confirmed Life Archive Event for Notion.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .constants import EventStatus
from .models import Event, EventCaptureContext
from .rhythm_persistence import RhythmPersistence
from .rhythm_spending import RhythmSpendingEventPersistence, utc_naive
from .rhythm_spending_models import RhythmSpendingEventLink

if TYPE_CHECKING:
    from .notion_sync import NotionSyncService

_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_CREDITS = 100_000
_SUPPORTED_GAMES = frozenset({"chunithm", "maimai", "ongeki"})
_GAME_LABELS = {"chunithm": "ウニ", "maimai": "マイ", "ongeki": "ゲキ"}


class LegacyRhythmImportError(ValueError):
    """Raised before changing the destination when the legacy file is invalid."""


@dataclass(frozen=True, slots=True)
class LegacyRhythmCredit:
    event_id: str
    game: str
    amount_yen: int
    occurred_at: datetime
    song_play_count: int


@dataclass(frozen=True, slots=True)
class LegacyRhythmImportPlan:
    source_path: Path
    source_sha256: str
    source_size_bytes: int
    scanned_credit_count: int
    supported_credit_count: int
    unsupported_credit_count: int
    song_play_count: int
    total_yen: int
    totals_by_game: tuple[tuple[str, int], ...]
    first_occurred_at: datetime | None
    last_occurred_at: datetime | None
    credits: tuple[LegacyRhythmCredit, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class LegacyRhythmImportResult:
    plan: LegacyRhythmImportPlan
    applied: bool
    spending_created_count: int = 0
    spending_updated_count: int = 0
    spending_idempotent_count: int = 0
    archive_event_created_count: int = 0
    notion_queued_count: int = 0
    notion_queue_failed_count: int = 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: object, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LegacyRhythmImportError(f"{field_name} must be an object")
    return value


def _sequence(value: object, *, field_name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise LegacyRhythmImportError(f"{field_name} must be an array")
    return value


def _iso_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise LegacyRhythmImportError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise LegacyRhythmImportError(
            f"{field_name} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LegacyRhythmImportError(f"{field_name} must include a timezone")
    return parsed


def _game_name(value: object) -> str | None:
    normalized = re.sub(r"[\s_-]+", "", str(value or "").casefold())
    if normalized in {"chunithm", "chuni", "チュウニズム", "ウニ"}:
        return "chunithm"
    if normalized in {"maimai", "マイマイ", "マイ"}:
        return "maimai"
    if normalized in {"ongeki", "オンゲキ", "ゲキ"}:
        return "ongeki"
    return None


class LegacyRhythmAppImporter:
    """Plan or apply an idempotent import without editing the source file."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory
        self.rhythm = RhythmPersistence(session_factory)
        self.spending = RhythmSpendingEventPersistence(session_factory)

    @staticmethod
    def plan_file(source_path: str | Path) -> LegacyRhythmImportPlan:
        path = Path(source_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise LegacyRhythmImportError("legacy source must be a file")
        size = path.stat().st_size
        if size <= 0 or size > _MAX_SOURCE_BYTES:
            raise LegacyRhythmImportError("legacy source size is outside the safe limit")
        digest = _sha256_file(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LegacyRhythmImportError("legacy source is not valid UTF-8 JSON") from exc
        root = _mapping(payload, field_name="root")
        games = _sequence(root.get("games"), field_name="games")
        sessions = _sequence(root.get("sessions"), field_name="sessions")

        games_by_id: dict[str, str | None] = {}
        for index, raw_game in enumerate(games):
            game = _mapping(raw_game, field_name=f"games[{index}]")
            game_id = str(game.get("id") or "").strip()
            if not game_id:
                raise LegacyRhythmImportError("game id is missing")
            games_by_id[game_id] = _game_name(game.get("name")) or _game_name(
                game.get("shortName")
            )

        credits_by_id: dict[str, LegacyRhythmCredit] = {}
        scanned = 0
        unsupported = 0
        song_play_count = 0
        for session_index, raw_session in enumerate(sessions):
            session = _mapping(raw_session, field_name=f"sessions[{session_index}]")
            raw_credits = _sequence(
                session.get("credits", ()),
                field_name=f"sessions[{session_index}].credits",
            )
            for credit_index, raw_credit in enumerate(raw_credits):
                scanned += 1
                if scanned > _MAX_CREDITS:
                    raise LegacyRhythmImportError("legacy source contains too many credits")
                credit = _mapping(
                    raw_credit,
                    field_name=f"sessions[{session_index}].credits[{credit_index}]",
                )
                try:
                    event_id = str(UUID(str(credit.get("id") or "")))
                except ValueError as exc:
                    raise LegacyRhythmImportError("credit id must be a UUID") from exc
                amount = credit.get("amountJPY")
                if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                    raise LegacyRhythmImportError("credit amountJPY must be a positive integer")
                if amount > 10_000_000:
                    raise LegacyRhythmImportError("credit amountJPY exceeds the safe limit")
                raw_plays = _sequence(
                    credit.get("songPlays", ()),
                    field_name="credit.songPlays",
                )
                song_play_count += len(raw_plays)
                game = games_by_id.get(str(credit.get("gameID") or ""))
                if game not in _SUPPORTED_GAMES:
                    unsupported += 1
                    continue
                occurred_at = _iso_datetime(
                    credit.get("createdAt") or session.get("date"),
                    field_name="credit.createdAt",
                )
                parsed = LegacyRhythmCredit(
                    event_id=event_id,
                    game=game,
                    amount_yen=amount,
                    occurred_at=occurred_at,
                    song_play_count=len(raw_plays),
                )
                previous = credits_by_id.get(event_id)
                if previous is not None and previous != parsed:
                    raise LegacyRhythmImportError(
                        "duplicate credit UUID contains conflicting data"
                    )
                credits_by_id[event_id] = parsed

        credits = tuple(
            sorted(credits_by_id.values(), key=lambda item: (item.occurred_at, item.event_id))
        )
        totals = {
            game: sum(item.amount_yen for item in credits if item.game == game)
            for game in sorted(_SUPPORTED_GAMES)
        }
        dates = [item.occurred_at for item in credits]
        return LegacyRhythmImportPlan(
            source_path=path,
            source_sha256=digest,
            source_size_bytes=size,
            scanned_credit_count=scanned,
            supported_credit_count=len(credits),
            unsupported_credit_count=unsupported,
            song_play_count=song_play_count,
            total_yen=sum(item.amount_yen for item in credits),
            totals_by_game=tuple((game, totals[game]) for game in sorted(totals)),
            first_occurred_at=min(dates) if dates else None,
            last_occurred_at=max(dates) if dates else None,
            credits=credits,
        )

    def import_file(
        self,
        source_path: str | Path,
        *,
        apply: bool,
        display_name: str,
        discord_user_id: str | int | None,
        source_user_id: str,
        time_zone: str = "Asia/Tokyo",
        notion_sync: NotionSyncService | None = None,
    ) -> LegacyRhythmImportResult:
        plan = self.plan_file(source_path)
        if not apply:
            return LegacyRhythmImportResult(plan=plan, applied=False)

        rhythm_user_id = self.rhythm.ensure_user(
            display_name=display_name,
            time_zone=time_zone,
            discord_user_id=discord_user_id,
        )
        created_count = 0
        updated_count = 0
        idempotent_count = 0
        archive_created_count = 0
        notion_queued_count = 0
        notion_failed_count = 0
        destination_timezone = ZoneInfo(time_zone)

        for credit in plan.credits:
            existing = self.spending.get_event(
                rhythm_user_id=rhythm_user_id,
                event_id=credit.event_id,
            )
            revision = 1
            if existing is not None:
                same_payload = (
                    existing.source_user_id == source_user_id.strip()
                    and existing.game == credit.game
                    and existing.amount_yen == credit.amount_yen
                    and existing.occurred_at == utc_naive(credit.occurred_at)
                    and existing.time_zone == time_zone
                    and existing.source == "import"
                    and not existing.deleted
                )
                revision = existing.revision if same_payload else existing.revision + 1
            synced = self.spending.sync_event(
                rhythm_user_id=rhythm_user_id,
                event_id=credit.event_id,
                source_user_id=source_user_id,
                game=credit.game,
                amount_yen=credit.amount_yen,
                # ArcadeRhythmLog writes ISO-8601 instants in UTC (``Z``).
                # The sync contract deliberately requires the timestamp offset
                # to agree with ``timeZone``, so express the same instant in the
                # destination zone before deriving its local calendar date.
                occurred_at=credit.occurred_at.astimezone(destination_timezone),
                time_zone=time_zone,
                revision=revision,
                source="import",
                deleted=False,
            )
            if synced.created:
                created_count += 1
            elif synced.idempotent:
                idempotent_count += 1
            else:
                updated_count += 1

            event_id, event_created = self._upsert_archive_event(
                credit,
                spending_event_id=synced.record.id,
                discord_user_id=discord_user_id,
                source_user_id=source_user_id,
            )
            archive_created_count += int(event_created)
            if notion_sync is not None:
                try:
                    result = notion_sync.enqueue_event_if_eligible(event_id)
                except Exception:  # noqa: BLE001 - import remains durable; no provider data logged
                    notion_failed_count += 1
                else:
                    notion_queued_count += int(result is not None)

        if _sha256_file(plan.source_path) != plan.source_sha256:
            raise LegacyRhythmImportError("legacy source changed during import")
        return LegacyRhythmImportResult(
            plan=plan,
            applied=True,
            spending_created_count=created_count,
            spending_updated_count=updated_count,
            spending_idempotent_count=idempotent_count,
            archive_event_created_count=archive_created_count,
            notion_queued_count=notion_queued_count,
            notion_queue_failed_count=notion_failed_count,
        )

    def _upsert_archive_event(
        self,
        credit: LegacyRhythmCredit,
        *,
        spending_event_id: int,
        discord_user_id: str | int | None,
        source_user_id: str,
    ) -> tuple[int, bool]:
        occurred_at = utc_naive(credit.occurred_at)
        message_id = f"legacy-rhythm-{credit.event_id}"
        raw_text = f"{_GAME_LABELS[credit.game]} ¥{credit.amount_yen:,}"
        session = self.session_factory()
        try:
            event = session.scalar(
                select(Event).where(Event.discord_message_id == message_id)
            )
            created = event is None
            if event is None:
                event = Event(
                    discord_message_id=message_id,
                    discord_channel_id="legacy-rhythm-import",
                    discord_user_id=str(discord_user_id or source_user_id)[:64],
                    captured_at=occurred_at,
                    occurred_at=occurred_at,
                    raw_text=raw_text,
                    source="legacy_rhythm_app",
                    status=EventStatus.CONFIRMED.value,
                    event_type="rhythm_spending",
                    event_kinds=["rhythm_spending", f"rhythm_game_{credit.game}"],
                )
                event.capture_context = EventCaptureContext(
                    capture_type="rhythm",
                    source_label="legacyApp",
                    route_category="rhythm_spending",
                )
                session.add(event)
                session.flush()
            elif event.source != "legacy_rhythm_app":
                raise LegacyRhythmImportError(
                    "legacy event key is already used by another source"
                )
            else:
                event.captured_at = occurred_at
                event.occurred_at = occurred_at
                event.raw_text = raw_text
                event.status = EventStatus.CONFIRMED.value
                event.event_type = "rhythm_spending"
                event.event_kinds = ["rhythm_spending", f"rhythm_game_{credit.game}"]
                if event.capture_context is None:
                    event.capture_context = EventCaptureContext(
                        capture_type="rhythm",
                        source_label="legacyApp",
                        route_category="rhythm_spending",
                    )

            link = session.get(RhythmSpendingEventLink, event.id)
            if link is None:
                occupied = session.scalar(
                    select(RhythmSpendingEventLink).where(
                        RhythmSpendingEventLink.spending_event_id == spending_event_id
                    )
                )
                if occupied is not None:
                    raise LegacyRhythmImportError(
                        "spending event is already linked to another archive event"
                    )
                session.add(
                    RhythmSpendingEventLink(
                        event_id=event.id,
                        spending_event_id=spending_event_id,
                    )
                )
            elif link.spending_event_id != spending_event_id:
                raise LegacyRhythmImportError(
                    "legacy archive event is linked to another spending event"
                )
            session.commit()
            return event.id, created
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


__all__ = [
    "LegacyRhythmAppImporter",
    "LegacyRhythmCredit",
    "LegacyRhythmImportError",
    "LegacyRhythmImportPlan",
    "LegacyRhythmImportResult",
]
