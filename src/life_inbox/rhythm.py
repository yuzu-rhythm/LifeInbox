"""Local-first persistence service for rhythm-game TODOs and result logs."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from .attachments import SUPPORTED_IMAGE_TYPES
from .constants import EventKind, EventStatus, RhythmGame, RhythmTodoStatus
from .maimai import (
    MaimaiMyBestRecord,  # noqa: F401
    MaimaiMyBestService,  # noqa: F401
    MaimaiNetFavoriteRecord,  # noqa: F401
    MaimaiNetFavoriteService,  # noqa: F401
    MaimaiSnapshotImportService,  # noqa: F401
)
from .models import Event, RhythmFavorite, RhythmResult, RhythmTodo

TITLE_MAX_LENGTH = 255
NOTE_MAX_LENGTH = 1_000
FAVORITE_EXTERNAL_ID_MAX_LENGTH = 255
PROVIDER_FAVORITE_MAX_ITEMS = 500
FAVORITE_SOURCES = frozenset({"manual", "maimai_net"})


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def local_day_window(
    target_date: date,
    timezone_name: str | ZoneInfo,
) -> tuple[datetime, datetime]:
    """Return one local calendar day as UTC-naive database bounds."""

    if isinstance(target_date, datetime) or not isinstance(target_date, date):
        raise ValueError("target_date must be a date")
    try:
        timezone = (
            timezone_name
            if isinstance(timezone_name, ZoneInfo)
            else ZoneInfo(timezone_name)
        )
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc
    start_local = datetime.combine(target_date, time.min, tzinfo=timezone)
    end_local = datetime.combine(
        target_date + timedelta(days=1), time.min, tzinfo=timezone
    )
    return (
        start_local.astimezone(UTC).replace(tzinfo=None),
        end_local.astimezone(UTC).replace(tzinfo=None),
    )


@dataclass(frozen=True, slots=True)
class RhythmTodoResult:
    """Detached snapshot returned for a persisted rhythm TODO."""

    id: int
    event_id: int
    discord_message_id: str
    game: RhythmGame
    title: str
    status: RhythmTodoStatus
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def event_message_id(self) -> str:
        """Alias useful when a caller is linking a Discord reply."""

        return self.discord_message_id


@dataclass(frozen=True, slots=True)
class RhythmResultRecord:
    """Detached snapshot returned for a persisted rhythm result."""

    id: int
    event_id: int
    discord_message_id: str
    todo_id: int | None
    game: RhythmGame
    note: str | None
    created_at: datetime
    updated_at: datetime
    discord_channel_id: str | None = None
    occurred_at: datetime | None = None
    attachment_count: int = 0
    todo_title: str | None = None

    @property
    def event_message_id(self) -> str:
        """Alias useful when a caller needs the source Discord message ID."""

        return self.discord_message_id


@dataclass(frozen=True, slots=True)
class RhythmFavoriteRecord:
    """Detached snapshot returned for a local or provider favorite."""

    id: int
    game: RhythmGame
    song_title: str
    normalized_title: str
    note: str | None
    source: str
    external_id: str | None
    active: bool
    created_at: datetime
    updated_at: datetime

    @property
    def title(self) -> str:
        """Compatibility alias for callers that use the generic title name."""

        return self.song_title


# Descriptive aliases keep the snapshot boundary discoverable to callers that
# prefer that terminology over the service result names.
RhythmTodoSnapshot = RhythmTodoResult
RhythmResultSnapshot = RhythmResultRecord


class RhythmService:
    """Persist rhythm-game TODOs and results through a supplied session factory."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def _new_session(self) -> Session:
        return self.session_factory()

    @staticmethod
    def _game(value: RhythmGame | str) -> RhythmGame:
        if isinstance(value, RhythmGame):
            return value
        if not isinstance(value, str):
            raise ValueError("unsupported rhythm game")
        try:
            return RhythmGame(value.strip().casefold())
        except ValueError as exc:
            raise ValueError("unsupported rhythm game") from exc

    @staticmethod
    def _title(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("TODO title must be a string")
        title = value.strip()
        if not title:
            raise ValueError("TODO title must not be blank")
        if len(title) > TITLE_MAX_LENGTH:
            raise ValueError("TODO title is too long")
        return title

    @staticmethod
    def _note(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("result note must be a string")
        note = value.strip()
        if not note:
            return None
        if len(note) > NOTE_MAX_LENGTH:
            raise ValueError("result note is too long")
        return note

    @staticmethod
    def _favorite_text(value: str, *, field_name: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be a string")
        normalized = unicodedata.normalize("NFKC", value)
        normalized = " ".join(normalized.split())
        if not normalized:
            raise ValueError(f"{field_name} must not be blank")
        if len(normalized) > maximum:
            raise ValueError(f"{field_name} is too long")
        return normalized

    @classmethod
    def _favorite_title(cls, value: str) -> tuple[str, str]:
        title = cls._favorite_text(
            value,
            field_name="favorite song title",
            maximum=TITLE_MAX_LENGTH,
        )
        normalized_key = title.casefold()
        if len(normalized_key) > TITLE_MAX_LENGTH:
            raise ValueError("favorite song title is too long")
        return title, normalized_key

    @classmethod
    def _favorite_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return cls._favorite_text(value, field_name="favorite note", maximum=NOTE_MAX_LENGTH)

    @staticmethod
    def _favorite_source(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("favorite source must be a string")
        source = value.strip().casefold()
        if source not in FAVORITE_SOURCES:
            raise ValueError("unsupported favorite source")
        return source

    @classmethod
    def _favorite_external_id(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("favorite external ID must be a string")
        normalized = unicodedata.normalize("NFKC", value).strip()
        if not normalized:
            return None
        if len(normalized) > FAVORITE_EXTERNAL_ID_MAX_LENGTH:
            raise ValueError("favorite external ID is too long")
        return normalized

    @staticmethod
    def _event(session: Session, event_id: int) -> Event:
        event = session.scalar(select(Event).where(Event.id == event_id))
        if event is None:
            raise ValueError("event not found")
        return event

    @staticmethod
    def _todo_for_event(session: Session, event_id: int) -> RhythmTodo | None:
        return session.scalar(
            select(RhythmTodo)
            .where(RhythmTodo.event_id == event_id)
            .options(joinedload(RhythmTodo.event))
        )

    @staticmethod
    def _result_for_event(session: Session, event_id: int) -> RhythmResult | None:
        return session.scalar(
            select(RhythmResult)
            .where(RhythmResult.event_id == event_id)
            .options(
                joinedload(RhythmResult.event).selectinload(Event.attachments),
                joinedload(RhythmResult.todo),
            )
        )

    @staticmethod
    def _mark_event(event: Event, kind: EventKind) -> None:
        event.status = EventStatus.CONFIRMED.value
        event.event_type = kind.value
        event.event_kinds = [kind.value]
        event.updated_at = _utc_now()

    @staticmethod
    def _complete_todo(todo: RhythmTodo, completed_at: datetime) -> None:
        if todo.status != RhythmTodoStatus.COMPLETED.value:
            todo.status = RhythmTodoStatus.COMPLETED.value
            todo.completed_at = completed_at
            todo.updated_at = completed_at
        elif todo.completed_at is None:
            # Repair an incomplete legacy row while retaining the completed
            # state and making the completion timestamp explicit.
            todo.completed_at = completed_at
            todo.updated_at = completed_at

    @staticmethod
    def _todo_result(todo: RhythmTodo) -> RhythmTodoResult:
        return RhythmTodoResult(
            id=todo.id,
            event_id=todo.event_id,
            discord_message_id=todo.event.discord_message_id,
            game=RhythmGame(todo.game),
            title=todo.title,
            status=RhythmTodoStatus(todo.status),
            completed_at=todo.completed_at,
            created_at=todo.created_at,
            updated_at=todo.updated_at,
        )

    @staticmethod
    def _result_record(result: RhythmResult) -> RhythmResultRecord:
        image_count = sum(
            1
            for attachment in result.event.attachments or []
            if (attachment.content_type or "").lower().split(";", 1)[0].strip()
            in SUPPORTED_IMAGE_TYPES
        )
        return RhythmResultRecord(
            id=result.id,
            event_id=result.event_id,
            discord_message_id=result.event.discord_message_id,
            todo_id=result.todo_id,
            game=RhythmGame(result.game),
            note=result.note,
            created_at=result.created_at,
            updated_at=result.updated_at,
            discord_channel_id=result.event.discord_channel_id,
            occurred_at=result.event.occurred_at,
            attachment_count=image_count,
            todo_title=result.todo.title if result.todo is not None else None,
        )

    @staticmethod
    def _favorite_record(favorite: RhythmFavorite) -> RhythmFavoriteRecord:
        return RhythmFavoriteRecord(
            id=favorite.id,
            game=RhythmGame(favorite.game),
            song_title=favorite.song_title,
            normalized_title=favorite.normalized_title,
            note=favorite.note,
            source=favorite.source,
            external_id=favorite.external_id,
            active=bool(favorite.active),
            created_at=favorite.created_at,
            updated_at=favorite.updated_at,
        )

    @staticmethod
    def _todo_matches(todo: RhythmTodo, *, game: RhythmGame, title: str) -> bool:
        return todo.game == game.value and todo.title == title

    @staticmethod
    def _result_matches(
        result: RhythmResult,
        *,
        game: RhythmGame,
        note: str | None,
        todo_id: int | None,
    ) -> bool:
        return (
            result.game == game.value
            and result.note == note
            and result.todo_id == todo_id
        )

    def create_todo(
        self,
        event_id: int,
        game: RhythmGame | str,
        title: str,
    ) -> RhythmTodoResult:
        """Create or retrieve the TODO represented by one Event.

        Repeating the same normalized game/title for an Event is idempotent;
        changing either value is rejected as a conflicting link.
        """

        normalized_game = self._game(game)
        normalized_title = self._title(title)
        session = self._new_session()
        try:
            event = self._event(session, event_id)
            existing = self._todo_for_event(session, event.id)
            if existing is not None:
                if self._todo_matches(
                    existing,
                    game=normalized_game,
                    title=normalized_title,
                ):
                    return self._todo_result(existing)
                raise ValueError("event already has a different rhythm TODO")

            todo = RhythmTodo(
                event_id=event.id,
                game=normalized_game.value,
                title=normalized_title,
                status=RhythmTodoStatus.OPEN.value,
            )
            session.add(todo)
            self._mark_event(event, EventKind.RHYTHM_TODO)
            try:
                session.flush()
            except IntegrityError as exc:
                # A concurrent caller may have won the event_id unique
                # constraint. Re-read the winner and preserve exact-repeat
                # idempotence without swallowing a real conflict.
                session.rollback()
                existing = self._todo_for_event(session, event.id)
                if existing is not None and self._todo_matches(
                    existing,
                    game=normalized_game,
                    title=normalized_title,
                ):
                    return self._todo_result(existing)
                if existing is not None:
                    raise ValueError("event already has a different rhythm TODO") from exc
                raise ValueError("could not create rhythm TODO") from exc
            snapshot = self._todo_result(todo)
            session.commit()
            return snapshot
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record_result(
        self,
        event_id: int,
        game: RhythmGame | str | None = None,
        note: str | None = None,
        todo_id: int | None = None,
    ) -> RhythmResultRecord:
        """Persist one result Event, optionally linking it to a TODO.

        A linked TODO is completed on the first linked result. Further result
        Events may reference the same TODO and remain part of its history.
        """

        normalized_game = self._game(game) if game is not None else None
        normalized_note = self._note(note)
        session = self._new_session()
        try:
            event = self._event(session, event_id)
            existing = self._result_for_event(session, event.id)

            todo: RhythmTodo | None = None
            if todo_id is not None:
                todo = session.get(RhythmTodo, todo_id)
                if todo is None:
                    raise ValueError("TODO not found")
                todo_game = self._game(todo.game)
                if normalized_game is None:
                    normalized_game = todo_game
                elif normalized_game != todo_game:
                    raise ValueError("result game does not match TODO game")
            elif normalized_game is None and existing is not None:
                normalized_game = self._game(existing.game)

            if normalized_game is None:
                raise ValueError("game is required when TODO is not supplied")

            if existing is not None:
                if self._result_matches(
                    existing,
                    game=normalized_game,
                    note=normalized_note,
                    todo_id=todo_id,
                ):
                    if todo is not None:
                        now = _utc_now()
                        self._complete_todo(todo, now)
                        session.commit()
                    return self._result_record(existing)
                raise ValueError("event already has a different rhythm result")

            result = RhythmResult(
                event_id=event.id,
                todo_id=todo.id if todo is not None else None,
                game=normalized_game.value,
                note=normalized_note,
            )
            session.add(result)
            if todo is not None:
                self._complete_todo(todo, _utc_now())
            self._mark_event(event, EventKind.RHYTHM_RESULT)
            try:
                session.flush()
            except IntegrityError as exc:
                # As with TODO creation, make a concurrent exact repeat safe
                # while reporting a different payload as a conflict.
                session.rollback()
                existing = self._result_for_event(session, event.id)
                if existing is not None and self._result_matches(
                    existing,
                    game=normalized_game,
                    note=normalized_note,
                    todo_id=todo_id,
                ):
                    return self._result_record(existing)
                if existing is not None:
                    raise ValueError("event already has a different rhythm result") from exc
                raise ValueError("could not record rhythm result") from exc
            snapshot = self._result_record(result)
            session.commit()
            return snapshot
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_open_todos(self, game: RhythmGame | str | None = None) -> list[RhythmTodoResult]:
        """Return open TODOs, optionally restricted to one supported game."""

        normalized_game = self._game(game) if game is not None else None
        session = self._new_session()
        try:
            statement = (
                select(RhythmTodo)
                .where(RhythmTodo.status == RhythmTodoStatus.OPEN.value)
                .options(joinedload(RhythmTodo.event))
                .order_by(RhythmTodo.created_at.desc(), RhythmTodo.id.desc())
            )
            if normalized_game is not None:
                statement = statement.where(RhythmTodo.game == normalized_game.value)
            todos = list(session.scalars(statement).all())
            return [self._todo_result(todo) for todo in todos]
        finally:
            session.close()

    def list_results(self, game: RhythmGame | str | None = None) -> list[RhythmResultRecord]:
        """Return result logs newest first, optionally restricted to one game."""

        normalized_game = self._game(game) if game is not None else None
        session = self._new_session()
        try:
            statement = (
                select(RhythmResult)
                .join(RhythmResult.event)
                .options(
                    joinedload(RhythmResult.event).selectinload(Event.attachments),
                    joinedload(RhythmResult.todo),
                )
                .order_by(Event.occurred_at.desc(), RhythmResult.id.desc())
            )
            if normalized_game is not None:
                statement = statement.where(RhythmResult.game == normalized_game.value)
            results = list(session.scalars(statement).all())
            return [self._result_record(result) for result in results]
        finally:
            session.close()

    def list_results_between(
        self,
        start_utc: datetime,
        end_utc: datetime,
        game: RhythmGame | str | None = None,
    ) -> list[RhythmResultRecord]:
        """Return result records inside a half-open UTC-naive time window."""

        if not isinstance(start_utc, datetime) or not isinstance(end_utc, datetime):
            raise ValueError("result window must use datetime values")
        if start_utc.tzinfo is not None or end_utc.tzinfo is not None:
            raise ValueError("result window must use UTC-naive datetimes")
        if end_utc <= start_utc:
            raise ValueError("result window must have a positive duration")
        normalized_game = self._game(game) if game is not None else None
        session = self._new_session()
        try:
            statement = (
                select(RhythmResult)
                .join(RhythmResult.event)
                .where(Event.occurred_at >= start_utc, Event.occurred_at < end_utc)
                .options(
                    joinedload(RhythmResult.event).selectinload(Event.attachments),
                    joinedload(RhythmResult.todo),
                )
                .order_by(Event.occurred_at.desc(), RhythmResult.id.desc())
            )
            if normalized_game is not None:
                statement = statement.where(RhythmResult.game == normalized_game.value)
            results = list(session.scalars(statement).all())
            return [self._result_record(result) for result in results]
        finally:
            session.close()

    def list_results_today(
        self,
        game: RhythmGame | str | None = None,
        *,
        timezone_name: str = "Asia/Tokyo",
        target_date: date | None = None,
    ) -> list[RhythmResultRecord]:
        """Return only results occurring on today's configured local date."""

        try:
            timezone = ZoneInfo(timezone_name)
        except (TypeError, ZoneInfoNotFoundError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        local_date = target_date or datetime.now(timezone).date()
        start_utc, end_utc = local_day_window(local_date, timezone)
        return self.list_results_between(start_utc, end_utc, game)

    # This domain-oriented alias keeps the day query discoverable to callers
    # that do not use the Discord command name.
    def list_today_results(
        self,
        game: RhythmGame | str | None = None,
        *,
        timezone_name: str = "Asia/Tokyo",
        target_date: date | None = None,
    ) -> list[RhythmResultRecord]:
        return self.list_results_today(
            game,
            timezone_name=timezone_name,
            target_date=target_date,
        )

    def add_favorite(
        self,
        game: RhythmGame | str,
        song_title: str,
        note: str | None = None,
    ) -> RhythmFavoriteRecord:
        """Add or update one local manual favorite idempotently."""

        normalized_game = self._game(game)
        normalized_title, normalized_key = self._favorite_title(song_title)
        normalized_note = self._favorite_note(note)
        session = self._new_session()
        try:
            favorite = session.scalar(
                select(RhythmFavorite).where(
                    RhythmFavorite.game == normalized_game.value,
                    RhythmFavorite.normalized_title == normalized_key,
                    RhythmFavorite.source == "manual",
                )
            )
            if favorite is None:
                favorite = RhythmFavorite(
                    game=normalized_game.value,
                    song_title=normalized_title,
                    normalized_title=normalized_key,
                    note=normalized_note,
                    source="manual",
                    active=True,
                )
                session.add(favorite)
            else:
                changed = (
                    favorite.song_title != normalized_title
                    or favorite.note != normalized_note
                    or not favorite.active
                )
                favorite.song_title = normalized_title
                favorite.note = normalized_note
                favorite.active = True
                if changed:
                    favorite.updated_at = _utc_now()
            try:
                session.flush()
            except IntegrityError as exc:
                session.rollback()
                favorite = session.scalar(
                    select(RhythmFavorite).where(
                        RhythmFavorite.game == normalized_game.value,
                        RhythmFavorite.normalized_title == normalized_key,
                        RhythmFavorite.source == "manual",
                    )
                )
                if favorite is None:
                    raise ValueError("could not add favorite") from exc
            snapshot = self._favorite_record(favorite)
            session.commit()
            return snapshot
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _provider_value(item: Mapping[str, object] | object, name: str) -> object:
        if isinstance(item, Mapping):
            return item.get(name)
        return getattr(item, name, None)

    def upsert_provider_favorites(
        self,
        source: str,
        favorites: Iterable[Mapping[str, object] | object],
        *,
        max_items: int = PROVIDER_FAVORITE_MAX_ITEMS,
    ) -> list[RhythmFavoriteRecord]:
        """Upsert a bounded provider batch without deleting local favorites.

        This is intentionally a persistence-only seam. It accepts already
        acquired provider data, performs no network or credential work, and
        treats an empty batch as a no-op so a failed/empty provider response
        cannot clear the local list.
        """

        normalized_source = self._favorite_source(source)
        if normalized_source == "manual":
            raise ValueError("provider source must not be manual")
        if isinstance(max_items, bool) or not isinstance(max_items, int):
            raise ValueError("max_items must be an integer")
        if max_items < 1 or max_items > PROVIDER_FAVORITE_MAX_ITEMS:
            raise ValueError(
                f"max_items must be between 1 and {PROVIDER_FAVORITE_MAX_ITEMS}"
            )

        normalized_entries: list[tuple[RhythmGame, str, str, str | None, str | None]] = []
        for index, item in enumerate(favorites):
            if index >= max_items:
                raise ValueError(f"provider favorite batch exceeds {max_items} items")
            game_value = self._provider_value(item, "game")
            title_value = self._provider_value(item, "song_title")
            if title_value is None:
                title_value = self._provider_value(item, "title")
            title, normalized_key = self._favorite_title(title_value)  # type: ignore[arg-type]
            normalized_entries.append(
                (
                    self._game(game_value),  # type: ignore[arg-type]
                    title,
                    normalized_key,
                    self._favorite_note(
                        self._provider_value(item, "note")  # type: ignore[arg-type]
                    ),
                    self._favorite_external_id(
                        self._provider_value(item, "external_id")
                        or self._provider_value(item, "external_key")
                    ),
                )
            )

        if not normalized_entries:
            return []

        session = self._new_session()
        try:
            snapshots: list[RhythmFavoriteRecord] = []
            for game, title, normalized_key, note, external_id in normalized_entries:
                if external_id is not None:
                    favorite = session.scalar(
                        select(RhythmFavorite).where(
                            RhythmFavorite.source == normalized_source,
                            RhythmFavorite.external_id == external_id,
                        )
                    )
                else:
                    favorite = session.scalar(
                        select(RhythmFavorite).where(
                            RhythmFavorite.source == normalized_source,
                            RhythmFavorite.game == game.value,
                            RhythmFavorite.normalized_title == normalized_key,
                            RhythmFavorite.external_id.is_(None),
                        )
                    )
                if favorite is None:
                    favorite = RhythmFavorite(
                        game=game.value,
                        song_title=title,
                        normalized_title=normalized_key,
                        note=note,
                        source=normalized_source,
                        external_id=external_id,
                        active=True,
                    )
                    session.add(favorite)
                else:
                    changed = (
                        favorite.game != game.value
                        or favorite.song_title != title
                        or favorite.normalized_title != normalized_key
                        or favorite.note != note
                        or favorite.external_id != external_id
                        or not favorite.active
                    )
                    favorite.game = game.value
                    favorite.song_title = title
                    favorite.normalized_title = normalized_key
                    favorite.note = note
                    favorite.external_id = external_id
                    favorite.active = True
                    if changed:
                        favorite.updated_at = _utc_now()
                session.flush()
                snapshots.append(self._favorite_record(favorite))
            session.commit()
            return snapshots
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def upsert_maimai_net_favorites(
        self,
        favorites: Iterable[Mapping[str, object] | object],
        *,
        max_items: int = PROVIDER_FAVORITE_MAX_ITEMS,
    ) -> list[RhythmFavoriteRecord]:
        """Convenience alias for the future authenticated maimai NET importer."""

        return self.upsert_provider_favorites(
            "maimai_net",
            favorites,
            max_items=max_items,
        )

    def list_favorites(
        self,
        game: RhythmGame | str | None = None,
        *,
        active_only: bool = True,
    ) -> list[RhythmFavoriteRecord]:
        """Return detached favorites in stable game/title/id order."""

        normalized_game = self._game(game) if game is not None else None
        session = self._new_session()
        try:
            statement = select(RhythmFavorite).order_by(
                RhythmFavorite.game.asc(),
                RhythmFavorite.normalized_title.asc(),
                RhythmFavorite.id.asc(),
            )
            if active_only:
                statement = statement.where(RhythmFavorite.active.is_(True))
            if normalized_game is not None:
                statement = statement.where(RhythmFavorite.game == normalized_game.value)
            favorites = list(session.scalars(statement).all())
            return [self._favorite_record(favorite) for favorite in favorites]
        finally:
            session.close()

    def get_todo_by_event_message_id(
        self, discord_message_id: str | int
    ) -> RhythmTodoResult | None:
        """Resolve a TODO from its source Discord message ID for reply linking."""

        if isinstance(discord_message_id, bool) or not isinstance(discord_message_id, (str, int)):
            raise ValueError("Discord message ID must be a string or integer")
        message_id = str(discord_message_id).strip()
        if not message_id:
            raise ValueError("Discord message ID must not be blank")

        session = self._new_session()
        try:
            todo = session.scalar(
                select(RhythmTodo)
                .join(RhythmTodo.event)
                .where(Event.discord_message_id == message_id)
                .options(joinedload(RhythmTodo.event))
            )
            return self._todo_result(todo) if todo is not None else None
        finally:
            session.close()

    # A name that describes the same lookup in domain terms is useful to
    # adapters that do not expose Discord-specific terminology.
    def get_todo_by_event_discord_message_id(
        self, discord_message_id: str | int
    ) -> RhythmTodoResult | None:
        return self.get_todo_by_event_message_id(discord_message_id)
