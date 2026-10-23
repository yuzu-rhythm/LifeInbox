"""Focused persistence helpers used by the application services."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .constants import DEFAULT_ALIASES, EventStatus
from .models import Alias, Event, Expense, MaimaiMyBestEntry, MaimaiNetFavoriteEntry


class EventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def by_id(self, event_id: int) -> Event | None:
        return self.session.get(Event, event_id)

    def by_discord_message_id(self, message_id: str | int) -> Event | None:
        return self.session.scalar(select(Event).where(Event.discord_message_id == str(message_id)))

    def pending(self) -> list[Event]:
        return list(
            self.session.scalars(
                select(Event).where(Event.status == EventStatus.PENDING).order_by(Event.occurred_at)
            ).all()
        )

    def confirmed(self) -> list[Event]:
        return list(
            self.session.scalars(
                select(Event)
                .where(Event.status == EventStatus.CONFIRMED)
                .order_by(Event.occurred_at)
            ).all()
        )

    def uncopied_confirmed(self) -> list[Event]:
        return list(
            self.session.scalars(
                select(Event)
                .where(
                    Event.status == EventStatus.CONFIRMED,
                    Event.copied_to_planner_at.is_(None),
                )
                .order_by(Event.occurred_at)
            ).all()
        )


class AliasRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def mapping(self, *, entity_type: str = "merchant") -> dict[str, str]:
        rows = self.session.scalars(
            select(Alias).where(Alias.entity_type == entity_type).order_by(Alias.id)
        ).all()
        return {row.alias: row.canonical for row in rows}

    def seed_defaults(self) -> None:
        existing = {
            row.alias
            for row in self.session.scalars(
                select(Alias).where(Alias.entity_type == "merchant")
            ).all()
        }
        for alias, canonical in DEFAULT_ALIASES.items():
            if alias not in existing:
                self.session.add(Alias(alias=alias, canonical=canonical, entity_type="merchant"))
        self.session.flush()

    def upsert(self, alias: str, canonical: str, *, entity_type: str = "merchant") -> Alias:
        alias = alias.strip()
        canonical = canonical.strip()
        if not alias or not canonical:
            raise ValueError("alias and canonical must not be blank")
        row = self.session.scalar(
            select(Alias).where(Alias.alias == alias, Alias.entity_type == entity_type)
        )
        if row is None:
            row = Alias(alias=alias, canonical=canonical, entity_type=entity_type)
            self.session.add(row)
        else:
            row.canonical = canonical
        self.session.flush()
        return row


class MaimaiMyBestRepository:
    """Persistence primitives for the credential-free MY BEST snapshots."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def current(self) -> list[MaimaiMyBestEntry]:
        return list(
            self.session.scalars(
                select(MaimaiMyBestEntry)
                .where(MaimaiMyBestEntry.active.is_(True))
                .order_by(MaimaiMyBestEntry.rank.asc(), MaimaiMyBestEntry.id.asc())
            ).all()
        )

    def by_snapshot_key(self, snapshot_key: str) -> list[MaimaiMyBestEntry]:
        return list(
            self.session.scalars(
                select(MaimaiMyBestEntry)
                .where(MaimaiMyBestEntry.snapshot_key == snapshot_key)
                .order_by(MaimaiMyBestEntry.rank.asc(), MaimaiMyBestEntry.id.asc())
            ).all()
        )

    def deactivate_current(self) -> None:
        self.session.execute(
            update(MaimaiMyBestEntry)
            .where(MaimaiMyBestEntry.active.is_(True))
            .values(active=False)
        )

    def activate_snapshot(self, rows: list[MaimaiMyBestEntry]) -> None:
        for row in rows:
            row.active = True

    def add_snapshot(
        self,
        *,
        snapshot_key: str,
        fetched_at: datetime,
        source: str,
        entries: list[tuple[int, str, int]],
    ) -> list[MaimaiMyBestEntry]:
        rows = [
            MaimaiMyBestEntry(
                snapshot_key=snapshot_key,
                rank=rank,
                title=title,
                play_count=play_count,
                fetched_at=fetched_at,
                active=True,
                source=source,
            )
            for rank, title, play_count in entries
        ]
        self.session.add_all(rows)
        self.session.flush()
        return rows


class MaimaiNetFavoriteRepository:
    """Persistence primitives for official maimai NET favorite snapshots."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def current(self) -> list[MaimaiNetFavoriteEntry]:
        return list(
            self.session.scalars(
                select(MaimaiNetFavoriteEntry)
                .where(MaimaiNetFavoriteEntry.active.is_(True))
                .order_by(MaimaiNetFavoriteEntry.rank.asc(), MaimaiNetFavoriteEntry.id.asc())
            ).all()
        )

    def by_snapshot_key(self, snapshot_key: str) -> list[MaimaiNetFavoriteEntry]:
        return list(
            self.session.scalars(
                select(MaimaiNetFavoriteEntry)
                .where(MaimaiNetFavoriteEntry.snapshot_key == snapshot_key)
                .order_by(MaimaiNetFavoriteEntry.rank.asc(), MaimaiNetFavoriteEntry.id.asc())
            ).all()
        )

    def deactivate_current(self) -> None:
        self.session.execute(
            update(MaimaiNetFavoriteEntry)
            .where(MaimaiNetFavoriteEntry.active.is_(True))
            .values(active=False)
        )

    def activate_snapshot(self, rows: list[MaimaiNetFavoriteEntry]) -> None:
        for row in rows:
            row.active = True

    def add_snapshot(
        self,
        *,
        snapshot_key: str,
        fetched_at: datetime,
        source: str,
        entries: list[tuple[int, str, str, str]],
    ) -> list[MaimaiNetFavoriteEntry]:
        rows = [
            MaimaiNetFavoriteEntry(
                snapshot_key=snapshot_key,
                rank=rank,
                category=category,
                title=title,
                artist=artist,
                fetched_at=fetched_at,
                active=True,
                source=source,
            )
            for rank, category, title, artist in entries
        ]
        self.session.add_all(rows)
        self.session.flush()
        return rows


def confirmed_expense_for_merchant(
    session: Session, merchant_raw: str | None, merchant_normalized: str | None
) -> Expense | None:
    """Find the most recent confirmed category learned from a merchant."""

    values = [value for value in (merchant_raw, merchant_normalized) if value]
    if not values:
        return None
    return session.scalar(
        select(Expense)
        .join(Event, Expense.event_id == Event.id)
        .where(
            Event.status == EventStatus.CONFIRMED,
            or_(Expense.merchant_raw.in_(values), Expense.merchant_normalized.in_(values)),
        )
        .order_by(Event.occurred_at.desc())
    )
