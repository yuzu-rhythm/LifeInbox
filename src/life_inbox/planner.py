"""Compact, handwriting-friendly summaries for confirmed events."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from .constants import DEFAULT_CATEGORIES, MealType
from .models import Event


def local_datetime(value: datetime, timezone_name: str = "Asia/Tokyo") -> datetime:
    tz = ZoneInfo(timezone_name)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(tz)


def local_date(value: datetime, timezone_name: str = "Asia/Tokyo") -> date:
    return local_datetime(value, timezone_name).date()


def events_on_date(
    events: list[Event], target_date: date, *, timezone_name: str = "Asia/Tokyo"
) -> list[Event]:
    return [
        event
        for event in events
        if event.status == "confirmed"
        and local_date(event.occurred_at, timezone_name) == target_date
    ]


def _yen(value: int | None) -> str:
    return f"{value:,}" if value is not None else "金額不明"


def _meal_label(meal_type: str) -> str:
    return {
        MealType.BREAKFAST.value: "朝",
        MealType.LUNCH.value: "昼",
        MealType.DINNER.value: "夜",
        MealType.SNACK.value: "おやつ",
    }.get(meal_type, "不明")


def render_planner(
    events: list[Event],
    target_date: date,
    *,
    timezone_name: str = "Asia/Tokyo",
    categories: Iterable[str] = DEFAULT_CATEGORIES,
) -> str:
    """Render only confirmed events for one local calendar date."""

    selected = events_on_date(events, target_date, timezone_name=timezone_name)
    by_category: dict[str, list[tuple[str, int | None]]] = defaultdict(list)
    total = 0
    meals: dict[str, list[str]] = {key: [] for key in ("朝", "昼", "夜", "おやつ", "不明")}

    for event in selected:
        for expense in event.expenses:
            label = (
                expense.merchant_normalized
                or expense.merchant_raw
                or expense.description
                or "未分類"
            )
            by_category[expense.category or "未分類"].append((label, expense.amount))
            if expense.amount is not None:
                total += expense.amount
        for meal in event.meals:
            label = " / ".join(
                part
                for part in (meal.restaurant_normalized or meal.restaurant_raw, meal.description)
                if part
            )
            meals[_meal_label(meal.meal_type)].append(label or "記録あり")

    lines = [target_date.strftime("%Y/%m/%d"), ""]
    for category in categories:
        entries = by_category.get(category)
        if not entries:
            continue
        lines.append(f"【{category}】")
        for label, amount in entries:
            lines.append(f"{label:<16}{_yen(amount):>8}")
        lines.append("")

    lines.extend(["----------------------", f"合計             {_yen(total)}円", "", "【食事】"])
    for meal_key in ("朝", "昼", "夜", "おやつ", "不明"):
        value = " / ".join(meals[meal_key])
        lines.append(f"{meal_key}：{value}")
    return "\n".join(lines).rstrip()


def render_today(
    events: list[Event],
    target_date: date,
    *,
    timezone_name: str = "Asia/Tokyo",
    categories: Iterable[str] = DEFAULT_CATEGORIES,
) -> str:
    selected = events_on_date(events, target_date, timezone_name=timezone_name)
    if not selected:
        return f"{target_date:%Y/%m/%d} の確定記録はありません。"
    return render_planner(
        selected,
        target_date,
        timezone_name=timezone_name,
        categories=categories,
    )


def render_month(
    events: list[Event],
    year: int,
    month: int,
    *,
    timezone_name: str = "Asia/Tokyo",
    categories: Iterable[str] = DEFAULT_CATEGORIES,
) -> str:
    selected = [
        event
        for event in events
        if event.status == "confirmed"
        and (
            local_date(event.occurred_at, timezone_name).year,
            local_date(event.occurred_at, timezone_name).month,
        )
        == (year, month)
    ]
    totals: dict[str, int] = defaultdict(int)
    total = 0
    for event in selected:
        for expense in event.expenses:
            if expense.amount is not None:
                totals[expense.category or "未分類"] += expense.amount
                total += expense.amount
    lines = [f"{year:04d}/{month:02d} 支出概要"]
    for category in categories:
        if totals.get(category):
            lines.append(f"{category}: {totals[category]:,}円")
    lines.append(f"合計: {total:,}円")
    return "\n".join(lines)


def _inbox_label(event: Event) -> str:
    parts: list[str] = []
    for expense in event.expenses:
        merchant = expense.merchant_normalized or expense.merchant_raw or "支出"
        parts.append(f"{_yen(expense.amount)}円 {merchant}")
    for meal in event.meals:
        parts.append(
            meal.description or meal.restaurant_normalized or meal.restaurant_raw or "食事"
        )
    if not parts:
        parts.append(event.raw_text.strip() or "未解析")
    return " / ".join(parts)


def render_inbox(events: list[Event], *, timezone_name: str = "Asia/Tokyo") -> str:
    pending = [event for event in events if event.status == "pending"]
    if not pending:
        return "未整理 0件"
    lines = [f"未整理 {len(pending)}件"]
    for event in pending:
        timestamp = local_datetime(event.occurred_at, timezone_name).strftime("%H:%M")
        lines.append(f"{timestamp}  {_inbox_label(event)} [要確認] #{event.id}")
    return "\n".join(lines)
