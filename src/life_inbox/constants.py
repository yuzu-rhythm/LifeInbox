"""Domain constants shared by the parser, services, and presentation layer."""

from __future__ import annotations

from enum import StrEnum


class EventStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    IGNORED = "ignored"


class EventKind(StrEnum):
    EXPENSE = "expense"
    MEAL = "meal"
    INCOME = "income"
    TRANSFER = "transfer"
    MEMO = "memo"
    RHYTHM_TODO = "rhythm_todo"
    RHYTHM_RESULT = "rhythm_result"


class RhythmGame(StrEnum):
    MAIMAI = "maimai"
    ONGEKI = "ongeki"
    CHUNITHM = "chunithm"


class RhythmTodoStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"


# String tuples are convenient for SQL CheckConstraints and callers that need
# to enumerate the supported values without depending on enum internals.
SUPPORTED_RHYTHM_GAMES = tuple(game.value for game in RhythmGame)
RHYTHM_TODO_STATUSES = tuple(status.value for status in RhythmTodoStatus)

# Keep the short name discoverable for callers that use the generic TODO
# terminology while retaining the more explicit domain-specific enum above.
TodoStatus = RhythmTodoStatus


class AccountKind(StrEnum):
    CASH = "cash"
    BANK = "bank"
    CARD = "card"
    E_MONEY = "e_money"
    INVESTMENT = "investment"


class ActivityKind(StrEnum):
    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"


# More explicit aliases are useful to callers that want to distinguish these
# from other account/activity concepts while keeping the short enum names
# convenient for the rest of the application.
FinancialAccountKind = AccountKind
LedgerActivityKind = ActivityKind


class MealType(StrEnum):
    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SNACK = "snack"
    UNKNOWN = "unknown"


class OutsideHome(StrEnum):
    OUTSIDE = "outside"
    HOME = "home"
    UNKNOWN = "unknown"


DEFAULT_CATEGORIES = (
    "食費",
    "交通",
    "娯楽",
    "日用品",
    "衣服",
    "交際",
    "サブスク",
    "学習",
    "その他",
    "未分類",
)

FOOD_SUBCATEGORIES = ("外食", "カフェ", "コンビニ", "自炊材料", "その他")

# This is a domain guardrail. Settings.max_expense_yen can tighten it for a
# particular installation, while this upper bound keeps accidental huge
# integer values out of the database even if configuration is mis-set.
ABSOLUTE_MAX_EXPENSE_YEN = 10_000_000

# MY BEST is an already-sanitized, bounded snapshot from the official maimai
# NET page.  Keep its source and size limits explicit so the import seam cannot
# accidentally become a general external-account importer.
MAIMAI_MYBEST_SOURCE = "maimai_net"
MAIMAI_MYBEST_MAX_ITEMS = 500
MAIMAI_MYBEST_MAX_PLAY_COUNT = 1_000_000
MAIMAI_NET_FAVORITES_SOURCE = "maimai_net"

DEFAULT_ALIASES: dict[str, str] = {
    "マック": "マクドナルド",
    "マクド": "マクドナルド",
    "mcdonald's": "マクドナルド",
    "マクドナルド": "マクドナルド",
    "セブン": "セブン-イレブン",
    "セブンイレブン": "セブン-イレブン",
    "セブン-イレブン": "セブン-イレブン",
    "コメダ": "コメダ珈琲店",
    "コメダ珈琲店": "コメダ珈琲店",
}

# These mappings are intentionally conservative. Unknown merchants remain
# 未分類 and can be resolved by the text analyzer or a later user edit.
MERCHANT_CATEGORY_RULES: tuple[tuple[tuple[str, ...], str, str | None], ...] = (
    (("マクドナルド", "すき家", "吉野家", "松屋", "魂心家", "ラーメン", "食堂"), "食費", "外食"),
    (
        ("コメダ珈琲店", "スタバ", "スターバックス", "ドトール", "タリーズ", "カフェ"),
        "食費",
        "カフェ",
    ),
    (("セブン-イレブン", "ローソン", "ファミリーマート", "コンビニ"), "食費", "コンビニ"),
    (("電車", "鉄道", "jr", "地下鉄", "バス", "タクシー", "交通"), "交通", None),
    (("ゲーセン", "ゲームセンター", "ラウンドワン", "音ゲー"), "娯楽", None),
    (("amazon", "アマゾン", "楽天"), "日用品", None),
)
