"""Local daily household-budget snapshots and Discord delivery state.

The module intentionally stops at a pure, text-producing service boundary.  It
does not import discord.py, call a network service, or send a message.  A
parent scheduler can build a snapshot, call :meth:`FinanceDigestService.prepare_delivery`,
perform the returned create/edit operation, and then record the outcome with
``mark_sent`` or ``mark_failed``.

Finance data follows the existing application's conventions:

* ``Event.occurred_at`` is UTC-naive in SQLite and is converted to the
  configured local timezone at calendar boundaries.
* Only ``confirmed`` events contribute to spending, budgets, and balances.
* Expense amounts are resolved from the ``Expense`` row and only positive,
  bounded integers are accepted as spending.
* The delivery table has its own metadata because the existing base metadata
  is also used by strict legacy-schema tests.  It is created idempotently when
  this service is constructed (or through ``init_storage``).
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, selectinload

from .budgets import OVERALL_SCOPE
from .constants import ABSOLUTE_MAX_EXPENSE_YEN, AccountKind, EventStatus
from .finance import (
    ASSET_ACCOUNT_KINDS,
    AccountBalance,
    AccountDailySnapshot,
    AccountSnapshotComparison,
    AccountSnapshotMeasure,
    FinanceService,
    account_kind_label,
    account_kind_role,
    mask_account_identity,
)
from .models import Event, Expense, MonthlyBudget

# The shared Discord transport applies this conservative limit before sending.
# Keep the digest below it so required totals are never lost to downstream
# truncation.
DISCORD_MAX_CONTENT_LENGTH = 1_900
DEFAULT_TIMEZONE = "Asia/Tokyo"
DEFAULT_LARGE_EXPENSE_YEN = 5_000
DEFAULT_STALE_AFTER_SECONDS = 300
DELIVERY_KIND = "daily_household_budget"
UNKNOWN_DELIVERY_ERROR_CODE = "delivery_outcome_unknown"
STALE_EDIT_ERROR_CODE = "stale_edit_claim_recovered"
STALE_CREATE_ERROR_CODE = "stale_create_outcome_unknown"


def _utc_now_naive() -> datetime:
    """Return the UTC-naive timestamp used by the existing SQLite schema."""

    return datetime.now(UTC).replace(tzinfo=None)


def _utc_naive(value: datetime | None) -> datetime:
    """Normalize a timestamp to the application's UTC-naive convention."""

    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None:
        # Naive timestamps at service boundaries are treated as UTC, matching
        # the way existing SQLite rows are interpreted by the web/finance code.
        return resolved
    return resolved.astimezone(UTC).replace(tzinfo=None)


def _zone_info(value: ZoneInfo | str | tzinfo) -> ZoneInfo | tzinfo:
    if isinstance(value, ZoneInfo):
        return value
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("timezone must be a valid IANA timezone")
        try:
            return ZoneInfo(value.strip())
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
    if isinstance(value, tzinfo):
        return value
    raise ValueError("timezone must be a valid timezone")


def _timezone_name(value: ZoneInfo | tzinfo) -> str:
    return getattr(value, "key", None) or str(value)


def _normalise_local_date(
    value: date | datetime, timezone: ZoneInfo | tzinfo | None = None
) -> date:
    if isinstance(value, datetime):
        if timezone is not None and value.tzinfo is not None:
            return value.astimezone(timezone).date()
        return value.date()
    if isinstance(value, date):
        return value
    raise ValueError("local_date must be a date")


def _local_date_from_now(value: datetime, timezone: ZoneInfo | tzinfo) -> date:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(timezone).date()


def _day_bounds(target_date: date, timezone: ZoneInfo | tzinfo) -> tuple[datetime, datetime]:
    start_local = datetime.combine(target_date, time.min, tzinfo=timezone)
    end_local = datetime.combine(target_date + timedelta(days=1), time.min, tzinfo=timezone)
    return (
        start_local.astimezone(UTC).replace(tzinfo=None),
        end_local.astimezone(UTC).replace(tzinfo=None),
    )


def _month_bounds(target_date: date, timezone: ZoneInfo | tzinfo) -> tuple[datetime, datetime]:
    month_start = target_date.replace(day=1)
    if month_start.month == 12:
        next_month = date(month_start.year + 1, 1, 1)
    else:
        next_month = date(month_start.year, month_start.month + 1, 1)
    start_local = datetime.combine(month_start, time.min, tzinfo=timezone)
    end_local = datetime.combine(next_month, time.min, tzinfo=timezone)
    return (
        start_local.astimezone(UTC).replace(tzinfo=None),
        end_local.astimezone(UTC).replace(tzinfo=None),
    )


def _valid_expense_amount(value: object) -> int | None:
    if type(value) is not int or not 0 < value <= ABSOLUTE_MAX_EXPENSE_YEN:
        return None
    return value


def _clean_display_text(value: object, *, fallback: str, limit: int = 80) -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = " ".join(value.replace("\x00", " ").split())
    return cleaned[:limit] or fallback


def _normalised_category(value: object) -> str:
    if not isinstance(value, str):
        return "未分類"
    cleaned = " ".join(value.replace("\x00", " ").split())
    return cleaned or "未分類"


def _is_unclassified(category: object) -> bool:
    return _normalised_category(category) == "未分類"


def _normalised_channel_id(value: str | int) -> str:
    if isinstance(value, bool):
        raise ValueError("channel_id must be a non-empty identifier")
    channel_id = str(value).strip()
    if not channel_id or len(channel_id) > 64:
        raise ValueError("channel_id must be a non-empty identifier")
    return channel_id


def _normalised_message_id(value: str | int) -> str:
    if isinstance(value, bool):
        raise ValueError("discord_message_id must be a non-empty identifier")
    message_id = str(value).strip()
    if not message_id or len(message_id) > 64:
        raise ValueError("discord_message_id must be a non-empty identifier")
    return message_id


def _safe_error_text(value: object, *, limit: int = 1_000) -> str | None:
    if value is None:
        return None
    if isinstance(value, BaseException):
        text = type(value).__name__
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    cleaned = " ".join(text.replace("\x00", " ").split())
    return cleaned[:limit] or None


class DigestBase(DeclarativeBase):
    """Private metadata for the additive household-digest table."""


def _delivery_default_timestamp() -> datetime:
    return _utc_now_naive()


class FinanceDigestDeliveryRow(DigestBase):
    """Durable state for one local date and Discord channel.

    ``posting`` is an in-flight claim.  The parent integration must call
    ``mark_sent`` only after Discord confirms create/edit; a failed operation
    remains retryable and never becomes ``posted``.  The table is intentionally
    independent from rhythm's user-scoped delivery table.
    """

    __tablename__ = "finance_digest_deliveries"
    __table_args__ = (
        UniqueConstraint("local_date", "channel_id", name="uq_finance_digest_delivery_daily"),
        Index("ix_finance_digest_deliveries_status_updated", "status", "updated_at"),
        CheckConstraint(
            "status IN ('pending', 'posting', 'posted', 'failed', 'unknown')",
            name="ck_finance_digest_delivery_status",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_finance_digest_delivery_attempt_count"
        ),
        CheckConstraint(
            "length(trim(channel_id)) > 0", name="ck_finance_digest_delivery_channel_id"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    semantic_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discord_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_delivery_default_timestamp, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_delivery_default_timestamp,
        onupdate=_delivery_default_timestamp,
        nullable=False,
    )


# A shorter public name is convenient for parent integrations and tests.
FinanceDigestDelivery = FinanceDigestDeliveryRow


_storage_lock = threading.RLock()


def init_storage(session_factory: Callable[[], Session]) -> None:
    """Create the digest delivery table and index idempotently.

    The existing application uses ``create_all`` and has no migration runner.
    Keeping this helper public lets a parent application initialize the table
    during startup while the service also calls it defensively.
    """

    session = session_factory()
    try:
        with _storage_lock:
            DigestBase.metadata.create_all(session.get_bind())
    finally:
        session.close()


@dataclass(frozen=True, slots=True)
class CategoryTotal:
    category: str
    amount: int

    @property
    def total(self) -> int:
        return self.amount


@dataclass(frozen=True, slots=True)
class NotableExpense:
    amount: int
    category: str
    subcategory: str | None
    merchant: str
    reasons: tuple[str, ...]
    event_id: int

    @property
    def is_large(self) -> bool:
        return "大きな支出" in self.reasons

    @property
    def is_dining(self) -> bool:
        return "外食" in self.reasons


@dataclass(frozen=True, slots=True)
class AccountDigest:
    id: int
    name: str
    kind: AccountKind
    currency: str
    balance: int | None
    warnings: tuple[str, ...] = ()
    measure: AccountSnapshotMeasure = AccountSnapshotMeasure.BALANCE
    previous_local_date: date | None = None
    previous_value: int | None = None
    delta: int | None = None
    source: str | None = None
    observed_at: datetime | None = None
    settlement_account_name: str | None = None
    snapshot_persisted: bool = False
    previous_day_local_date: date | None = None
    previous_day_value: int | None = None
    previous_day_delta: int | None = None
    previous_available_local_date: date | None = None
    previous_available_value: int | None = None
    previous_available_delta: int | None = None

    @property
    def displayed_balance(self) -> int | None:
        return self.balance

    @property
    def account_type(self) -> AccountKind:
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

    @property
    def is_asset(self) -> bool:
        return self.kind in ASSET_ACCOUNT_KINDS

    @property
    def is_liability(self) -> bool:
        return self.kind == AccountKind.CARD

    @property
    def previous_day_available(self) -> bool:
        return self.previous_day_local_date is not None


@dataclass(frozen=True, slots=True)
class MonthlyBudgetDigest:
    month: str
    amount: int
    spent: int
    remaining: int

    @property
    def budget_amount(self) -> int:
        return self.amount

    @property
    def remaining_amount(self) -> int:
        return self.remaining

    @property
    def overage(self) -> int:
        return max(-self.remaining, 0)

    @property
    def is_over_budget(self) -> bool:
        return self.remaining < 0


BudgetDigest = MonthlyBudgetDigest


def _semantic_payload(snapshot: DailyFinanceSnapshot) -> dict[str, Any]:
    budget = snapshot.monthly_budget
    return {
        "version": 3,
        "local_date": snapshot.local_date.isoformat(),
        "timezone": snapshot.timezone,
        "large_expense_threshold_yen": snapshot.large_expense_threshold_yen,
        "daily_total": snapshot.daily_total,
        "category_totals": sorted(snapshot.category_totals.items()),
        "notable_spending": [
            {
                "amount": item.amount,
                "category": item.category,
                "subcategory": item.subcategory,
                "merchant": item.merchant,
                "reasons": item.reasons,
            }
            for item in snapshot.notable_spending
        ],
        "month_to_date_total": snapshot.month_to_date_total,
        "monthly_budget": (
            {
                "month": budget.month,
                "amount": budget.amount,
                "spent": budget.spent,
                "remaining": budget.remaining,
            }
            if budget is not None
            else None
        ),
        "pending_expense_count": snapshot.pending_expense_count,
        "unclassified_confirmed_expense_count": snapshot.unclassified_confirmed_expense_count,
        "asset_total": snapshot.asset_total,
        "liability_total": snapshot.liability_total,
        "net_assets": snapshot.net_assets,
        "accounts": [
            {
                "id": account.id,
                "display_identity": account.display_identity,
                "kind": account.kind.value,
                "currency": account.currency,
                "balance": account.balance,
                "measure": account.measure.value,
                "previous_local_date": (
                    account.previous_local_date.isoformat()
                    if account.previous_local_date is not None
                    else None
                ),
                "previous_value": account.previous_value,
                "delta": account.delta,
                "previous_day_local_date": (
                    account.previous_day_local_date.isoformat()
                    if account.previous_day_local_date is not None
                    else None
                ),
                "previous_day_value": account.previous_day_value,
                "previous_day_delta": account.previous_day_delta,
                "previous_available_local_date": (
                    account.previous_available_local_date.isoformat()
                    if account.previous_available_local_date is not None
                    else None
                ),
                "previous_available_value": account.previous_available_value,
                "previous_available_delta": account.previous_available_delta,
                "source": account.source,
                "settlement_account_name": (
                    mask_account_identity(account.settlement_account_name)
                    if account.settlement_account_name is not None
                    else None
                ),
                "snapshot_persisted": account.snapshot_persisted,
                "warnings": account.warnings,
            }
            for account in snapshot.accounts
        ],
        "warnings": snapshot.warnings,
    }


@dataclass(frozen=True, slots=True)
class DailyFinanceSnapshot:
    """Detached, deterministic finance content for one local calendar date."""

    local_date: date
    timezone: str
    generated_at: datetime
    daily_total: int
    category_totals: Mapping[str, int]
    notable_spending: tuple[NotableExpense, ...]
    month_to_date_total: int
    monthly_budget: MonthlyBudgetDigest | None
    pending_expense_count: int
    unclassified_confirmed_expense_count: int
    accounts: tuple[AccountDigest, ...]
    warnings: tuple[str, ...]
    large_expense_threshold_yen: int = DEFAULT_LARGE_EXPENSE_YEN
    asset_total: int | None = None
    liability_total: int | None = None
    net_assets: int | None = None

    @property
    def date(self) -> date:
        return self.local_date

    @property
    def confirmed_expense_total(self) -> int:
        return self.daily_total

    @property
    def total(self) -> int:
        return self.daily_total

    @property
    def category_rows(self) -> tuple[CategoryTotal, ...]:
        return tuple(
            CategoryTotal(category, amount)
            for category, amount in self.category_totals.items()
        )

    @property
    def categories(self) -> tuple[CategoryTotal, ...]:
        return self.category_rows

    @property
    def month_total(self) -> int:
        return self.month_to_date_total

    @property
    def mtd_total(self) -> int:
        return self.month_to_date_total

    @property
    def budget(self) -> MonthlyBudgetDigest | None:
        return self.monthly_budget

    @property
    def pending_count(self) -> int:
        return self.pending_expense_count

    @property
    def unclassified_count(self) -> int:
        return self.unclassified_confirmed_expense_count

    @property
    def account_balances(self) -> tuple[AccountDigest, ...]:
        return self.accounts

    @property
    def total_assets(self) -> int | None:
        return self.asset_total

    @property
    def assets_total(self) -> int | None:
        return self.asset_total

    @property
    def total_liabilities(self) -> int | None:
        return self.liability_total

    @property
    def liabilities_total(self) -> int | None:
        return self.liability_total

    @property
    def net_asset_total(self) -> int | None:
        return self.net_assets

    @property
    def semantic_fingerprint(self) -> str:
        payload = json.dumps(
            _semantic_payload(self),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DeliveryAction(StrEnum):
    CREATE = "create"
    EDIT = "edit"
    SKIP = "skip"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class DigestDeliveryState:
    id: int
    local_date: date
    channel_id: str
    status: str
    attempt_count: int
    semantic_fingerprint: str | None
    discord_message_id: str | None
    last_error_code: str | None
    last_error: str | None
    posted_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def message_id(self) -> str | None:
        return self.discord_message_id

    @property
    def sent(self) -> bool:
        return self.status == "posted"

    @property
    def is_sent(self) -> bool:
        return self.sent

    @property
    def discord_channel_id(self) -> str:
        return self.channel_id


def _delivery_state(row: FinanceDigestDeliveryRow) -> DigestDeliveryState:
    return DigestDeliveryState(
        id=row.id,
        local_date=row.local_date,
        channel_id=row.channel_id,
        status=row.status,
        attempt_count=row.attempt_count,
        semantic_fingerprint=row.semantic_fingerprint,
        discord_message_id=row.discord_message_id,
        last_error_code=row.last_error_code,
        last_error=row.last_error,
        posted_at=row.posted_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@dataclass(frozen=True, slots=True)
class DigestDeliveryDecision:
    action: DeliveryAction
    delivery_id: int | None
    message_id: str | None
    attempt_count: int
    status: str | None
    state: DigestDeliveryState | None = None

    @property
    def existing_message_id(self) -> str | None:
        return self.message_id

    @property
    def should_create(self) -> bool:
        return self.action == DeliveryAction.CREATE

    @property
    def should_edit(self) -> bool:
        return self.action == DeliveryAction.EDIT

    @property
    def should_skip(self) -> bool:
        return self.action == DeliveryAction.SKIP


@dataclass(frozen=True, slots=True)
class DigestDeliveryPlan:
    """A claimed (or dry-run) create/edit/skip decision for a snapshot."""

    action: DeliveryAction
    delivery_id: int | None
    local_date: date
    channel_id: str
    semantic_fingerprint: str
    content: str
    message_id: str | None
    attempt_count: int
    claimed: bool
    dry_run: bool
    state: DigestDeliveryState | None = None

    @property
    def decision(self) -> DeliveryAction:
        return self.action

    @property
    def existing_message_id(self) -> str | None:
        return self.message_id

    @property
    def should_create(self) -> bool:
        return self.action == DeliveryAction.CREATE

    @property
    def should_edit(self) -> bool:
        return self.action == DeliveryAction.EDIT

    @property
    def should_skip(self) -> bool:
        return self.action == DeliveryAction.SKIP

    @property
    def in_progress(self) -> bool:
        return self.action == DeliveryAction.IN_PROGRESS

    @property
    def should_send(self) -> bool:
        return self.claimed and self.action in {
            DeliveryAction.CREATE,
            DeliveryAction.EDIT,
        }


class DeliveryClaimConflictError(ValueError):
    """Raised when a stale or already-finalized claim is completed."""


def _format_money(amount: int, currency: str = "JPY") -> str:
    if not isinstance(currency, str) or not currency.strip():
        currency = "JPY"
    if currency.casefold() == "jpy":
        return f"¥{amount:,}"
    # Household expenses are yen, but preserve an explicitly different
    # account currency without attempting a conversion.  In particular, do
    # not introduce a rhythm-game ``cr`` unit at this boundary.
    code = currency.strip().upper()[:8]
    if code.casefold() == "cr":
        code = ""
    return f"{code} {amount:,}".strip()


def _format_delta(amount: int, currency: str = "JPY") -> str:
    """Format a signed change without turning an unavailable value into zero."""

    if amount == 0:
        return f"±{_format_money(0, currency)}"
    sign = "+" if amount > 0 else "-"
    return f"{sign}{_format_money(abs(amount), currency)}"


def _is_outside_meal(event: Event) -> bool:
    for meal in event.meals:
        outside_home = str(meal.outside_home or "").strip().casefold()
        if outside_home in {"outside", "外食", "外"}:
            return True
    return False


def _meal_merchant(event: Event) -> str | None:
    for meal in event.meals:
        value = meal.restaurant_normalized or meal.restaurant_raw
        if isinstance(value, str) and value.strip():
            return _clean_display_text(value, fallback="", limit=80)
    return None


def _is_dining(category: str, subcategory: str | None, event: Event) -> bool:
    if category != "食費":
        return False
    normalized_subcategory = (subcategory or "").strip().casefold()
    return normalized_subcategory in {"外食", "カフェ", "dining", "outside"} or _is_outside_meal(
        event
    )


def _account_warning(account: AccountDigest) -> tuple[str, ...]:
    if account.balance is None:
        return ()
    amount = _format_money(account.balance, account.currency)
    account_name = _clean_display_text(account.display_identity, fallback="口座", limit=64)
    if account.kind == AccountKind.BANK and account.balance < 0:
        return (f"銀行口座「{account_name}」の残高がマイナスです：{amount}",)
    if account.kind == AccountKind.CARD and account.balance > 0:
        return (
            f"カード「{account_name}」の未精算額：{amount}（支払日・限度額は未登録）",
        )
    return ()


def _month_key(target_date: date) -> str:
    return f"{target_date.year:04d}-{target_date.month:02d}"


def _account_type_suffix(account: AccountDigest) -> str:
    """Make the account kind and its asset/liability role explicit."""

    kind_value = account.kind.value
    kind_label = account_kind_label(account.kind)
    role = account_kind_role(account.kind)
    if role is None:
        return f"【種別：{kind_label}（{kind_value}）】"
    return f"【種別：{kind_label}（{kind_value}）／{role}】"


def _account_delta_role(account: AccountDigest, delta: int) -> str:
    """Describe the direction in financial terms, not only with a sign."""

    role = "負債" if account.kind == AccountKind.CARD else "資産"
    if delta > 0:
        return f"{role}増"
    if delta < 0:
        return f"{role}減"
    return "変化なし"


def _exact_previous_day_delta(
    account: AccountDigest,
    *,
    local_date: date,
) -> tuple[bool, int | None]:
    """Resolve the exact previous-local-day comparison.

    New snapshots carry an explicit previous-day field.  The fallback keeps
    hand-built compatibility DTOs useful when their legacy previous date is
    exactly yesterday; an older, non-consecutive observation is never used as
    a daily delta.
    """

    if account.previous_day_local_date is not None:
        return True, account.previous_day_delta
    if (
        account.previous_local_date == local_date - timedelta(days=1)
        and account.delta is not None
    ):
        return True, account.delta
    return False, None


def _account_total_line(label: str, value: int | None) -> str:
    return f"{label}：{_format_money(value) if value is not None else '未取得'}"


def format_daily_digest(
    snapshot: DailyFinanceSnapshot,
    *,
    max_length: int = DISCORD_MAX_CONTENT_LENGTH,
) -> str:
    """Format a Japanese, Discord-ready digest without dropping totals.

    Detail sections are line-bounded.  Core totals always remain in the
    message; when details do not fit, an explicit note states that details were
    omitted while totals still cover every qualifying record.
    """

    if not isinstance(snapshot, DailyFinanceSnapshot):
        raise TypeError("snapshot must be a DailyFinanceSnapshot")
    if type(max_length) is not int or max_length < 1:
        raise ValueError("max_length must be a positive integer")
    # Callers may pass the platform's nominal 2,000-character limit, but the
    # shared transport safely caps content at 1,900.  Apply that cap here so
    # the returned string is never silently truncated after formatting.
    max_length = min(max_length, DISCORD_MAX_CONTENT_LENGTH)

    budget = snapshot.monthly_budget
    if budget is None:
        budget_line = "予算：未設定"
    elif budget.remaining >= 0:
        budget_line = (
            f"予算：{_format_money(budget.amount)} / 残り { _format_money(budget.remaining) }"
        )
    else:
        budget_line = (
            f"予算：{_format_money(budget.amount)} / 超過 { _format_money(budget.overage) }"
        )

    mandatory = [
        f"【家計の毎日自動サマリー｜{snapshot.local_date.isoformat()}】",
        "確定済み支出のみを集計しています。",
        f"今日の支出：{_format_money(snapshot.daily_total)}",
        "支出はありません。" if snapshot.daily_total == 0 else "",
        f"月初来（{snapshot.local_date.year:04d}-{snapshot.local_date.month:02d}）："
        f"{_format_money(snapshot.month_to_date_total)}",
        budget_line,
        f"保留中の支出：{snapshot.pending_expense_count}件",
        f"未分類（確定）：{snapshot.unclassified_confirmed_expense_count}件",
        "※詳細を省略する場合も、合計値は全件分です。",
    ]
    if any(
        value is not None
        for value in (snapshot.asset_total, snapshot.liability_total, snapshot.net_assets)
    ):
        # Totals are mandatory once any account value exists.  A partial
        # source is shown as unavailable rather than silently treating a
        # missing bank/card as zero.
        mandatory.extend(
            [
                _account_total_line("資産合計", snapshot.asset_total),
                _account_total_line("負債合計", snapshot.liability_total),
                _account_total_line("純資産", snapshot.net_assets),
            ]
        )
    mandatory = [line for line in mandatory if line or line == "支出はありません。"]

    sections: list[tuple[str, list[str]]] = []
    if snapshot.category_totals:
        category_lines = [
            (
                f"・{_clean_display_text(category, fallback='未分類', limit=64)}："
                f"{_format_money(amount)}"
            )
            for category, amount in sorted(
                snapshot.category_totals.items(), key=lambda item: (-item[1], item[0])
            )
        ]
        sections.append(("カテゴリ", category_lines))

    if snapshot.notable_spending:
        notable_lines = []
        for item in snapshot.notable_spending:
            reason = "／".join(item.reasons)
            label = _clean_display_text(item.merchant, fallback=item.category, limit=80)
            notable_lines.append(f"・{reason}：{_format_money(item.amount)}（{label}）")
        sections.append(("注目の支出", notable_lines))

    if snapshot.accounts:
        account_lines = []
        for account in snapshot.accounts:
            account_name = _clean_display_text(
                account.display_identity,
                fallback="口座",
                limit=64,
            )
            if account.balance is None:
                label = "利用残高" if account.kind == AccountKind.CARD else "残高"
                line = f"・{account_name}：{label} 未取得"
                if account.previous_local_date is not None and account.previous_value is not None:
                    line += (
                        f"（前回観測 {account.previous_local_date.isoformat()}："
                        f"{_format_money(account.previous_value, account.currency)}）"
                    )
                else:
                    line += "（比較不可）"
                has_previous_day, _ = _exact_previous_day_delta(
                    account,
                    local_date=snapshot.local_date,
                )
                if not has_previous_day:
                    line += "【前日比較なし】"
            else:
                label = "利用残高" if account.kind == AccountKind.CARD else "残高"
                line = (
                    f"・{account_name}：{label} "
                    f"{_format_money(account.balance, account.currency)}"
                )
                if account.snapshot_persisted:
                    has_previous_day, previous_day_delta = _exact_previous_day_delta(
                        account,
                        local_date=snapshot.local_date,
                    )
                    if not has_previous_day or previous_day_delta is None:
                        if (
                            account.previous_local_date is None
                            and account.previous_available_local_date is None
                        ):
                            # Retain the old parenthetical for compatibility
                            # while making the required missing-day wording
                            # unambiguous for notification readers.
                            line += "（比較なし：初回）【前日比較なし】"
                        else:
                            line += "（前日比較なし）"
                    else:
                        previous_label = (
                            f"前回観測 {account.previous_local_date.isoformat()}"
                            if account.previous_local_date is not None
                            else "前回"
                        )
                        line += (
                            f"（{previous_label}との差 "
                            f"{_format_delta(previous_day_delta, account.currency)}）"
                        )
                        comparison_label = (
                            "前日差"
                            if account.previous_day_local_date
                            == snapshot.local_date - timedelta(days=1)
                            else "前回差"
                        )
                        line += (
                            f"【{comparison_label}／"
                            f"{_account_delta_role(account, previous_day_delta)}】"
                        )
                else:
                    line += "（前日比較なし）"
            if account.settlement_account_name is not None:
                settlement_name = _clean_display_text(
                    mask_account_identity(account.settlement_account_name),
                    fallback="未設定",
                    limit=64,
                )
                line += f"（引落口座：{settlement_name}）"
            line += _account_type_suffix(account)
            account_lines.append(line)
        sections.append(("口座残高", account_lines))

    if snapshot.warnings:
        sections.append(
            (
                "注意",
                [
                    f"・{_clean_display_text(warning, fallback='確認が必要です。')}"
                    for warning in snapshot.warnings
                ],
            )
        )

    lines = list(mandatory)
    for title, details in sections:
        lines, _ = _append_bounded_section(lines, title, details, max_length)

    content = "\n".join(lines)
    if len(content) > max_length:
        # The mandatory content should fit at Discord's limit.  If a caller
        # intentionally supplied a smaller bound, refusing is safer than
        # returning a message with a missing total.
        raise ValueError("max_length is too small for the digest totals")
    return content


def _append_bounded_section(
    lines: list[str], title: str, details: list[str], max_length: int
) -> tuple[list[str], int]:
    if not details:
        return lines, 0
    header = f"{title}："
    candidate = lines + [header]
    if len("\n".join(candidate)) > max_length:
        # The global omission note in the mandatory section makes this safe.
        return lines, len(details)

    result = candidate
    included = 0
    for detail in details:
        if len("\n".join(result + [detail])) > max_length:
            break
        result.append(detail)
        included += 1

    omitted = len(details) - included
    if omitted:
        marker = f"・詳細を{omitted}件省略（合計値は全件分）"
        while included and len("\n".join(result + [marker])) > max_length:
            result.pop()
            included -= 1
            omitted += 1
        if len("\n".join(result + [marker])) <= max_length:
            result.append(marker)
        elif len("\n".join(lines + [header, marker])) <= max_length:
            result = lines + [header, marker]
        else:
            # The mandatory note still tells the reader that omitted detail
            # does not change totals.
            result = lines
    return result, omitted


def format_finance_digest(
    snapshot: DailyFinanceSnapshot,
    *,
    max_length: int = DISCORD_MAX_CONTENT_LENGTH,
) -> str:
    """Compatibility alias for the public formatter."""

    return format_daily_digest(snapshot, max_length=max_length)


def snapshot_fingerprint(snapshot: DailyFinanceSnapshot) -> str:
    return snapshot.semantic_fingerprint


class FinanceDigestService:
    """Build local finance snapshots and coordinate durable delivery claims."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        timezone: ZoneInfo | str | tzinfo = DEFAULT_TIMEZONE,
        *,
        large_expense_yen: int = DEFAULT_LARGE_EXPENSE_YEN,
        stale_after_seconds: int | float = DEFAULT_STALE_AFTER_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(large_expense_yen) is not int or large_expense_yen <= 0:
            raise ValueError("large_expense_yen must be a positive integer")
        if isinstance(stale_after_seconds, bool) or not isinstance(
            stale_after_seconds, (int, float)
        ) or stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be a positive number")
        try:
            finite_stale_after = math.isfinite(stale_after_seconds)
        except OverflowError as exc:
            raise ValueError("stale_after_seconds must be finite") from exc
        if not finite_stale_after:
            raise ValueError("stale_after_seconds must be finite")
        self.session_factory = session_factory
        self.timezone = _zone_info(timezone)
        self.timezone_name = _timezone_name(self.timezone)
        self.large_expense_yen = large_expense_yen
        self.stale_after_seconds = stale_after_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        init_storage(session_factory)

    def init_storage(self) -> None:
        init_storage(self.session_factory)

    def _resolve_local_date(
        self,
        local_date: date | datetime | None,
        *,
        target_date: date | datetime | None,
        now: datetime | None,
    ) -> date:
        if local_date is not None and target_date is not None:
            raise ValueError("pass only one of local_date or target_date")
        selected = target_date if target_date is not None else local_date
        if selected is not None:
            return _normalise_local_date(selected, self.timezone)
        resolved_now = now if now is not None else self.clock()
        return _local_date_from_now(resolved_now, self.timezone)

    def build_snapshot(
        self,
        local_date: date | datetime | None = None,
        *,
        target_date: date | datetime | None = None,
        now: datetime | None = None,
    ) -> DailyFinanceSnapshot:
        """Return a detached snapshot for one configured local calendar day."""

        selected_date = self._resolve_local_date(local_date, target_date=target_date, now=now)
        generated_at = now if now is not None else self.clock()
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=UTC)
        generated_at = generated_at.astimezone(UTC)
        day_start, day_end = _day_bounds(selected_date, self.timezone)
        month_start, _ = _month_bounds(selected_date, self.timezone)
        session = self.session_factory()
        try:
            events = list(
                session.scalars(
                    select(Event)
                    .where(
                        Event.occurred_at >= month_start,
                        Event.occurred_at < day_end,
                        Event.expenses.any(),
                    )
                    .options(selectinload(Event.expenses), selectinload(Event.meals))
                    .order_by(Event.occurred_at, Event.id)
                ).all()
            )
            daily_total = 0
            month_to_date_total = 0
            category_totals: defaultdict[str, int] = defaultdict(int)
            notable_candidates: list[NotableExpense] = []
            pending_count = 0
            unclassified_count = 0

            for event in events:
                is_daily = day_start <= event.occurred_at < day_end
                is_confirmed = event.status == EventStatus.CONFIRMED.value
                is_pending = not is_confirmed and event.status != EventStatus.IGNORED.value
                expenses = sorted(event.expenses, key=lambda expense: expense.id)
                if is_daily and is_pending:
                    pending_count += len(expenses)

                for expense in expenses:
                    if is_daily and is_confirmed and _is_unclassified(expense.category):
                        unclassified_count += 1
                    if not is_confirmed:
                        continue
                    amount = _valid_expense_amount(expense.amount)
                    if amount is None:
                        # Invalid, negative, zero, or unreadable amounts are
                        # never presented as valid spending.
                        continue
                    if is_daily:
                        daily_total += amount
                        category_totals[_normalised_category(expense.category)] += amount
                    month_to_date_total += amount
                    if is_daily:
                        self._append_notable(
                            notable_candidates,
                            event=event,
                            expense=expense,
                            amount=amount,
                        )

            category_map = dict(
                sorted(category_totals.items(), key=lambda item: (-item[1], item[0]))
            )
            notable = tuple(
                sorted(
                    notable_candidates,
                    key=lambda item: (
                        -item.amount,
                        item.category,
                        item.subcategory or "",
                        item.merchant,
                        item.event_id,
                    ),
                )
            )
            budget = self._monthly_budget(
                session,
                month=_month_key(selected_date),
                spent=month_to_date_total,
            )
        finally:
            session.close()

        accounts = self._active_account_snapshots(selected_date)
        warnings = tuple(
            warning
            for account in accounts
            for warning in account.warnings
        )
        asset_total, liability_total, net_assets = self._account_totals(accounts)
        return DailyFinanceSnapshot(
            local_date=selected_date,
            timezone=self.timezone_name,
            generated_at=generated_at,
            daily_total=daily_total,
            category_totals=category_map,
            notable_spending=notable,
            month_to_date_total=month_to_date_total,
            monthly_budget=budget,
            pending_expense_count=pending_count,
            unclassified_confirmed_expense_count=unclassified_count,
            accounts=accounts,
            warnings=warnings,
            large_expense_threshold_yen=self.large_expense_yen,
            asset_total=asset_total,
            liability_total=liability_total,
            net_assets=net_assets,
        )

    snapshot = build_snapshot
    build_daily_snapshot = build_snapshot

    def capture_daily_snapshots(
        self,
        local_date: date | datetime | None = None,
        *,
        target_date: date | datetime | None = None,
        now: datetime | None = None,
    ) -> tuple[AccountDailySnapshot, ...]:
        """Persist ledger values before a daily digest is built.

        The digest service deliberately keeps capture separate from
        ``build_snapshot`` so read-only commands and dry-runs do not mutate
        finance state.  The scheduler calls this method immediately before
        publishing a daily or reconciliation digest.
        """

        selected_date = self._resolve_local_date(
            local_date,
            target_date=target_date,
            now=now,
        )
        return tuple(
            FinanceService(self.session_factory).capture_daily_snapshots(
                selected_date,
                timezone=self.timezone,
                observed_at=now if now is not None else self.clock(),
            )
        )

    def _append_notable(
        self,
        destination: list[NotableExpense],
        *,
        event: Event,
        expense: Expense,
        amount: int,
    ) -> None:
        category = _normalised_category(expense.category)
        subcategory = (
            " ".join(expense.subcategory.replace("\x00", " ").split())
            if isinstance(expense.subcategory, str) and expense.subcategory.strip()
            else None
        )
        dining = _is_dining(category, subcategory, event)
        large = amount >= self.large_expense_yen
        if not large and not dining:
            return
        merchant = (
            expense.merchant_normalized
            or expense.merchant_raw
            or _meal_merchant(event)
            or subcategory
            or category
        )
        reasons = tuple(
            reason
            for reason, present in (("大きな支出", large), ("外食", dining))
            if present
        )
        destination.append(
            NotableExpense(
                amount=amount,
                category=category,
                subcategory=subcategory,
                merchant=_clean_display_text(merchant, fallback=category, limit=80),
                reasons=reasons,
                event_id=event.id,
            )
        )

    @staticmethod
    def _monthly_budget(
        session: Session, *, month: str, spent: int
    ) -> MonthlyBudgetDigest | None:
        row = session.scalar(
            select(MonthlyBudget)
            .where(
                MonthlyBudget.month == month,
                MonthlyBudget.scope == OVERALL_SCOPE,
            )
            .order_by(MonthlyBudget.id)
        )
        if row is None or _valid_expense_amount(row.amount) is None:
            return None
        return MonthlyBudgetDigest(
            month=month,
            amount=row.amount,
            spent=spent,
            remaining=row.amount - spent,
        )

    @staticmethod
    def _account_digest_from_comparison(
        comparison: AccountSnapshotComparison,
    ) -> AccountDigest:
        digest = AccountDigest(
            id=comparison.account_id,
            name=comparison.account_name,
            kind=comparison.kind,
            currency=comparison.currency,
            balance=comparison.value,
            measure=comparison.measure,
            previous_local_date=comparison.previous_day_local_date,
            previous_value=comparison.previous_day_value,
            # The digest's delta is strictly the immediately preceding local
            # day.  ``comparison.delta`` remains the legacy previous-available
            # value exposed by FinanceService for compatibility callers.
            delta=comparison.previous_day_delta,
            source=comparison.source,
            observed_at=comparison.observed_at,
            settlement_account_name=comparison.settlement_account_name,
            snapshot_persisted=comparison.value is not None,
            previous_day_local_date=comparison.previous_day_local_date,
            previous_day_value=comparison.previous_day_value,
            previous_day_delta=comparison.previous_day_delta,
            previous_available_local_date=comparison.previous_local_date,
            previous_available_value=comparison.previous_value,
            previous_available_delta=comparison.delta,
        )
        return AccountDigest(
            id=digest.id,
            name=digest.name,
            kind=digest.kind,
            currency=digest.currency,
            balance=digest.balance,
            warnings=_account_warning(digest),
            measure=digest.measure,
            previous_local_date=digest.previous_local_date,
            previous_value=digest.previous_value,
            delta=digest.delta,
            source=digest.source,
            observed_at=digest.observed_at,
            settlement_account_name=digest.settlement_account_name,
            snapshot_persisted=digest.snapshot_persisted,
            previous_day_local_date=digest.previous_day_local_date,
            previous_day_value=digest.previous_day_value,
            previous_day_delta=digest.previous_day_delta,
            previous_available_local_date=digest.previous_available_local_date,
            previous_available_value=digest.previous_available_value,
            previous_available_delta=digest.previous_available_delta,
        )

    @staticmethod
    def _account_digest_from_balance(account: AccountBalance) -> AccountDigest:
        digest = AccountDigest(
            id=account.id,
            name=account.name,
            kind=account.kind,
            currency=account.currency,
            balance=account.balance,
            measure=(
                AccountSnapshotMeasure.OUTSTANDING
                if account.kind == AccountKind.CARD
                else AccountSnapshotMeasure.BALANCE
            ),
        )
        return AccountDigest(
            id=digest.id,
            name=digest.name,
            kind=digest.kind,
            currency=digest.currency,
            balance=digest.balance,
            warnings=_account_warning(digest),
            measure=digest.measure,
        )

    @staticmethod
    def _account_totals(
        accounts: tuple[AccountDigest, ...],
    ) -> tuple[int | None, int | None, int | None]:
        """Return complete asset/liability totals without zero-filling gaps."""

        asset_accounts = tuple(account for account in accounts if account.is_asset)
        liability_accounts = tuple(account for account in accounts if account.is_liability)

        if not asset_accounts and not liability_accounts:
            return None, None, None

        def complete_total(group: tuple[AccountDigest, ...]) -> int | None:
            # An absent account kind is a real zero.  A configured account
            # whose current value is unavailable remains unknown.
            if not group:
                return 0
            if any(account.balance is None for account in group):
                return None
            currencies = {account.currency.strip().upper() for account in group}
            if len(currencies) != 1:
                # Summing unlike currencies would fabricate a net worth.  The
                # current account creation path uses JPY, but keep this guard
                # for legacy/directly-created rows.
                return None
            return sum(account.balance for account in group if account.balance is not None)

        assets = complete_total(asset_accounts)
        liabilities = complete_total(liability_accounts)
        net_assets = (
            assets - liabilities
            if assets is not None and liabilities is not None
            else None
        )
        return assets, liabilities, net_assets

    def _active_account_snapshots(self, local_date: date) -> tuple[AccountDigest, ...]:
        finance = FinanceService(self.session_factory)
        if finance.has_daily_snapshot_history(local_date, timezone=self.timezone):
            comparisons = {
                comparison.account_id: comparison
                for comparison in finance.list_daily_snapshot_comparisons(
                    local_date,
                    timezone=self.timezone,
                )
            }
            return tuple(
                self._account_digest_from_comparison(comparisons[account.id])
                if account.id in comparisons
                else self._account_digest_from_balance(account)
                for account in finance.list_accounts()
                if account.active
            )

        # Before the first capture, retain the existing read-only preview
        # behavior used by slash commands and compatibility callers.
        account_snapshots = finance.list_accounts()
        active: list[AccountDigest] = []
        for account in account_snapshots:
            if not account.active:
                continue
            active.append(self._account_digest_from_balance(account))
        return tuple(active)

    @staticmethod
    def _snapshot_values(
        snapshot: DailyFinanceSnapshot,
    ) -> tuple[date, str, str]:
        if not isinstance(snapshot, DailyFinanceSnapshot):
            raise TypeError("snapshot must be a DailyFinanceSnapshot")
        return snapshot.local_date, snapshot.semantic_fingerprint, format_daily_digest(snapshot)

    def _row_for(
        self,
        session: Session,
        *,
        local_date: date,
        channel_id: str,
    ) -> FinanceDigestDeliveryRow | None:
        return session.scalar(
            select(FinanceDigestDeliveryRow).where(
                FinanceDigestDeliveryRow.local_date == local_date,
                FinanceDigestDeliveryRow.channel_id == channel_id,
            )
        )

    @staticmethod
    def _decision_for_row(
        row: FinanceDigestDeliveryRow | None,
        *,
        fingerprint: str,
        force: bool,
    ) -> DigestDeliveryDecision:
        if row is None:
            return DigestDeliveryDecision(DeliveryAction.CREATE, None, None, 0, None)
        state = _delivery_state(row)
        if not force and row.status == "posted" and row.semantic_fingerprint == fingerprint:
            return DigestDeliveryDecision(
                DeliveryAction.SKIP,
                row.id,
                row.discord_message_id,
                row.attempt_count,
                row.status,
                state,
            )
        # An active external operation must never be bypassed, even by a
        # manual force request.  Force is reserved for an already reconciled
        # terminal/unknown state, not for racing an in-flight create.
        if row.status == "posting":
            return DigestDeliveryDecision(
                DeliveryAction.IN_PROGRESS,
                row.id,
                row.discord_message_id,
                row.attempt_count,
                row.status,
                state,
            )
        if not force and row.status == "unknown":
            if row.discord_message_id:
                # An ambiguous edit still has a concrete Bot message target;
                # retrying that edit cannot create a duplicate message.
                return DigestDeliveryDecision(
                    DeliveryAction.EDIT,
                    row.id,
                    row.discord_message_id,
                    row.attempt_count,
                    row.status,
                    state,
                )
            return DigestDeliveryDecision(
                DeliveryAction.BLOCKED,
                row.id,
                row.discord_message_id,
                row.attempt_count,
                row.status,
                state,
            )
        action = DeliveryAction.EDIT if row.discord_message_id else DeliveryAction.CREATE
        return DigestDeliveryDecision(
            action,
            row.id,
            row.discord_message_id,
            row.attempt_count,
            row.status,
            state,
        )

    def decide_delivery(
        self,
        snapshot: DailyFinanceSnapshot,
        channel_id: str | int,
        *,
        force: bool = False,
    ) -> DigestDeliveryDecision:
        """Read the create/edit/skip decision without claiming or mutating."""

        local_date, fingerprint, _ = self._snapshot_values(snapshot)
        normalized_channel_id = _normalised_channel_id(channel_id)
        session = self.session_factory()
        try:
            row = self._row_for(
                session,
                local_date=local_date,
                channel_id=normalized_channel_id,
            )
            return self._decision_for_row(row, fingerprint=fingerprint, force=force)
        finally:
            session.close()

    decide = decide_delivery

    def prepare_delivery(
        self,
        snapshot: DailyFinanceSnapshot,
        channel_id: str | int,
        *,
        now: datetime | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> DigestDeliveryPlan:
        """Claim one create/edit operation, or return a safe no-op decision.

        A dry-run reads existing state and returns the same action/content but
        never inserts, updates, or increments an attempt count.
        """

        local_date, fingerprint, content = self._snapshot_values(snapshot)
        normalized_channel_id = _normalised_channel_id(channel_id)
        if dry_run:
            decision = self.decide_delivery(snapshot, normalized_channel_id, force=force)
            return DigestDeliveryPlan(
                action=decision.action,
                delivery_id=decision.delivery_id,
                local_date=local_date,
                channel_id=normalized_channel_id,
                semantic_fingerprint=fingerprint,
                content=content,
                message_id=decision.message_id,
                attempt_count=decision.attempt_count,
                claimed=False,
                dry_run=True,
                state=decision.state,
            )

        changed_at = _utc_naive(now)
        # A normal (non-dry-run) prepare is also a safe restart boundary.  It
        # never treats age as evidence of success: it only releases stale
        # known-message edits for retry and protects ambiguous new creates.
        self.recover_stale_postings(
            now=changed_at,
            stale_after_seconds=self.stale_after_seconds,
        )
        for _ in range(5):
            session = self.session_factory()
            try:
                row = self._row_for(
                    session,
                    local_date=local_date,
                    channel_id=normalized_channel_id,
                )
                decision = self._decision_for_row(row, fingerprint=fingerprint, force=force)
                if row is None:
                    row = FinanceDigestDeliveryRow(
                        local_date=local_date,
                        channel_id=normalized_channel_id,
                        status="posting",
                        attempt_count=1,
                        semantic_fingerprint=fingerprint,
                        updated_at=changed_at,
                    )
                    session.add(row)
                    try:
                        session.commit()
                    except IntegrityError:
                        session.rollback()
                        continue
                    return DigestDeliveryPlan(
                        action=DeliveryAction.CREATE,
                        delivery_id=row.id,
                        local_date=local_date,
                        channel_id=normalized_channel_id,
                        semantic_fingerprint=fingerprint,
                        content=content,
                        message_id=None,
                        attempt_count=1,
                        claimed=True,
                        dry_run=False,
                        state=_delivery_state(row),
                    )

                if decision.action in {
                    DeliveryAction.SKIP,
                    DeliveryAction.IN_PROGRESS,
                    DeliveryAction.BLOCKED,
                }:
                    return DigestDeliveryPlan(
                        action=decision.action,
                        delivery_id=decision.delivery_id,
                        local_date=local_date,
                        channel_id=normalized_channel_id,
                        semantic_fingerprint=fingerprint,
                        content=content,
                        message_id=decision.message_id,
                        attempt_count=decision.attempt_count,
                        claimed=False,
                        dry_run=False,
                        state=decision.state,
                    )

                expected_status = row.status
                expected_attempt_count = row.attempt_count
                next_attempt_count = expected_attempt_count + 1
                result = session.execute(
                    update(FinanceDigestDeliveryRow)
                    .where(
                        FinanceDigestDeliveryRow.id == row.id,
                        FinanceDigestDeliveryRow.status == expected_status,
                        FinanceDigestDeliveryRow.attempt_count == expected_attempt_count,
                    )
                    .values(
                        status="posting",
                        attempt_count=next_attempt_count,
                        semantic_fingerprint=fingerprint,
                        last_error_code=None,
                        last_error=None,
                        updated_at=changed_at,
                    )
                )
                if result.rowcount != 1:
                    session.rollback()
                    continue
                session.commit()
                row.status = "posting"
                row.attempt_count = next_attempt_count
                row.semantic_fingerprint = fingerprint
                row.last_error_code = None
                row.last_error = None
                row.updated_at = changed_at
                action = (
                    DeliveryAction.EDIT
                    if row.discord_message_id
                    else DeliveryAction.CREATE
                )
                return DigestDeliveryPlan(
                    action=action,
                    delivery_id=row.id,
                    local_date=local_date,
                    channel_id=normalized_channel_id,
                    semantic_fingerprint=fingerprint,
                    content=content,
                    message_id=row.discord_message_id,
                    attempt_count=next_attempt_count,
                    claimed=True,
                    dry_run=False,
                    state=_delivery_state(row),
                )
            finally:
                session.close()
        raise DeliveryClaimConflictError("daily digest delivery claim contention")

    claim_or_prepare = prepare_delivery
    claim_delivery = prepare_delivery
    prepare = prepare_delivery

    def get_delivery(
        self, local_date: date | datetime, channel_id: str | int
    ) -> DigestDeliveryState | None:
        selected_date = _normalise_local_date(local_date, self.timezone)
        normalized_channel_id = _normalised_channel_id(channel_id)
        session = self.session_factory()
        try:
            row = self._row_for(
                session,
                local_date=selected_date,
                channel_id=normalized_channel_id,
            )
            return _delivery_state(row) if row is not None else None
        finally:
            session.close()

    delivery_state = get_delivery

    @staticmethod
    def _delivery_id_and_attempt(
        delivery: DigestDeliveryPlan | int | None,
        *,
        delivery_id: int | None,
        attempt_count: int | None,
        claim_attempt_count: int | None,
    ) -> tuple[int, int]:
        if delivery is not None and delivery_id is not None:
            embedded_id = (
                delivery.delivery_id
                if isinstance(delivery, DigestDeliveryPlan)
                else delivery
            )
            if embedded_id != delivery_id:
                raise ValueError("delivery and delivery_id must match")
        resolved_delivery_id = delivery_id if delivery_id is not None else delivery
        if attempt_count is not None and claim_attempt_count is not None:
            if attempt_count != claim_attempt_count:
                raise ValueError("attempt_count and claim_attempt_count must match")
        resolved_attempt = (
            claim_attempt_count
            if claim_attempt_count is not None
            else attempt_count
        )
        if isinstance(delivery, DigestDeliveryPlan):
            resolved_delivery_id = delivery.delivery_id
            if resolved_attempt is None:
                resolved_attempt = delivery.attempt_count
        if type(resolved_delivery_id) is not int or resolved_delivery_id <= 0:
            raise ValueError("delivery_id must be a positive integer")
        if type(resolved_attempt) is not int or resolved_attempt <= 0:
            raise ValueError("attempt_count must be a positive integer")
        return resolved_delivery_id, resolved_attempt

    def recover_stale_postings(
        self,
        *,
        now: datetime | None = None,
        stale_after_seconds: int | float | timedelta | None = None,
        stale_after: int | float | timedelta | None = None,
        claim_timeout_seconds: int | float | timedelta | None = None,
    ) -> tuple[DigestDeliveryState, ...]:
        """Safely release old in-flight claims after a process restart.

        Age is used only as a protection threshold; it is never evidence that
        Discord accepted a message.  A claim with a known message ID was an
        edit of a Bot message and can safely become ``failed`` for an idempotent
        edit retry.  A claim without a message ID was a new create whose
        external outcome is ambiguous, so it becomes ``unknown`` and remains
        blocked until the parent explicitly reconciles or forces a retry.
        """

        provided_thresholds = [
            value
            for value in (stale_after_seconds, stale_after, claim_timeout_seconds)
            if value is not None
        ]
        if len(provided_thresholds) > 1:
            raise ValueError(
                "pass only one of stale_after_seconds, stale_after, or claim_timeout_seconds"
            )
        threshold = (
            provided_thresholds[0]
            if provided_thresholds
            else self.stale_after_seconds
        )
        if isinstance(threshold, timedelta):
            threshold_seconds = threshold.total_seconds()
        elif isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError("stale threshold must be a positive number or timedelta")
        else:
            threshold_seconds = float(threshold)
        if not math.isfinite(threshold_seconds) or threshold_seconds <= 0:
            raise ValueError("stale threshold must be a positive finite duration")

        changed_at = _utc_naive(now)
        cutoff = changed_at - timedelta(seconds=threshold_seconds)
        session = self.session_factory()
        recovered_ids: list[int] = []
        try:
            rows = list(
                session.scalars(
                    select(FinanceDigestDeliveryRow)
                    .where(
                        FinanceDigestDeliveryRow.status == "posting",
                        FinanceDigestDeliveryRow.updated_at <= cutoff,
                    )
                    .order_by(FinanceDigestDeliveryRow.id)
                ).all()
            )
            for row in rows:
                known_message = row.discord_message_id is not None
                status = "failed" if known_message else "unknown"
                error_code = STALE_EDIT_ERROR_CODE if known_message else STALE_CREATE_ERROR_CODE
                error_message = (
                    "編集 claim が古いため再試行可能な失敗として戻しました。"
                    if known_message
                    else "新規送信の結果を確認できないため保護しました。"
                    "Discord確認後に明示的に再試行してください"
                )
                result = session.execute(
                    update(FinanceDigestDeliveryRow)
                    .where(
                        FinanceDigestDeliveryRow.id == row.id,
                        FinanceDigestDeliveryRow.status == "posting",
                        FinanceDigestDeliveryRow.attempt_count == row.attempt_count,
                        FinanceDigestDeliveryRow.updated_at <= cutoff,
                    )
                    .values(
                        status=status,
                        last_error_code=error_code,
                        last_error=error_message,
                        updated_at=changed_at,
                    )
                )
                if result.rowcount == 1:
                    recovered_ids.append(row.id)
            session.commit()
            if not recovered_ids:
                return ()
            recovered_rows = list(
                session.scalars(
                    select(FinanceDigestDeliveryRow)
                    .where(FinanceDigestDeliveryRow.id.in_(recovered_ids))
                    .order_by(FinanceDigestDeliveryRow.id)
                ).all()
            )
            return tuple(_delivery_state(row) for row in recovered_rows)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    recover_stale_deliveries = recover_stale_postings
    recover_posting_claims = recover_stale_postings

    def mark_replacement_create_started(
        self,
        delivery: DigestDeliveryPlan | int | None = None,
        *,
        delivery_id: int | None = None,
        attempt_count: int | None = None,
        claim_attempt_count: int | None = None,
        now: datetime | None = None,
    ) -> DigestDeliveryState:
        """Durably leave a deleted edit target before creating a replacement.

        Discord has already confirmed that the prior Bot message does not
        exist.  Clearing its ID *before* the non-idempotent create means a
        process crash during or immediately after that create is recovered as
        an ambiguous create, never as a retryable edit of the deleted target.
        """

        resolved_delivery_id, expected_attempt = self._delivery_id_and_attempt(
            delivery,
            delivery_id=delivery_id,
            attempt_count=attempt_count,
            claim_attempt_count=claim_attempt_count,
        )
        changed_at = _utc_naive(now)
        session = self.session_factory()
        try:
            result = session.execute(
                update(FinanceDigestDeliveryRow)
                .where(
                    FinanceDigestDeliveryRow.id == resolved_delivery_id,
                    FinanceDigestDeliveryRow.status == "posting",
                    FinanceDigestDeliveryRow.attempt_count == expected_attempt,
                    FinanceDigestDeliveryRow.discord_message_id.is_not(None),
                )
                .values(
                    discord_message_id=None,
                    posted_at=None,
                    last_error_code=None,
                    last_error=None,
                    updated_at=changed_at,
                )
            )
            if result.rowcount != 1:
                raise DeliveryClaimConflictError(
                    "replacement create requires the active deleted-message claim"
                )
            session.commit()
            row = session.get(FinanceDigestDeliveryRow, resolved_delivery_id)
            if row is None:
                raise DeliveryClaimConflictError("daily digest delivery disappeared")
            return _delivery_state(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    begin_replacement_create = mark_replacement_create_started

    def mark_sent(
        self,
        delivery: DigestDeliveryPlan | int | None = None,
        message_id: str | int | None = None,
        *,
        delivery_id: int | None = None,
        discord_message_id: str | int | None = None,
        attempt_count: int | None = None,
        claim_attempt_count: int | None = None,
        posted_at: datetime | None = None,
    ) -> DigestDeliveryState:
        """Finalize a claimed create/edit as ``posted`` after Discord success."""

        if message_id is not None and discord_message_id is not None:
            if str(message_id).strip() != str(discord_message_id).strip():
                raise ValueError("message_id and discord_message_id must match")
        resolved_message_id = discord_message_id if discord_message_id is not None else message_id
        if resolved_message_id is None:
            raise ValueError("discord_message_id is required")
        normalized_message_id = _normalised_message_id(resolved_message_id)
        delivery_id, expected_attempt = self._delivery_id_and_attempt(
            delivery,
            delivery_id=delivery_id,
            attempt_count=attempt_count,
            claim_attempt_count=claim_attempt_count,
        )
        changed_at = _utc_naive(posted_at)
        session = self.session_factory()
        try:
            result = session.execute(
                update(FinanceDigestDeliveryRow)
                .where(
                    FinanceDigestDeliveryRow.id == delivery_id,
                    FinanceDigestDeliveryRow.status == "posting",
                    FinanceDigestDeliveryRow.attempt_count == expected_attempt,
                )
                .values(
                    status="posted",
                    discord_message_id=normalized_message_id,
                    last_error_code=None,
                    last_error=None,
                    posted_at=changed_at,
                    updated_at=changed_at,
                )
            )
            if result.rowcount != 1:
                raise DeliveryClaimConflictError("stale daily digest delivery claim")
            session.commit()
            row = session.get(FinanceDigestDeliveryRow, delivery_id)
            if row is None:
                raise DeliveryClaimConflictError("daily digest delivery disappeared")
            return _delivery_state(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    mark_delivery_sent = mark_sent
    mark_delivery_succeeded = mark_sent

    def mark_unknown(
        self,
        delivery: DigestDeliveryPlan | int | None = None,
        error: object | None = None,
        *,
        delivery_id: int | None = None,
        safe_message: object | None = None,
        last_error: object | None = None,
        error_code: object | None = None,
        attempt_count: int | None = None,
        claim_attempt_count: int | None = None,
        now: datetime | None = None,
        clear_message_target: bool = False,
    ) -> DigestDeliveryState:
        """Record an ambiguous Discord outcome without marking it sent.

        The existing message ID is preserved by default.  A replacement
        create attempted after Discord reported that target missing passes
        ``clear_message_target=True`` so an ambiguous result cannot be retried
        through the deleted ID.  A create with no known ID remains blocked
        until a parent has checked Discord and explicitly reconciled it.
        """

        resolved_delivery_id, expected_attempt = self._delivery_id_and_attempt(
            delivery,
            delivery_id=delivery_id,
            attempt_count=attempt_count,
            claim_attempt_count=claim_attempt_count,
        )
        resolved_error = (
            safe_message
            if safe_message is not None
            else last_error
            if last_error is not None
            else error
        )
        normalized_error = _safe_error_text(resolved_error) or (
            "Discord送信結果を確認できないため保護しました。"
        )
        normalized_code = _safe_error_text(error_code, limit=64) or UNKNOWN_DELIVERY_ERROR_CODE
        changed_at = _utc_naive(now)
        values: dict[str, object | None] = {
            "status": "unknown",
            "last_error_code": normalized_code,
            "last_error": normalized_error,
            "updated_at": changed_at,
        }
        if clear_message_target:
            values["discord_message_id"] = None
            values["posted_at"] = None
        session = self.session_factory()
        try:
            result = session.execute(
                update(FinanceDigestDeliveryRow)
                .where(
                    FinanceDigestDeliveryRow.id == resolved_delivery_id,
                    FinanceDigestDeliveryRow.status == "posting",
                    FinanceDigestDeliveryRow.attempt_count == expected_attempt,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise DeliveryClaimConflictError("stale daily digest delivery claim")
            session.commit()
            row = session.get(FinanceDigestDeliveryRow, resolved_delivery_id)
            if row is None:
                raise DeliveryClaimConflictError("daily digest delivery disappeared")
            return _delivery_state(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    mark_delivery_unknown = mark_unknown

    def mark_failed(
        self,
        delivery: DigestDeliveryPlan | int | None = None,
        error: object | None = None,
        *,
        delivery_id: int | None = None,
        safe_message: object | None = None,
        last_error: object | None = None,
        error_code: object | None = None,
        attempt_count: int | None = None,
        claim_attempt_count: int | None = None,
        now: datetime | None = None,
        clear_message_target: bool = False,
    ) -> DigestDeliveryState:
        """Record a safe failure while keeping the claim retryable.

        ``clear_message_target`` is used only after Discord has definitively
        reported the old Bot message missing and a replacement create failed.
        The next safe retry is then a create, not another edit of a deleted ID.
        """

        delivery_id, expected_attempt = self._delivery_id_and_attempt(
            delivery,
            delivery_id=delivery_id,
            attempt_count=attempt_count,
            claim_attempt_count=claim_attempt_count,
        )
        resolved_error = (
            safe_message
            if safe_message is not None
            else last_error
            if last_error is not None
            else error
        )
        changed_at = _utc_naive(now)
        values: dict[str, object | None] = {
            "status": "failed",
            "last_error_code": _safe_error_text(error_code, limit=64),
            "last_error": _safe_error_text(resolved_error),
            "updated_at": changed_at,
        }
        if clear_message_target:
            values["discord_message_id"] = None
            values["posted_at"] = None
        session = self.session_factory()
        try:
            result = session.execute(
                update(FinanceDigestDeliveryRow)
                .where(
                    FinanceDigestDeliveryRow.id == delivery_id,
                    FinanceDigestDeliveryRow.status == "posting",
                    FinanceDigestDeliveryRow.attempt_count == expected_attempt,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise DeliveryClaimConflictError("stale daily digest delivery claim")
            session.commit()
            row = session.get(FinanceDigestDeliveryRow, delivery_id)
            if row is None:
                raise DeliveryClaimConflictError("daily digest delivery disappeared")
            return _delivery_state(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    mark_delivery_failed = mark_failed


# Public formatter aliases that read naturally at integration boundaries.
format_snapshot = format_daily_digest
format_digest = format_daily_digest


__all__ = [
    "AccountDigest",
    "BudgetDigest",
    "CategoryTotal",
    "DEFAULT_LARGE_EXPENSE_YEN",
    "DEFAULT_STALE_AFTER_SECONDS",
    "DEFAULT_TIMEZONE",
    "DISCORD_MAX_CONTENT_LENGTH",
    "DELIVERY_KIND",
    "DailyFinanceSnapshot",
    "DeliveryAction",
    "DeliveryClaimConflictError",
    "DigestBase",
    "DigestDeliveryDecision",
    "DigestDeliveryPlan",
    "DigestDeliveryState",
    "FinanceDigestDelivery",
    "FinanceDigestDeliveryRow",
    "FinanceDigestService",
    "MonthlyBudgetDigest",
    "NotableExpense",
    "STALE_CREATE_ERROR_CODE",
    "STALE_EDIT_ERROR_CODE",
    "UNKNOWN_DELIVERY_ERROR_CODE",
    "format_daily_digest",
    "format_digest",
    "format_finance_digest",
    "format_snapshot",
    "init_storage",
    "snapshot_fingerprint",
]
