"""Safety policy for Discord records mirrored into the private Notion archive.

The policy does not enqueue anything. It allows user-reviewed life records
after the durable Discord archive exists, while raw/unknown input, continuous
Kiseki traces, account secrets, balances, and transfer ledgers remain local.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .constants import EventKind, EventStatus
from .models import Event

# ``event_type`` is intentionally a free-form column in this project.  The
# enum-backed values below come from EventKind; the remaining labels are the
# reviewed classifier/destination labels already used by the application.
ELIGIBLE_EVENT_TYPES = frozenset(
    {
        EventKind.MEAL.value,
        EventKind.EXPENSE.value,
        EventKind.MEMO.value,
        EventKind.RHYTHM_TODO.value,
        EventKind.RHYTHM_RESULT.value,
        "rhythm_spending",
        "career",
        "dining_out",
        "life_task",
        "memories",
        "monologue",
        "saved_link",
        "self_talk",
        "shopping",
        "social_draft",
        "task",
        "todo",
        "link",
        "links",
    }
)
SENSITIVE_EVENT_TYPES = frozenset(
    {
        EventKind.INCOME.value,
        EventKind.TRANSFER.value,
        "account",
        "balance",
        "bank",
        "card",
        "finance_account",
        "finance_card",
        "financial_account",
        "financial_balance",
        "investment",
    }
)
EXCLUDED_EVENT_TYPES = frozenset(
    {
        "unknown",
        "raw",
        "raw_input",
    }
)

_TOKEN_SPLIT_RE = re.compile(r"[+,|\s]+")
_EXACT_COORDINATE_PAIR_RE = re.compile(
    r"(?<!\d)[+-]?\d{1,3}\.\d{3,}\s*[,;/\s]\s*[+-]?\d{1,3}\.\d{3,}(?!\d)"
)
_KISEKI_SENSITIVE_MARKERS = frozenset(
    {
        "kiseki",
        "kiseki_raw",
        "kiseki_archive",
        "kiseki_full_archive",
        "full_kiseki_archive",
        "キセキ",
        "gps",
        "raw_gps",
        "gps_track",
        "gps_trace",
        "coordinate",
        "coordinates",
        "exact_coordinate",
        "exact_coordinates",
        "latitude",
        "longitude",
        "緯度",
        "経度",
        "位置履歴",
        "位置情報",
        "位置ログ",
        "gps軌跡",
        "軌跡",
        "full_archive",
        "fullarchive",
        "全アーカイブ",
        "全記録",
    }
)


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    """A stable, safe-to-display result of the archive policy."""

    eligible: bool
    reason_code: str
    message: str


def normalize_policy_token(value: Any) -> str:
    """Normalize a free-form Event label without inventing enum values."""

    text = str(value or "").strip().casefold()
    return text.replace("-", "_").replace(" ", "_")


def event_policy_tokens(event: Event) -> tuple[str, ...]:
    """Return normalized labels from the existing Event schema."""

    values: list[str] = []
    values.extend(_TOKEN_SPLIT_RE.split(normalize_policy_token(event.event_type)))
    for kind in event.event_kinds or []:
        values.extend(_TOKEN_SPLIT_RE.split(normalize_policy_token(kind)))
    return tuple(token for token in values if token)


def _contains_kiseki_sensitive_marker(event: Event, tokens: tuple[str, ...]) -> bool:
    if any(
        token in _KISEKI_SENSITIVE_MARKERS
        or (token.startswith("kiseki_") and ("raw" in token or "archive" in token))
        for token in tokens
    ):
        return True
    source = normalize_policy_token(event.source)
    if source in {"kiseki", "kiseki_raw", "kiseki_archive"} and "kiseki_moment" not in tokens:
        return True
    text = str(event.raw_text or "").casefold()
    compact = re.sub(r"[\s_\-]+", "", text)
    marker_patterns = (
        r"\bkiseki\b",
        r"\bgps\b",
        r"\b(?:raw|exact)[\s_-]*(?:gps|coordinate|coordinates)\b",
        r"\b(?:latitude|longitude)\b",
        r"\bfull[\s_-]*archive\b",
        r"(?:緯度|経度|位置履歴|位置情報|位置ログ|gps軌跡|軌跡|全アーカイブ|全記録)",
    )
    if _EXACT_COORDINATE_PAIR_RE.search(text) or any(
        re.search(pattern, text, re.IGNORECASE) for pattern in marker_patterns
    ):
        return True
    return any(marker.replace("_", "") in compact for marker in _KISEKI_SENSITIVE_MARKERS)


class NotionEligibilityPolicy:
    """Conservative safety filter applied after a user promotion."""

    def evaluate(self, event: Event) -> EligibilityDecision:
        tokens = event_policy_tokens(event)
        if event.status != EventStatus.CONFIRMED.value:
            return EligibilityDecision(False, "not_confirmed", "event is not confirmed")
        if _contains_kiseki_sensitive_marker(event, tokens):
            return EligibilityDecision(
                False,
                "sensitive_kiseki_data",
                "raw GPS, exact coordinates, and full Kiseki archives are excluded",
            )
        if (
            set(tokens) & SENSITIVE_EVENT_TYPES
            or event.ledger_activity is not None
        ):
            return EligibilityDecision(
                False,
                "sensitive_financial_data",
                "financial, account, card, and balance records are excluded",
            )
        if set(tokens) & EXCLUDED_EVENT_TYPES:
            return EligibilityDecision(False, "excluded_by_policy", "event category is excluded")
        if set(tokens) & ELIGIBLE_EVENT_TYPES:
            return EligibilityDecision(True, "eligible", "event is eligible after promotion")
        return EligibilityDecision(False, "unsupported_event_kind", "event kind is not allowlisted")


__all__ = [
    "ELIGIBLE_EVENT_TYPES",
    "EXCLUDED_EVENT_TYPES",
    "SENSITIVE_EVENT_TYPES",
    "EligibilityDecision",
    "NotionEligibilityPolicy",
    "event_policy_tokens",
    "normalize_policy_token",
]
