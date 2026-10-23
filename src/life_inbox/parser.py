"""Low-friction deterministic parsing for ordinary Discord messages."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .constants import EventKind, MealType

# The parser deliberately accepts both separated and attached forms such as
# ``電車220``. Latin-letter adjacency and malformed comma/decimal forms are
# rejected so that an accidental number is not silently treated as money.
_NUMBER_RE = re.compile(r"(?P<sign>[+-]?)(?P<digits>\d[\d,]*)")
_VALID_COMMA_RE = re.compile(r"\d{1,3}(?:,\d{3})+")

_MEAL_HINTS: tuple[tuple[re.Pattern[str], MealType], ...] = (
    (re.compile(r"朝ご飯|朝飯|朝食|朝"), MealType.BREAKFAST),
    (re.compile(r"昼ご飯|昼飯|昼食|昼"), MealType.LUNCH),
    (re.compile(r"夕ご飯|夕飯|晩飯|夕食|夜ご飯|夜食|夜"), MealType.DINNER),
    (re.compile(r"おやつ|間食"), MealType.SNACK),
)

_MEAL_WORD_RE = re.compile(r"食事|ごはん|ご飯|料理|ラーメン|食べた|食べる|弁当|定食")
_NON_MERCHANT_WORDS = {"これ", "それ", "メモ", "写真", "食事写真", "レシート"}


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    """Result of local parsing.

    ``amount_invalid`` is distinct from ``amount is None``: it prevents a
    malformed/negative/extreme number from being "repaired" into an expense
    without an explicit user edit, while still retaining the raw Event.
    """

    raw_text: str
    amount: int | None
    amount_invalid: bool
    amount_text: str | None
    merchant_raw: str | None
    description: str | None
    meal_type: MealType
    has_meal_hint: bool
    kind_hint: EventKind | None

    @property
    def has_signal(self) -> bool:
        return bool(
            self.amount is not None
            or self.amount_invalid
            or self.merchant_raw
            or self.has_meal_hint
            or self.description
        )


def _number_is_malformed(text: str, start: int, end: int, sign: str, digits: str) -> bool:
    if sign:
        return True
    if "," in digits and not _VALID_COMMA_RE.fullmatch(digits):
        return True
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    if before.isascii() and before.isalnum():
        return True
    if after.isascii() and after.isalnum():
        return True
    # ``"" in ".．"`` is true in Python, so both boundary values must be
    # checked explicitly. Otherwise every number at the start or end of a
    # message is incorrectly classified as a malformed decimal.
    if (before and before in ".．") or (after and after in ".．"):
        return True
    return False


def _extract_amount(
    text: str, max_amount_yen: int
) -> tuple[int | None, bool, str | None, list[tuple[int, int]]]:
    matches = list(_NUMBER_RE.finditer(text))
    invalid = False
    valid: list[tuple[int, str, int]] = []
    spans: list[tuple[int, int]] = []

    for match in matches:
        start, end = match.span()
        sign = match.group("sign")
        digits = match.group("digits")
        spans.append((start, end))
        if _number_is_malformed(text, start, end, sign, digits):
            invalid = True
            continue
        try:
            value = int(digits.replace(",", ""))
        except ValueError:
            invalid = True
            continue
        if value <= 0 or value > max_amount_yen:
            invalid = True
            continue
        valid.append((start, digits, value))

    # A malformed/invalid money-looking token wins over a valid one. This is
    # safer for messages like ``-680 マック`` and keeps the event recoverable.
    if invalid:
        return None, True, None, spans
    if not valid:
        return None, False, None, spans

    # Ordinary inbox messages contain one amount. If a future free-form input
    # contains several, the first one is the least surprising interpretation.
    start, digits, value = valid[0]
    match = next(match for match in matches if match.start() == start)
    return value, False, match.group(0), spans


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    for start, end in reversed(spans):
        text = text[:start] + " " + text[end:]
    return text


def _extract_meal_hint(text: str) -> tuple[str, MealType, bool]:
    for pattern, meal_type in _MEAL_HINTS:
        match = pattern.search(text)
        if match:
            return text[: match.start()] + " " + text[match.end() :], meal_type, True
    if _MEAL_WORD_RE.search(text):
        return text, MealType.UNKNOWN, True
    return text, MealType.UNKNOWN, False


def _clean_remainder(text: str) -> str:
    text = re.sub(r"[\s+、,：:]+", " ", text).strip()
    return text


def parse_message(text: str | None, *, max_amount_yen: int = 1_000_000) -> ParsedMessage:
    """Parse amount, merchant-like remainder, and meal hints without AI."""

    raw_text = text or ""
    if not raw_text.strip():
        return ParsedMessage(
            raw_text=raw_text,
            amount=None,
            amount_invalid=False,
            amount_text=None,
            merchant_raw=None,
            description=None,
            meal_type=MealType.UNKNOWN,
            has_meal_hint=False,
            kind_hint=None,
        )

    amount, invalid, amount_text, spans = _extract_amount(raw_text, max_amount_yen)
    remainder = _remove_spans(raw_text, spans)
    remainder, meal_type, has_meal_hint = _extract_meal_hint(remainder)
    remainder = _clean_remainder(remainder)

    merchant_raw: str | None = remainder or None
    description: str | None = None
    if merchant_raw in _NON_MERCHANT_WORDS:
        description = merchant_raw
        merchant_raw = None

    kind_hint: EventKind | None
    if amount is not None:
        kind_hint = EventKind.EXPENSE
    elif has_meal_hint:
        kind_hint = EventKind.MEAL
    else:
        kind_hint = None

    return ParsedMessage(
        raw_text=raw_text,
        amount=amount,
        amount_invalid=invalid,
        amount_text=amount_text,
        merchant_raw=merchant_raw,
        description=description,
        meal_type=meal_type,
        has_meal_hint=has_meal_hint,
        kind_hint=kind_hint,
    )
