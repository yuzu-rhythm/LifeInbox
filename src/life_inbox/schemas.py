"""Strict Pydantic contracts for analyzer outputs and domain validation."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StrictInt, model_validator

from .constants import (
    ABSOLUTE_MAX_EXPENSE_YEN,
    DEFAULT_CATEGORIES,
    FOOD_SUBCATEGORIES,
    MAIMAI_MYBEST_MAX_ITEMS,
    MAIMAI_MYBEST_MAX_PLAY_COUNT,
    MealType,
    OutsideHome,
    RhythmGame,
)


class DomainValidationError(ValueError):
    """Raised when a structurally valid analyzer result is unsafe to store."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _validate_safe_http_url(value: str) -> str:
    """Reject credential-bearing, whitespace-containing, non-web URLs."""

    if len(value) > 2_048 or any(character.isspace() for character in value):
        raise ValueError("career URL must be a safe http or https URL")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        parsed.port
        has_credentials = parsed.username is not None or parsed.password is not None
    except ValueError as exc:
        raise ValueError("career URL must be a safe http or https URL") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or has_credentials
    ):
        raise ValueError("career URL must be a safe http or https URL")
    return value


class TextAnalysisResult(StrictModel):
    event_kinds: list[Literal["expense", "meal"]] = Field(default_factory=list, max_length=2)
    amount: StrictInt | None = None
    merchant: str | None = Field(default=None, min_length=1, max_length=255)
    category: str | None = Field(default=None, min_length=1, max_length=64)
    subcategory: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, min_length=1, max_length=1_000)
    meal_type: MealType = MealType.UNKNOWN
    restaurant: str | None = Field(default=None, min_length=1, max_length=255)
    meal_description: str | None = Field(default=None, min_length=1, max_length=1_000)
    outside_home: OutsideHome = OutsideHome.UNKNOWN
    confidence: float | None = Field(default=None, ge=0, le=1)


class InboxRouteResult(StrictModel):
    """Strict, minimal destination selected for one already-persisted Event."""

    destination: Literal[
        "finance_or_meal",
        "memo",
        "rhythm_todo",
        "rhythm_result",
        "life_task",
        "career",
        "unknown",
    ]
    game: RhythmGame | None = None
    content: str | None = Field(default=None, min_length=1, max_length=2_000)
    task_title: str | None = Field(default=None, min_length=1, max_length=255)
    task_due_date: date | None = None
    task_due_time: time | None = None
    career_company: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("career_company", "company"),
    )
    career_title: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices(
            "career_title",
            "career_role",
            "career_role_title",
            "role",
            "title",
        ),
    )
    career_kind: Literal["company", "job", "event", "application"] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "career_kind",
            "career_item_kind",
            "kind",
            "item_kind",
        ),
    )
    career_location: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("career_location", "location"),
    )
    career_stage: Literal[
        "saved",
        "interested",
        "applied",
        "screening",
        "interview",
        "final_interview",
        "offer",
        "accepted",
        "rejected",
        "withdrawn",
        "closed",
    ] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "career_stage",
            "career_application_stage",
            "application_stage",
            "stage",
        ),
    )
    career_status: Literal[
        "open",
        "interested",
        "applied",
        "in_progress",
        "accepted",
        "closed",
        "rejected",
        "withdrawn",
        "archived",
    ] | None = Field(
        default=None,
        validation_alias=AliasChoices("career_status", "status"),
    )
    career_deadline_date: date | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "career_deadline_date",
            "career_deadline",
            "deadline_date",
            "deadline",
        ),
    )
    career_deadline_time: time | None = Field(
        default=None,
        validation_alias=AliasChoices("career_deadline_time", "deadline_time"),
    )
    career_deadline_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("career_deadline_at", "deadline_at"),
    )
    career_next_action: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("career_next_action", "next_action"),
    )
    career_next_action_date: date | None = Field(
        default=None,
        validation_alias=AliasChoices("career_next_action_date", "next_action_date"),
    )
    career_next_action_time: time | None = Field(
        default=None,
        validation_alias=AliasChoices("career_next_action_time", "next_action_time"),
    )
    career_next_action_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("career_next_action_at", "next_action_at"),
    )
    career_url: str | None = Field(
        default=None,
        max_length=2_048,
        validation_alias=AliasChoices("career_url", "url"),
    )
    career_notes: str | None = Field(
        default=None,
        max_length=4_000,
        validation_alias=AliasChoices("career_notes", "notes"),
    )
    career_source: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        validation_alias=AliasChoices("career_source", "source"),
    )
    career_external_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("career_external_id", "external_id"),
    )
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_destination_shape(self) -> InboxRouteResult:
        rhythm_destination = self.destination in {"rhythm_todo", "rhythm_result"}
        if not rhythm_destination and self.game is not None:
            raise ValueError("game is only valid for rhythm destinations")

        if self.destination == "memo":
            if self.content is None:
                raise ValueError("memo content is required")
        elif self.destination == "rhythm_todo":
            if self.content is None:
                raise ValueError("rhythm TODO content is required")
            if len(self.content) > 255:
                raise ValueError("rhythm TODO content is too long")
        elif self.destination == "rhythm_result":
            if self.content is not None and len(self.content) > 1_000:
                raise ValueError("rhythm result content is too long")
        elif self.content is not None:
            raise ValueError("content is not valid for this destination")

        task_fields = (self.task_title, self.task_due_date, self.task_due_time)
        if self.destination == "life_task":
            if self.task_title is None:
                raise ValueError("life task title is required")
            if self.task_due_time is not None and self.task_due_date is None:
                raise ValueError("task due time requires an explicit due date")
            if self.task_due_date is not None and not 2000 <= self.task_due_date.year <= 2100:
                raise ValueError("task due date is outside the accepted range")
            if self.task_due_time is not None and self.task_due_time.tzinfo is not None:
                raise ValueError("task due time must use the configured local timezone")
        elif any(value is not None for value in task_fields):
            raise ValueError("task fields are only valid for life_task")

        career_fields = (
            self.career_company,
            self.career_title,
            self.career_kind,
            self.career_location,
            self.career_stage,
            self.career_status,
            self.career_deadline_date,
            self.career_deadline_time,
            self.career_deadline_at,
            self.career_next_action,
            self.career_next_action_date,
            self.career_next_action_time,
            self.career_next_action_at,
            self.career_url,
            self.career_notes,
            self.career_source,
            self.career_external_id,
        )
        if self.destination == "career":
            if self.career_company is None:
                raise ValueError("career company is required")
            if self.career_title is None:
                raise ValueError("career title is required")
            if self.career_deadline_at is not None and any(
                value is not None
                for value in (self.career_deadline_date, self.career_deadline_time)
            ):
                raise ValueError("career deadline_at cannot be combined with date/time parts")
            if self.career_next_action_at is not None and any(
                value is not None
                for value in (self.career_next_action_date, self.career_next_action_time)
            ):
                raise ValueError(
                    "career next_action_at cannot be combined with date/time parts"
                )
            if self.career_deadline_time is not None and self.career_deadline_date is None:
                raise ValueError("career deadline time requires an explicit date")
            if (
                self.career_next_action_time is not None
                and self.career_next_action_date is None
            ):
                raise ValueError("career next action time requires an explicit date")
            for field_name, value in (
                ("career_deadline_date", self.career_deadline_date),
                ("career_next_action_date", self.career_next_action_date),
            ):
                if value is not None and not 2000 <= value.year <= 2100:
                    raise ValueError(f"{field_name} is outside the accepted range")
            for field_name, value in (
                ("career_deadline_time", self.career_deadline_time),
                ("career_next_action_time", self.career_next_action_time),
            ):
                if value is not None and value.tzinfo is not None:
                    raise ValueError(f"{field_name} must not include a timezone offset")
            for field_name, value in (
                ("career_deadline_at", self.career_deadline_at),
                ("career_next_action_at", self.career_next_action_at),
            ):
                if value is not None and not 2000 <= value.year <= 2100:
                    raise ValueError(f"{field_name} is outside the accepted range")
            if self.career_url is not None:
                _validate_safe_http_url(self.career_url)
            if self.career_source is not None and not self.career_source.strip():
                raise ValueError("career source must not be blank")
        elif any(value is not None for value in career_fields):
            raise ValueError("career fields are only valid for career")
        return self


class ReceiptItemResult(StrictModel):
    name: str = Field(min_length=1, max_length=500)
    quantity: StrictInt | None = None
    unit_price: StrictInt | None = None
    amount: StrictInt | None = None


class ReceiptAnalysisResult(StrictModel):
    merchant: str | None = Field(default=None, min_length=1, max_length=255)
    purchase_datetime: datetime | None = None
    subtotal: StrictInt | None = None
    tax: StrictInt | None = None
    total: StrictInt | None = None
    items: list[ReceiptItemResult] = Field(default_factory=list, max_length=200)
    confidence: float | None = Field(default=None, ge=0, le=1)


class MealAnalysisResult(StrictModel):
    meal_type: MealType = MealType.UNKNOWN
    description: str | None = Field(default=None, min_length=1, max_length=1_000)
    restaurant: str | None = Field(default=None, min_length=1, max_length=255)
    outside_home: OutsideHome = OutsideHome.UNKNOWN
    confidence: float | None = Field(default=None, ge=0, le=1)


class MaimaiMyBestEntryInput(StrictModel):
    """Strict, credential-free input row for a sanitized MY BEST snapshot."""

    rank: StrictInt = Field(gt=0, le=MAIMAI_MYBEST_MAX_ITEMS)
    title: str = Field(min_length=1, max_length=255)
    play_count: StrictInt = Field(ge=0, le=MAIMAI_MYBEST_MAX_PLAY_COUNT)


class MaimaiNetFavoriteEntryInput(StrictModel):
    """Strict, credential-free input row for official NET favorites."""

    rank: StrictInt = Field(gt=0, le=MAIMAI_MYBEST_MAX_ITEMS)
    category: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    artist: str = Field(min_length=1, max_length=255)


class MaimaiMyBestSnapshotInput(StrictModel):
    """Strict JSON envelope accepted by the local MY BEST import seam."""

    fetched_at: datetime
    source: Literal["maimai_net"] = "maimai_net"
    entries: list[MaimaiMyBestEntryInput] = Field(
        min_length=1,
        max_length=MAIMAI_MYBEST_MAX_ITEMS,
    )


class MaimaiSnapshotBundleInput(StrictModel):
    """Single local JSON envelope for both official maimai snapshots."""

    schema_version: Literal[1]
    source: Literal["maimai_net"]
    fetched_at: datetime
    favorites: list[MaimaiNetFavoriteEntryInput] = Field(
        max_length=MAIMAI_MYBEST_MAX_ITEMS,
    )
    my_best: list[MaimaiMyBestEntryInput] = Field(
        max_length=MAIMAI_MYBEST_MAX_ITEMS,
    )


def _validate_yen(
    value: int | None,
    *,
    field_name: str,
    max_amount_yen: int,
    allow_zero: bool = False,
) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise DomainValidationError(f"{field_name} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum or value > max_amount_yen or value > ABSOLUTE_MAX_EXPENSE_YEN:
        raise DomainValidationError(
            f"{field_name} must be between {minimum} and {max_amount_yen:,} yen"
        )
    return value


def validate_text_result(
    result: TextAnalysisResult,
    *,
    max_amount_yen: int,
    allowed_categories: Collection[str] = DEFAULT_CATEGORIES,
    allowed_food_subcategories: Collection[str] = FOOD_SUBCATEGORIES,
) -> TextAnalysisResult:
    _validate_yen(result.amount, field_name="amount", max_amount_yen=max_amount_yen)
    if result.category is not None and result.category not in allowed_categories:
        raise DomainValidationError(f"unknown category: {result.category}")
    if result.subcategory is not None and result.subcategory not in allowed_food_subcategories:
        raise DomainValidationError(f"unknown food subcategory: {result.subcategory}")
    if result.subcategory is not None and result.category != "食費":
        raise DomainValidationError("food subcategory requires category 食費")
    return result


def validate_receipt_result(
    result: ReceiptAnalysisResult, *, max_amount_yen: int
) -> ReceiptAnalysisResult:
    _validate_yen(result.subtotal, field_name="subtotal", max_amount_yen=max_amount_yen)
    _validate_yen(
        result.tax,
        field_name="tax",
        max_amount_yen=max_amount_yen,
        allow_zero=True,
    )
    _validate_yen(result.total, field_name="total", max_amount_yen=max_amount_yen)
    if result.purchase_datetime is not None:
        purchase_datetime = result.purchase_datetime
        comparable = (
            purchase_datetime.replace(tzinfo=UTC)
            if purchase_datetime.tzinfo is None
            else purchase_datetime.astimezone(UTC)
        )
        if purchase_datetime.year < 2000 or comparable > datetime.now(UTC) + timedelta(days=2):
            raise DomainValidationError("purchase_datetime is outside the accepted range")
    for index, item in enumerate(result.items):
        if not item.name.strip():
            raise DomainValidationError(f"items[{index}].name must not be blank")
        for field_name in ("quantity", "unit_price", "amount"):
            value = getattr(item, field_name)
            if field_name == "quantity":
                if value is not None and (type(value) is not int or value <= 0 or value > 1_000):
                    raise DomainValidationError(f"items[{index}].quantity is out of range")
            else:
                _validate_yen(
                    value, field_name=f"items[{index}].{field_name}", max_amount_yen=max_amount_yen
                )
    return result


def validate_meal_result(result: MealAnalysisResult) -> MealAnalysisResult:
    if result.description is not None and len(result.description) > 1_000:
        raise DomainValidationError("meal description is too long")
    return result
