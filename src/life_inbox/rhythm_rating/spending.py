"""Pure Japan-time, yen-only daily spending summaries."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .normalization import ensure_aware_datetime, normalize_text, utc_now

JAPAN_TIMEZONE = ZoneInfo("Asia/Tokyo")


def _timezone(value: str | tzinfo) -> tzinfo:
    if isinstance(value, str):
        try:
            return ZoneInfo(value.strip())
        except (TypeError, ZoneInfoNotFoundError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
    if isinstance(value, tzinfo):
        return value
    raise ValueError("timezone must be a valid timezone")


def japan_calendar_date(moment: datetime | str) -> date:
    """Return the calendar date containing an aware instant in Japan time."""

    return ensure_aware_datetime(moment, field_name="moment").astimezone(JAPAN_TIMEZONE).date()


def _amount(value: int, *, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _category_totals(
    value: Iterable[tuple[str, int]] | Mapping[str, int],
) -> tuple[tuple[str, int], ...]:
    values = value.items() if isinstance(value, Mapping) else value
    totals: dict[str, int] = {}
    try:
        for category, amount in values:
            name = normalize_text(category, field_name="category")
            totals[name] = totals.get(name, 0) + _amount(amount, field_name="category amount")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("category_totals must contain (category, amount) pairs") from exc
    return tuple(sorted(totals.items(), key=lambda item: item[0].casefold()))


@dataclass(frozen=True, slots=True)
class DailySpendingSummary:
    """Detached daily spending totals expressed only in Japanese yen."""

    day: date
    total_yen: int
    expense_count: int = 0
    category_totals: Iterable[tuple[str, int]] | Mapping[str, int] = ()
    timezone_name: str = "Asia/Tokyo"
    generated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if isinstance(self.day, datetime) or not isinstance(self.day, date):
            raise ValueError("day must be a date")
        if type(self.expense_count) is not int or self.expense_count < 0:
            raise ValueError("expense_count must be a non-negative integer")
        timezone_name = normalize_text(self.timezone_name, field_name="timezone_name")
        _timezone(timezone_name)
        object.__setattr__(self, "total_yen", _amount(self.total_yen, field_name="total_yen"))
        object.__setattr__(
            self,
            "category_totals",
            _category_totals(self.category_totals),
        )
        object.__setattr__(self, "timezone_name", timezone_name)
        object.__setattr__(
            self,
            "generated_at",
            ensure_aware_datetime(self.generated_at, field_name="generated_at"),
        )

    @classmethod
    def for_moment(
        cls,
        moment: datetime | str,
        *,
        total_yen: int,
        expense_count: int = 0,
        category_totals: Iterable[tuple[str, int]] | Mapping[str, int] = (),
    ) -> DailySpendingSummary:
        """Build a summary whose day is derived from the Japan-time boundary."""

        return cls(
            day=japan_calendar_date(moment),
            total_yen=total_yen,
            expense_count=expense_count,
            category_totals=category_totals,
        )


SpendingSummary = DailySpendingSummary


def _excluded_category(category: str) -> bool:
    """Keep provider-specific machine labels out of this summary surface."""

    key = category.casefold().replace(" ", "")
    return any(token in key for token in ("自販機", "自動販売機", "vendingmachine"))


def format_daily_spending_message(summary: DailySpendingSummary) -> str:
    """Format a compact, yen-only daily message for Discord or another UI."""

    if not isinstance(summary, DailySpendingSummary):
        raise TypeError("summary must be a DailySpendingSummary")
    lines = [
        f"{summary.day.year}年{summary.day.month}月{summary.day.day}日の支出",
        f"合計: {summary.total_yen:,}円（{summary.expense_count}件）",
    ]
    visible_categories = [
        (category, amount)
        for category, amount in summary.category_totals
        if not _excluded_category(category)
    ]
    lines.extend(f"{category}: {amount:,}円" for category, amount in visible_categories)
    return "\n".join(lines)


format_daily_spending = format_daily_spending_message
