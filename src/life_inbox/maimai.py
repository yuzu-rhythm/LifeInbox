"""Credential-free import and read services for sanitized maimai NET data.

The importer accepts only the small, already-sanitized snapshot envelope
described in the README.  It never logs, stores, or handles account
identifiers, passwords, cookies, sessions, or a source-site client.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .constants import (
    MAIMAI_MYBEST_MAX_ITEMS,
    MAIMAI_MYBEST_SOURCE,
    MAIMAI_NET_FAVORITES_SOURCE,
)
from .models import MaimaiMyBestEntry, MaimaiNetFavoriteEntry
from .repositories import MaimaiMyBestRepository, MaimaiNetFavoriteRepository
from .schemas import (
    MaimaiMyBestEntryInput,
    MaimaiNetFavoriteEntryInput,
    MaimaiSnapshotBundleInput,
)

MAX_IMPORT_FILE_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class MaimaiMyBestRecord:
    """Detached presentation-safe MY BEST row."""

    id: int
    snapshot_key: str
    rank: int
    title: str
    play_count: int
    fetched_at: datetime
    active: bool
    source: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MaimaiNetFavoriteRecord:
    """Detached presentation-safe official NET favorite row."""

    id: int
    snapshot_key: str
    rank: int
    category: str
    title: str
    artist: str
    fetched_at: datetime
    active: bool
    source: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MaimaiImportSummary:
    """Counts and mutation status for one bundle import."""

    favorites_count: int
    my_best_count: int
    favorites_changed: bool
    my_best_changed: bool
    no_op: bool = False

    @property
    def changed(self) -> bool:
        return self.favorites_changed or self.my_best_changed


def _safe_text(value: object, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = unicodedata.normalize("NFKC", value)
    normalized = " ".join(normalized.split())
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} is too long")
    return normalized


def _safe_fetched_at(value: datetime | str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("fetched_at must be an ISO 8601 datetime") from exc
    if not isinstance(value, datetime):
        raise ValueError("fetched_at must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("fetched_at must include a timezone")
    return value.astimezone(UTC).replace(tzinfo=None)


def _safe_source(value: object) -> str:
    if not isinstance(value, str) or value not in {
        MAIMAI_MYBEST_SOURCE,
        MAIMAI_NET_FAVORITES_SOURCE,
    }:
        raise ValueError("unsupported maimai snapshot source")
    return MAIMAI_MYBEST_SOURCE


def _validate_unique_ranks(ranks: Iterable[int]) -> None:
    seen: set[int] = set()
    for rank in ranks:
        if rank in seen:
            raise ValueError("snapshot ranks must be unique")
        seen.add(rank)


def _normalize_my_best_entries(
    entries: Iterable[Mapping[str, object] | object],
) -> list[tuple[int, str, int]]:
    try:
        values = list(entries)
    except TypeError as exc:
        raise ValueError("my_best must be an array") from exc
    if not values:
        raise ValueError("my_best must not be empty for a section import")
    if len(values) > MAIMAI_MYBEST_MAX_ITEMS:
        raise ValueError("my_best has too many entries")

    parsed: list[MaimaiMyBestEntryInput] = []
    for item in values:
        try:
            parsed.append(MaimaiMyBestEntryInput.model_validate(item))
        except (TypeError, ValidationError) as exc:
            raise ValueError("invalid my_best entry") from exc
    _validate_unique_ranks(entry.rank for entry in parsed)
    normalized = [
        (
            entry.rank,
            _safe_text(entry.title, field_name="my_best title", maximum=255),
            entry.play_count,
        )
        for entry in parsed
    ]
    return sorted(normalized, key=lambda entry: entry[0])


def _normalize_favorite_entries(
    entries: Iterable[Mapping[str, object] | object],
) -> list[tuple[int, str, str, str]]:
    try:
        values = list(entries)
    except TypeError as exc:
        raise ValueError("favorites must be an array") from exc
    if not values:
        raise ValueError("favorites must not be empty for a section import")
    if len(values) > MAIMAI_MYBEST_MAX_ITEMS:
        raise ValueError("favorites has too many entries")

    parsed: list[MaimaiNetFavoriteEntryInput] = []
    for item in values:
        try:
            parsed.append(MaimaiNetFavoriteEntryInput.model_validate(item))
        except (TypeError, ValidationError) as exc:
            raise ValueError("invalid official favorite entry") from exc
    _validate_unique_ranks(entry.rank for entry in parsed)
    normalized = [
        (
            entry.rank,
            _safe_text(entry.category, field_name="favorite category", maximum=64),
            _safe_text(entry.title, field_name="favorite title", maximum=255),
            _safe_text(entry.artist, field_name="favorite artist", maximum=255),
        )
        for entry in parsed
    ]
    return sorted(normalized, key=lambda entry: entry[0])


def _snapshot_key(
    *,
    kind: str,
    source: str,
    fetched_at: datetime,
    entries: list[tuple[object, ...]],
) -> str:
    canonical = {
        "kind": kind,
        "source": source,
        "fetched_at": fetched_at.isoformat(),
        "entries": entries,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _my_best_record(row: MaimaiMyBestEntry) -> MaimaiMyBestRecord:
    return MaimaiMyBestRecord(
        id=row.id,
        snapshot_key=row.snapshot_key,
        rank=row.rank,
        title=row.title,
        play_count=row.play_count,
        fetched_at=row.fetched_at,
        active=bool(row.active),
        source=row.source,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _favorite_record(row: MaimaiNetFavoriteEntry) -> MaimaiNetFavoriteRecord:
    return MaimaiNetFavoriteRecord(
        id=row.id,
        snapshot_key=row.snapshot_key,
        rank=row.rank,
        category=row.category,
        title=row.title,
        artist=row.artist,
        fetched_at=row.fetched_at,
        active=bool(row.active),
        source=row.source,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _my_best_rows_match(rows: list[MaimaiMyBestEntry], entries: list[tuple[int, str, int]]) -> bool:
    return [
        (row.rank, row.title, row.play_count)
        for row in rows
    ] == entries


def _favorite_rows_match(
    rows: list[MaimaiNetFavoriteEntry],
    entries: list[tuple[int, str, str, str]],
) -> bool:
    return [
        (row.rank, row.category, row.title, row.artist)
        for row in rows
    ] == entries


def _apply_my_best(
    session: Session,
    *,
    fetched_at: datetime,
    source: str,
    entries: list[tuple[int, str, int]],
) -> tuple[list[MaimaiMyBestRecord], bool]:
    snapshot_key = _snapshot_key(
        kind="my_best",
        source=source,
        fetched_at=fetched_at,
        entries=[tuple(entry) for entry in entries],
    )
    repository = MaimaiMyBestRepository(session)
    current = repository.current()
    existing = repository.by_snapshot_key(snapshot_key)
    if existing and not _my_best_rows_match(existing, entries):
        raise ValueError("MY BEST snapshot identity collision")
    if existing:
        if (
            len(current) == len(existing)
            and all(row.active for row in existing)
            and _my_best_rows_match(current, entries)
        ):
            return [_my_best_record(row) for row in current], False
        repository.deactivate_current()
        repository.activate_snapshot(existing)
        session.flush()
        return [_my_best_record(row) for row in existing], True

    repository.deactivate_current()
    rows = repository.add_snapshot(
        snapshot_key=snapshot_key,
        fetched_at=fetched_at,
        source=source,
        entries=entries,
    )
    return [_my_best_record(row) for row in rows], True


def _apply_favorites(
    session: Session,
    *,
    fetched_at: datetime,
    source: str,
    entries: list[tuple[int, str, str, str]],
) -> tuple[list[MaimaiNetFavoriteRecord], bool]:
    snapshot_key = _snapshot_key(
        kind="favorites",
        source=source,
        fetched_at=fetched_at,
        entries=[tuple(entry) for entry in entries],
    )
    repository = MaimaiNetFavoriteRepository(session)
    current = repository.current()
    existing = repository.by_snapshot_key(snapshot_key)
    if existing and not _favorite_rows_match(existing, entries):
        raise ValueError("official favorite snapshot identity collision")
    if existing:
        if (
            len(current) == len(existing)
            and all(row.active for row in existing)
            and _favorite_rows_match(current, entries)
        ):
            return [_favorite_record(row) for row in current], False
        repository.deactivate_current()
        repository.activate_snapshot(existing)
        session.flush()
        return [_favorite_record(row) for row in existing], True

    repository.deactivate_current()
    rows = repository.add_snapshot(
        snapshot_key=snapshot_key,
        fetched_at=fetched_at,
        source=source,
        entries=entries,
    )
    return [_favorite_record(row) for row in rows], True


class MaimaiMyBestService:
    """Read and import MY BEST rows without any network boundary."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def import_snapshot(
        self,
        entries: Iterable[Mapping[str, object] | object],
        *,
        fetched_at: datetime | str,
        source: str = MAIMAI_MYBEST_SOURCE,
    ) -> list[MaimaiMyBestRecord]:
        normalized_source = _safe_source(source)
        normalized_fetched_at = _safe_fetched_at(fetched_at)
        normalized_entries = _normalize_my_best_entries(entries)
        session = self.session_factory()
        try:
            records, _ = _apply_my_best(
                session,
                fetched_at=normalized_fetched_at,
                source=normalized_source,
                entries=normalized_entries,
            )
            session.commit()
            return records
        except IntegrityError as exc:
            session.rollback()
            raise ValueError("could not import MY BEST snapshot") from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_current(self) -> list[MaimaiMyBestRecord]:
        session = self.session_factory()
        try:
            return [_my_best_record(row) for row in MaimaiMyBestRepository(session).current()]
        finally:
            session.close()

    def list_history(self) -> list[MaimaiMyBestRecord]:
        session = self.session_factory()
        try:
            rows = session.query(MaimaiMyBestEntry).order_by(
                MaimaiMyBestEntry.fetched_at.desc(),
                MaimaiMyBestEntry.rank.asc(),
                MaimaiMyBestEntry.id.asc(),
            )
            return [_my_best_record(row) for row in rows]
        finally:
            session.close()


class MaimaiNetFavoriteService:
    """Read and import official maimai NET favorites independently."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def import_snapshot(
        self,
        entries: Iterable[Mapping[str, object] | object],
        *,
        fetched_at: datetime | str,
        source: str = MAIMAI_NET_FAVORITES_SOURCE,
    ) -> list[MaimaiNetFavoriteRecord]:
        normalized_source = _safe_source(source)
        normalized_fetched_at = _safe_fetched_at(fetched_at)
        normalized_entries = _normalize_favorite_entries(entries)
        session = self.session_factory()
        try:
            records, _ = _apply_favorites(
                session,
                fetched_at=normalized_fetched_at,
                source=normalized_source,
                entries=normalized_entries,
            )
            session.commit()
            return records
        except IntegrityError as exc:
            session.rollback()
            raise ValueError("could not import official favorite snapshot") from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_current(self) -> list[MaimaiNetFavoriteRecord]:
        session = self.session_factory()
        try:
            return [_favorite_record(row) for row in MaimaiNetFavoriteRepository(session).current()]
        finally:
            session.close()

    def list_history(self) -> list[MaimaiNetFavoriteRecord]:
        session = self.session_factory()
        try:
            rows = session.query(MaimaiNetFavoriteEntry).order_by(
                MaimaiNetFavoriteEntry.fetched_at.desc(),
                MaimaiNetFavoriteEntry.rank.asc(),
                MaimaiNetFavoriteEntry.id.asc(),
            )
            return [_favorite_record(row) for row in rows]
        finally:
            session.close()


class MaimaiSnapshotImportService:
    """Atomically import the two sections of one sanitized JSON file."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    @staticmethod
    def _bundle(payload: Mapping[str, object] | object) -> MaimaiSnapshotBundleInput:
        try:
            bundle = MaimaiSnapshotBundleInput.model_validate(payload)
        except (TypeError, ValidationError) as exc:
            raise ValueError("invalid maimai snapshot JSON") from exc
        _safe_source(bundle.source)
        _safe_fetched_at(bundle.fetched_at)
        return bundle

    def import_bundle(self, payload: Mapping[str, object] | object) -> MaimaiImportSummary:
        bundle = self._bundle(payload)
        fetched_at = _safe_fetched_at(bundle.fetched_at)
        source = _safe_source(bundle.source)
        favorite_entries = (
            _normalize_favorite_entries(bundle.favorites) if bundle.favorites else []
        )
        my_best_entries = _normalize_my_best_entries(bundle.my_best) if bundle.my_best else []
        if not favorite_entries and not my_best_entries:
            return MaimaiImportSummary(0, 0, False, False, no_op=True)

        session = self.session_factory()
        try:
            favorite_records: list[MaimaiNetFavoriteRecord] = []
            my_best_records: list[MaimaiMyBestRecord] = []
            favorites_changed = False
            my_best_changed = False
            if favorite_entries:
                favorite_records, favorites_changed = _apply_favorites(
                    session,
                    fetched_at=fetched_at,
                    source=source,
                    entries=favorite_entries,
                )
            if my_best_entries:
                my_best_records, my_best_changed = _apply_my_best(
                    session,
                    fetched_at=fetched_at,
                    source=source,
                    entries=my_best_entries,
                )
            session.commit()
            return MaimaiImportSummary(
                favorites_count=len(favorite_records),
                my_best_count=len(my_best_records),
                favorites_changed=favorites_changed,
                my_best_changed=my_best_changed,
            )
        except IntegrityError as exc:
            session.rollback()
            raise ValueError("could not import maimai snapshot") from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def import_json_file(self, path: str | Path) -> MaimaiImportSummary:
        candidate = Path(path)
        try:
            if not candidate.is_file() or candidate.stat().st_size > MAX_IMPORT_FILE_BYTES:
                raise ValueError("snapshot file is missing or too large")
            with candidate.open("r", encoding="utf-8") as handle:
                payload: Any = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("snapshot file is not valid UTF-8 JSON") from exc
        return self.import_bundle(payload)


# Short aliases make the two read models discoverable to callers that use
# "official" rather than "NET" in their terminology.
MaimaiOfficialFavoriteService = MaimaiNetFavoriteService
MaimaiOfficialFavoriteRecord = MaimaiNetFavoriteRecord
MyBestService = MaimaiMyBestService
