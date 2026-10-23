"""Small deterministic normalization helpers used by the domain models."""

from __future__ import annotations

import json
import math
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .enums import Game


def utc_now() -> datetime:
    """Return an aware UTC timestamp for machine-created domain records."""

    return datetime.now(UTC)


def ensure_aware_datetime(value: datetime | str, *, field_name: str) -> datetime:
    """Validate a timestamp and normalize it to aware UTC.

    Naive timestamps are rejected because they are ambiguous at integration
    boundaries.  ISO-8601 strings are accepted to keep fixture and API seams
    convenient, but the normalized result is always a ``datetime`` with UTC.
    """

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO 8601 datetime") from exc
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(UTC)


def normalize_text(
    value: str,
    *,
    field_name: str,
    casefold: bool = False,
) -> str:
    """Apply NFKC and whitespace normalization to one required text value."""

    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = unicodedata.normalize("NFKC", value)
    normalized = " ".join(normalized.split())
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized.casefold() if casefold else normalized


def normalize_optional_text(
    value: str | None,
    *,
    field_name: str,
    casefold: bool = False,
) -> str | None:
    """Normalize optional text while preserving ``None`` and rejecting blanks."""

    if value is None:
        return None
    return normalize_text(value, field_name=field_name, casefold=casefold)


def normalize_game(value: Game | str) -> Game:
    """Coerce a game value without accepting unsupported providers."""

    if isinstance(value, Game):
        return value
    if not isinstance(value, str):
        raise ValueError("game must be one of chunithm, maimai, or ongeki")
    try:
        return Game(value.strip().casefold())
    except ValueError as exc:
        raise ValueError("game must be one of chunithm, maimai, or ongeki") from exc


def normalize_measure(value: Any, *, field_name: str) -> int | float | str | None:
    """Normalize a provider score/rating value without losing text precision."""

    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must not be a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must be finite")
        return int(value) if value.is_integer() else value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{field_name} must be finite")
        if value == value.to_integral_value():
            return int(value)
        return format(value, "f")
    if isinstance(value, str):
        return normalize_text(value, field_name=field_name).casefold()
    raise ValueError(f"{field_name} must be a number, text, or null")


def canonical_measure(value: int | float | str | None) -> int | float | str | None:
    """Return a JSON-safe canonical scalar for a normalized measure."""

    if isinstance(value, float):
        return int(value) if value.is_integer() else format(value, ".15g")
    return value


def canonical_json(value: Any) -> str:
    """Serialize JSON-compatible content deterministically."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
