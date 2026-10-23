"""Monthly budget persistence and confirmed-expense progress calculations."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .constants import ABSOLUTE_MAX_EXPENSE_YEN, DEFAULT_CATEGORIES, EventStatus
from .models import Event, Expense, MonthlyBudget

OVERALL_SCOPE = "overall"
CATEGORY_SCOPE = "category"
OVERALL_CATEGORY_KEY = MonthlyBudget.OVERALL_CATEGORY_KEY


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _zone_info(value: ZoneInfo | str | tzinfo) -> tzinfo:
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


@dataclass(frozen=True, slots=True)
class BudgetProgress:
    """Detached progress for one overall or category budget."""

    month: str
    category: str | None
    amount: int
    spent: int
    remaining: int
    percent: float
    visual_percent: float

    @property
    def limit(self) -> int:
        return self.amount

    @property
    def budget_amount(self) -> int:
        return self.amount

    @property
    def spent_amount(self) -> int:
        return self.spent

    @property
    def remaining_amount(self) -> int:
        return self.remaining

    @property
    def over_amount(self) -> int:
        return max(-self.remaining, 0)

    @property
    def is_over_budget(self) -> bool:
        return self.remaining < 0


@dataclass(frozen=True, slots=True)
class BudgetMonthView:
    """Detached monthly budget snapshot."""

    month: str
    overall: BudgetProgress | None
    categories: tuple[BudgetProgress, ...]

    @property
    def overall_budget(self) -> BudgetProgress | None:
        return self.overall

    @property
    def category_budgets(self) -> tuple[BudgetProgress, ...]:
        return self.categories

    @property
    def category_progress(self) -> tuple[BudgetProgress, ...]:
        return self.categories

    @property
    def has_budget(self) -> bool:
        return self.overall is not None or bool(self.categories)


# The longer name reads well at integration boundaries while the shorter
# alias keeps the service API convenient for callers.
BudgetMonth = BudgetMonthView


class BudgetService:
    """Manage monthly budgets using only local SQLAlchemy and Expense rows."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        timezone: ZoneInfo | str | tzinfo = "Asia/Tokyo",
        allowed_categories: Iterable[str] = DEFAULT_CATEGORIES,
    ) -> None:
        self.session_factory = session_factory
        self.timezone = _zone_info(timezone)
        normalized_categories = tuple(
            self._normalize_category_name(category, allow_reserved=False)
            for category in allowed_categories
        )
        if not normalized_categories:
            raise ValueError("allowed_categories must contain at least one category")
        if len(normalized_categories) != len(set(normalized_categories)):
            raise ValueError("allowed_categories must not contain duplicates")
        self.allowed_categories = normalized_categories
        self._allowed_category_set = frozenset(normalized_categories)

    @staticmethod
    def _normalize_category_name(value: str, *, allow_reserved: bool) -> str:
        if not isinstance(value, str):
            raise ValueError("category must be a string")
        category = value.strip()
        if not category:
            raise ValueError("category must not be blank")
        if len(category) > 64:
            raise ValueError("category is too long")
        if not allow_reserved and category == OVERALL_CATEGORY_KEY:
            raise ValueError("category is reserved")
        return category

    def _category(self, value: str | None) -> str | None:
        if value is None:
            return None
        category = self._normalize_category_name(value, allow_reserved=False)
        if category not in self._allowed_category_set:
            raise ValueError("category is not allowed")
        return category

    @staticmethod
    def _month(value: str | None, timezone: tzinfo) -> str:
        if value is None:
            today = datetime.now(timezone).date()
            return f"{today.year:04d}-{today.month:02d}"
        if not isinstance(value, str):
            raise ValueError("month must be YYYY-MM")
        text = value.strip()
        if (
            len(text) != 7
            or text[4] != "-"
            or not all("0" <= character <= "9" for character in text[:4])
            or not all("0" <= character <= "9" for character in text[5:])
        ):
            raise ValueError("month must be YYYY-MM")
        try:
            parsed = date(int(text[:4]), int(text[5:]), 1)
        except ValueError:
            raise ValueError("month must be YYYY-MM") from None
        return f"{parsed.year:04d}-{parsed.month:02d}"

    @staticmethod
    def _amount(value: int) -> int:
        if type(value) is not int or not 0 < value <= ABSOLUTE_MAX_EXPENSE_YEN:
            raise ValueError(
                f"amount must be an integer between 1 and {ABSOLUTE_MAX_EXPENSE_YEN:,}"
            )
        return value

    def _month_bounds(self, month: str) -> tuple[datetime, datetime]:
        parsed = date(int(month[:4]), int(month[5:]), 1)
        if parsed.month == 12:
            next_month = date(parsed.year + 1, 1, 1)
        else:
            next_month = date(parsed.year, parsed.month + 1, 1)
        start_local = datetime.combine(parsed, time.min, tzinfo=self.timezone)
        end_local = datetime.combine(next_month, time.min, tzinfo=self.timezone)
        return (
            start_local.astimezone(UTC).replace(tzinfo=None),
            end_local.astimezone(UTC).replace(tzinfo=None),
        )

    def _spending_by_category(
        self,
        session: Session,
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> dict[str, int]:
        statement = (
            select(
                Expense.category,
                func.coalesce(func.sum(Expense.amount), 0),
            )
            .join(Event, Expense.event_id == Event.id)
            .where(
                Event.status == EventStatus.CONFIRMED.value,
                Event.occurred_at >= start_utc,
                Event.occurred_at < end_utc,
                Expense.amount.is_not(None),
            )
            .group_by(Expense.category)
        )
        return {
            (category or "未分類"): int(total or 0)
            for category, total in session.execute(statement).all()
        }

    @staticmethod
    def _progress(
        *,
        month: str,
        category: str | None,
        amount: int,
        spent: int,
    ) -> BudgetProgress:
        percent = spent * 100 / amount
        return BudgetProgress(
            month=month,
            category=category,
            amount=amount,
            spent=spent,
            remaining=amount - spent,
            percent=percent,
            visual_percent=min(max(percent, 0.0), 100.0),
        )

    def set_budget(
        self,
        amount: int,
        month: str | None = None,
        category: str | None = None,
    ) -> BudgetProgress:
        """Create or update one month/scope budget and return its progress."""

        normalized_month = self._month(month, self.timezone)
        normalized_amount = self._amount(amount)
        normalized_category = self._category(category)
        scope = CATEGORY_SCOPE if normalized_category is not None else OVERALL_SCOPE
        category_key = normalized_category or OVERALL_CATEGORY_KEY

        session = self.session_factory()
        try:
            budget = session.scalar(
                select(MonthlyBudget).where(
                    MonthlyBudget.month == normalized_month,
                    MonthlyBudget.scope == scope,
                    MonthlyBudget.category_key == category_key,
                )
            )
            if budget is None:
                budget = MonthlyBudget(
                    month=normalized_month,
                    scope=scope,
                    category_key=category_key,
                    amount=normalized_amount,
                )
                session.add(budget)
            else:
                budget.amount = normalized_amount
                budget.updated_at = _utc_now()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        snapshot = self.get_month(normalized_month)
        progress = snapshot.overall if normalized_category is None else next(
            item for item in snapshot.categories if item.category == normalized_category
        )
        return progress

    def get_month(self, month: str | None = None) -> BudgetMonthView:
        """Return detached overall/category progress for one local calendar month."""

        normalized_month = self._month(month, self.timezone)
        start_utc, end_utc = self._month_bounds(normalized_month)
        session = self.session_factory()
        try:
            budgets = list(
                session.scalars(
                    select(MonthlyBudget)
                    .where(MonthlyBudget.month == normalized_month)
                    .order_by(MonthlyBudget.scope, MonthlyBudget.category_key, MonthlyBudget.id)
                ).all()
            )
            spent_by_category = self._spending_by_category(
                session,
                start_utc=start_utc,
                end_utc=end_utc,
            )
            overall_spent = sum(spent_by_category.values())
            overall_budget = next(
                (budget for budget in budgets if budget.scope == OVERALL_SCOPE),
                None,
            )
            overall = (
                self._progress(
                    month=normalized_month,
                    category=None,
                    amount=overall_budget.amount,
                    spent=overall_spent,
                )
                if overall_budget is not None
                else None
            )
            categories = tuple(
                self._progress(
                    month=normalized_month,
                    category=budget.category_key,
                    amount=budget.amount,
                    spent=spent_by_category.get(budget.category_key, 0),
                )
                for budget in budgets
                if budget.scope == CATEGORY_SCOPE
            )
            return BudgetMonthView(
                month=normalized_month,
                overall=overall,
                categories=categories,
            )
        finally:
            session.close()
