"""Alias and conservative merchant/category normalization."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping

from .constants import (
    DEFAULT_ALIASES,
    DEFAULT_CATEGORIES,
    FOOD_SUBCATEGORIES,
    MERCHANT_CATEGORY_RULES,
)


def _key(value: str | None) -> str:
    return " ".join((value or "").strip().casefold().split())


def normalize_merchant(
    raw: str | None,
    aliases: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
) -> str | None:
    """Return a canonical merchant while preserving ``raw`` in the model."""

    if raw is None or not raw.strip():
        return None
    text = " ".join(raw.strip().split())
    key = _key(text)
    mapping: dict[str, str] = dict(DEFAULT_ALIASES)
    if aliases is not None:
        mapping.update(
            {_key(alias): canonical.strip() for alias, canonical in dict(aliases).items()}
        )

    if key in mapping:
        return mapping[key]

    # Support a short alias embedded in a small description, but prefer the
    # longest alias to avoid a broad/ambiguous replacement.
    for alias in sorted(mapping, key=len, reverse=True):
        if len(alias) >= 2 and alias in key:
            return mapping[alias]
    return text


def category_for_merchant(merchant: str | None) -> tuple[str, str | None]:
    if not merchant:
        return "未分類", None
    key = _key(merchant)
    for keywords, category, subcategory in MERCHANT_CATEGORY_RULES:
        if any(_key(keyword) in key for keyword in keywords):
            return category, subcategory
    return "未分類", None


def validate_category(
    category: str | None,
    allowed_categories: Collection[str] = DEFAULT_CATEGORIES,
) -> str:
    if category in allowed_categories:
        return category
    return "未分類"


def validate_subcategory(
    category: str,
    subcategory: str | None,
    allowed_subcategories: Collection[str] = FOOD_SUBCATEGORIES,
) -> str | None:
    if category == "食費" and subcategory in allowed_subcategories:
        return subcategory
    return None
