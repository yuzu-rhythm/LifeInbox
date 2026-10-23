"""SQLAlchemy 2.x persistence models."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, synonym

from .constants import (
    ABSOLUTE_MAX_EXPENSE_YEN,
    MAIMAI_MYBEST_MAX_ITEMS,
    MAIMAI_MYBEST_MAX_PLAY_COUNT,
    MAIMAI_MYBEST_SOURCE,
    MAIMAI_NET_FAVORITES_SOURCE,
    ActivityKind,
)

if TYPE_CHECKING:
    from .rhythm_spending_models import RhythmSpendingEventLink


class Base(DeclarativeBase):
    pass

def _utc_default() -> datetime:
    # SQLite stores UTC as a naive value; application boundaries attach UTC
    # before converting to the configured display timezone.
    return datetime.now(UTC).replace(tzinfo=None)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("discord_message_id", name="uq_events_discord_message_id"),
        Index("ix_events_status_occurred_at", "status", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    discord_channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    discord_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="discord", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    event_kinds: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )
    copied_to_planner_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    capture_context: Mapped[EventCaptureContext | None] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        lazy="joined",
        single_parent=True,
        uselist=False,
    )
    shortcut_delivery: Mapped[ShortcutInboxDelivery | None] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        lazy="joined",
        single_parent=True,
        uselist=False,
    )
    planner_items: Mapped[list[PlannerImageItem]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="PlannerImageItem.item_index",
    )

    expenses: Mapped[list[Expense]] = relationship(
        back_populates="event", cascade="all, delete-orphan", lazy="selectin"
    )
    meals: Mapped[list[Meal]] = relationship(
        back_populates="event", cascade="all, delete-orphan", lazy="selectin"
    )
    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="event", cascade="all, delete-orphan", lazy="selectin"
    )
    discord_archive: Mapped[DiscordArchiveRecord | None] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        lazy="joined",
        single_parent=True,
        uselist=False,
    )
    ai_runs: Mapped[list[AIRun]] = relationship(
        back_populates="event", cascade="all, delete-orphan", lazy="selectin"
    )
    ledger_activity: Mapped[LedgerActivity | None] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        lazy="joined",
        single_parent=True,
        uselist=False,
    )
    memo: Mapped[Memo | None] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        lazy="joined",
        single_parent=True,
        uselist=False,
    )
    rhythm_todo: Mapped[RhythmTodo | None] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        lazy="joined",
        single_parent=True,
        uselist=False,
    )
    rhythm_result: Mapped[RhythmResult | None] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        lazy="joined",
        single_parent=True,
        uselist=False,
    )
    rhythm_spending_link: Mapped[RhythmSpendingEventLink | None] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        lazy="joined",
        single_parent=True,
        uselist=False,
    )
    career_opportunity: Mapped[CareerOpportunity | None] = relationship(
        back_populates="source_event",
        lazy="selectin",
        uselist=False,
    )
    career_application: Mapped[CareerApplication | None] = relationship(
        back_populates="source_event",
        lazy="selectin",
        uselist=False,
    )


class EventCaptureContext(Base):
    """Explicit, one-shot capture context for an Event.

    This is deliberately separate from Kiseki and never represents a route,
    visit history, or continuous location stream. Coordinates are accepted
    only when an authenticated Shortcut explicitly includes the current
    location for the single capture.
    """

    __tablename__ = "event_capture_contexts"
    __table_args__ = (
        CheckConstraint(
            "latitude IS NULL OR (latitude >= -90 AND latitude <= 90)",
            name="ck_event_capture_contexts_latitude",
        ),
        CheckConstraint(
            "longitude IS NULL OR (longitude >= -180 AND longitude <= 180)",
            name="ck_event_capture_contexts_longitude",
        ),
        CheckConstraint(
            "horizontal_accuracy_meters IS NULL OR horizontal_accuracy_meters >= 0",
            name="ck_event_capture_contexts_accuracy",
        ),
    )

    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    capture_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    horizontal_accuracy_meters: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    location_shared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    route_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    destination_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    destination_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    event: Mapped[Event] = relationship(back_populates="capture_context")


class ShortcutInboxDelivery(Base):
    """Durable outbox mapping from one Shortcut Event to its Discord source card."""

    __tablename__ = "shortcut_inbox_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sending', 'posted', 'retryWaiting', 'failed')",
            name="ck_shortcut_inbox_deliveries_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_shortcut_inbox_attempt_count"),
        Index("ix_shortcut_inbox_delivery_queue", "status", "next_retry_at"),
    )

    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    source_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidate_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    event: Mapped[Event] = relationship(back_populates="shortcut_delivery")


class PlannerImageItem(Base):
    """One Luna-extracted handwritten item and its Discord delivery state."""

    __tablename__ = "planner_image_items"
    __table_args__ = (
        UniqueConstraint("event_id", "item_index", name="uq_planner_image_event_item"),
        CheckConstraint(
            "status IN ('pending', 'posted', 'failed', 'unknown')",
            name="ck_planner_image_items_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    candidate_categories: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    destination_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    destination_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    event: Mapped[Event] = relationship(back_populates="planner_items")


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        CheckConstraint(
            "amount IS NULL OR (amount > 0 AND amount <= 10000000)", name="ck_expenses_amount"
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_expenses_confidence",
        ),
        UniqueConstraint("event_id", name="uq_expenses_event_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    merchant_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    merchant_normalized: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str] = mapped_column(String(64), default="未分類", nullable=False)
    subcategory: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)

    event: Mapped[Event] = relationship(back_populates="expenses")
    receipt_items: Mapped[list[ReceiptItem]] = relationship(
        back_populates="expense", cascade="all, delete-orphan", lazy="selectin"
    )


class MonthlyBudget(Base):
    """A positive monthly spending limit for the whole month or one category.

    ``category_key`` is deliberately non-null.  ``__overall__`` represents
    the month-wide limit, while a real configured category represents a
    category limit.  Keeping the overall row as a concrete key avoids the
    database-specific NULL semantics of a nullable column in a unique
    constraint.
    """

    __tablename__ = "monthly_budgets"
    OVERALL_CATEGORY_KEY: ClassVar[str] = "__overall__"
    __table_args__ = (
        UniqueConstraint(
            "month",
            "scope",
            "category_key",
            name="uq_monthly_budgets_month_scope_category",
        ),
        Index("ix_monthly_budgets_month_scope", "month", "scope"),
        Index("ix_monthly_budgets_month_category", "month", "category_key"),
        CheckConstraint(
            "length(month) = 7 AND month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' "
            "AND substr(month, 1, 4) <> '0000' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_monthly_budgets_month",
        ),
        CheckConstraint(
            "scope IN ('overall', 'category')",
            name="ck_monthly_budgets_scope",
        ),
        CheckConstraint(
            "((scope = 'overall' AND category_key = '__overall__') "
            "OR (scope = 'category' AND category_key <> '__overall__'))",
            name="ck_monthly_budgets_scope_category_shape",
        ),
        CheckConstraint(
            "length(category_key) > 0 AND length(category_key) <= 64",
            name="ck_monthly_budgets_category_key",
        ),
        CheckConstraint(
            f"amount > 0 AND amount <= {ABSOLUTE_MAX_EXPENSE_YEN}",
            name="ck_monthly_budgets_amount",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    month: Mapped[str] = mapped_column(String(7), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), default="overall", nullable=False)
    category_key: Mapped[str] = mapped_column(
        String(64), default=OVERALL_CATEGORY_KEY, nullable=False
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    @property
    def category(self) -> str | None:
        """Expose the user-facing optional category without storing NULL."""

        if self.scope == "overall" or self.category_key == self.OVERALL_CATEGORY_KEY:
            return None
        return self.category_key

    @category.setter
    def category(self, value: str | None) -> None:
        if value is None:
            self.scope = "overall"
            self.category_key = self.OVERALL_CATEGORY_KEY
        else:
            self.scope = "category"
            self.category_key = value


class Meal(Base):
    __tablename__ = "meals"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_meals_confidence",
        ),
        UniqueConstraint("event_id", name="uq_meals_event_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    meal_type: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    restaurant_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    restaurant_normalized: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    outside_home: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    confidence: Mapped[float | None] = mapped_column(nullable=True)

    event: Mapped[Event] = relationship(back_populates="meals")


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        UniqueConstraint("discord_attachment_id", name="uq_attachments_discord_attachment_id"),
        Index("ix_attachments_sha256", "sha256"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    discord_attachment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    local_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)

    event: Mapped[Event] = relationship(back_populates="attachments")


class DiscordArchiveStatus(StrEnum):
    """Durable delivery states for the Discord copy of a general inbox Event."""

    PENDING = "pending"
    SENDING = "sending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DiscordArchiveRecord(Base):
    """One idempotent mapping from a source Discord message to its archive copy.

    The row deliberately stores only identifiers, delivery state, timestamps,
    and stable error labels.  The original text and attachment bytes remain in
    ``Event`` and ``Attachment`` so this mapping cannot become a second content
    store or leak provider response data into logs/database diagnostics.
    """

    __tablename__ = "discord_archive_records"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_discord_archive_records_event_id"),
        UniqueConstraint(
            "source_message_id",
            "destination_channel_id",
            name="uq_discord_archive_records_source_destination",
        ),
        Index(
            "ix_discord_archive_records_delivery",
            "status",
            "updated_at",
        ),
        CheckConstraint(
            "status IN ('pending', 'sending', 'succeeded', 'failed')",
            name="ck_discord_archive_records_status",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="ck_discord_archive_records_attempts",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    source_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    archive_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # A long original can require multiple Discord messages.  The first ID is
    # retained in ``archive_message_id`` for the common case and this ordered
    # list lets restart resume already acknowledged chunks without duplicating
    # them.  It contains IDs only, never copied content.
    archive_message_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=DiscordArchiveStatus.PENDING.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    event: Mapped[Event] = relationship("Event", back_populates="discord_archive", lazy="joined")


class Alias(Base):
    __tablename__ = "aliases"
    __table_args__ = (
        UniqueConstraint("alias", "entity_type", name="uq_aliases_alias_entity_type"),
        Index("ix_aliases_alias", "alias"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), default="merchant", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)


class AIRun(Base):
    __tablename__ = "ai_runs"
    __table_args__ = (Index("ix_ai_runs_event_created_at", "event_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_type: Mapped[str] = mapped_column(String(32), nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)

    event: Mapped[Event] = relationship(back_populates="ai_runs")


class ReceiptItem(Base):
    __tablename__ = "receipt_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    expense_id: Mapped[int] = mapped_column(
        ForeignKey("expenses.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    expense: Mapped[Expense] = relationship(back_populates="receipt_items")


class FinancialAccount(Base):
    __tablename__ = "financial_accounts"
    __table_args__ = (
        UniqueConstraint("name", name="uq_financial_accounts_name"),
        CheckConstraint(
            "length(trim(name)) > 0", name="ck_financial_accounts_name_not_blank"
        ),
        CheckConstraint(
            "kind IN ('cash', 'bank', 'card', 'e_money', 'investment')",
            name="ck_financial_accounts_kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    opening_balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="JPY", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    source_activities: Mapped[list[LedgerActivity]] = relationship(
        back_populates="source_account",
        foreign_keys="LedgerActivity.source_account_id",
        lazy="selectin",
    )
    destination_activities: Mapped[list[LedgerActivity]] = relationship(
        back_populates="destination_account",
        foreign_keys="LedgerActivity.destination_account_id",
        lazy="selectin",
    )
    snapshots: Mapped[list[FinancialAccountSnapshot]] = relationship(
        back_populates="account",
        foreign_keys="FinancialAccountSnapshot.account_id",
        lazy="selectin",
    )
    settlement_link: Mapped[FinancialAccountSettlementLink | None] = relationship(
        back_populates="card_account",
        foreign_keys="FinancialAccountSettlementLink.card_account_id",
        uselist=False,
        lazy="joined",
    )
    cards_using_as_settlement: Mapped[list[FinancialAccountSettlementLink]] = relationship(
        back_populates="settlement_account",
        foreign_keys="FinancialAccountSettlementLink.settlement_account_id",
        lazy="selectin",
    )


class LedgerActivity(Base):
    __tablename__ = "ledger_activities"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_ledger_activities_event_id"),
        Index("ix_ledger_activities_occurred_at", "occurred_at"),
        CheckConstraint(
            "kind IN ('expense', 'income', 'transfer')",
            name="ck_ledger_activities_kind",
        ),
        CheckConstraint(
            "((kind = 'expense' AND amount IS NULL "
            "AND source_account_id IS NOT NULL AND destination_account_id IS NULL) "
            "OR (kind = 'income' AND amount > 0 "
            "AND source_account_id IS NULL AND destination_account_id IS NOT NULL) "
            "OR (kind = 'transfer' AND amount > 0 "
            "AND source_account_id IS NOT NULL AND destination_account_id IS NOT NULL "
            "AND source_account_id <> destination_account_id))",
            name="ck_ledger_activities_shape",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("financial_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    destination_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("financial_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    # Expenses intentionally keep this column NULL. Their amount is resolved
    # from Event.expenses at read time so editing the source Expense cannot
    # leave a stale amount in the finance ledger.
    _amount: Mapped[int | None] = mapped_column("amount", Integer, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)

    event: Mapped[Event | None] = relationship(
        back_populates="ledger_activity", lazy="joined", uselist=False
    )
    source_account: Mapped[FinancialAccount | None] = relationship(
        back_populates="source_activities",
        foreign_keys=[source_account_id],
        lazy="joined",
    )
    destination_account: Mapped[FinancialAccount | None] = relationship(
        back_populates="destination_activities",
        foreign_keys=[destination_account_id],
        lazy="joined",
    )

    @hybrid_property
    def amount(self) -> int | None:
        """Return a ledger amount, resolving expenses from their source Event."""

        if self.kind == ActivityKind.EXPENSE.value:
            if self.event is None or not self.event.expenses:
                return None
            return self.event.expenses[0].amount
        return self._amount

    @amount.setter
    def amount(self, value: int | None) -> None:
        self._amount = value

    @amount.expression
    def amount(cls):
        # SQL expressions address the stored amount. Expense amounts are
        # intentionally NULL and are resolved in Python from Event.expenses.
        return cls._amount

    @property
    def effective_amount(self) -> int | None:
        """Explicit alias for callers that need the dynamic expense amount."""

        return self.amount


class FinancialAccountSnapshot(Base):
    """One canonical absolute value for an account and local calendar date.

    A missing source value is represented by the absence of a row, never by a
    synthetic zero.  Same-day corrections update this row and increment
    ``revision`` so the delivery layer can edit the existing daily message.
    """

    __tablename__ = "financial_account_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "local_date",
            name="uq_financial_account_snapshots_account_date",
        ),
        Index(
            "ix_financial_account_snapshots_account_date",
            "account_id",
            "local_date",
        ),
        CheckConstraint(
            "measure IN ('balance', 'outstanding')",
            name="ck_financial_account_snapshots_measure",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_financial_account_snapshots_revision",
        ),
        CheckConstraint(
            "length(trim(source)) > 0",
            name="ck_financial_account_snapshots_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("financial_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    measure: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utc_default,
        onupdate=_utc_default,
        nullable=False,
    )

    account: Mapped[FinancialAccount] = relationship(
        back_populates="snapshots",
        foreign_keys=[account_id],
        lazy="joined",
    )


class FinancialAccountSettlementLink(Base):
    """Primary settlement account associated with one credit-card account."""

    __tablename__ = "financial_account_settlement_links"
    __table_args__ = (
        CheckConstraint(
            "card_account_id <> settlement_account_id",
            name="ck_financial_account_settlement_links_distinct",
        ),
    )

    card_account_id: Mapped[int] = mapped_column(
        ForeignKey("financial_accounts.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    settlement_account_id: Mapped[int] = mapped_column(
        ForeignKey("financial_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utc_default,
        onupdate=_utc_default,
        nullable=False,
    )

    card_account: Mapped[FinancialAccount] = relationship(
        back_populates="settlement_link",
        foreign_keys=[card_account_id],
        lazy="joined",
    )
    settlement_account: Mapped[FinancialAccount] = relationship(
        back_populates="cards_using_as_settlement",
        foreign_keys=[settlement_account_id],
        lazy="joined",
    )


class Memo(Base):
    __tablename__ = "memos"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_memos_event_id"),
        CheckConstraint("length(trim(body)) > 0", name="ck_memos_body_not_blank"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    event: Mapped[Event] = relationship(back_populates="memo", lazy="joined")


class RhythmTodo(Base):
    """A rhythm-game TODO attached to exactly one source Event."""

    __tablename__ = "rhythm_todos"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_rhythm_todos_event_id"),
        CheckConstraint(
            "game IN ('maimai', 'ongeki', 'chunithm')",
            name="ck_rhythm_todos_game",
        ),
        CheckConstraint(
            "status IN ('open', 'completed')",
            name="ck_rhythm_todos_status",
        ),
        CheckConstraint(
            "length(title) <= 255 AND length(trim(title)) > 0",
            name="ck_rhythm_todos_title",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    game: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    event: Mapped[Event] = relationship(back_populates="rhythm_todo", lazy="joined")
    results: Mapped[list[RhythmResult]] = relationship(
        back_populates="todo", lazy="selectin", passive_deletes=True
    )


class RhythmResult(Base):
    """A rhythm-game result attached to exactly one source Event."""

    __tablename__ = "rhythm_results"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_rhythm_results_event_id"),
        Index("ix_rhythm_results_todo_created_at", "todo_id", "created_at"),
        CheckConstraint(
            "game IN ('maimai', 'ongeki', 'chunithm')",
            name="ck_rhythm_results_game",
        ),
        CheckConstraint(
            "note IS NULL OR (length(note) <= 1000 AND length(trim(note)) > 0)",
            name="ck_rhythm_results_note",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    todo_id: Mapped[int | None] = mapped_column(
        ForeignKey("rhythm_todos.id", ondelete="SET NULL"), nullable=True
    )
    game: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    event: Mapped[Event] = relationship(back_populates="rhythm_result", lazy="joined")
    todo: Mapped[RhythmTodo | None] = relationship(
        back_populates="results", lazy="joined"
    )


class RhythmFavorite(Base):
    """A local rhythm-game favorite independent from Discord Events.

    Manual favorites are unique by game and normalized title. Provider rows
    use a non-null ``source``/``external_id`` identity when one exists, while
    rows without an external ID are unique by provider, game, and normalized
    title. The partial indexes deliberately avoid SQL NULL uniqueness
    ambiguity and keep this table unrelated to existing Events/results.
    """

    __tablename__ = "rhythm_favorites"
    __table_args__ = (
        Index(
            "uq_rhythm_favorites_manual_game_title_key",
            "game",
            "normalized_title",
            unique=True,
            sqlite_where=text("source = 'manual'"),
        ),
        Index(
            "uq_rhythm_favorites_provider_game_title_no_external",
            "source",
            "game",
            "normalized_title",
            unique=True,
            sqlite_where=text("source = 'maimai_net' AND external_id IS NULL"),
        ),
        Index(
            "uq_rhythm_favorites_source_external_id",
            "source",
            "external_id",
            unique=True,
            sqlite_where=text("external_id IS NOT NULL"),
        ),
        Index(
            "ix_rhythm_favorites_active_game_title",
            "active",
            "game",
            "normalized_title",
        ),
        CheckConstraint(
            "game IN ('maimai', 'ongeki', 'chunithm')",
            name="ck_rhythm_favorites_game",
        ),
        CheckConstraint(
            "source IN ('manual', 'maimai_net')",
            name="ck_rhythm_favorites_source",
        ),
        CheckConstraint(
            "length(song_title) <= 255 AND length(trim(song_title)) > 0",
            name="ck_rhythm_favorites_song_title",
        ),
        CheckConstraint(
            "length(normalized_title) <= 255 AND length(trim(normalized_title)) > 0",
            name="ck_rhythm_favorites_normalized_title",
        ),
        CheckConstraint(
            "note IS NULL OR (length(note) <= 1000 AND length(trim(note)) > 0)",
            name="ck_rhythm_favorites_note",
        ),
        CheckConstraint(
            "external_id IS NULL OR "
            "(length(external_id) <= 255 AND length(trim(external_id)) > 0)",
            name="ck_rhythm_favorites_external_id",
        ),
        CheckConstraint(
            "active IN (0, 1)",
            name="ck_rhythm_favorites_active",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game: Mapped[str] = mapped_column(String(16), nullable=False)
    song_title: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_title: Mapped[str] = mapped_column(String(255), nullable=False)
    note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    # Keep the domain-oriented alias queryable while retaining the explicit
    # ``song_title`` storage column used by the provider import seam.
    title = synonym("song_title")


class MaimaiNetFavoriteEntry(Base):
    """One row in an official maimai NET favorites snapshot.

    The official favorites list has a different shape and meaning from both
    the manual ``RhythmFavorite`` table and the play-count-ranked MY BEST
    table, so it receives its own history-preserving table.
    """

    __tablename__ = "maimai_net_favorite_entries"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_key",
            "rank",
            name="uq_maimai_net_favorite_entries_snapshot_rank",
        ),
        Index(
            "ix_maimai_net_favorite_entries_active_rank",
            "active",
            "rank",
        ),
        Index(
            "ix_maimai_net_favorite_entries_source_fetched_at",
            "source",
            "fetched_at",
        ),
        CheckConstraint(
            "length(snapshot_key) = 64 AND snapshot_key GLOB '[0-9a-f]*'",
            name="ck_maimai_net_favorite_entries_snapshot_key",
        ),
        CheckConstraint(
            f"source = '{MAIMAI_NET_FAVORITES_SOURCE}'",
            name="ck_maimai_net_favorite_entries_source",
        ),
        CheckConstraint(
            f"rank > 0 AND rank <= {MAIMAI_MYBEST_MAX_ITEMS}",
            name="ck_maimai_net_favorite_entries_rank",
        ),
        CheckConstraint(
            "length(category) <= 64 AND length(trim(category)) > 0",
            name="ck_maimai_net_favorite_entries_category",
        ),
        CheckConstraint(
            "length(title) <= 255 AND length(trim(title)) > 0",
            name="ck_maimai_net_favorite_entries_title",
        ),
        CheckConstraint(
            "length(artist) <= 255 AND length(trim(artist)) > 0",
            name="ck_maimai_net_favorite_entries_artist",
        ),
        CheckConstraint(
            "active IN (0, 1)",
            name="ck_maimai_net_favorite_entries_active",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_key: Mapped[str] = mapped_column(String(64), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    artist: Mapped[str] = mapped_column(String(255), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source: Mapped[str] = mapped_column(
        String(32), default=MAIMAI_NET_FAVORITES_SOURCE, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )


class MaimaiMyBestEntry(Base):
    """One row in a sanitized official maimai NET MY BEST snapshot.

    This table is deliberately independent from ``RhythmFavorite``.  A MY
    BEST row is ordered by play count on the official site, while a favorite
    is a user-maintained list.  ``snapshot_key`` lets imports be idempotent;
    older rows remain available as history after they become inactive.
    """

    __tablename__ = "maimai_mybest_entries"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_key",
            "rank",
            name="uq_maimai_mybest_entries_snapshot_rank",
        ),
        Index(
            "ix_maimai_mybest_entries_active_rank",
            "active",
            "rank",
        ),
        Index(
            "ix_maimai_mybest_entries_source_fetched_at",
            "source",
            "fetched_at",
        ),
        CheckConstraint(
            "length(snapshot_key) = 64 AND snapshot_key GLOB '[0-9a-f]*'",
            name="ck_maimai_mybest_entries_snapshot_key",
        ),
        CheckConstraint(
            f"source = '{MAIMAI_MYBEST_SOURCE}'",
            name="ck_maimai_mybest_entries_source",
        ),
        CheckConstraint(
            f"rank > 0 AND rank <= {MAIMAI_MYBEST_MAX_ITEMS}",
            name="ck_maimai_mybest_entries_rank",
        ),
        CheckConstraint(
            "length(title) <= 255 AND length(trim(title)) > 0",
            name="ck_maimai_mybest_entries_title",
        ),
        CheckConstraint(
            f"play_count >= 0 AND play_count <= {MAIMAI_MYBEST_MAX_PLAY_COUNT}",
            name="ck_maimai_mybest_entries_play_count",
        ),
        CheckConstraint(
            "active IN (0, 1)",
            name="ck_maimai_mybest_entries_active",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_key: Mapped[str] = mapped_column(String(64), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    play_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source: Mapped[str] = mapped_column(
        String(32), default=MAIMAI_MYBEST_SOURCE, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )


class LifeTask(Base):
    """A provider-neutral task kept independently from Discord Events."""

    __tablename__ = "life_tasks"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "external_id",
            name="uq_life_tasks_source_external_id",
        ),
        Index("ix_life_tasks_status_due_at", "status", "due_at"),
        Index("ix_life_tasks_status_completed_at", "status", "completed_at"),
        CheckConstraint(
            "source IN ('manual', 'google_classroom', 'notion')",
            name="ck_life_tasks_source",
        ),
        CheckConstraint(
            "status IN ('open', 'completed')",
            name="ck_life_tasks_status",
        ),
        CheckConstraint(
            "length(title) <= 255 AND length(trim(title)) > 0",
            name="ck_life_tasks_title",
        ),
        CheckConstraint(
            "external_id IS NULL OR "
            "(length(external_id) <= 255 AND length(trim(external_id)) > 0)",
            name="ck_life_tasks_external_id",
        ),
        CheckConstraint(
            "description IS NULL OR length(description) <= 4000",
            name="ck_life_tasks_description",
        ),
        CheckConstraint(
            "source_url IS NULL OR "
            "(length(source_url) <= 2048 AND length(trim(source_url)) > 0 AND "
            "(source_url LIKE 'http://%' OR source_url LIKE 'https://%'))",
            name="ck_life_tasks_source_url",
        ),
        CheckConstraint(
            "(status = 'open' AND completed_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL)",
            name="ck_life_tasks_completion_shape",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )


class CareerOpportunity(Base):
    """A durable company, job listing, or career event.

    Career data deliberately lives outside ``Event`` and ``LifeTask``.  The
    optional source Event keeps the raw Discord audit trail, while
    ``linked_task_id`` points at a normal manual LifeTask so the existing
    reminder worker can handle career deadlines without changing its source
    constraint.
    """

    __tablename__ = "career_opportunities"
    __table_args__ = (
        UniqueConstraint("source_event_id", name="uq_career_opportunities_source_event_id"),
        UniqueConstraint(
            "source",
            "external_id",
            name="uq_career_opportunities_source_external_id",
        ),
        UniqueConstraint("linked_task_id", name="uq_career_opportunities_linked_task_id"),
        Index("ix_career_opportunities_status_deadline_at", "status", "deadline_at"),
        Index("ix_career_opportunities_kind_status", "kind", "status"),
        CheckConstraint("id > 0", name="ck_career_opportunities_id_positive"),
        CheckConstraint(
            "source_event_id IS NULL OR source_event_id > 0",
            name="ck_career_opportunities_source_event_id_positive",
        ),
        CheckConstraint(
            "linked_task_id IS NULL OR linked_task_id > 0",
            name="ck_career_opportunities_linked_task_id_positive",
        ),
        CheckConstraint(
            "length(company) <= 255 AND length(trim(company)) > 0",
            name="ck_career_opportunities_company",
        ),
        CheckConstraint(
            "length(title) <= 255 AND length(trim(title)) > 0",
            name="ck_career_opportunities_title",
        ),
        CheckConstraint(
            "kind IN ('company', 'job', 'event')",
            name="ck_career_opportunities_kind",
        ),
        CheckConstraint(
            "status IN ('open', 'interested', 'applied', 'in_progress', 'accepted', "
            "'closed', 'rejected', 'withdrawn', 'archived')",
            name="ck_career_opportunities_status",
        ),
        CheckConstraint(
            "length(source) <= 64 AND length(trim(source)) > 0",
            name="ck_career_opportunities_source",
        ),
        CheckConstraint(
            "external_id IS NULL OR "
            "(length(external_id) <= 255 AND length(trim(external_id)) > 0)",
            name="ck_career_opportunities_external_id",
        ),
        CheckConstraint(
            "url IS NULL OR "
            "(length(url) <= 2048 AND length(trim(url)) > 0 AND "
            "(url LIKE 'http://%' OR url LIKE 'https://%'))",
            name="ck_career_opportunities_url",
        ),
        CheckConstraint(
            "location IS NULL OR length(location) <= 255",
            name="ck_career_opportunities_location",
        ),
        CheckConstraint(
            "notes IS NULL OR length(notes) <= 4000",
            name="ck_career_opportunities_notes",
        ),
        CheckConstraint(
            "next_action IS NULL OR length(next_action) <= 255",
            name="ck_career_opportunities_next_action",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    company: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), default="job", nullable=False)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="open", nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="manual", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_action: Mapped[str | None] = mapped_column(String(255), nullable=True)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    linked_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("life_tasks.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)

    source_event: Mapped[Event | None] = relationship(
        back_populates="career_opportunity",
        foreign_keys=[source_event_id],
        lazy="joined",
    )
    linked_task: Mapped[LifeTask | None] = relationship(
        foreign_keys=[linked_task_id],
        lazy="joined",
    )

    @property
    def role(self) -> str:
        """Compatibility alias for callers that use role terminology."""

        return self.title

    @role.setter
    def role(self, value: str) -> None:
        self.title = value

    @property
    def deadline(self) -> datetime | None:
        """Short alias for the explicit deadline timestamp."""

        return self.deadline_at

    @deadline.setter
    def deadline(self, value: datetime | None) -> None:
        self.deadline_at = value


class CareerApplication(Base):
    """An application and its stage history for one career opportunity."""

    __tablename__ = "career_applications"
    __table_args__ = (
        UniqueConstraint("source_event_id", name="uq_career_applications_source_event_id"),
        UniqueConstraint(
            "source",
            "external_id",
            name="uq_career_applications_source_external_id",
        ),
        UniqueConstraint("linked_task_id", name="uq_career_applications_linked_task_id"),
        Index("ix_career_applications_opportunity_stage", "opportunity_id", "stage"),
        Index("ix_career_applications_status_deadline_at", "status", "deadline_at"),
        CheckConstraint("id > 0", name="ck_career_applications_id_positive"),
        CheckConstraint(
            "opportunity_id > 0",
            name="ck_career_applications_opportunity_id_positive",
        ),
        CheckConstraint(
            "source_event_id IS NULL OR source_event_id > 0",
            name="ck_career_applications_source_event_id_positive",
        ),
        CheckConstraint(
            "linked_task_id IS NULL OR linked_task_id > 0",
            name="ck_career_applications_linked_task_id_positive",
        ),
        CheckConstraint(
            "stage IN ('saved', 'interested', 'applied', 'screening', 'interview', "
            "'final_interview', 'offer', 'accepted', 'rejected', 'withdrawn', 'closed')",
            name="ck_career_applications_stage",
        ),
        CheckConstraint(
            "status IN ('open', 'interested', 'applied', 'in_progress', 'accepted', "
            "'closed', 'rejected', 'withdrawn', 'archived')",
            name="ck_career_applications_status",
        ),
        CheckConstraint(
            "length(source) <= 64 AND length(trim(source)) > 0",
            name="ck_career_applications_source",
        ),
        CheckConstraint(
            "external_id IS NULL OR "
            "(length(external_id) <= 255 AND length(trim(external_id)) > 0)",
            name="ck_career_applications_external_id",
        ),
        CheckConstraint(
            "notes IS NULL OR length(notes) <= 4000",
            name="ck_career_applications_notes",
        ),
        CheckConstraint(
            "next_action IS NULL OR length(next_action) <= 255",
            name="ck_career_applications_next_action",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    opportunity_id: Mapped[int] = mapped_column(
        ForeignKey("career_opportunities.id", ondelete="RESTRICT"), nullable=False
    )
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    stage: Mapped[str] = mapped_column(String(32), default="saved", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="open", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="manual", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_action: Mapped[str | None] = mapped_column(String(255), nullable=True)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    linked_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("life_tasks.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)

    opportunity: Mapped[CareerOpportunity] = relationship(
        foreign_keys=[opportunity_id],
        lazy="joined",
    )
    source_event: Mapped[Event | None] = relationship(
        back_populates="career_application",
        foreign_keys=[source_event_id],
        lazy="joined",
    )
    linked_task: Mapped[LifeTask | None] = relationship(
        foreign_keys=[linked_task_id],
        lazy="joined",
    )

    @property
    def company(self) -> str:
        """Expose the normalized company through the related opportunity."""

        return self.opportunity.company

    @property
    def title(self) -> str:
        """Expose the related role/title for compact list consumers."""

        return self.opportunity.title

    @property
    def role(self) -> str:
        return self.title

    @property
    def application_stage(self) -> str:
        return self.stage

    @application_stage.setter
    def application_stage(self, value: str) -> None:
        self.stage = value

    @property
    def deadline(self) -> datetime | None:
        return self.deadline_at

    @deadline.setter
    def deadline(self, value: datetime | None) -> None:
        self.deadline_at = value


class DailyRoutine(Base):
    """A local-time daily routine definition."""

    __tablename__ = "daily_routines"
    __table_args__ = (
        Index("ix_daily_routines_enabled_reminder_time", "enabled", "reminder_time"),
        CheckConstraint(
            "length(title) <= 255 AND length(trim(title)) > 0",
            name="ck_daily_routines_title",
        ),
        CheckConstraint(
            "length(reminder_time) = 5 "
            "AND substr(reminder_time, 3, 1) = ':' "
            "AND substr(reminder_time, 1, 2) BETWEEN '00' AND '23' "
            "AND substr(reminder_time, 4, 2) BETWEEN '00' AND '59'",
            name="ck_daily_routines_reminder_time",
        ),
        CheckConstraint("enabled IN (0, 1)", name="ck_daily_routines_enabled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    reminder_time: Mapped[str] = mapped_column(String(5), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )

    completions: Mapped[list[RoutineCompletion]] = relationship(
        back_populates="routine", cascade="all, delete-orphan", lazy="selectin"
    )


class RoutineCompletion(Base):
    """One completion marker for one routine and one local calendar date."""

    __tablename__ = "routine_completions"
    __table_args__ = (
        UniqueConstraint(
            "routine_id",
            "local_date",
            name="uq_routine_completions_routine_local_date",
        ),
        Index("ix_routine_completions_local_date", "local_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    routine_id: Mapped[int] = mapped_column(
        ForeignKey("daily_routines.id", ondelete="CASCADE"), nullable=False
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)

    routine: Mapped[DailyRoutine] = relationship(back_populates="completions")


class ReminderDelivery(Base):
    """Durable claim and delivery state for one reminder occurrence.

    ``dedupe_key`` is intentionally materialized instead of relying on
    nullable task/routine foreign-key columns.  SQLite treats NULL values as
    distinct in unique constraints, while this single subject identity keeps
    concurrent Bot instances on the same occurrence mutually exclusive.
    """

    __tablename__ = "reminder_deliveries"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_reminder_deliveries_dedupe_key"),
        UniqueConstraint(
            "subject_type",
            "subject_id",
            "phase",
            "occurrence_key",
            name="uq_reminder_deliveries_occurrence",
        ),
        Index(
            "ix_reminder_deliveries_status_claim_expires_at",
            "status",
            "claim_expires_at",
        ),
        CheckConstraint(
            "subject_type IN ('task', 'routine')",
            name="ck_reminder_deliveries_subject_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'claiming', 'sent', 'failed')",
            name="ck_reminder_deliveries_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_reminder_deliveries_attempt_count"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    occurrence_key: Mapped[str] = mapped_column(String(128), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_default, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_default, onupdate=_utc_default, nullable=False
    )


def event_to_dict(event: Event) -> dict[str, Any]:
    """Small safe serialization helper for tests and adapters."""

    return {
        "id": event.id,
        "discord_message_id": event.discord_message_id,
        "discord_channel_id": event.discord_channel_id,
        "discord_user_id": event.discord_user_id,
        "captured_at": event.captured_at,
        "occurred_at": event.occurred_at,
        "raw_text": event.raw_text,
        "source": event.source,
        "status": event.status,
        "event_type": event.event_type,
        "event_kinds": list(event.event_kinds or []),
        "created_at": event.created_at,
        "updated_at": event.updated_at,
        "copied_to_planner_at": event.copied_to_planner_at,
    }
