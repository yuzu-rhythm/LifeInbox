"""Personal-finance persistence and balance calculations.

This module deliberately contains no Discord or AI integration.  It treats an
Event as the source of truth for an expense amount and keeps all finance
operations local to the supplied SQLAlchemy session factory.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from .constants import AccountKind, ActivityKind, EventKind, EventStatus
from .models import (
    Event,
    FinancialAccount,
    FinancialAccountSettlementLink,
    FinancialAccountSnapshot,
    LedgerActivity,
    Memo,
)

DEFAULT_ACCOUNTS: tuple[tuple[str, AccountKind], ...] = (
    ("現金", AccountKind.CASH),
    ("普通口座", AccountKind.BANK),
    ("クレジットカード", AccountKind.CARD),
)

# Account names are the existing schema's canonical lookup identity.  Do not
# change that identity just to make a notification safe: callers still need
# to be able to link a ledger event by its configured name.  Instead, every
# presentation boundary can use ``mask_account_identity``.  The pattern also
# joins common space/hyphen-separated card and bank number formats and accepts
# full-width digits used in Japanese input.
_ACCOUNT_NUMBER_PATTERN = re.compile(
    r"(?<![0-9０-９])(?:[0-9０-９][ \t-]*){3,}[0-9０-９](?![0-9０-９])"
)

ASSET_ACCOUNT_KINDS = frozenset(
    {
        AccountKind.CASH,
        AccountKind.BANK,
        AccountKind.E_MONEY,
        AccountKind.INVESTMENT,
    }
)
LIABILITY_ACCOUNT_KINDS = frozenset({AccountKind.CARD})

_ACCOUNT_KIND_LABELS: dict[AccountKind, str] = {
    AccountKind.CASH: "現金",
    AccountKind.BANK: "銀行口座",
    AccountKind.CARD: "クレジットカード",
    AccountKind.E_MONEY: "電子マネー",
    AccountKind.INVESTMENT: "投資口座",
}


def mask_account_identity(value: object) -> str:
    """Return a display-safe account identity without full number exposure.

    The database's account name remains the canonical, user-configured lookup
    key.  This helper is intentionally for display only.  Four or more digit
    sequences are rendered as bullets, retaining only the last four digits
    when there are more than four.  Short labels such as ``口座2`` remain
    readable while full card/account numbers never reach a notification.
    """

    if not isinstance(value, str):
        return "口座"
    cleaned = " ".join(value.replace("\x00", " ").split())

    def replace_number(match: re.Match[str]) -> str:
        digits = "".join(
            chr(ord("0") + ord(character) - ord("０"))
            if "０" <= character <= "９"
            else character
            for character in match.group(0)
            if character.isdigit()
        )
        if len(digits) < 4:
            return match.group(0)
        suffix = f" {digits[-4:]}" if len(digits) > 4 else ""
        return f"••••{suffix}"

    return _ACCOUNT_NUMBER_PATTERN.sub(replace_number, cleaned) or "口座"


# These aliases make the display-only contract discoverable without forcing
# integrations to know whether they call the value an identity or a name.
mask_account_name = mask_account_identity
masked_account_identity = mask_account_identity


def account_kind_label(value: AccountKind | str) -> str:
    """Return a stable Japanese label for an explicit account kind."""

    try:
        kind = (
            value
            if isinstance(value, AccountKind)
            else AccountKind(value.strip().lower())
            if isinstance(value, str)
            else AccountKind(value)
        )
    except (TypeError, ValueError):
        return "口座"
    return _ACCOUNT_KIND_LABELS[kind]


account_type_label = account_kind_label


def account_kind_role(value: AccountKind | str) -> str | None:
    """Return the balance role used by the daily asset/liability digest."""

    try:
        kind = (
            value
            if isinstance(value, AccountKind)
            else AccountKind(value.strip().lower())
            if isinstance(value, str)
            else AccountKind(value)
        )
    except (TypeError, ValueError):
        return None
    if kind in ASSET_ACCOUNT_KINDS:
        return "資産"
    if kind in LIABILITY_ACCOUNT_KINDS:
        return "負債"
    return None


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class AccountBalance:
    """A detached account snapshot with its current displayed balance."""

    id: int
    name: str
    kind: AccountKind
    opening_balance: int
    currency: str
    active: bool
    balance: int
    created_at: datetime
    updated_at: datetime

    @property
    def computed_balance(self) -> int:
        return self.balance

    @property
    def displayed_balance(self) -> int:
        return self.balance

    @property
    def account_type(self) -> AccountKind:
        """Explicit account-type alias for presentation/integration callers."""

        return self.kind

    @property
    def display_name(self) -> str:
        return mask_account_identity(self.name)

    @property
    def display_identity(self) -> str:
        return self.display_name

    @property
    def masked_identity(self) -> str:
        return self.display_name


@dataclass(frozen=True, slots=True)
class LedgerActivityResult:
    """A detached ledger activity snapshot."""

    id: int
    event_id: int | None
    kind: ActivityKind
    amount: int | None
    source_account_id: int | None
    source_account_name: str | None
    destination_account_id: int | None
    destination_account_name: str | None
    occurred_at: datetime
    description: str | None
    event_status: EventStatus | None
    created_at: datetime

    @property
    def account_name(self) -> str | None:
        if self.kind == ActivityKind.EXPENSE:
            return self.source_account_name
        if self.kind == ActivityKind.INCOME:
            return self.destination_account_name
        return None

    @property
    def status(self) -> EventStatus | None:
        return self.event_status


@dataclass(frozen=True, slots=True)
class MemoResult:
    """A detached memo snapshot."""

    id: int
    event_id: int
    body: str
    event_status: EventStatus
    created_at: datetime
    updated_at: datetime

    @property
    def status(self) -> EventStatus:
        return self.event_status


class AccountSnapshotMeasure(StrEnum):
    """The absolute value represented by a daily account snapshot."""

    BALANCE = "balance"
    OUTSTANDING = "outstanding"


@dataclass(frozen=True, slots=True)
class AccountDailySnapshot:
    """A detached, persisted absolute value for one account and local date."""

    id: int
    account_id: int
    account_name: str
    kind: AccountKind
    local_date: date
    value: int
    measure: AccountSnapshotMeasure
    currency: str
    source: str
    observed_at: datetime
    revision: int
    fingerprint: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def balance(self) -> int:
        """Compatibility alias for callers that use balance terminology."""

        return self.value

    @property
    def account_type(self) -> AccountKind:
        return self.kind

    @property
    def display_name(self) -> str:
        return mask_account_identity(self.account_name)

    @property
    def display_identity(self) -> str:
        return self.display_name

    @property
    def masked_identity(self) -> str:
        return self.display_name


@dataclass(frozen=True, slots=True)
class AccountSnapshotComparison:
    """One account's current value and both prior-day comparison views.

    ``previous_*`` is retained as the previous available observation for
    compatibility with history/report callers.  ``previous_day_*`` is the
    exact immediately preceding local calendar day used by notifications.
    """

    account_id: int
    account_name: str
    kind: AccountKind
    currency: str
    local_date: date
    value: int | None
    measure: AccountSnapshotMeasure
    source: str | None
    observed_at: datetime | None
    revision: int | None
    fingerprint: str | None
    previous_local_date: date | None
    previous_value: int | None
    delta: int | None
    settlement_account_id: int | None
    settlement_account_name: str | None
    # The legacy fields above remain the previous available observation.
    # These optional fields identify the exact immediately preceding local
    # calendar day used by the daily digest.
    previous_day_local_date: date | None = None
    previous_day_value: int | None = None
    previous_day_delta: int | None = None

    @property
    def available(self) -> bool:
        return self.value is not None

    @property
    def previous_available_local_date(self) -> date | None:
        return self.previous_local_date

    @property
    def previous_available_value(self) -> int | None:
        return self.previous_value

    @property
    def previous_available_delta(self) -> int | None:
        return self.delta

    @property
    def previous_day_available(self) -> bool:
        return self.previous_day_local_date is not None

    @property
    def account_type(self) -> AccountKind:
        return self.kind

    @property
    def display_name(self) -> str:
        return mask_account_identity(self.account_name)

    @property
    def display_identity(self) -> str:
        return self.display_name

    @property
    def masked_identity(self) -> str:
        return self.display_name


@dataclass(frozen=True, slots=True)
class SettlementAccountLink:
    """A detached card-to-settlement-account relationship."""

    card_account_id: int
    card_account_name: str
    settlement_account_id: int
    settlement_account_name: str

    @property
    def card_display_name(self) -> str:
        return mask_account_identity(self.card_account_name)

    @property
    def settlement_display_name(self) -> str:
        return mask_account_identity(self.settlement_account_name)


# Descriptive aliases keep the public API discoverable for callers that prefer
# the model names while retaining short names for application code.
FinancialAccountResult = AccountBalance
ActivityResult = LedgerActivityResult


class FinanceService:
    """Local-first finance operations over an existing Event-centered schema."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def _new_session(self) -> Session:
        return self.session_factory()

    @staticmethod
    def _account_name(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("account name must be a string")
        name = value.strip()
        if not name:
            raise ValueError("account name must not be blank")
        if len(name) > 255:
            raise ValueError("account name is too long")
        return name

    @staticmethod
    def _account_kind(value: AccountKind | str) -> AccountKind:
        if isinstance(value, AccountKind):
            return value
        if not isinstance(value, str):
            raise ValueError("invalid account kind")
        try:
            return AccountKind(value.strip().lower())
        except ValueError as exc:
            raise ValueError("invalid account kind") from exc

    @staticmethod
    def _positive_amount(value: int) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError("amount must be a positive integer")
        return value

    @staticmethod
    def _opening_balance(value: int) -> int:
        if type(value) is not int:
            raise ValueError("opening_balance must be an integer")
        return value

    @staticmethod
    def _description(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("description must be a string")
        return value.strip() or None

    @staticmethod
    def _event(session: Session, event_id: int) -> Event:
        event = session.scalar(
            select(Event)
            .where(Event.id == event_id)
            .options(
                selectinload(Event.expenses),
                selectinload(Event.ledger_activity),
                selectinload(Event.memo),
            )
        )
        if event is None:
            raise ValueError("event not found")
        return event

    @staticmethod
    def _account(session: Session, name: str) -> FinancialAccount:
        account = session.scalar(select(FinancialAccount).where(FinancialAccount.name == name))
        if account is None:
            raise ValueError(f"account not found: {name}")
        return account

    @staticmethod
    def _optional_description(value: str | None) -> str | None:
        return FinanceService._description(value)

    @staticmethod
    def _snapshot_date(
        value: date | datetime,
        timezone: ZoneInfo | tzinfo | str,
    ) -> date:
        if isinstance(timezone, str):
            try:
                timezone = ZoneInfo(timezone.strip())
            except ZoneInfoNotFoundError as exc:
                raise ValueError("timezone must be a valid IANA timezone") from exc
        if not isinstance(timezone, tzinfo):
            raise ValueError("timezone must be a valid timezone")
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                return value.astimezone(timezone).date()
            return value.date()
        if isinstance(value, date):
            return value
        raise ValueError("local_date must be a date")

    @staticmethod
    def _snapshot_day_end(
        local_date: date,
        timezone: ZoneInfo | tzinfo | str,
    ) -> datetime:
        if isinstance(timezone, str):
            try:
                timezone = ZoneInfo(timezone.strip())
            except ZoneInfoNotFoundError as exc:
                raise ValueError("timezone must be a valid IANA timezone") from exc
        if not isinstance(timezone, tzinfo):
            raise ValueError("timezone must be a valid timezone")
        end_local = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=timezone)
        return end_local.astimezone(UTC).replace(tzinfo=None)

    @staticmethod
    def _snapshot_source(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("snapshot source must be a string")
        source = value.strip()
        if not source or len(source) > 64:
            raise ValueError("snapshot source must be 1-64 characters")
        return source

    @staticmethod
    def _snapshot_value(value: int) -> int:
        if type(value) is not int:
            raise ValueError("snapshot value must be an integer")
        return value

    @staticmethod
    def _snapshot_measure(
        account: FinancialAccount,
        value: AccountSnapshotMeasure | str | None,
    ) -> AccountSnapshotMeasure:
        expected = (
            AccountSnapshotMeasure.OUTSTANDING
            if account.kind == AccountKind.CARD.value
            else AccountSnapshotMeasure.BALANCE
        )
        if value is None:
            return expected
        try:
            measure = (
                value
                if isinstance(value, AccountSnapshotMeasure)
                else AccountSnapshotMeasure(value)
            )
        except ValueError as exc:
            raise ValueError("snapshot measure must be balance or outstanding") from exc
        if measure != expected:
            raise ValueError(f"{account.kind} account requires measure {expected.value}")
        return measure

    @staticmethod
    def _snapshot_currency(account: FinancialAccount, value: str | None) -> str:
        currency = account.currency if value is None else value
        if not isinstance(currency, str):
            raise ValueError("snapshot currency must be a string")
        currency = currency.strip().upper()
        if len(currency) != 3:
            raise ValueError("snapshot currency must be a three-letter code")
        if currency != account.currency.strip().upper():
            raise ValueError("snapshot currency must match the account currency")
        return currency

    @staticmethod
    def _snapshot_fingerprint(
        *,
        account_id: int,
        local_date: date,
        value: int,
        measure: AccountSnapshotMeasure,
        currency: str,
        source: str,
    ) -> str:
        payload = json.dumps(
            {
                "account_id": account_id,
                "local_date": local_date.isoformat(),
                "value": value,
                "measure": measure.value,
                "currency": currency,
                "source": source,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _snapshot_result(row: FinancialAccountSnapshot) -> AccountDailySnapshot:
        return AccountDailySnapshot(
            id=row.id,
            account_id=row.account_id,
            account_name=row.account.name,
            kind=AccountKind(row.account.kind),
            local_date=row.local_date,
            value=row.value,
            measure=AccountSnapshotMeasure(row.measure),
            currency=row.currency,
            source=row.source,
            observed_at=row.observed_at,
            revision=row.revision,
            fingerprint=row.fingerprint,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _settlement_result(row: FinancialAccountSettlementLink) -> SettlementAccountLink:
        return SettlementAccountLink(
            card_account_id=row.card_account_id,
            card_account_name=row.card_account.name,
            settlement_account_id=row.settlement_account_id,
            settlement_account_name=row.settlement_account.name,
        )

    @staticmethod
    def _mark_event(event: Event, kind: EventKind) -> None:
        event.status = EventStatus.CONFIRMED.value
        event.event_type = kind.value
        event.event_kinds = [kind.value]
        event.updated_at = _utc_now()

    @staticmethod
    def _require_unlinked_activity(event: Event) -> None:
        if event.ledger_activity is not None:
            raise ValueError("event already has a ledger activity")

    @staticmethod
    def _activity_matches(
        activity: LedgerActivity,
        *,
        kind: ActivityKind,
        source_account_id: int | None,
        destination_account_id: int | None,
        amount: int | None,
        description: str | None,
    ) -> bool:
        stored_amount = activity._amount
        return (
            activity.kind == kind.value
            and activity.source_account_id == source_account_id
            and activity.destination_account_id == destination_account_id
            and (kind == ActivityKind.EXPENSE or stored_amount == amount)
            and activity.description == description
        )

    @staticmethod
    def _activity_result(activity: LedgerActivity) -> LedgerActivityResult:
        event_status = None
        if activity.event is not None:
            event_status = EventStatus(activity.event.status)
        return LedgerActivityResult(
            id=activity.id,
            event_id=activity.event_id,
            kind=ActivityKind(activity.kind),
            amount=activity.effective_amount,
            source_account_id=activity.source_account_id,
            source_account_name=(
                activity.source_account.name if activity.source_account is not None else None
            ),
            destination_account_id=activity.destination_account_id,
            destination_account_name=(
                activity.destination_account.name
                if activity.destination_account is not None
                else None
            ),
            occurred_at=activity.occurred_at,
            description=activity.description,
            event_status=event_status,
            created_at=activity.created_at,
        )

    @staticmethod
    def _memo_result(memo: Memo) -> MemoResult:
        return MemoResult(
            id=memo.id,
            event_id=memo.event_id,
            body=memo.body,
            event_status=EventStatus(memo.event.status),
            created_at=memo.created_at,
            updated_at=memo.updated_at,
        )

    @staticmethod
    def _is_card(account: FinancialAccount) -> bool:
        return account.kind == AccountKind.CARD.value

    @classmethod
    def _debit(
        cls, balances: dict[int, int], account: FinancialAccount, amount: int
    ) -> None:
        # Assets lose available balance; card debits increase displayed debt.
        balances[account.id] += amount if cls._is_card(account) else -amount

    @classmethod
    def _credit(
        cls, balances: dict[int, int], account: FinancialAccount, amount: int
    ) -> None:
        # Assets gain available balance; card credits/payments reduce debt.
        balances[account.id] += -amount if cls._is_card(account) else amount

    @classmethod
    def _account_snapshots(
        cls,
        session: Session,
        *,
        occurred_before: datetime | None = None,
    ) -> list[AccountBalance]:
        accounts = list(
            session.scalars(select(FinancialAccount).order_by(FinancialAccount.id)).all()
        )
        balances = {account.id: account.opening_balance for account in accounts}
        activity_statement = (
            select(LedgerActivity)
            .options(
                selectinload(LedgerActivity.event).selectinload(Event.expenses),
                joinedload(LedgerActivity.source_account),
                joinedload(LedgerActivity.destination_account),
            )
            .order_by(LedgerActivity.occurred_at, LedgerActivity.id)
        )
        if occurred_before is not None:
            activity_statement = activity_statement.where(
                LedgerActivity.occurred_at < occurred_before
            )
        activities = list(session.scalars(activity_statement).all())
        for activity in activities:
            if activity.event is None or activity.event.status != EventStatus.CONFIRMED.value:
                continue
            amount = activity.effective_amount
            if amount is None:
                # Existing receipt data can represent an unreadable total.
                # Keep its link, but do not invent a balance movement.
                continue
            if activity.kind == ActivityKind.EXPENSE.value:
                if activity.source_account is not None:
                    cls._debit(balances, activity.source_account, amount)
            elif activity.kind == ActivityKind.INCOME.value:
                if activity.destination_account is not None:
                    cls._credit(balances, activity.destination_account, amount)
            elif activity.kind == ActivityKind.TRANSFER.value:
                if activity.source_account is not None:
                    cls._debit(balances, activity.source_account, amount)
                if activity.destination_account is not None:
                    cls._credit(balances, activity.destination_account, amount)

        return [
            AccountBalance(
                id=account.id,
                name=account.name,
                kind=AccountKind(account.kind),
                opening_balance=account.opening_balance,
                currency=account.currency,
                active=account.active,
                balance=balances[account.id],
                created_at=account.created_at,
                updated_at=account.updated_at,
            )
            for account in accounts
        ]

    def ensure_default_accounts(self) -> list[AccountBalance]:
        session = self._new_session()
        try:
            for name, kind in DEFAULT_ACCOUNTS:
                account = session.scalar(
                    select(FinancialAccount).where(FinancialAccount.name == name)
                )
                if account is None:
                    session.add(
                        FinancialAccount(
                            name=name,
                            kind=kind.value,
                            opening_balance=0,
                            currency="JPY",
                            active=True,
                        )
                    )
                elif account.kind != kind.value:
                    raise ValueError(f"default account has an incompatible kind: {name}")
            session.commit()
            return self._account_snapshots(session)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_account(
        self,
        name: str,
        kind: AccountKind | str,
        opening_balance: int = 0,
    ) -> AccountBalance:
        account_name = self._account_name(name)
        account_kind = self._account_kind(kind)
        opening = self._opening_balance(opening_balance)
        session = self._new_session()
        try:
            if session.scalar(
                select(FinancialAccount).where(FinancialAccount.name == account_name)
            ) is not None:
                raise ValueError(f"account already exists: {account_name}")
            account = FinancialAccount(
                name=account_name,
                kind=account_kind.value,
                opening_balance=opening,
                currency="JPY",
                active=True,
            )
            session.add(account)
            session.flush()
            session.commit()
            return next(
                snapshot
                for snapshot in self._account_snapshots(session)
                if snapshot.id == account.id
            )
        except IntegrityError as exc:
            session.rollback()
            raise ValueError(f"account already exists: {account_name}") from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_account_by_name(self, name: str) -> AccountBalance | None:
        account_name = self._account_name(name)
        session = self._new_session()
        try:
            account = session.scalar(
                select(FinancialAccount).where(FinancialAccount.name == account_name)
            )
            if account is None:
                return None
            return next(
                snapshot
                for snapshot in self._account_snapshots(session)
                if snapshot.id == account.id
            )
        finally:
            session.close()

    def list_accounts(self) -> list[AccountBalance]:
        session = self._new_session()
        try:
            return self._account_snapshots(session)
        finally:
            session.close()

    def has_daily_snapshots(
        self,
        local_date: date | datetime,
        *,
        timezone: ZoneInfo | tzinfo | str = "Asia/Tokyo",
    ) -> bool:
        """Return whether at least one value was persisted for ``local_date``.

        This distinction lets the digest keep its existing read-only preview
        behavior before the first capture while still showing an unavailable
        row when a capture partially lacks a source value.
        """

        selected_date = self._snapshot_date(local_date, timezone)
        session = self._new_session()
        try:
            return (
                session.scalar(
                    select(FinancialAccountSnapshot.id)
                    .where(FinancialAccountSnapshot.local_date == selected_date)
                    .limit(1)
                )
                is not None
            )
        finally:
            session.close()

    def has_daily_snapshot_history(
        self,
        local_date: date | datetime,
        *,
        timezone: ZoneInfo | tzinfo | str = "Asia/Tokyo",
    ) -> bool:
        """Return whether snapshot mode has an observation on or before a date."""

        selected_date = self._snapshot_date(local_date, timezone)
        session = self._new_session()
        try:
            return (
                session.scalar(
                    select(FinancialAccountSnapshot.id)
                    .where(FinancialAccountSnapshot.local_date <= selected_date)
                    .limit(1)
                )
                is not None
            )
        finally:
            session.close()

    @staticmethod
    def _normalise_observed_at(value: datetime | None) -> datetime:
        resolved = value or datetime.now(UTC)
        if resolved.tzinfo is None:
            return resolved
        return resolved.astimezone(UTC).replace(tzinfo=None)

    @classmethod
    def _upsert_daily_snapshot_row(
        cls,
        session: Session,
        *,
        account: FinancialAccount,
        local_date: date,
        value: int,
        measure: AccountSnapshotMeasure | str | None,
        currency: str | None,
        source: str,
        observed_at: datetime | None,
    ) -> FinancialAccountSnapshot:
        normalized_value = cls._snapshot_value(value)
        normalized_measure = cls._snapshot_measure(account, measure)
        normalized_currency = cls._snapshot_currency(account, currency)
        normalized_source = cls._snapshot_source(source)
        normalized_observed_at = cls._normalise_observed_at(observed_at)
        fingerprint = cls._snapshot_fingerprint(
            account_id=account.id,
            local_date=local_date,
            value=normalized_value,
            measure=normalized_measure,
            currency=normalized_currency,
            source=normalized_source,
        )
        row = session.scalar(
            select(FinancialAccountSnapshot)
            .where(
                FinancialAccountSnapshot.account_id == account.id,
                FinancialAccountSnapshot.local_date == local_date,
            )
            .options(joinedload(FinancialAccountSnapshot.account))
        )
        changed_at = _utc_now()
        if row is None:
            row = FinancialAccountSnapshot(
                account=account,
                local_date=local_date,
                value=normalized_value,
                measure=normalized_measure.value,
                currency=normalized_currency,
                source=normalized_source,
                observed_at=normalized_observed_at,
                revision=1,
                fingerprint=fingerprint,
                created_at=changed_at,
                updated_at=changed_at,
            )
            session.add(row)
            session.flush()
            return row

        if row.fingerprint != fingerprint:
            row.value = normalized_value
            row.measure = normalized_measure.value
            row.currency = normalized_currency
            row.source = normalized_source
            row.revision += 1
            row.fingerprint = fingerprint
        row.observed_at = normalized_observed_at
        row.updated_at = changed_at
        session.flush()
        return row

    def upsert_daily_snapshot(
        self,
        account_name: str,
        local_date: date | datetime,
        value: int,
        *,
        measure: AccountSnapshotMeasure | str | None = None,
        currency: str | None = None,
        source: str = "manual",
        observed_at: datetime | None = None,
        timezone: ZoneInfo | tzinfo | str = "Asia/Tokyo",
    ) -> AccountDailySnapshot:
        """Insert or update one absolute daily value without zero-filling gaps."""

        normalized_name = self._account_name(account_name)
        selected_date = self._snapshot_date(local_date, timezone)
        session = self._new_session()
        try:
            account = self._account(session, normalized_name)
            if account.kind not in {AccountKind.BANK.value, AccountKind.CARD.value}:
                raise ValueError("daily snapshots require a bank or card account")
            row = self._upsert_daily_snapshot_row(
                session,
                account=account,
                local_date=selected_date,
                value=value,
                measure=measure,
                currency=currency,
                source=source,
                observed_at=observed_at,
            )
            session.commit()
            return self._snapshot_result(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def capture_daily_snapshots(
        self,
        local_date: date | datetime,
        *,
        timezone: ZoneInfo | tzinfo | str = "Asia/Tokyo",
        observed_at: datetime | None = None,
        source: str = "ledger",
    ) -> list[AccountDailySnapshot]:
        """Persist current ledger values as of the end of one local date.

        Only active bank and card accounts are captured.  This source is the
        existing confirmed ledger, not an external bank or card provider.
        """

        selected_date = self._snapshot_date(local_date, timezone)
        occurred_before = self._snapshot_day_end(selected_date, timezone)
        normalized_source = self._snapshot_source(source)
        normalized_observed_at = self._normalise_observed_at(observed_at)
        session = self._new_session()
        try:
            balances = self._account_snapshots(session, occurred_before=occurred_before)
            rows: list[FinancialAccountSnapshot] = []
            for balance in balances:
                if not balance.active or balance.kind not in {AccountKind.BANK, AccountKind.CARD}:
                    continue
                account = session.get(FinancialAccount, balance.id)
                if account is None:
                    raise ValueError("account disappeared while capturing daily snapshots")
                rows.append(
                    self._upsert_daily_snapshot_row(
                        session,
                        account=account,
                        local_date=selected_date,
                        value=balance.balance,
                        measure=None,
                        currency=balance.currency,
                        source=normalized_source,
                        observed_at=normalized_observed_at,
                    )
                )
            session.commit()
            return [self._snapshot_result(row) for row in rows]
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_daily_snapshot_comparisons(
        self,
        local_date: date | datetime,
        *,
        timezone: ZoneInfo | tzinfo | str = "Asia/Tokyo",
    ) -> list[AccountSnapshotComparison]:
        """Return active bank/card rows and both prior-day comparison views."""

        selected_date = self._snapshot_date(local_date, timezone)
        session = self._new_session()
        try:
            accounts = list(
                session.scalars(
                    select(FinancialAccount)
                    .where(
                        FinancialAccount.active.is_(True),
                        FinancialAccount.kind.in_(
                            [AccountKind.BANK.value, AccountKind.CARD.value]
                        ),
                    )
                    .order_by(FinancialAccount.id)
                ).all()
            )
            comparisons: list[AccountSnapshotComparison] = []
            for account in accounts:
                current = session.scalar(
                    select(FinancialAccountSnapshot)
                    .where(
                        FinancialAccountSnapshot.account_id == account.id,
                        FinancialAccountSnapshot.local_date == selected_date,
                    )
                    .options(joinedload(FinancialAccountSnapshot.account))
                )
                previous = session.scalar(
                    select(FinancialAccountSnapshot)
                    .where(
                        FinancialAccountSnapshot.account_id == account.id,
                        FinancialAccountSnapshot.local_date < selected_date,
                    )
                    .order_by(
                        FinancialAccountSnapshot.local_date.desc(),
                        FinancialAccountSnapshot.id.desc(),
                    )
                    .limit(1)
                )
                previous_day = session.scalar(
                    select(FinancialAccountSnapshot)
                    .where(
                        FinancialAccountSnapshot.account_id == account.id,
                        FinancialAccountSnapshot.local_date
                        == selected_date - timedelta(days=1),
                    )
                    .order_by(FinancialAccountSnapshot.id.desc())
                    .limit(1)
                )
                measure = self._snapshot_measure(account, None)
                link = account.settlement_link
                comparisons.append(
                    AccountSnapshotComparison(
                        account_id=account.id,
                        account_name=account.name,
                        kind=AccountKind(account.kind),
                        currency=account.currency,
                        local_date=selected_date,
                        value=current.value if current is not None else None,
                        measure=(
                            AccountSnapshotMeasure(current.measure)
                            if current is not None
                            else measure
                        ),
                        source=current.source if current is not None else None,
                        observed_at=current.observed_at if current is not None else None,
                        revision=current.revision if current is not None else None,
                        fingerprint=current.fingerprint if current is not None else None,
                        previous_local_date=(
                            previous.local_date if previous is not None else None
                        ),
                        previous_value=previous.value if previous is not None else None,
                        delta=(
                            current.value - previous.value
                            if current is not None and previous is not None
                            else None
                        ),
                        previous_day_local_date=(
                            previous_day.local_date if previous_day is not None else None
                        ),
                        previous_day_value=(
                            previous_day.value if previous_day is not None else None
                        ),
                        previous_day_delta=(
                            current.value - previous_day.value
                            if current is not None and previous_day is not None
                            else None
                        ),
                        settlement_account_id=(
                            link.settlement_account_id if link is not None else None
                        ),
                        settlement_account_name=(
                            link.settlement_account.name if link is not None else None
                        ),
                    )
                )
            return comparisons
        finally:
            session.close()

    def link_settlement_account(
        self,
        card_account_name: str,
        settlement_account_name: str,
    ) -> SettlementAccountLink:
        """Create or update the primary settlement account for one card."""

        normalized_card_name = self._account_name(card_account_name)
        normalized_settlement_name = self._account_name(settlement_account_name)
        if normalized_card_name == normalized_settlement_name:
            raise ValueError("card and settlement accounts must differ")
        session = self._new_session()
        try:
            card = self._account(session, normalized_card_name)
            settlement = self._account(session, normalized_settlement_name)
            if card.kind != AccountKind.CARD.value:
                raise ValueError("settlement link source must be a card account")
            if settlement.kind != AccountKind.BANK.value:
                raise ValueError("settlement account must be a bank account")
            link = session.get(FinancialAccountSettlementLink, card.id)
            if link is None:
                link = FinancialAccountSettlementLink(
                    card_account=card,
                    settlement_account=settlement,
                    created_at=_utc_now(),
                    updated_at=_utc_now(),
                )
                session.add(link)
            else:
                link.settlement_account = settlement
                link.updated_at = _utc_now()
            session.commit()
            return self._settlement_result(link)
        except IntegrityError as exc:
            session.rollback()
            raise ValueError("could not save settlement account link") from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_settlement_account_links(self) -> list[SettlementAccountLink]:
        """Return settlement links in stable card-account order."""

        session = self._new_session()
        try:
            links = list(
                session.scalars(
                    select(FinancialAccountSettlementLink)
                    .options(
                        joinedload(FinancialAccountSettlementLink.card_account),
                        joinedload(FinancialAccountSettlementLink.settlement_account),
                    )
                    .order_by(FinancialAccountSettlementLink.card_account_id)
                ).all()
            )
            return [self._settlement_result(link) for link in links]
        finally:
            session.close()

    def unlink_settlement_account(self, card_account_name: str) -> bool:
        """Remove one card's settlement link and report whether one existed."""

        normalized_card_name = self._account_name(card_account_name)
        session = self._new_session()
        try:
            card = self._account(session, normalized_card_name)
            if card.kind != AccountKind.CARD.value:
                raise ValueError("settlement link source must be a card account")
            link = session.get(FinancialAccountSettlementLink, card.id)
            if link is None:
                return False
            session.delete(link)
            session.commit()
            return True
        except IntegrityError as exc:
            session.rollback()
            raise ValueError("could not remove settlement account link") from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def link_expense(self, event_id: int, account_name: str) -> LedgerActivityResult:
        normalized_name = self._account_name(account_name)
        session = self._new_session()
        try:
            event = self._event(session, event_id)
            account = self._account(session, normalized_name)
            existing = event.ledger_activity
            if existing is not None:
                if self._activity_matches(
                    existing,
                    kind=ActivityKind.EXPENSE,
                    source_account_id=account.id,
                    destination_account_id=None,
                    amount=None,
                    description=existing.description,
                ):
                    return self._activity_result(existing)
                raise ValueError("event already has a different ledger activity")
            if not event.expenses:
                raise ValueError("event has no expense")
            activity = LedgerActivity(
                event_id=event.id,
                kind=ActivityKind.EXPENSE.value,
                source_account_id=account.id,
                destination_account_id=None,
                amount=None,
                occurred_at=event.occurred_at,
                description=None,
            )
            session.add(activity)
            session.flush()
            session.commit()
            return self._activity_result(activity)
        except IntegrityError as exc:
            session.rollback()
            raise ValueError("event already has a ledger activity") from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record_income(
        self,
        event_id: int,
        account_name: str,
        amount: int,
        description: str | None = None,
    ) -> LedgerActivityResult:
        normalized_name = self._account_name(account_name)
        positive_amount = self._positive_amount(amount)
        normalized_description = self._optional_description(description)
        session = self._new_session()
        try:
            event = self._event(session, event_id)
            account = self._account(session, normalized_name)
            existing = event.ledger_activity
            if existing is not None:
                if self._activity_matches(
                    existing,
                    kind=ActivityKind.INCOME,
                    source_account_id=None,
                    destination_account_id=account.id,
                    amount=positive_amount,
                    description=normalized_description,
                ):
                    return self._activity_result(existing)
                raise ValueError("event already has a different ledger activity")
            if event.expenses:
                raise ValueError("event already has an expense")
            activity = LedgerActivity(
                event_id=event.id,
                kind=ActivityKind.INCOME.value,
                source_account_id=None,
                destination_account_id=account.id,
                amount=positive_amount,
                occurred_at=event.occurred_at,
                description=normalized_description,
            )
            session.add(activity)
            self._mark_event(event, EventKind.INCOME)
            session.flush()
            session.commit()
            return self._activity_result(activity)
        except IntegrityError as exc:
            session.rollback()
            raise ValueError("event already has a ledger activity") from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record_transfer(
        self,
        event_id: int,
        source_name: str,
        destination_name: str,
        amount: int,
        description: str | None = None,
    ) -> LedgerActivityResult:
        normalized_source = self._account_name(source_name)
        normalized_destination = self._account_name(destination_name)
        if normalized_source == normalized_destination:
            raise ValueError("transfer source and destination must differ")
        positive_amount = self._positive_amount(amount)
        normalized_description = self._optional_description(description)
        session = self._new_session()
        try:
            event = self._event(session, event_id)
            source = self._account(session, normalized_source)
            destination = self._account(session, normalized_destination)
            existing = event.ledger_activity
            if existing is not None:
                if self._activity_matches(
                    existing,
                    kind=ActivityKind.TRANSFER,
                    source_account_id=source.id,
                    destination_account_id=destination.id,
                    amount=positive_amount,
                    description=normalized_description,
                ):
                    return self._activity_result(existing)
                raise ValueError("event already has a different ledger activity")
            if event.expenses:
                raise ValueError("event already has an expense")
            activity = LedgerActivity(
                event_id=event.id,
                kind=ActivityKind.TRANSFER.value,
                source_account_id=source.id,
                destination_account_id=destination.id,
                amount=positive_amount,
                occurred_at=event.occurred_at,
                description=normalized_description,
            )
            session.add(activity)
            self._mark_event(event, EventKind.TRANSFER)
            session.flush()
            session.commit()
            return self._activity_result(activity)
        except IntegrityError as exc:
            session.rollback()
            raise ValueError("event already has a ledger activity") from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record_memo(self, event_id: int, body: str) -> MemoResult:
        if not isinstance(body, str) or not body.strip():
            raise ValueError("memo body must not be blank")
        session = self._new_session()
        try:
            event = self._event(session, event_id)
            existing = event.memo
            if existing is not None:
                if existing.body == body:
                    return self._memo_result(existing)
                raise ValueError("event already has a different memo")
            memo = Memo(event_id=event.id, body=body)
            session.add(memo)
            self._mark_event(event, EventKind.MEMO)
            session.flush()
            session.commit()
            return self._memo_result(memo)
        except IntegrityError as exc:
            session.rollback()
            raise ValueError("event already has a memo") from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_activities(self) -> list[LedgerActivityResult]:
        session = self._new_session()
        try:
            activities = list(
                session.scalars(
                    select(LedgerActivity)
                    .options(
                        selectinload(LedgerActivity.event).selectinload(Event.expenses),
                        joinedload(LedgerActivity.source_account),
                        joinedload(LedgerActivity.destination_account),
                    )
                    .order_by(
                        LedgerActivity.occurred_at.desc(),
                        LedgerActivity.created_at.desc(),
                        LedgerActivity.id.desc(),
                    )
                ).all()
            )
            return [self._activity_result(activity) for activity in activities]
        finally:
            session.close()

    def list_memos(self) -> list[MemoResult]:
        session = self._new_session()
        try:
            memos = list(
                session.scalars(
                    select(Memo)
                    .options(joinedload(Memo.event))
                    .order_by(Memo.created_at.desc(), Memo.id.desc())
                ).all()
            )
            return [self._memo_result(memo) for memo in memos]
        finally:
            session.close()
