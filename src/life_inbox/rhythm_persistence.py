"""Transactional persistence for rhythm collection, spending sync, and leases."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from .rhythm_collectors.common import safe_message, sanitize_for_output
from .rhythm_images import verify_rating_image
from .rhythm_models import (
    CollectionLeaseRow,
    CollectionRunEventRow,
    CollectionRunRow,
    DailyGameSpendingRow,
    DiscordDeliveryRow,
    RatingEntryRow,
    RatingImageRow,
    RatingSnapshotImageRow,
    RatingSnapshotRow,
    RhythmProviderAccountRow,
    RhythmUserRow,
)
from .rhythm_rating import ALLOWED_TRANSITIONS, CollectionState

RHYTHM_GAMES = frozenset({"chunithm", "maimai", "ongeki"})
TERMINAL_RUN_STATES = frozenset({"posted", "unchanged", "failed", "cancelled"})
INTERRUPTIBLE_RUN_STATES = frozenset(
    {
        "openingProvider",
        "authenticationRequired",
        "refreshingSource",
        "collectingData",
        "renderingImage",
        "comparingSnapshot",
        "readyToPost",
        "posting",
    }
)


def utc_naive(value: datetime | None = None) -> datetime:
    """Normalize an aware datetime to the project's UTC-naive DB convention."""

    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None:
        return resolved
    return resolved.astimezone(UTC).replace(tzinfo=None)


_ALLOWED_RUN_TRANSITIONS = {
    source.value: frozenset(target.value for target in targets)
    for source, targets in ALLOWED_TRANSITIONS.items()
}


def _safe_text(value: str | None, *, limit: int = 1_000) -> str | None:
    """Return a bounded, redacted value suitable for DB/API/Discord boundaries."""

    cleaned = safe_message(value)
    return cleaned[:limit] if cleaned else None


class SpendingRevisionError(ValueError):
    """Base error for a rejected iPhone spending revision."""


class StaleSpendingRevisionError(SpendingRevisionError):
    pass


class SpendingRevisionConflictError(SpendingRevisionError):
    pass


class CollectionRunConflictError(ValueError):
    pass


class DeliveryClaimConflictError(ValueError):
    """A stale Discord worker tried to finalize a claim it no longer owns."""


@dataclass(frozen=True, slots=True)
class SpendingRecord:
    id: int
    user_id: int
    local_date: date
    time_zone: str
    chunithm_amount_yen: int
    maimai_amount_yen: int
    ongeki_amount_yen: int
    total_amount_yen: int
    revision: int
    synced_at: datetime
    deleted: bool


@dataclass(frozen=True, slots=True)
class SpendingSyncResult:
    record: SpendingRecord
    created: bool
    idempotent: bool


@dataclass(frozen=True, slots=True)
class CollectionRunRecord:
    id: str
    user_id: int
    game: str
    trigger: str
    state: str
    progress_message: str
    manual_action: str | None
    manual_confirmed_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    retry_count: int
    snapshot_id: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    delivery_id: int
    claimed: bool
    duplicate: bool
    in_progress: bool
    existing_message_id: str | None
    attempt_count: int
    uncertain: bool = False


@dataclass(frozen=True, slots=True)
class ProviderStatusRecord:
    game: str
    provider: str
    session_status: str
    last_authenticated_at: datetime | None
    last_error_code: str | None
    last_error: str | None
    enabled: bool


@dataclass(frozen=True, slots=True)
class DeliveryStatusRecord:
    game: str
    last_posted_at: datetime | None
    pending_or_failed_count: int


def _spending_record(row: DailyGameSpendingRow) -> SpendingRecord:
    return SpendingRecord(
        id=row.id,
        user_id=row.user_id,
        local_date=row.local_date,
        time_zone=row.time_zone,
        chunithm_amount_yen=row.chunithm_amount_yen,
        maimai_amount_yen=row.maimai_amount_yen,
        ongeki_amount_yen=row.ongeki_amount_yen,
        total_amount_yen=row.total_amount_yen,
        revision=row.revision,
        synced_at=row.synced_at,
        deleted=row.deleted_at is not None,
    )


def _run_record(row: CollectionRunRow) -> CollectionRunRecord:
    return CollectionRunRecord(
        id=row.id,
        user_id=row.user_id,
        game=row.game,
        trigger=row.trigger,
        state=row.state,
        progress_message=row.progress_message,
        manual_action=row.manual_action,
        manual_confirmed_at=row.manual_confirmed_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        last_error_code=row.last_error_code,
        last_error_message=row.last_error_message,
        retry_count=row.retry_count,
        snapshot_id=row.snapshot_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class RhythmPersistence:
    """Small explicit unit-of-work API shared by Bot, Web API, and scheduler."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def ensure_user(
        self,
        *,
        display_name: str,
        time_zone: str,
        discord_user_id: str | int | None,
    ) -> int:
        normalized_discord_id = str(discord_user_id) if discord_user_id is not None else None
        session = self.session_factory()
        try:
            row = None
            if normalized_discord_id is not None:
                row = session.scalar(
                    select(RhythmUserRow).where(
                        RhythmUserRow.discord_user_id == normalized_discord_id
                    )
                )
            if row is None and normalized_discord_id is None:
                row = session.scalar(select(RhythmUserRow).order_by(RhythmUserRow.id).limit(1))
            if row is None:
                row = RhythmUserRow(
                    display_name=display_name.strip(),
                    time_zone=time_zone,
                    discord_user_id=normalized_discord_id,
                )
                session.add(row)
            else:
                row.display_name = display_name.strip()
                row.time_zone = time_zone
                row.enabled = True
            session.commit()
            return row.id
        except IntegrityError:
            session.rollback()
            if normalized_discord_id is None:
                raise
            # A concurrent API/Bot request may have inserted the unique user.
            retry = self.session_factory()
            try:
                row = retry.scalar(
                    select(RhythmUserRow).where(
                        RhythmUserRow.discord_user_id == normalized_discord_id
                    )
                )
                if row is None:
                    raise RuntimeError(
                        "concurrent rhythm user insert could not be resolved"
                    )
                return row.id
            finally:
                retry.close()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def update_provider_status(
        self,
        *,
        user_id: int,
        game: str,
        provider: str,
        session_status: str,
        external_user_id: str | None = None,
        credentials_reference: str | None = None,
        authenticated_at: datetime | None = None,
        error_code: str | None = None,
        safe_error: str | None = None,
        enabled: bool = True,
    ) -> None:
        if game not in RHYTHM_GAMES:
            raise ValueError("invalid rhythm game")
        session = self.session_factory()
        try:
            row = session.scalar(
                select(RhythmProviderAccountRow).where(
                    RhythmProviderAccountRow.user_id == user_id,
                    RhythmProviderAccountRow.game == game,
                    RhythmProviderAccountRow.provider == provider,
                )
            )
            if row is None:
                row = RhythmProviderAccountRow(
                    user_id=user_id,
                    game=game,
                    provider=provider,
                )
                session.add(row)
            row.session_status = (_safe_text(session_status, limit=64) or "unknown")
            row.external_user_id = external_user_id
            row.encrypted_credentials_reference = credentials_reference
            row.last_authenticated_at = (
                utc_naive(authenticated_at) if authenticated_at is not None else None
            )
            row.last_error_code = _safe_text(error_code, limit=64)
            row.last_error = _safe_text(safe_error)
            row.enabled = enabled
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def provider_statuses(self, *, user_id: int) -> dict[str, ProviderStatusRecord]:
        session = self.session_factory()
        try:
            rows = session.scalars(
                select(RhythmProviderAccountRow)
                .where(RhythmProviderAccountRow.user_id == user_id)
                .order_by(RhythmProviderAccountRow.updated_at.desc())
            ).all()
            statuses: dict[str, ProviderStatusRecord] = {}
            for row in rows:
                statuses.setdefault(
                    row.game,
                    ProviderStatusRecord(
                        game=row.game,
                        provider=row.provider,
                        session_status=row.session_status,
                        last_authenticated_at=row.last_authenticated_at,
                        last_error_code=row.last_error_code,
                        last_error=row.last_error,
                        enabled=row.enabled,
                    ),
                )
            return statuses
        finally:
            session.close()

    def sync_spending(
        self,
        *,
        user_id: int,
        local_date: date,
        time_zone: str,
        chunithm_amount_yen: int,
        maimai_amount_yen: int,
        ongeki_amount_yen: int,
        total_amount_yen: int,
        revision: int,
        synced_at: datetime | None = None,
        _retry_count: int = 0,
    ) -> SpendingSyncResult:
        amounts = (chunithm_amount_yen, maimai_amount_yen, ongeki_amount_yen)
        if any(isinstance(amount, bool) or amount < 0 for amount in amounts):
            raise ValueError("rhythm spending amounts must be non-negative yen integers")
        if total_amount_yen != sum(amounts):
            raise ValueError("totalAmountYen must equal the three game amounts")
        if isinstance(revision, bool) or revision < 0:
            raise ValueError("revision must be a non-negative integer")

        normalized_synced_at = utc_naive(synced_at)
        session = self.session_factory()
        try:
            row = session.scalar(
                select(DailyGameSpendingRow).where(
                    DailyGameSpendingRow.user_id == user_id,
                    DailyGameSpendingRow.local_date == local_date,
                )
            )
            created = row is None
            if row is None:
                row = DailyGameSpendingRow(
                    user_id=user_id,
                    local_date=local_date,
                    time_zone=time_zone,
                    chunithm_amount_yen=chunithm_amount_yen,
                    maimai_amount_yen=maimai_amount_yen,
                    ongeki_amount_yen=ongeki_amount_yen,
                    total_amount_yen=total_amount_yen,
                    revision=revision,
                    synced_at=normalized_synced_at,
                )
                session.add(row)
                session.commit()
                return SpendingSyncResult(_spending_record(row), True, False)

            incoming = (
                time_zone,
                chunithm_amount_yen,
                maimai_amount_yen,
                ongeki_amount_yen,
                total_amount_yen,
                False,
            )
            existing = (
                row.time_zone,
                row.chunithm_amount_yen,
                row.maimai_amount_yen,
                row.ongeki_amount_yen,
                row.total_amount_yen,
                row.deleted_at is not None,
            )
            if revision < row.revision:
                raise StaleSpendingRevisionError("an older spending revision was rejected")
            if revision == row.revision:
                if incoming != existing:
                    raise SpendingRevisionConflictError(
                        "the same revision has different spending content"
                    )
                return SpendingSyncResult(_spending_record(row), False, True)

            expected_revision = row.revision
            result = session.execute(
                update(DailyGameSpendingRow)
                .where(
                    DailyGameSpendingRow.id == row.id,
                    DailyGameSpendingRow.revision == expected_revision,
                )
                .values(
                    time_zone=time_zone,
                    chunithm_amount_yen=chunithm_amount_yen,
                    maimai_amount_yen=maimai_amount_yen,
                    ongeki_amount_yen=ongeki_amount_yen,
                    total_amount_yen=total_amount_yen,
                    revision=revision,
                    synced_at=normalized_synced_at,
                    deleted_at=None,
                    updated_at=normalized_synced_at,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                if _retry_count >= 4:
                    raise SpendingRevisionConflictError(
                        "spending changed concurrently; retry with the same revision"
                    )
                return self.sync_spending(
                    user_id=user_id,
                    local_date=local_date,
                    time_zone=time_zone,
                    chunithm_amount_yen=chunithm_amount_yen,
                    maimai_amount_yen=maimai_amount_yen,
                    ongeki_amount_yen=ongeki_amount_yen,
                    total_amount_yen=total_amount_yen,
                    revision=revision,
                    synced_at=normalized_synced_at,
                    _retry_count=_retry_count + 1,
                )
            session.commit()
            session.expire_all()
            saved = session.get(DailyGameSpendingRow, row.id)
            if saved is None:
                raise RuntimeError("spending row disappeared after update")
            return SpendingSyncResult(_spending_record(saved), created, False)
        except IntegrityError:
            session.rollback()
            if _retry_count >= 4:
                raise
            return self.sync_spending(
                user_id=user_id,
                local_date=local_date,
                time_zone=time_zone,
                chunithm_amount_yen=chunithm_amount_yen,
                maimai_amount_yen=maimai_amount_yen,
                ongeki_amount_yen=ongeki_amount_yen,
                total_amount_yen=total_amount_yen,
                revision=revision,
                synced_at=normalized_synced_at,
                _retry_count=_retry_count + 1,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_spending(
        self,
        *,
        user_id: int,
        local_date: date,
        time_zone: str,
        revision: int,
        synced_at: datetime | None = None,
        _retry_count: int = 0,
    ) -> SpendingSyncResult:
        normalized_synced_at = utc_naive(synced_at)
        session = self.session_factory()
        try:
            row = session.scalar(
                select(DailyGameSpendingRow).where(
                    DailyGameSpendingRow.user_id == user_id,
                    DailyGameSpendingRow.local_date == local_date,
                )
            )
            if row is None:
                row = DailyGameSpendingRow(
                    user_id=user_id,
                    local_date=local_date,
                    time_zone=time_zone,
                    chunithm_amount_yen=0,
                    maimai_amount_yen=0,
                    ongeki_amount_yen=0,
                    total_amount_yen=0,
                    revision=revision,
                    synced_at=normalized_synced_at,
                    deleted_at=normalized_synced_at,
                )
                session.add(row)
                session.commit()
                return SpendingSyncResult(_spending_record(row), True, False)
            if revision < row.revision:
                raise StaleSpendingRevisionError("an older deletion revision was rejected")
            if revision == row.revision:
                if row.deleted_at is None:
                    raise SpendingRevisionConflictError(
                        "the same revision already contains non-deleted spending"
                    )
                return SpendingSyncResult(_spending_record(row), False, True)
            expected_revision = row.revision
            result = session.execute(
                update(DailyGameSpendingRow)
                .where(
                    DailyGameSpendingRow.id == row.id,
                    DailyGameSpendingRow.revision == expected_revision,
                )
                .values(
                    time_zone=time_zone,
                    chunithm_amount_yen=0,
                    maimai_amount_yen=0,
                    ongeki_amount_yen=0,
                    total_amount_yen=0,
                    revision=revision,
                    synced_at=normalized_synced_at,
                    deleted_at=normalized_synced_at,
                    updated_at=normalized_synced_at,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                if _retry_count >= 4:
                    raise SpendingRevisionConflictError(
                        "spending changed concurrently; retry deletion with the same revision"
                    )
                return self.delete_spending(
                    user_id=user_id,
                    local_date=local_date,
                    time_zone=time_zone,
                    revision=revision,
                    synced_at=normalized_synced_at,
                    _retry_count=_retry_count + 1,
                )
            session.commit()
            session.expire_all()
            saved = session.get(DailyGameSpendingRow, row.id)
            if saved is None:
                raise RuntimeError("spending row disappeared after deletion")
            return SpendingSyncResult(_spending_record(saved), False, False)
        except IntegrityError:
            session.rollback()
            if _retry_count >= 4:
                raise
            return self.delete_spending(
                user_id=user_id,
                local_date=local_date,
                time_zone=time_zone,
                revision=revision,
                synced_at=normalized_synced_at,
                _retry_count=_retry_count + 1,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_spending(self, *, user_id: int, local_date: date) -> SpendingRecord | None:
        session = self.session_factory()
        try:
            row = session.scalar(
                select(DailyGameSpendingRow).where(
                    DailyGameSpendingRow.user_id == user_id,
                    DailyGameSpendingRow.local_date == local_date,
                )
            )
            return _spending_record(row) if row is not None else None
        finally:
            session.close()

    def has_posted_spending_delivery(
        self,
        *,
        user_id: int,
        local_date: date,
        channel_id: str | int,
        delivery_kind: str = "spending",
    ) -> bool:
        """Return whether the Bot already owns a posted message for this day."""

        session = self.session_factory()
        try:
            return (
                session.scalar(
                    select(DiscordDeliveryRow.id).where(
                        DiscordDeliveryRow.user_id == user_id,
                        DiscordDeliveryRow.local_date == local_date,
                        DiscordDeliveryRow.delivery_kind == delivery_kind,
                        DiscordDeliveryRow.game_key == "",
                        DiscordDeliveryRow.channel_id == str(channel_id),
                        DiscordDeliveryRow.status == "posted",
                        DiscordDeliveryRow.discord_message_id.is_not(None),
                    )
                )
                is not None
            )
        finally:
            session.close()

    def latest_rating_delivery_state(
        self,
        *,
        user_id: int,
        game: str,
        semantic_fingerprint: str,
    ) -> str | None:
        """Return the newest external-delivery state for identical semantics.

        This is intentionally independent of ``local_date``. If a process
        saved a Snapshot and rendering succeeded but Discord rejected the
        send, the next identical collection must be able to retry rather than
        being hidden by semantic deduplication. Conversely, ``unknown`` and
        active ``posting`` outcomes must remain manual-reconciliation gates.
        """

        if game not in RHYTHM_GAMES:
            raise ValueError("invalid rhythm game")
        session = self.session_factory()
        try:
            return session.scalar(
                select(DiscordDeliveryRow.status)
                .where(
                    DiscordDeliveryRow.user_id == user_id,
                    DiscordDeliveryRow.delivery_kind == "rating",
                    DiscordDeliveryRow.game_key == game,
                    DiscordDeliveryRow.semantic_fingerprint == semantic_fingerprint,
                )
                .order_by(
                    DiscordDeliveryRow.local_date.desc(),
                    DiscordDeliveryRow.updated_at.desc(),
                    DiscordDeliveryRow.id.desc(),
                )
                .limit(1)
            )
        finally:
            session.close()

    def spending_records_with_posted_deliveries(
        self,
        *,
        user_id: int,
        channel_id: str | int,
        limit: int = 50,
    ) -> list[SpendingRecord]:
        """Return records eligible to reconcile an existing Bot message.

        This deliberately excludes failed, posting, and unknown deliveries.
        The publisher already performs bounded retries for one attempt, while
        unknown outcomes require administrator reconciliation to avoid an
        accidental duplicate external side effect.
        """

        if limit < 1 or limit > 366:
            raise ValueError("invalid spending reconciliation limit")
        session = self.session_factory()
        try:
            rows = session.scalars(
                select(DailyGameSpendingRow)
                .join(
                    DiscordDeliveryRow,
                    (DiscordDeliveryRow.user_id == DailyGameSpendingRow.user_id)
                    & (DiscordDeliveryRow.local_date == DailyGameSpendingRow.local_date)
                    & (DiscordDeliveryRow.delivery_kind == "spending")
                    & (DiscordDeliveryRow.game_key == "")
                    & (DiscordDeliveryRow.channel_id == str(channel_id))
                    & (DiscordDeliveryRow.status == "posted")
                    & (DiscordDeliveryRow.discord_message_id.is_not(None)),
                )
                .where(DailyGameSpendingRow.user_id == user_id)
                .order_by(DailyGameSpendingRow.updated_at, DailyGameSpendingRow.local_date)
                .limit(limit)
            ).all()
            return [_spending_record(row) for row in rows]
        finally:
            session.close()

    def queue_collection_runs(
        self,
        *,
        user_id: int,
        games: list[str] | tuple[str, ...],
        trigger: str,
        request_key: str | None = None,
        now: datetime | None = None,
        _allow_integrity_retry: bool = True,
    ) -> list[CollectionRunRecord]:
        unique_games = list(dict.fromkeys(games))
        if not unique_games or any(game not in RHYTHM_GAMES for game in unique_games):
            raise ValueError("games must contain only chunithm, maimai, or ongeki")
        created_at = utc_naive(now)
        request_key_hash = (
            hashlib.sha256(f"{trigger}:{request_key}".encode()).hexdigest()
            if request_key is not None
            else None
        )
        session = self.session_factory()
        try:
            if request_key_hash is not None:
                existing_group = session.scalars(
                    select(CollectionRunRow).where(
                        CollectionRunRow.user_id == user_id,
                        CollectionRunRow.request_key_hash == request_key_hash,
                    )
                ).all()
                if existing_group:
                    existing_games = {row.game for row in existing_group}
                    if existing_games != set(unique_games):
                        raise CollectionRunConflictError(
                            "the idempotency key was already used for a different game set"
                        )
                    by_game = {row.game: row for row in existing_group}
                    return [_run_record(by_game[game]) for game in unique_games]
            rows: list[CollectionRunRow] = []
            for game in unique_games:
                existing = None
                if request_key_hash is not None:
                    existing = session.scalar(
                        select(CollectionRunRow).where(
                            CollectionRunRow.user_id == user_id,
                            CollectionRunRow.game == game,
                            CollectionRunRow.request_key_hash == request_key_hash,
                        )
                    )
                if existing is not None:
                    rows.append(existing)
                    continue
                row = CollectionRunRow(
                    id=str(uuid4()),
                    user_id=user_id,
                    game=game,
                    trigger=trigger,
                    request_key_hash=request_key_hash,
                    state="queued",
                    progress_message="取得待ちです",
                    created_at=created_at,
                    updated_at=created_at,
                )
                row.events.append(
                    CollectionRunEventRow(
                        from_state=None,
                        to_state="queued",
                        event_type="queued",
                        message="collection run queued",
                        created_at=created_at,
                    )
                )
                session.add(row)
                rows.append(row)
            session.commit()
            return [_run_record(row) for row in rows]
        except IntegrityError:
            session.rollback()
            if request_key is None or not _allow_integrity_retry:
                raise
            return self.queue_collection_runs(
                user_id=user_id,
                games=unique_games,
                trigger=trigger,
                request_key=request_key,
                now=created_at,
                _allow_integrity_retry=False,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_run(self, run_id: str) -> CollectionRunRecord | None:
        session = self.session_factory()
        try:
            row = session.get(CollectionRunRow, run_id)
            return _run_record(row) if row is not None else None
        finally:
            session.close()

    def latest_runs(self, *, user_id: int) -> dict[str, CollectionRunRecord]:
        session = self.session_factory()
        try:
            rows = session.scalars(
                select(CollectionRunRow)
                .where(CollectionRunRow.user_id == user_id)
                .order_by(CollectionRunRow.created_at.desc())
            ).all()
            latest: dict[str, CollectionRunRecord] = {}
            for row in rows:
                latest.setdefault(row.game, _run_record(row))
            return latest
        finally:
            session.close()

    def latest_failed_runs(self, *, user_id: int) -> dict[str, CollectionRunRecord]:
        session = self.session_factory()
        try:
            rows = session.scalars(
                select(CollectionRunRow)
                .where(
                    CollectionRunRow.user_id == user_id,
                    CollectionRunRow.state == "failed",
                )
                .order_by(CollectionRunRow.finished_at.desc(), CollectionRunRow.id.desc())
            ).all()
            latest: dict[str, CollectionRunRecord] = {}
            for row in rows:
                latest.setdefault(row.game, _run_record(row))
            return latest
        finally:
            session.close()

    def delivery_statuses(self, *, user_id: int) -> dict[str, DeliveryStatusRecord]:
        session = self.session_factory()
        try:
            rows = session.scalars(
                select(DiscordDeliveryRow).where(
                    DiscordDeliveryRow.user_id == user_id,
                    DiscordDeliveryRow.delivery_kind == "rating",
                )
            ).all()
            values: dict[str, DeliveryStatusRecord] = {}
            for game in RHYTHM_GAMES:
                matching = [row for row in rows if row.game_key == game]
                posted_values = [
                    row.posted_at
                    for row in matching
                    if row.status == "posted" and row.posted_at is not None
                ]
                values[game] = DeliveryStatusRecord(
                    game=game,
                    last_posted_at=max(posted_values, default=None),
                    pending_or_failed_count=sum(
                        row.status in {"pending", "posting", "failed", "unknown"}
                        for row in matching
                    ),
                )
            return values
        finally:
            session.close()

    def processable_runs(self, *, limit: int = 10) -> list[CollectionRunRecord]:
        if limit < 1 or limit > 100:
            raise ValueError("invalid processable run limit")
        session = self.session_factory()
        try:
            rows = session.scalars(
                select(CollectionRunRow)
                .where(
                    (CollectionRunRow.state == "queued")
                    | (
                        (CollectionRunRow.state == "waitingForManualAction")
                        & (CollectionRunRow.manual_confirmed_at.is_not(None))
                        & (CollectionRunRow.manual_action.is_(None))
                    )
                )
                .order_by(CollectionRunRow.created_at, CollectionRunRow.id)
                .limit(limit)
            ).all()
            return [_run_record(row) for row in rows]
        finally:
            session.close()

    def transition_run(
        self,
        *,
        run_id: str,
        to_state: str,
        event_type: str,
        message: str,
        allowed_from: set[str] | frozenset[str] | None = None,
        manual_action: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        snapshot_id: int | None = None,
        explicit_confirmation: bool = False,
        now: datetime | None = None,
    ) -> CollectionRunRecord:
        changed_at = utc_naive(now)
        session = self.session_factory()
        try:
            row = session.get(CollectionRunRow, run_id)
            if row is None:
                raise ValueError("collection run not found")
            if to_state not in _ALLOWED_RUN_TRANSITIONS:
                raise CollectionRunConflictError("unsupported collection run state")
            if row.state == to_state and row.state in TERMINAL_RUN_STATES:
                return _run_record(row)
            if row.state in TERMINAL_RUN_STATES:
                raise CollectionRunConflictError("terminal collection run cannot transition")
            if to_state not in _ALLOWED_RUN_TRANSITIONS.get(row.state, frozenset()):
                raise CollectionRunConflictError(
                    f"collection run cannot transition from {row.state} to {to_state}"
                )
            if allowed_from is not None and row.state not in allowed_from:
                raise CollectionRunConflictError(
                    f"collection run cannot transition from {row.state}"
                )
            if (
                row.state == CollectionState.WAITING_FOR_MANUAL_ACTION.value
                and to_state == CollectionState.REFRESHING_SOURCE.value
                and (
                    not explicit_confirmation
                    or row.manual_confirmed_at is None
                    or row.manual_action is not None
                )
            ):
                raise CollectionRunConflictError(
                    "explicit administrator confirmation is required before refresh"
                )
            resolved_snapshot_id = snapshot_id if snapshot_id is not None else row.snapshot_id
            if to_state in {
                CollectionState.POSTED.value,
                CollectionState.UNCHANGED.value,
            } and resolved_snapshot_id is None:
                raise CollectionRunConflictError(
                    "a verified snapshot is required before a successful terminal state"
                )
            if to_state in {
                CollectionState.POSTED.value,
                CollectionState.UNCHANGED.value,
            }:
                verified_snapshot_id = session.scalar(
                    select(RatingSnapshotRow.id).where(
                        RatingSnapshotRow.id == resolved_snapshot_id,
                        RatingSnapshotRow.user_id == row.user_id,
                        RatingSnapshotRow.game == row.game,
                        RatingSnapshotRow.image_links.any(),
                    )
                )
                if verified_snapshot_id is None:
                    raise CollectionRunConflictError(
                        "successful terminal state requires this run's verified image snapshot"
                    )
            if to_state == CollectionState.WAITING_FOR_MANUAL_ACTION.value:
                if not manual_action:
                    raise CollectionRunConflictError(
                        "waitingForManualAction requires an explicit manual action"
                    )
            elif manual_action is not None:
                raise CollectionRunConflictError(
                    "manual actions are valid only in waitingForManualAction"
                )
            from_state = row.state
            row.state = to_state
            row.progress_message = _safe_text(message) or "状態を更新しました"
            row.manual_action = _safe_text(manual_action, limit=64)
            row.last_error_code = _safe_text(error_code, limit=64)
            row.last_error_message = _safe_text(error_message)
            if snapshot_id is not None:
                row.snapshot_id = snapshot_id
            if row.started_at is None and to_state != "queued":
                row.started_at = changed_at
            if to_state in TERMINAL_RUN_STATES:
                row.finished_at = changed_at
            row.events.append(
                CollectionRunEventRow(
                    from_state=from_state,
                    to_state=to_state,
                    event_type=event_type,
                    message=row.progress_message,
                    error_code=row.last_error_code,
                    explicit_confirmation=explicit_confirmation,
                    created_at=changed_at,
                )
            )
            session.commit()
            return _run_record(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def confirm_manual_action(
        self,
        *,
        run_id: str,
        now: datetime | None = None,
    ) -> CollectionRunRecord:
        """Record a human event without pretending collection succeeded."""

        confirmed_at = utc_naive(now)
        session = self.session_factory()
        try:
            row = session.get(CollectionRunRow, run_id)
            if row is None:
                raise ValueError("collection run not found")
            if row.state != "waitingForManualAction" or row.manual_action is None:
                if (
                    row.state == "waitingForManualAction"
                    and row.manual_action is None
                    and row.manual_confirmed_at is not None
                ):
                    return _run_record(row)
                raise CollectionRunConflictError("collection run is not awaiting manual action")
            action = row.manual_action
            row.manual_confirmed_at = confirmed_at
            row.manual_action = None
            row.progress_message = "管理者の確認を受け付けました。取得処理の再開待ちです"
            row.events.append(
                CollectionRunEventRow(
                    from_state=row.state,
                    to_state=row.state,
                    event_type=f"manualConfirmed:{action}",
                    message="explicit administrator confirmation recorded",
                    explicit_confirmation=True,
                    created_at=confirmed_at,
                )
            )
            session.commit()
            return _run_record(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def recover_interrupted_runs(self, *, now: datetime | None = None) -> list[str]:
        """Fail in-flight work after restart; queued/manual runs remain resumable."""

        recovered_at = utc_naive(now)
        session = self.session_factory()
        try:
            candidates = session.scalars(
                select(CollectionRunRow).where(CollectionRunRow.state.in_(INTERRUPTIBLE_RUN_STATES))
            ).all()
            recovered_ids: list[str] = []
            for row in candidates:
                from_state = row.state
                active_lease = exists(
                    select(CollectionLeaseRow.id).where(
                        CollectionLeaseRow.user_id == CollectionRunRow.user_id,
                        CollectionLeaseRow.game == CollectionRunRow.game,
                        CollectionLeaseRow.expires_at > recovered_at,
                    )
                )
                result = session.execute(
                    update(CollectionRunRow)
                    .where(
                        CollectionRunRow.id == row.id,
                        CollectionRunRow.state == from_state,
                        ~active_lease,
                    )
                    .values(
                        state="failed",
                        finished_at=recovered_at,
                        last_error_code="process_restarted",
                        last_error_message="Bot再起動により中断されました。再試行できます",
                        progress_message="Bot再起動により中断されました。再試行できます",
                        updated_at=recovered_at,
                    )
                )
                if result.rowcount != 1:
                    continue
                session.add(
                    CollectionRunEventRow(
                        run_id=row.id,
                        from_state=from_state,
                        to_state="failed",
                        event_type="processRestarted",
                        message="Bot再起動により中断されました。再試行できます",
                        error_code="process_restarted",
                        created_at=recovered_at,
                    )
                )
                recovered_ids.append(row.id)
            session.commit()
            return recovered_ids
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def acquire_lease(
        self,
        *,
        user_id: int,
        game: str,
        owner_id: str,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> bool:
        if game not in RHYTHM_GAMES:
            raise ValueError("invalid rhythm game")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        acquired_at = utc_naive(now)
        expires_at = acquired_at + timedelta(seconds=lease_seconds)
        session = self.session_factory()
        try:
            result = session.execute(
                update(CollectionLeaseRow)
                .where(
                    CollectionLeaseRow.user_id == user_id,
                    CollectionLeaseRow.game == game,
                    or_(
                        CollectionLeaseRow.owner_id == owner_id,
                        CollectionLeaseRow.expires_at <= acquired_at,
                    ),
                )
                .values(
                    owner_id=owner_id,
                    acquired_at=acquired_at,
                    expires_at=expires_at,
                    updated_at=acquired_at,
                )
            )
            if result.rowcount == 1:
                session.commit()
                return True
            session.add(
                CollectionLeaseRow(
                    user_id=user_id,
                    game=game,
                    owner_id=owner_id,
                    acquired_at=acquired_at,
                    expires_at=expires_at,
                )
            )
            session.commit()
            return True
        except IntegrityError:
            session.rollback()
            return False
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def release_lease(self, *, user_id: int, game: str, owner_id: str) -> bool:
        session = self.session_factory()
        try:
            result = session.execute(
                delete(CollectionLeaseRow).where(
                    CollectionLeaseRow.user_id == user_id,
                    CollectionLeaseRow.game == game,
                    CollectionLeaseRow.owner_id == owner_id,
                )
            )
            session.commit()
            return result.rowcount == 1
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def renew_lease(
        self,
        *,
        user_id: int,
        game: str,
        owner_id: str,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> bool:
        """Extend only the caller's still-active lease; never steal ownership."""

        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        renewed_at = utc_naive(now)
        session = self.session_factory()
        try:
            result = session.execute(
                update(CollectionLeaseRow)
                .where(
                    CollectionLeaseRow.user_id == user_id,
                    CollectionLeaseRow.game == game,
                    CollectionLeaseRow.owner_id == owner_id,
                    CollectionLeaseRow.expires_at > renewed_at,
                )
                .values(
                    expires_at=renewed_at + timedelta(seconds=lease_seconds),
                    updated_at=renewed_at,
                )
            )
            session.commit()
            return result.rowcount == 1
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def lease_owned(
        self,
        *,
        user_id: int,
        game: str,
        owner_id: str,
        now: datetime | None = None,
    ) -> bool:
        checked_at = utc_naive(now)
        session = self.session_factory()
        try:
            return (
                session.scalar(
                    select(CollectionLeaseRow.id).where(
                        CollectionLeaseRow.user_id == user_id,
                        CollectionLeaseRow.game == game,
                        CollectionLeaseRow.owner_id == owner_id,
                        CollectionLeaseRow.expires_at > checked_at,
                    )
                )
                is not None
            )
        finally:
            session.close()

    def claim_delivery(
        self,
        *,
        user_id: int,
        local_date: date,
        delivery_kind: str,
        game: str | None,
        channel_id: str | int,
        semantic_fingerprint: str | None,
        image_sha256: str | None,
        now: datetime | None = None,
        claim_seconds: int = 300,
        force: bool = False,
        allow_presentation_refresh: bool = False,
        _retry_count: int = 0,
    ) -> DeliveryClaim:
        """Atomically claim a daily delivery or report why it should be skipped."""

        changed_at = utc_naive(now)
        game_key = game or ""
        session = self.session_factory()
        try:
            row = session.scalar(
                select(DiscordDeliveryRow).where(
                    DiscordDeliveryRow.user_id == user_id,
                    DiscordDeliveryRow.local_date == local_date,
                    DiscordDeliveryRow.delivery_kind == delivery_kind,
                    DiscordDeliveryRow.game_key == game_key,
                    DiscordDeliveryRow.channel_id == str(channel_id),
                )
            )
            if row is None:
                row = DiscordDeliveryRow(
                    user_id=user_id,
                    local_date=local_date,
                    delivery_kind=delivery_kind,
                    game_key=game_key,
                    channel_id=str(channel_id),
                    semantic_fingerprint=semantic_fingerprint,
                    image_sha256=image_sha256,
                    status="posting",
                    attempt_count=1,
                    updated_at=changed_at,
                )
                session.add(row)
                session.commit()
                return DeliveryClaim(row.id, True, False, False, None, 1)

            same_semantics = row.semantic_fingerprint == semantic_fingerprint
            same_image = (
                image_sha256 is not None
                and row.image_sha256 is not None
                and row.image_sha256 == image_sha256
            )
            presentation_refresh = (
                allow_presentation_refresh
                and delivery_kind == "rating"
                and row.status == "posted"
                and row.discord_message_id is not None
                and same_semantics
                and image_sha256 is not None
                and row.image_sha256 is not None
                and not same_image
            )
            duplicate = not force and row.status == "posted" and (
                same_image or (same_semantics and not presentation_refresh)
            )
            if duplicate:
                return DeliveryClaim(
                    row.id,
                    False,
                    True,
                    False,
                    row.discord_message_id,
                    row.attempt_count,
                )
            claim_cutoff = changed_at - timedelta(seconds=claim_seconds)
            if row.status == "posting" and row.updated_at > claim_cutoff:
                return DeliveryClaim(
                    row.id,
                    False,
                    False,
                    True,
                    row.discord_message_id,
                    row.attempt_count,
                )
            if row.status == "posting" and not force:
                # The worker may have sent the Discord message and crashed
                # before recording its ID. Automatic resend could duplicate an
                # external side effect, so require explicit reconciliation.
                result = session.execute(
                    update(DiscordDeliveryRow)
                    .where(
                        DiscordDeliveryRow.id == row.id,
                        DiscordDeliveryRow.status == "posting",
                        DiscordDeliveryRow.attempt_count == row.attempt_count,
                        DiscordDeliveryRow.updated_at <= claim_cutoff,
                    )
                    .values(
                        status="unknown",
                        last_error_code="delivery_outcome_unknown",
                        last_error=(
                            "前回送信の結果を確認できません。"
                            "管理者がDiscordを確認して再投稿してください"
                        ),
                        updated_at=changed_at,
                    )
                )
                if result.rowcount != 1:
                    session.rollback()
                    if _retry_count >= 4:
                        return DeliveryClaim(
                            row.id,
                            False,
                            False,
                            True,
                            row.discord_message_id,
                            row.attempt_count,
                        )
                    session.close()
                    return self.claim_delivery(
                        user_id=user_id,
                        local_date=local_date,
                        delivery_kind=delivery_kind,
                        game=game,
                        channel_id=channel_id,
                        semantic_fingerprint=semantic_fingerprint,
                        image_sha256=image_sha256,
                        now=changed_at,
                        claim_seconds=claim_seconds,
                        force=force,
                        allow_presentation_refresh=allow_presentation_refresh,
                        _retry_count=_retry_count + 1,
                    )
                session.commit()
                return DeliveryClaim(
                    row.id,
                    False,
                    False,
                    False,
                    row.discord_message_id,
                    row.attempt_count,
                    True,
                )
            if row.status == "unknown" and not force:
                return DeliveryClaim(
                    row.id,
                    False,
                    False,
                    False,
                    row.discord_message_id,
                    row.attempt_count,
                    True,
                )
            existing_message_id = row.discord_message_id
            expected_attempt_count = row.attempt_count
            next_attempt_count = expected_attempt_count + 1
            expected_status = row.status
            result = session.execute(
                update(DiscordDeliveryRow)
                .where(
                    DiscordDeliveryRow.id == row.id,
                    DiscordDeliveryRow.status == expected_status,
                    DiscordDeliveryRow.attempt_count == expected_attempt_count,
                )
                .values(
                    status="posting",
                    attempt_count=next_attempt_count,
                    semantic_fingerprint=semantic_fingerprint,
                    image_sha256=image_sha256,
                    last_error_code=None,
                    last_error=None,
                    updated_at=changed_at,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                if _retry_count >= 4:
                    return DeliveryClaim(
                        row.id,
                        False,
                        False,
                        True,
                        existing_message_id,
                        expected_attempt_count,
                    )
                session.close()
                return self.claim_delivery(
                    user_id=user_id,
                    local_date=local_date,
                    delivery_kind=delivery_kind,
                    game=game,
                    channel_id=channel_id,
                    semantic_fingerprint=semantic_fingerprint,
                    image_sha256=image_sha256,
                    now=changed_at,
                    claim_seconds=claim_seconds,
                    force=force,
                    allow_presentation_refresh=allow_presentation_refresh,
                    _retry_count=_retry_count + 1,
                )
            session.commit()
            return DeliveryClaim(
                row.id,
                True,
                False,
                False,
                existing_message_id,
                next_attempt_count,
            )
        except IntegrityError:
            session.rollback()
            if _retry_count >= 4:
                raise DeliveryClaimConflictError("Discord delivery claim contention")
            # Another process won the unique insert.  A second attempt sees
            # its active claim and returns in_progress/duplicate safely.
            session.close()
            return self.claim_delivery(
                user_id=user_id,
                local_date=local_date,
                delivery_kind=delivery_kind,
                game=game,
                channel_id=channel_id,
                semantic_fingerprint=semantic_fingerprint,
                image_sha256=image_sha256,
                now=changed_at,
                claim_seconds=claim_seconds,
                force=force,
                allow_presentation_refresh=allow_presentation_refresh,
                _retry_count=_retry_count + 1,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mark_delivery_succeeded(
        self,
        *,
        delivery_id: int,
        claim_attempt_count: int,
        discord_message_id: str | int,
        posted_at: datetime | None = None,
    ) -> None:
        session = self.session_factory()
        try:
            succeeded_at = utc_naive(posted_at)
            result = session.execute(
                update(DiscordDeliveryRow)
                .where(
                    DiscordDeliveryRow.id == delivery_id,
                    DiscordDeliveryRow.status == "posting",
                    DiscordDeliveryRow.attempt_count == claim_attempt_count,
                )
                .values(
                    discord_message_id=str(discord_message_id),
                    status="posted",
                    posted_at=succeeded_at,
                    last_error_code=None,
                    last_error=None,
                    updated_at=succeeded_at,
                )
            )
            if result.rowcount != 1:
                raise DeliveryClaimConflictError("stale Discord delivery claim")
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mark_delivery_failed(
        self,
        *,
        delivery_id: int,
        claim_attempt_count: int,
        error_code: str,
        safe_message: str,
        now: datetime | None = None,
    ) -> None:
        session = self.session_factory()
        try:
            changed_at = utc_naive(now)
            result = session.execute(
                update(DiscordDeliveryRow)
                .where(
                    DiscordDeliveryRow.id == delivery_id,
                    DiscordDeliveryRow.status == "posting",
                    DiscordDeliveryRow.attempt_count == claim_attempt_count,
                )
                .values(
                    status="failed",
                    last_error_code=_safe_text(error_code, limit=64),
                    last_error=_safe_text(safe_message),
                    updated_at=changed_at,
                )
            )
            if result.rowcount != 1:
                raise DeliveryClaimConflictError("stale Discord delivery claim")
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mark_delivery_unknown(
        self,
        *,
        delivery_id: int,
        claim_attempt_count: int,
        error_code: str,
        safe_message: str,
        now: datetime | None = None,
    ) -> None:
        """Record an ambiguous external outcome without automatically resending."""

        session = self.session_factory()
        try:
            changed_at = utc_naive(now)
            result = session.execute(
                update(DiscordDeliveryRow)
                .where(
                    DiscordDeliveryRow.id == delivery_id,
                    DiscordDeliveryRow.status == "posting",
                    DiscordDeliveryRow.attempt_count == claim_attempt_count,
                )
                .values(
                    status="unknown",
                    last_error_code=_safe_text(error_code, limit=64),
                    last_error=_safe_text(safe_message),
                    updated_at=changed_at,
                )
            )
            if result.rowcount != 1:
                raise DeliveryClaimConflictError("stale Discord delivery claim")
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def latest_snapshot(self, *, user_id: int, game: str) -> RatingSnapshotRow | None:
        session = self.session_factory()
        try:
            row = session.scalar(
                select(RatingSnapshotRow)
                .options(
                    selectinload(RatingSnapshotRow.entries),
                    selectinload(RatingSnapshotRow.image_links).selectinload(
                        RatingSnapshotImageRow.image
                    ),
                )
                .where(
                    RatingSnapshotRow.user_id == user_id,
                    RatingSnapshotRow.game == game,
                )
                .order_by(RatingSnapshotRow.collected_at.desc(), RatingSnapshotRow.id.desc())
                .limit(1)
            )
            if row is not None:
                session.expunge(row)
            return row
        finally:
            session.close()

    def latest_rendered_snapshot(
        self,
        *,
        user_id: int,
        game: str,
    ) -> RatingSnapshotRow | None:
        """Return the newest snapshot with a persisted, verified image link.

        A Collector may safely persist structured data before rendering. Such
        observations remain in history, but they must not be advertised to the
        iPhone client as a fully successful latest rating.
        """

        session = self.session_factory()
        try:
            row = session.scalar(
                select(RatingSnapshotRow)
                .options(
                    selectinload(RatingSnapshotRow.entries),
                    selectinload(RatingSnapshotRow.image_links).selectinload(
                        RatingSnapshotImageRow.image
                    ),
                )
                .where(
                    RatingSnapshotRow.user_id == user_id,
                    RatingSnapshotRow.game == game,
                    RatingSnapshotRow.image_links.any(),
                )
                .order_by(RatingSnapshotRow.collected_at.desc(), RatingSnapshotRow.id.desc())
                .limit(1)
            )
            if row is not None:
                session.expunge(row)
            return row
        finally:
            session.close()

    def latest_successful_snapshot(
        self,
        *,
        user_id: int,
        game: str,
    ) -> RatingSnapshotRow | None:
        """Return the last snapshot with a verified image link.

        This is intentionally an alias with a success-oriented name for
        collectors.  A newer observation may have persisted structured data
        and then failed during rendering or publication; callers comparing a
        new result must use this durable success boundary so that such a
        failed observation cannot replace the previous successful baseline.
        """

        return self.latest_rendered_snapshot(user_id=user_id, game=game)

    def get_snapshot(self, *, user_id: int, snapshot_id: int) -> RatingSnapshotRow | None:
        session = self.session_factory()
        try:
            row = session.scalar(
                select(RatingSnapshotRow)
                .options(
                    selectinload(RatingSnapshotRow.entries),
                    selectinload(RatingSnapshotRow.image_links).selectinload(
                        RatingSnapshotImageRow.image
                    ),
                )
                .where(
                    RatingSnapshotRow.user_id == user_id,
                    RatingSnapshotRow.id == snapshot_id,
                )
            )
            if row is not None:
                session.expunge(row)
            return row
        finally:
            session.close()

    def list_snapshots(
        self,
        *,
        user_id: int,
        game: str | None = None,
        collected_from: datetime | None = None,
        collected_to: datetime | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[RatingSnapshotRow], int]:
        if game is not None and game not in RHYTHM_GAMES:
            raise ValueError("invalid rhythm game")
        if offset < 0 or limit < 1 or limit > 100:
            raise ValueError("invalid pagination")
        session = self.session_factory()
        try:
            filters = [RatingSnapshotRow.user_id == user_id]
            if game is not None:
                filters.append(RatingSnapshotRow.game == game)
            if collected_from is not None:
                filters.append(RatingSnapshotRow.collected_at >= utc_naive(collected_from))
            if collected_to is not None:
                filters.append(RatingSnapshotRow.collected_at < utc_naive(collected_to))
            total = session.scalar(
                select(func.count()).select_from(RatingSnapshotRow).where(*filters)
            )
            rows = session.scalars(
                select(RatingSnapshotRow)
                .options(
                    selectinload(RatingSnapshotRow.image_links).selectinload(
                        RatingSnapshotImageRow.image
                    )
                )
                .where(*filters)
                .order_by(RatingSnapshotRow.collected_at.desc(), RatingSnapshotRow.id.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            for row in rows:
                session.expunge(row)
            return list(rows), int(total or 0)
        finally:
            session.close()

    def save_snapshot(
        self,
        *,
        user_id: int,
        game: str,
        player_name: str,
        rating: Decimal,
        previous_rating: Decimal | None,
        semantic_fingerprint: str,
        source_updated_at: datetime | None,
        collected_at: datetime,
        raw_data: dict[str, Any],
        entries: list[dict[str, Any]],
    ) -> int:
        if game not in RHYTHM_GAMES:
            raise ValueError("invalid rhythm game")
        normalized_fingerprint = (
            semantic_fingerprint.strip().casefold()
            if isinstance(semantic_fingerprint, str)
            else ""
        )
        if len(normalized_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_fingerprint
        ):
            raise ValueError("semantic fingerprint must be SHA-256 hex")
        semantic_fingerprint = normalized_fingerprint
        sanitized_raw = sanitize_for_output(raw_data)
        if not isinstance(sanitized_raw, dict):
            sanitized_raw = {"value": sanitized_raw}
        sanitized_entries: list[dict[str, Any]] = []
        for raw_entry in entries:
            if not isinstance(raw_entry, dict):
                raise ValueError("rating entries must be mappings")
            raw_source_updated_at = raw_entry.get("source_updated_at")
            if raw_source_updated_at is not None and not isinstance(
                raw_source_updated_at, datetime
            ):
                raise ValueError("rating entry source_updated_at must be a datetime")
            sanitized_entry = sanitize_for_output(raw_entry)
            if not isinstance(sanitized_entry, dict):
                raise ValueError("rating entries must be mappings")
            # ``sanitize_for_output`` intentionally serializes unknown object
            # types to strings so raw provider data is JSON-safe.  This field,
            # however, crosses into a typed SQLAlchemy DateTime column and must
            # retain its datetime type.
            sanitized_entry["source_updated_at"] = (
                utc_naive(raw_source_updated_at)
                if raw_source_updated_at is not None
                else None
            )
            sanitized_entries.append(sanitized_entry)
        rating_delta = rating - previous_rating if previous_rating is not None else None
        session = self.session_factory()
        try:
            row = RatingSnapshotRow(
                user_id=user_id,
                game=game,
                player_name=_safe_text(player_name, limit=100) or "Player",
                rating=rating,
                previous_rating=previous_rating,
                rating_delta=rating_delta,
                semantic_fingerprint=semantic_fingerprint,
                source_updated_at=utc_naive(source_updated_at)
                if source_updated_at is not None
                else None,
                collected_at=utc_naive(collected_at),
                raw_data=sanitized_raw,
            )
            for entry in sanitized_entries:
                row.entries.append(RatingEntryRow(**entry))
            session.add(row)
            session.commit()
            return row.id
        except IntegrityError:
            session.rollback()
            existing = session.scalar(
                select(RatingSnapshotRow.id).where(
                    RatingSnapshotRow.user_id == user_id,
                    RatingSnapshotRow.game == game,
                    RatingSnapshotRow.semantic_fingerprint == semantic_fingerprint,
                    RatingSnapshotRow.collected_at == utc_naive(collected_at),
                )
            )
            if existing is not None:
                return int(existing)
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def attach_image(
        self,
        *,
        snapshot_id: int,
        game: str,
        image_path: str,
        image_sha256: str,
        width: int,
        height: int,
        byte_size: int,
        generated_at: datetime,
        renderer_version: str,
        image_root: str,
        require_complete_png: bool = False,
    ) -> tuple[int, bool]:
        """Link a snapshot to one content-addressed image, reusing identical bytes."""

        if (
            not isinstance(image_sha256, str)
            or len(image_sha256.strip()) != 64
            or any(
                character not in "0123456789abcdefABCDEF"
                for character in image_sha256.strip()
            )
        ):
            raise ValueError("rating image SHA-256 metadata is invalid")
        image_sha256 = image_sha256.strip().casefold()
        verified_path = verify_rating_image(
            image_root=image_root,
            image_path=image_path,
            expected_sha256=image_sha256,
            expected_byte_size=byte_size,
            expected_width=width,
            expected_height=height,
            require_complete_png=require_complete_png,
        )

        session = self.session_factory()
        try:
            snapshot = session.get(RatingSnapshotRow, snapshot_id)
            if snapshot is None:
                raise ValueError("rating snapshot not found")
            if snapshot.game != game:
                raise ValueError("rating image game does not match snapshot")
            if type(width) is not int or width < 1 or type(height) is not int or height < 1:
                raise ValueError("rating image dimensions must be positive integers")
            if type(byte_size) is not int or byte_size < 1:
                raise ValueError("rating image byte size must be a positive integer")
            image = session.scalar(
                select(RatingImageRow).where(RatingImageRow.image_sha256 == image_sha256)
            )
            if image is not None and image.game != game:
                raise ValueError("rating image SHA is already owned by another game")
            if image is not None and (
                image.width != width
                or image.height != height
                or image.byte_size != byte_size
            ):
                raise ValueError("rating image metadata does not match the existing SHA")
            created = image is None
            if image is None:
                image = RatingImageRow(
                    game=game,
                    image_path=str(verified_path),
                    image_sha256=image_sha256,
                    width=width,
                    height=height,
                    byte_size=byte_size,
                    generated_at=utc_naive(generated_at),
                    renderer_version=renderer_version,
                )
                session.add(image)
                session.flush()
            existing_link = session.scalar(
                select(RatingSnapshotImageRow).where(
                    RatingSnapshotImageRow.snapshot_id == snapshot_id
                )
            )
            if existing_link is None:
                session.add(RatingSnapshotImageRow(snapshot_id=snapshot_id, image_id=image.id))
            elif existing_link.image_id != image.id:
                raise ValueError("snapshot already has a different rating image")
            session.commit()
            return image.id, created
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
