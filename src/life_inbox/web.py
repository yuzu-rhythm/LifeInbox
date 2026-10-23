"""Local web UI plus authenticated iPhone integration API.

Most HTML routes are read-only.  The deliberately narrow ``/quick`` form may
store one raw, still-unconfirmed inbox Event after a page-issued anti-CSRF
token is presented; it never calls Discord, AI, or an external provider.  The
dedicated ``/api/v1`` routes retain their separate bearer-authenticated
iPhone-sync boundary.
"""

from __future__ import annotations

import re
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload, selectinload

from .analyzer import NullAnalyzer
from .archive_browser import LocalArchiveBrowser
from .archive_health import ArchiveHealthService
from .attachments import SUPPORTED_IMAGE_TYPES
from .budgets import BudgetMonthView, BudgetService
from .career import CareerRecord, CareerService, normalize_url
from .config import Settings
from .constants import (
    AccountKind,
    ActivityKind,
    EventStatus,
    MealType,
    OutsideHome,
    RhythmGame,
    RhythmTodoStatus,
)
from .db import Database
from .finance import FinanceService
from .health_api import install_health_api
from .maimai import (
    MaimaiMyBestService,
    MaimaiNetFavoriteService,
)
from .models import (
    Attachment,
    Event,
    Expense,
    LifeTask,
    Meal,
    Memo,
    ReminderDelivery,
    RhythmResult,
    RhythmTodo,
)
from .reminders import RoutineService
from .rhythm import RhythmService
from .rhythm_api import install_rhythm_api
from .rhythm_images import RatingImageVerificationError, verify_rating_image
from .rhythm_models import RhythmUserRow
from .rhythm_persistence import RhythmPersistence
from .rhythm_spending_api import install_rhythm_spending_event_api
from .services import InboxService, IncomingMessage
from .social_drafts import SocialDraftService, social_state_label, social_target_label
from .social_models import SocialDraft
from .tasks import TaskService

ACCOUNT_KIND_LABELS = {
    AccountKind.CASH.value: "現金",
    AccountKind.BANK.value: "銀行",
    AccountKind.CARD.value: "クレジットカード",
    AccountKind.E_MONEY.value: "電子マネー",
    AccountKind.INVESTMENT.value: "投資・NISA",
}

RHYTHM_GAME_LABELS = {
    RhythmGame.MAIMAI.value: "maimai",
    RhythmGame.ONGEKI.value: "オンゲキ",
    RhythmGame.CHUNITHM.value: "チュウニズム",
}

RHYTHM_GAME_SHORT_NAMES = {
    RhythmGame.MAIMAI.value: "舞",
    RhythmGame.ONGEKI.value: "音",
    RhythmGame.CHUNITHM.value: "チュ",
}

MEAL_TYPE_LABELS = {
    MealType.BREAKFAST.value: "朝食",
    MealType.LUNCH.value: "昼食",
    MealType.DINNER.value: "夕食",
    MealType.SNACK.value: "間食",
    MealType.UNKNOWN.value: "不明",
}

OUTSIDE_HOME_LABELS = {
    OutsideHome.OUTSIDE.value: "外食",
    OutsideHome.HOME.value: "自炊・家",
    OutsideHome.UNKNOWN.value: "不明",
}

EVENT_STATUS_LABELS = {
    EventStatus.PENDING.value: "確認待ち",
    EventStatus.CONFIRMED.value: "確定",
    EventStatus.IGNORED.value: "除外",
}

ACTIVITY_KIND_LABELS = {
    ActivityKind.EXPENSE.value: "支出",
    ActivityKind.INCOME.value: "収入",
    ActivityKind.TRANSFER.value: "振替",
}

MEAL_ICONS = {
    MealType.BREAKFAST.value: "☀️",
    MealType.LUNCH.value: "🍱",
    MealType.DINNER.value: "🌙",
    MealType.SNACK.value: "🍪",
    MealType.UNKNOWN.value: "🍽️",
}

RHYTHM_RUN_STATE_LABELS = {
    "not_started": "未取得",
    "queued": "取得待ち",
    "openingProvider": "取得元を確認中",
    "authenticationRequired": "認証確認待ち",
    "waitingForManualAction": "手動操作待ち",
    "refreshingSource": "取得元を更新中",
    "collectingData": "データ取得中",
    "renderingImage": "画像を生成中",
    "comparingSnapshot": "前回と比較中",
    "readyToPost": "投稿準備完了",
    "posting": "Discordへ投稿中",
    "posted": "取得・投稿済み",
    "unchanged": "変更なし",
    "failed": "取得失敗",
    "cancelled": "処理終了",
}

RHYTHM_PROVIDER_LABELS = {
    "chunirec_api_v2": "CHUNIREC API",
    "official_net_browser": "公式サイト（ブラウザー）",
}

SOCIAL_STATE_TONES = {
    "pending": "neutral",
    "ready": "info",
    "blocked": "danger",
    "held": "warning",
    "posted": "success",
    "error": "danger",
}

RHYTHM_ERROR_LABELS = {
    "provider_not_configured": "実サイトの設定がありません",
    "authentication_required": "認証が必要です",
    "manual_confirmation_required": "管理者の明示確認が必要です",
    "captcha_required": "CAPTCHAの手動確認が必要です",
    "terms_confirmation_required": "規約確認の手動操作が必要です",
    "provider_network_outage": "取得元に接続できませんでした",
    "provider_outage": "取得元に接続できませんでした",
    "rate_limited": "取得元のレート制限に達しました",
    "timeout": "取得元が時間内に応答しませんでした",
    "invalid_response": "取得元の応答を確認できませんでした",
    "normalized_data_not_observed": "正規化されたデータを確認できませんでした",
    "image_missing": "検証済み画像がありません",
    "discord_publish_failed": "Discordへの投稿に失敗しました",
    "discord_delivery_unknown": "Discordの送信結果を確認できませんでした",
    "discord_channel_not_configured": "Discord投稿先が未設定です",
}

REMINDER_PHASE_LABELS = {
    "24h": "24時間前",
    "1h": "1時間前",
    "due": "期限時刻",
    "overdue": "期限超過",
    "routine": "日課",
}

REMINDER_STATUS_LABELS = {
    "pending": ("待機中", "neutral"),
    "claiming": ("送信中", "warning"),
    "sent": ("送信済み", "success"),
    "failed": ("失敗・再試行待ち", "danger"),
}

CAREER_KIND_LABELS = {
    "company": "企業",
    "job": "求人",
    "event": "イベント",
    "application": "応募",
}

CAREER_STATUS_LABELS = {
    "open": "未処理",
    "interested": "興味あり",
    "applied": "応募済み",
    "in_progress": "進行中",
    "accepted": "承諾",
    "closed": "完了",
    "rejected": "不採用",
    "withdrawn": "辞退",
    "archived": "アーカイブ",
}

CAREER_STAGE_LABELS = {
    "saved": "保存",
    "interested": "興味あり",
    "applied": "応募済み",
    "screening": "書類選考",
    "interview": "面接",
    "final_interview": "最終面接",
    "offer": "オファー",
    "accepted": "承諾",
    "rejected": "不採用",
    "withdrawn": "辞退",
    "closed": "クローズ",
}

CAREER_TASK_STATUS_LABELS = {
    "open": "未完了",
    "completed": "完了",
}

CAREER_TERMINAL_STATUSES = frozenset(
    {"accepted", "closed", "rejected", "withdrawn", "archived"}
)

QUICK_TEXT_MAX_LENGTH = 4_000
# URL-encoded UTF-8 expands Japanese characters to nine ASCII bytes. Keep
# enough room for the advertised 4,000-character field plus form overhead,
# while still bounding the request before decoding it.
QUICK_FORM_MAX_BYTES = 48_000
# ``InboxService`` stores the namespaced token in Event.discord_message_id,
# whose existing schema is 64 characters wide.
QUICK_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,48}$")
QUICK_CAPTURE_TYPES = (
    ("normal", "通常", "用途を決めず、そのまま保留保存"),
    ("memo", "メモ", "あとで読み返す一言"),
    ("task", "タスク", "期限なしのやること"),
    ("meal", "食事", "食べたもの・外食・自炊"),
    ("expense", "支出", "金額や店名を含む支出"),
    ("rhythm", "音ゲー", "TODOやリザルトのメモ"),
    ("career", "就活", "会社・応募・求人のメモ"),
)
QUICK_CAPTURE_TYPE_LABELS = dict((key, label) for key, label, _ in QUICK_CAPTURE_TYPES)
QUICK_CAPTURE_TYPE_KEYS = frozenset(QUICK_CAPTURE_TYPE_LABELS)
QUICK_ERROR_LABELS = {
    "body_too_large": "送信内容が大きすぎます。画面を再読み込みして短く入力してください。",
    "invalid_encoding": "入力を読み取れませんでした。画面を再読み込みしてください。",
    "invalid_form": "送信内容を確認できませんでした。画面を再読み込みしてください。",
    "invalid_type": "記録の種類を選び直してください。",
    "empty": "記録する内容を入力してください。",
    "too_long": f"内容は{QUICK_TEXT_MAX_LENGTH:,}文字以内で入力してください。",
    "invalid_text": "使用できない制御文字が含まれています。",
    "invalid_token": "送信トークンが無効です。画面を再読み込みして再送信してください。",
    "idempotency_conflict": (
        "この送信トークンは別の内容で使用済みです。画面を再読み込みしてください。"
    ),
    "save_failed": "ローカルSQLiteへ保存できませんでした。内容は再送信されていません。",
}


class _QuickCaptureError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _QuickTokenRegistry:
    """Bound page-issued quick-capture tokens without cookies or persistence.

    A random-looking form token alone is not a CSRF defence when an attacker
    can invent another syntactically valid token.  Only tokens issued by this
    process through ``GET /quick`` are accepted.  Successful tokens remain
    valid briefly so an interrupted browser retry can still converge through
    the Event idempotency key.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 30 * 60,
        max_entries: int = 1_024,
    ) -> None:
        if ttl_seconds <= 0 or max_entries <= 0:
            raise ValueError("quick token registry limits must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._expires_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        expired = [token for token, expiry in self._expires_at.items() if expiry < now]
        for token in expired:
            self._expires_at.pop(token, None)
        overflow = len(self._expires_at) - self.max_entries + 1
        if overflow > 0:
            for token, _ in sorted(
                self._expires_at.items(), key=lambda item: item[1]
            )[:overflow]:
                self._expires_at.pop(token, None)

    def issue(self) -> str:
        now = monotonic()
        with self._lock:
            self._prune(now)
            token = _new_quick_token()
            while token in self._expires_at:
                token = _new_quick_token()
            self._expires_at[token] = now + self.ttl_seconds
            return token

    def accepts(self, token: str) -> bool:
        now = monotonic()
        with self._lock:
            self._prune(now)
            return self._expires_at.get(token, 0) >= now


@dataclass(frozen=True, slots=True)
class CategoryTotalView:
    name: str
    total: int
    percent: int | float


@dataclass(frozen=True, slots=True)
class EventRowView:
    id: int
    occurred_label: str
    title: str
    category: str
    subcategory: str | None
    description: str | None
    raw_text: str
    amount_label: str
    account_name: str | None


@dataclass(frozen=True, slots=True)
class AccountView:
    id: int
    name: str
    kind: str
    kind_label: str
    opening_balance: int
    balance: int
    is_debt: bool
    active: bool


@dataclass(frozen=True, slots=True)
class AccountSummaryView:
    kind: str
    label: str
    count: int
    balance: int
    balance_label: str
    is_debt: bool


@dataclass(frozen=True, slots=True)
class ActivityView:
    id: int
    occurred_label: str
    kind_label: str
    description: str | None
    route_label: str
    amount_label: str


@dataclass(frozen=True, slots=True)
class MemoView:
    event_id: int
    body: str
    occurred_label: str


@dataclass(frozen=True, slots=True)
class ReceiptItemView:
    name: str
    quantity: int | None
    unit_price_label: str
    amount_label: str


@dataclass(frozen=True, slots=True)
class AttachmentView:
    id: int
    filename: str


@dataclass(frozen=True, slots=True)
class ExpenseDetailView:
    merchant: str
    amount_label: str
    category_label: str
    description: str | None
    confidence_label: str


@dataclass(frozen=True, slots=True)
class MealDetailView:
    meal_type_label: str
    restaurant: str
    outside_label: str
    description: str | None


@dataclass(frozen=True, slots=True)
class EventDetailView:
    id: int
    title: str
    occurred_label: str
    status_label: str
    capture_hint: str | None
    copied_label: str
    raw_text: str
    amount_label: str | None
    category_label: str | None
    account_name: str | None
    expense: ExpenseDetailView | None
    meal: MealDetailView | None
    receipt_items: tuple[ReceiptItemView, ...]
    attachments: tuple[AttachmentView, ...]


@dataclass(frozen=True, slots=True)
class MealView:
    event_id: int
    attachment_id: int | None
    meal_icon: str
    meal_type_label: str
    title: str
    description: str | None
    raw_text: str
    occurred_label: str
    outside_label: str


@dataclass(frozen=True, slots=True)
class TodoView:
    id: int
    title: str
    occurred_label: str


@dataclass(frozen=True, slots=True)
class GameView:
    key: str
    short: str
    label: str
    todos: tuple[TodoView, ...]


@dataclass(frozen=True, slots=True)
class ResultView:
    attachment_id: int | None
    game_label: str
    note: str | None
    todo_title: str | None
    occurred_label: str


@dataclass(frozen=True, slots=True)
class TodayResultView:
    id: int
    event_id: int
    game_label: str
    note: str | None
    todo_title: str | None
    occurred_label: str
    attachment_ids: tuple[int, ...]

    @property
    def image_count(self) -> int:
        return len(self.attachment_ids)


@dataclass(frozen=True, slots=True)
class MaimaiNetFavoriteView:
    rank: int
    category: str
    title: str
    artist: str


@dataclass(frozen=True, slots=True)
class MaimaiMyBestView:
    rank: int
    title: str
    play_count: int


@dataclass(frozen=True, slots=True)
class ReminderDeliveryView:
    subject_type: str
    subject_id: int
    title: str
    phase_label: str
    status_label: str
    status_tone: str
    attempt_count: int
    updated_label: str
    error_label: str | None


@dataclass(frozen=True, slots=True)
class CareerTaskStatusView:
    id: int
    status_label: str


@dataclass(frozen=True, slots=True)
class CareerRecordView:
    id: int
    record_type_label: str
    company: str
    title: str
    kind_label: str
    status_label: str
    status_tone: str
    stage_label: str | None
    next_action: str | None
    next_action_at_label: str | None
    deadline_label: str | None
    upcoming_focus_label: str | None
    upcoming_at_label: str | None
    url: str | None
    linked_task_id: int | None
    linked_task_status_label: str | None
    is_terminal: bool
    is_overdue: bool


@dataclass(frozen=True, slots=True)
class RatingHistoryView:
    snapshot_id: int
    rating_label: str
    previous_rating_label: str
    delta_label: str
    collected_label: str
    image_url: str | None
    image_label: str


@dataclass(frozen=True, slots=True)
class RhythmStatusView:
    key: str
    short: str
    label: str
    source_label: str
    source_tone: str
    status_label: str
    status_tone: str
    provider_label: str
    latest_rating_label: str
    previous_rating_label: str
    delta_label: str
    latest_collected_label: str
    image_url: str | None
    image_label: str
    last_success_label: str
    last_attempt_label: str
    last_failure_label: str
    error_label: str | None
    history: tuple[RatingHistoryView, ...]


@dataclass(frozen=True, slots=True)
class GuideSectionView:
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class SocialDraftMediaView:
    id: int
    filename: str
    content_type: str


@dataclass(frozen=True, slots=True)
class SocialDraftPageView:
    event_id: int
    raw_text: str
    target: str
    target_label: str
    state: str
    state_label: str
    state_tone: str
    has_media: bool
    media: tuple[SocialDraftMediaView, ...]
    created_label: str
    updated_label: str
    selected_label: str
    published_label: str
    error_label: str | None


def _value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _is_confirmed(value: Any) -> bool:
    return _value(value) == EventStatus.CONFIRMED.value


def _normalize_content_type(value: str | None) -> str:
    return (value or "").lower().split(";", 1)[0].strip()


def _is_image_attachment(attachment: Attachment) -> bool:
    return _normalize_content_type(attachment.content_type) in SUPPORTED_IMAGE_TYPES


def _format_yen(amount: int | None, *, signed: str | None = None) -> str:
    if amount is None:
        return "金額不明"
    prefix = signed or ""
    return f"{prefix}{amount:,}円"


def _format_rating(value: Any, *, signed: bool = False) -> str:
    """Format a persisted rating without exposing provider payloads."""

    if value is None:
        return "未取得"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "不明"
    if not number.is_finite():
        return "不明"
    text = format(number.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-0"}:
        text = "0"
    if signed and number > 0:
        text = "+" + text
    return text


def _confidence_label(confidence: float | None) -> str:
    if confidence is None:
        return "不明"
    if confidence >= 0.9:
        level = "高"
    elif confidence >= 0.7:
        level = "中"
    else:
        level = "低"
    return f"{level}（{confidence:.2f}）"


def _category_label(category: str | None, subcategory: str | None = None) -> str:
    name = category or "未分類"
    return f"{name} > {subcategory}" if subcategory else name


def _display_timezone(settings: Settings) -> ZoneInfo:
    return settings.timezone_info()


def _local_datetime(value: datetime, timezone: ZoneInfo) -> datetime:
    """Interpret database naive timestamps as UTC before displaying them."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(timezone)


def _format_datetime(value: datetime, timezone: ZoneInfo) -> str:
    return _local_datetime(value, timezone).strftime("%Y-%m-%d %H:%M:%S")


def _format_career_datetime(value: datetime, timezone: ZoneInfo) -> str:
    """Format explicit career dates in the configured local timezone."""

    local_value = _local_datetime(value, timezone)
    if local_value.hour == 0 and local_value.minute == 0 and local_value.second == 0:
        return local_value.strftime("%Y-%m-%d")
    return local_value.strftime("%Y-%m-%d %H:%M")


def _career_utc_datetime(value: datetime) -> datetime:
    """Normalize stored timestamps for comparisons without changing storage."""

    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _career_schedule_timestamp(record: CareerRecord) -> datetime | None:
    values = [
        _career_utc_datetime(value)
        for value in (record.deadline_at, record.next_action_at)
        if value is not None
    ]
    return min(values) if values else None


def _safe_career_url(value: str | None) -> str | None:
    """Pass through only URLs that satisfy the career domain's URL guardrail."""

    try:
        return normalize_url(value)
    except (TypeError, ValueError):
        return None


def _format_local_date(value: date) -> str:
    return f"{value.year}年{value.month}月{value.day}日"


def _day_window(target_date: date, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    start_local = datetime.combine(target_date, time.min, tzinfo=timezone)
    end_local = datetime.combine(target_date + timedelta(days=1), time.min, tzinfo=timezone)
    return (
        start_local.astimezone(UTC).replace(tzinfo=None),
        end_local.astimezone(UTC).replace(tzinfo=None),
    )


def _month_text(value: date) -> str:
    return f"{value.year}年{value.month}月"


def _month_window(month: str | None, timezone: ZoneInfo) -> tuple[str, date, datetime, datetime]:
    """Return a validated YYYY-MM and UTC-naive database bounds."""

    if month is None or month == "":
        local_today = datetime.now(timezone).date()
        selected = f"{local_today.year:04d}-{local_today.month:02d}"
    else:
        try:
            year, month_number = (int(part) for part in month.split("-"))
            if len(month) != 7 or month[4] != "-" or not 1 <= month_number <= 12:
                raise ValueError
            selected_date = date(year, month_number, 1)
            selected = f"{selected_date.year:04d}-{selected_date.month:02d}"
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="month must be YYYY-MM") from None

    year, month_number = (int(part) for part in selected.split("-"))
    selected_date = date(year, month_number, 1)
    if month_number == 12:
        next_date = date(year + 1, 1, 1)
    else:
        next_date = date(year, month_number + 1, 1)
    start_local = datetime.combine(selected_date, time.min, tzinfo=timezone)
    end_local = datetime.combine(next_date, time.min, tzinfo=timezone)
    start_utc = start_local.astimezone(UTC).replace(tzinfo=None)
    end_utc = end_local.astimezone(UTC).replace(tzinfo=None)
    return selected, selected_date, start_utc, end_utc


def _parse_minimum(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        minimum = int(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="minimum must be a non-negative integer"
        ) from None
    if minimum < 0:
        raise HTTPException(status_code=400, detail="minimum must be a non-negative integer")
    return minimum


def _safe_attachment_path(data_dir: Path, local_path: str) -> Path | None:
    """Resolve an attachment only when it remains below data_dir/attachments.

    The database normally contains the path produced by AttachmentStore.  A
    second data-dir-relative interpretation is accepted for old rows that
    stored a relative path, while both interpretations go through the same
    resolved-root check.
    """

    try:
        root = (data_dir / "attachments").resolve()
        raw_path = Path(local_path)
        candidates = [raw_path] if raw_path.is_absolute() else [raw_path, data_dir / raw_path]
        for candidate in candidates:
            resolved = candidate.resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            if resolved.is_file():
                return resolved
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return None


def _safe_rhythm_error(code: str | None) -> str | None:
    """Map persisted error codes to a small, non-provider-facing message."""

    if not code:
        return None
    normalized = str(code).strip().casefold()
    if not normalized:
        return None
    for known_code, label in RHYTHM_ERROR_LABELS.items():
        if _error_code_matches(normalized, known_code):
            return label
    return "取得エラー（詳細は非表示）"


def _error_code_matches(code: str | None, known_code: str) -> bool:
    normalized = str(code or "").strip().casefold()
    return normalized == known_code or normalized.endswith(f"_{known_code}")


def _safe_rhythm_provider(value: str | None) -> str:
    if not value:
        return "未設定"
    return RHYTHM_PROVIDER_LABELS.get(str(value), "接続先（詳細非表示）")


def _safe_reminder_error(value: str | None) -> str | None:
    if not value:
        return None
    normalized = str(value).strip().casefold()
    if normalized.startswith("send_failed"):
        return "Discordへの送信に失敗しました"
    return _safe_rhythm_error(normalized) or "送信エラー（詳細は非表示）"


def _safe_rhythm_state(value: str | None) -> tuple[str, str]:
    state = str(value or "not_started")
    label = RHYTHM_RUN_STATE_LABELS.get(state, "状態を確認中")
    if state in {"posted", "unchanged"}:
        return label, "success"
    if state in {"failed", "authenticationRequired", "waitingForManualAction"}:
        return label, "danger" if state == "failed" else "warning"
    if state == "not_started":
        return label, "neutral"
    return label, "info"


def _is_fixture_snapshot(snapshot: Any, run: Any) -> bool:
    """Recognize fixture markers without displaying the stored provider data."""

    def fixture_marker(value: Any) -> bool:
        normalized = str(value or "").strip().casefold()
        return normalized in {"fixture", "test", "sample"} or any(
            normalized.startswith(f"{marker}:")
            or normalized.startswith(f"{marker}-")
            or normalized.startswith(f"{marker}_")
            for marker in ("fixture", "test", "sample")
        )

    if fixture_marker(getattr(run, "trigger", "")):
        return True
    raw_data = getattr(snapshot, "raw_data", None)
    if not isinstance(raw_data, dict):
        return False
    for key in ("fixture", "is_fixture", "test_fixture"):
        if raw_data.get(key) is True:
            return True
    source = raw_data.get("source")
    return fixture_marker(source)


def _quick_capture_type(value: str | None) -> str:
    normalized = (value or "").strip().casefold()
    if normalized not in QUICK_CAPTURE_TYPE_KEYS:
        raise _QuickCaptureError("invalid_type")
    return normalized


def _quick_text(value: str | None) -> str:
    if not isinstance(value, str):
        raise _QuickCaptureError("empty")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise _QuickCaptureError("empty")
    if len(normalized) > QUICK_TEXT_MAX_LENGTH:
        raise _QuickCaptureError("too_long")
    if any(ord(character) < 32 and character not in "\n\t" for character in normalized):
        raise _QuickCaptureError("invalid_text")
    return normalized


def _quick_token(value: str | None) -> str:
    token = (value or "").strip()
    if QUICK_TOKEN_PATTERN.fullmatch(token) is None:
        raise _QuickCaptureError("invalid_token")
    return token


def _new_quick_token() -> str:
    return secrets.token_urlsafe(24)


def _quick_redirect_url(
    *,
    state: str,
    capture_type: str | None = None,
    event_id: int | None = None,
    error: str | None = None,
) -> str:
    values: dict[str, str | int] = {"state": state}
    if capture_type in QUICK_CAPTURE_TYPE_KEYS:
        values["capture_type"] = capture_type
    if event_id is not None and event_id > 0:
        values["event_id"] = event_id
    if error in QUICK_ERROR_LABELS:
        values["error"] = error
    return f"/quick?{urlencode(values)}"


async def _read_quick_form(request: Request) -> dict[str, str]:
    """Read the browser's URL-encoded form without adding multipart support."""

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type != "application/x-www-form-urlencoded":
        raise _QuickCaptureError("invalid_form")
    body_parts: list[bytes] = []
    body_size = 0
    async for chunk in request.stream():
        body_size += len(chunk)
        if body_size > QUICK_FORM_MAX_BYTES:
            raise _QuickCaptureError("body_too_large")
        body_parts.append(chunk)
    body = b"".join(body_parts)
    try:
        values = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            max_num_fields=8,
        )
    except (UnicodeDecodeError, ValueError):
        raise _QuickCaptureError("invalid_encoding") from None

    result: dict[str, str] = {}
    for name in ("capture_type", "text", "idempotency_token"):
        candidates = values.get(name, [])
        if len(candidates) > 1:
            raise _QuickCaptureError("invalid_form")
        result[name] = candidates[0] if candidates else ""
    return result


class _WebReader:
    """Read and detach all data needed by the existing templates."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        finance: FinanceService,
        task_service: TaskService | None = None,
        routine_service: RoutineService | None = None,
        inbox_service: InboxService | None = None,
        budget_service: BudgetService | None = None,
        rhythm_service: RhythmService | None = None,
        mybest_service: MaimaiMyBestService | None = None,
        net_favorites_service: MaimaiNetFavoriteService | None = None,
        career_service: CareerService | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.finance = finance
        self.career = career_service or CareerService(
            database.session_factory,
            timezone=settings.timezone_info(),
        )
        self.rhythm_service = rhythm_service or RhythmService(database.session_factory)
        self.rhythm_persistence = RhythmPersistence(database.session_factory)
        self.mybest_service = mybest_service or MaimaiMyBestService(database.session_factory)
        self.net_favorites_service = net_favorites_service or MaimaiNetFavoriteService(
            database.session_factory
        )
        self.timezone = _display_timezone(settings)
        self.tasks = task_service or TaskService(
            database.session_factory,
            timezone=self.timezone,
        )
        self.routines = routine_service or RoutineService(
            database.session_factory,
            timezone=self.timezone,
        )
        # ``capture_raw`` is deliberately the only quick-capture operation
        # used by this UI. NullAnalyzer makes the boundary explicit: a Web
        # submit never invokes AI, Discord, or a provider.
        self.inbox = inbox_service or InboxService(
            database.session_factory,
            settings,
            NullAnalyzer(),
        )
        self.social_drafts = SocialDraftService(database.session_factory)
        self.budgets = budget_service or BudgetService(
            database.session_factory,
            timezone=self.timezone,
            allowed_categories=settings.categories,
        )
        self.budget_service = self.budgets

    def quick_capture(self, capture_type: str | None, text: str | None, token: str | None) -> int:
        """Persist one bounded raw Web capture through the existing InboxService.

        The idempotency token becomes the namespaced Event message identity.
        Replays with the same token and exact payload return the original Event;
        a changed payload is rejected instead of creating a second record.
        Type is retained in the namespaced channel marker as a hint only. The
        Web boundary never pretends that a pending raw Event is a confirmed
        expense, meal, task, rhythm result, or career record.
        """

        normalized_type = _quick_capture_type(capture_type)
        normalized_text = _quick_text(text)
        normalized_token = _quick_token(token)
        channel_marker = f"web-quick:{normalized_type}"
        message = IncomingMessage(
            discord_message_id=f"web-quick:{normalized_token}",
            discord_channel_id=channel_marker,
            discord_user_id=(
                str(self.settings.allowed_user_id)
                if self.settings.allowed_user_id is not None
                else "web-local"
            ),
            raw_text=normalized_text,
            captured_at=datetime.now(UTC),
        )
        captured = self.inbox.capture_raw(message)
        event = self.inbox.get_event(captured.event_id)
        if (
            event is None
            or event.discord_channel_id != channel_marker
            or event.raw_text != normalized_text
        ):
            raise _QuickCaptureError("idempotency_conflict")
        return captured.event_id

    def _confirmed_expense_pairs(
        self,
        start_utc: datetime | None = None,
        end_utc: datetime | None = None,
        *,
        category: str | None = None,
        minimum: int | None = None,
        dining_only: bool = False,
    ) -> list[tuple[Expense, Event]]:
        conditions = [Event.status == EventStatus.CONFIRMED.value]
        if start_utc is not None and end_utc is not None:
            conditions.extend(
                [Event.occurred_at >= start_utc, Event.occurred_at < end_utc]
            )
        statement = (
            select(Expense, Event)
            .join(Event, Expense.event_id == Event.id)
            .where(*conditions)
            .order_by(Event.occurred_at.desc(), Event.id.desc(), Expense.id.desc())
        )
        if category:
            statement = statement.where(Expense.category == category)
        if minimum is not None:
            statement = statement.where(Expense.amount >= minimum)
        if dining_only:
            statement = statement.where(
                Expense.category == "食費", Expense.subcategory == "外食"
            )
        with self.database.session() as session:
            return list(session.execute(statement).all())

    def _activity_views(self) -> tuple[list[ActivityView], dict[int, str]]:
        views: list[ActivityView] = []
        account_names: dict[int, str] = {}
        for activity in self.finance.list_activities():
            if activity.event_id is not None and activity.event_status is not None:
                if not _is_confirmed(activity.event_status):
                    continue
            kind = _value(activity.kind)
            source = getattr(activity, "source_account_name", None)
            destination = getattr(activity, "destination_account_name", None)
            if kind == ActivityKind.EXPENSE.value:
                route = f"支払元: {source}" if source else "支払元: 未指定"
                account_name = source
                amount_label = _format_yen(activity.amount, signed="-")
            elif kind == ActivityKind.INCOME.value:
                route = f"入金先: {destination}" if destination else "入金先: 未指定"
                account_name = destination
                amount_label = _format_yen(activity.amount, signed="+")
            else:
                route = f"{source or '未指定'} → {destination or '未指定'}"
                account_name = route
                amount_label = _format_yen(activity.amount)
            if activity.event_id is not None:
                account_names[activity.event_id] = account_name or route
            views.append(
                ActivityView(
                    id=activity.id,
                    occurred_label=_format_datetime(activity.occurred_at, self.timezone),
                    kind_label=ACTIVITY_KIND_LABELS.get(kind, kind),
                    description=activity.description,
                    route_label=route,
                    amount_label=amount_label,
                )
            )
        return views, account_names

    def account_views(self) -> list[AccountView]:
        accounts: list[AccountView] = []
        for account in self.finance.list_accounts():
            kind = _value(account.kind)
            accounts.append(
                AccountView(
                    id=account.id,
                    name=account.name,
                    kind=kind,
                    kind_label=ACCOUNT_KIND_LABELS.get(kind, kind),
                    opening_balance=account.opening_balance,
                    balance=account.balance,
                    is_debt=kind == AccountKind.CARD.value and account.balance > 0,
                    active=account.active,
                )
            )
        return accounts

    @staticmethod
    def account_summary_views(accounts: list[AccountView]) -> list[AccountSummaryView]:
        """Group balances for the dashboard while retaining account detail links."""

        grouped: dict[str, list[AccountView]] = {}
        for account in accounts:
            grouped.setdefault(account.kind, []).append(account)
        summaries: list[AccountSummaryView] = []
        for kind in (
            AccountKind.CASH.value,
            AccountKind.BANK.value,
            AccountKind.CARD.value,
            AccountKind.E_MONEY.value,
            AccountKind.INVESTMENT.value,
        ):
            matching = grouped.get(kind)
            if not matching:
                continue
            balance = sum(account.balance for account in matching)
            is_debt = kind == AccountKind.CARD.value
            summaries.append(
                AccountSummaryView(
                    kind=kind,
                    label=ACCOUNT_KIND_LABELS.get(kind, kind),
                    count=len(matching),
                    balance=balance,
                    balance_label=_format_yen(balance),
                    is_debt=is_debt and balance > 0,
                )
            )
        return summaries

    def _event_row(
        self,
        expense: Expense,
        event: Event,
        account_names: dict[int, str],
    ) -> EventRowView:
        merchant = expense.merchant_normalized or expense.merchant_raw
        title = merchant or expense.description or event.raw_text or f"記録 #{event.id}"
        return EventRowView(
            id=event.id,
            occurred_label=_format_datetime(event.occurred_at, self.timezone),
            title=title,
            category=expense.category or "未分類",
            subcategory=expense.subcategory,
            description=expense.description,
            raw_text=event.raw_text,
            amount_label=_format_yen(expense.amount),
            account_name=account_names.get(event.id),
        )

    def _memos(self, *, limit: int | None = None) -> list[MemoView]:
        statement = (
            select(Memo, Event)
            .join(Event, Memo.event_id == Event.id)
            .where(Event.status == EventStatus.CONFIRMED.value)
            .order_by(Event.occurred_at.desc(), Memo.created_at.desc(), Memo.id.desc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        with self.database.session() as session:
            return [
                MemoView(
                    event_id=event.id,
                    body=memo.body,
                    occurred_label=_format_datetime(event.occurred_at, self.timezone),
                )
                for memo, event in session.execute(statement).all()
            ]

    def _meal_pairs(
        self,
        start_utc: datetime | None = None,
        end_utc: datetime | None = None,
    ) -> list[tuple[Meal, Event]]:
        conditions = [Event.status == EventStatus.CONFIRMED.value]
        if start_utc is not None and end_utc is not None:
            conditions.extend(
                [Event.occurred_at >= start_utc, Event.occurred_at < end_utc]
            )
        statement = (
            select(Meal, Event)
            .join(Event, Meal.event_id == Event.id)
            .where(*conditions)
            .order_by(Event.occurred_at.desc(), Event.id.desc(), Meal.id.desc())
        )
        with self.database.session() as session:
            return list(session.execute(statement).all())

    @staticmethod
    def _first_image_by_event(
        session: Any,
        event_ids: list[int],
    ) -> dict[int, int]:
        if not event_ids:
            return {}
        attachments = session.scalars(
            select(Attachment)
            .where(Attachment.event_id.in_(event_ids))
            .order_by(Attachment.event_id, Attachment.id)
        ).all()
        result: dict[int, int] = {}
        for attachment in attachments:
            if _is_image_attachment(attachment) and attachment.event_id not in result:
                result[attachment.event_id] = attachment.id
        return result

    def meal_views(self, *, month: str | None = None) -> tuple[list[MealView], int]:
        if month is None:
            pairs = self._meal_pairs()
        else:
            _, _, start_utc, end_utc = _month_window(month, self.timezone)
            pairs = self._meal_pairs(start_utc, end_utc)
        event_ids = [event.id for _, event in pairs]
        with self.database.session() as session:
            image_ids = self._first_image_by_event(session, event_ids)
        meals = [
            MealView(
                event_id=event.id,
                attachment_id=image_ids.get(event.id),
                meal_icon=MEAL_ICONS.get(
                    _value(meal.meal_type), MEAL_ICONS[MealType.UNKNOWN.value]
                ),
                meal_type_label=MEAL_TYPE_LABELS.get(_value(meal.meal_type), "不明"),
                title=(
                    meal.restaurant_normalized
                    or meal.restaurant_raw
                    or meal.description
                    or event.raw_text
                    or f"食事記録 #{event.id}"
                ),
                description=meal.description,
                raw_text=event.raw_text,
                occurred_label=_format_datetime(event.occurred_at, self.timezone),
                outside_label=OUTSIDE_HOME_LABELS.get(_value(meal.outside_home), "不明"),
            )
            for meal, event in pairs
        ]
        outside_count = sum(
            1 for meal, _ in pairs if _value(meal.outside_home) == OutsideHome.OUTSIDE.value
        )
        return meals, outside_count

    def _task_groups(self) -> dict[str, list[Any]]:
        task_views = self.tasks.list_tasks()
        open_tasks = [task for task in task_views if task.status == "open"]
        completed_tasks = [task for task in task_views if task.status == "completed"]
        today = datetime.now(self.timezone).date()
        overdue_tasks = [task for task in open_tasks if task.is_overdue]
        due_today = [
            task
            for task in open_tasks
            if task.due_date == today and not task.is_overdue
        ]
        upcoming_tasks = [
            task for task in open_tasks if task.due_date is not None and task.due_date > today
        ]
        return {
            "tasks": task_views,
            "open_tasks": open_tasks,
            "completed_tasks": completed_tasks,
            "overdue_tasks": overdue_tasks,
            "due_today_tasks": due_today,
            "upcoming_tasks": upcoming_tasks,
        }

    def _career_task_statuses(
        self,
        records: list[CareerRecord],
    ) -> dict[int, CareerTaskStatusView]:
        task_ids = sorted(
            {
                record.linked_task_id
                for record in records
                if isinstance(record.linked_task_id, int) and record.linked_task_id > 0
            }
        )
        if not task_ids:
            return {}
        try:
            with self.database.session() as session:
                tasks = session.scalars(
                    select(LifeTask).where(LifeTask.id.in_(task_ids))
                ).all()
        except SQLAlchemyError:
            return {}
        return {
            int(task.id): CareerTaskStatusView(
                id=int(task.id),
                status_label=CAREER_TASK_STATUS_LABELS.get(
                    str(task.status), "状態を確認中"
                ),
            )
            for task in tasks
        }

    def _career_record_view(
        self,
        record: CareerRecord,
        *,
        task_statuses: dict[int, CareerTaskStatusView],
        now_utc: datetime,
    ) -> CareerRecordView:
        status = str(record.status)
        stage = str(record.stage) if record.stage is not None else None
        is_terminal = record.is_closed or status in CAREER_TERMINAL_STATUSES
        if is_terminal:
            status_tone = "neutral"
        elif status == "interested":
            status_tone = "success"
        elif status in {"applied", "in_progress"}:
            status_tone = "info"
        else:
            status_tone = "neutral"

        deadline_at = (
            _career_utc_datetime(record.deadline_at) if record.deadline_at is not None else None
        )
        next_action_at = (
            _career_utc_datetime(record.next_action_at)
            if record.next_action_at is not None
            else None
        )
        if next_action_at is not None and (
            deadline_at is None or next_action_at <= deadline_at
        ):
            upcoming_focus_label = "次の行動"
            upcoming_at = record.next_action_at
        elif deadline_at is not None:
            upcoming_focus_label = "明示期限"
            upcoming_at = record.deadline_at
        elif record.next_action:
            upcoming_focus_label = "次の行動"
            upcoming_at = None
        else:
            upcoming_focus_label = None
            upcoming_at = None

        linked_task_id = record.linked_task_id
        linked_task_status = (
            task_statuses.get(linked_task_id) if linked_task_id is not None else None
        )
        schedule_timestamp = _career_schedule_timestamp(record)
        return CareerRecordView(
            id=record.id,
            record_type_label=("応募" if record.record_type == "application" else "保存"),
            company=record.company,
            title=record.title,
            kind_label=CAREER_KIND_LABELS.get(record.kind, "種別を確認中"),
            status_label=CAREER_STATUS_LABELS.get(status, "状態を確認中"),
            status_tone=status_tone,
            stage_label=CAREER_STAGE_LABELS.get(stage, "段階を確認中") if stage else None,
            next_action=record.next_action,
            next_action_at_label=(
                _format_career_datetime(record.next_action_at, self.timezone)
                if record.next_action_at is not None
                else None
            ),
            deadline_label=(
                _format_career_datetime(record.deadline_at, self.timezone)
                if record.deadline_at is not None
                else None
            ),
            upcoming_focus_label=upcoming_focus_label,
            upcoming_at_label=(
                _format_career_datetime(upcoming_at, self.timezone)
                if upcoming_at is not None
                else "日時未設定"
                if upcoming_focus_label is not None
                else None
            ),
            url=_safe_career_url(record.url),
            linked_task_id=linked_task_id,
            linked_task_status_label=(
                linked_task_status.status_label
                if linked_task_status is not None
                else "状態不明"
                if linked_task_id is not None
                else None
            ),
            is_terminal=is_terminal,
            is_overdue=(
                not is_terminal
                and schedule_timestamp is not None
                and schedule_timestamp < now_utc
            ),
        )

    def _career_views(self) -> tuple[list[CareerRecordView], bool]:
        """Read career records and detach only fields suitable for the Web UI."""

        try:
            records = self.career.list_records(include_closed=True)
        except SQLAlchemyError:
            # Keep an older database useful until its additive career tables
            # are created by the normal database initialization path.
            return [], False

        records = sorted(
            records,
            key=lambda record: (
                record.is_closed,
                _career_schedule_timestamp(record) or datetime.max,
                record.id,
                record.record_type,
            ),
        )
        task_statuses = self._career_task_statuses(records)
        now_utc = datetime.now(UTC).replace(tzinfo=None)
        return [
            self._career_record_view(
                record,
                task_statuses=task_statuses,
                now_utc=now_utc,
            )
            for record in records
        ], True

    def _career_context_parts(self) -> dict[str, Any]:
        records, available = self._career_views()
        active = [record for record in records if not record.is_terminal]
        upcoming = [
            record
            for record in active
            if record.upcoming_focus_label is not None
        ]
        applications = [
            record
            for record in active
            if record.record_type_label == "応募"
        ]
        opportunities = [
            record
            for record in active
            if record.record_type_label != "応募"
        ]
        terminal = [record for record in records if record.is_terminal]
        return {
            "career_available": available,
            "career_records": records,
            "active_applications": applications,
            "saved_opportunities": opportunities,
            "upcoming_career": upcoming,
            "terminal_career": terminal,
            "career_active_count": len(active),
            "career_next_item": upcoming[0] if upcoming else None,
        }

    def _routine_views(self) -> tuple[list[Any], bool]:
        """Read routines without making the dashboard depend on Discord."""

        try:
            return self.routines.list_routines(), True
        except SQLAlchemyError:
            # A pre-upgrade database may not have the additive routine table
            # until the normal ``init`` path runs. Keep the rest of the page
            # useful and describe the missing optional section in the UI.
            return [], False

    def _reminder_delivery_views(
        self,
        tasks: list[Any],
        routines: list[Any],
    ) -> tuple[list[ReminderDeliveryView], bool]:
        task_titles = {task.id: task.title for task in tasks}
        routine_titles = {routine.id: routine.title for routine in routines}
        try:
            with self.database.session() as session:
                rows = list(
                    session.scalars(
                        select(ReminderDelivery)
                        .order_by(ReminderDelivery.updated_at.desc(), ReminderDelivery.id.desc())
                        .limit(20)
                    ).all()
                )
        except SQLAlchemyError:
            return [], False

        views: list[ReminderDeliveryView] = []
        for row in rows:
            subject_type = str(row.subject_type)
            subject_id = int(row.subject_id)
            if subject_type == "task":
                title = task_titles.get(subject_id, f"タスク #{subject_id}")
            elif subject_type == "routine":
                title = routine_titles.get(subject_id, f"日課 #{subject_id}")
            else:
                # The schema restricts this value, but do not trust old rows
                # when constructing a CSS class or a user-facing label.
                continue
            status_label, status_tone = REMINDER_STATUS_LABELS.get(
                str(row.status), ("状態不明", "neutral")
            )
            views.append(
                ReminderDeliveryView(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    title=title,
                    phase_label=REMINDER_PHASE_LABELS.get(str(row.phase), "リマインダー"),
                    status_label=status_label,
                    status_tone=status_tone,
                    attempt_count=max(0, int(row.attempt_count or 0)),
                    updated_label=_format_datetime(row.updated_at, self.timezone),
                    error_label=_safe_reminder_error(row.last_error),
                )
            )
        return views, True

    def _rhythm_user_id(self) -> int | None:
        """Find the already-created local rhythm user without creating one."""

        statement = select(RhythmUserRow.id)
        if self.settings.allowed_user_id is not None:
            statement = statement.where(
                RhythmUserRow.discord_user_id == str(self.settings.allowed_user_id)
            )
        statement = statement.order_by(RhythmUserRow.id).limit(1)
        with self.database.session() as session:
            return session.scalar(statement)

    def _verified_rating_image(self, snapshot: Any) -> bool:
        for link in getattr(snapshot, "image_links", ()) or ():
            image = getattr(link, "image", None)
            if image is None:
                continue
            try:
                verify_rating_image(
                    image_root=self.settings.rhythm_image_dir,
                    image_path=image.image_path,
                    expected_sha256=image.image_sha256,
                    expected_byte_size=image.byte_size,
                )
            except (RatingImageVerificationError, OSError, RuntimeError, TypeError, ValueError):
                continue
            return True
        return False

    def _rating_history_view(self, rows: list[Any]) -> tuple[RatingHistoryView, ...]:
        views: list[RatingHistoryView] = []
        for row in rows:
            image_available = self._verified_rating_image(row)
            views.append(
                RatingHistoryView(
                    snapshot_id=int(row.id),
                    rating_label=_format_rating(row.rating),
                    previous_rating_label=_format_rating(row.previous_rating),
                    delta_label=(
                        _format_rating(row.rating_delta, signed=True)
                        if row.rating_delta is not None
                        else "—"
                    ),
                    collected_label=_format_datetime(row.collected_at, self.timezone),
                    image_url=(
                        f"/rhythm/images/{int(row.id)}" if image_available else None
                    ),
                    image_label="画像あり" if image_available else "画像なし",
                )
            )
        return tuple(views)

    def _empty_rhythm_status_views(self) -> list[RhythmStatusView]:
        return [
            RhythmStatusView(
                key=game,
                short=RHYTHM_GAME_SHORT_NAMES[game],
                label=RHYTHM_GAME_LABELS[game],
                source_label="未設定",
                source_tone="neutral",
                status_label="未取得",
                status_tone="neutral",
                provider_label="未設定",
                latest_rating_label="未取得",
                previous_rating_label="未取得",
                delta_label="—",
                latest_collected_label="—",
                image_url=None,
                image_label="画像なし",
                last_success_label="—",
                last_attempt_label="—",
                last_failure_label="—",
                error_label=None,
                history=(),
            )
            for game in RHYTHM_GAME_LABELS
        ]

    def rhythm_status_views(
        self,
        *,
        include_history: bool = True,
    ) -> tuple[list[RhythmStatusView], bool]:
        """Build safe per-game collection/rating views from local snapshots."""

        try:
            user_id = self._rhythm_user_id()
            if user_id is None:
                return self._empty_rhythm_status_views(), True
            latest_runs = self.rhythm_persistence.latest_runs(user_id=user_id)
            failed_runs = self.rhythm_persistence.latest_failed_runs(user_id=user_id)
            providers = self.rhythm_persistence.provider_statuses(user_id=user_id)
            status_views: list[RhythmStatusView] = []
            for game in RHYTHM_GAME_LABELS:
                latest = self.rhythm_persistence.latest_snapshot(user_id=user_id, game=game)
                rendered = self.rhythm_persistence.latest_rendered_snapshot(
                    user_id=user_id,
                    game=game,
                )
                rows: list[Any] = []
                if include_history:
                    rows, _ = self.rhythm_persistence.list_snapshots(
                        user_id=user_id,
                        game=game,
                        limit=12,
                    )
                run = latest_runs.get(game)
                failed = failed_runs.get(game)
                provider = providers.get(game)
                latest_image_available = latest is not None and self._verified_rating_image(latest)
                rendered_image_available = rendered is not None and self._verified_rating_image(
                    rendered
                )
                state = run.state if run is not None else None
                if state is None and provider is not None:
                    state = provider.session_status
                if run is None and latest is not None:
                    status_label, status_tone = ("保存済み", "success")
                elif state in {"posted", "unchanged", "cancelled"} and rendered is not None:
                    status_label = "保存済み"
                    status_tone = "success"
                elif state in {"failed"}:
                    status_label, status_tone = _safe_rhythm_state(state)
                elif state in {"success"} and rendered is not None:
                    status_label, status_tone = "取得済み", "success"
                elif state in {"success"}:
                    status_label, status_tone = "取得済み（画像なし）", "warning"
                elif state in {"not_configured", "unknown", None}:
                    status_label, status_tone = "未取得", "neutral"
                else:
                    status_label, status_tone = _safe_rhythm_state(state)

                error_code = None
                if run is not None and run.state == "failed":
                    error_code = run.last_error_code
                if error_code is None and provider is not None:
                    error_code = provider.last_error_code
                if error_code is None and failed is not None:
                    error_code = failed.last_error_code

                source_is_fixture = _is_fixture_snapshot(latest, run)
                if source_is_fixture:
                    source_label, source_tone = "fixture", "warning"
                elif _error_code_matches(error_code, "provider_not_configured") or (
                    provider is not None and provider.session_status == "not_configured"
                ):
                    source_label, source_tone = "未設定", "neutral"
                elif latest is not None or run is not None or provider is not None:
                    source_label, source_tone = "実ソース", "success"
                else:
                    source_label, source_tone = "未設定", "neutral"

                current = latest or rendered
                last_attempt_at = None
                if run is not None:
                    last_attempt_at = run.started_at or run.created_at
                elif current is not None:
                    last_attempt_at = current.collected_at
                status_views.append(
                    RhythmStatusView(
                        key=game,
                        short=RHYTHM_GAME_SHORT_NAMES[game],
                        label=RHYTHM_GAME_LABELS[game],
                        source_label=source_label,
                        source_tone=source_tone,
                        status_label=status_label,
                        status_tone=status_tone,
                        provider_label=_safe_rhythm_provider(
                            provider.provider if provider is not None else None
                        ),
                        latest_rating_label=(
                            _format_rating(current.rating) if current is not None else "未取得"
                        ),
                        previous_rating_label=(
                            _format_rating(current.previous_rating)
                            if current is not None
                            else "未取得"
                        ),
                        delta_label=(
                            _format_rating(current.rating_delta, signed=True)
                            if current is not None and current.rating_delta is not None
                            else "—"
                        ),
                        latest_collected_label=(
                            _format_datetime(current.collected_at, self.timezone)
                            if current is not None
                            else "—"
                        ),
                        image_url=(
                            f"/rhythm/images/{int(current.id)}"
                            if current is not None and latest_image_available
                            else None
                        ),
                        image_label=(
                            "画像あり"
                            if latest_image_available
                            else "画像なし"
                            if current is None or not getattr(current, "image_links", None)
                            else "画像未検証"
                        ),
                        last_success_label=(
                            _format_datetime(rendered.collected_at, self.timezone)
                            if rendered is not None and rendered_image_available
                            else "—"
                        ),
                        last_attempt_label=(
                            _format_datetime(last_attempt_at, self.timezone)
                            if last_attempt_at is not None
                            else "—"
                        ),
                        last_failure_label=(
                            _format_datetime(failed.finished_at, self.timezone)
                            if failed is not None and failed.finished_at is not None
                            else "—"
                        ),
                        error_label=_safe_rhythm_error(error_code),
                        history=self._rating_history_view(rows),
                    )
                )
            return status_views, True
        except SQLAlchemyError:
            return self._empty_rhythm_status_views(), False

    def rating_image(self, snapshot_id: int) -> Path:
        """Serve only an owned, linked, verified rating PNG."""

        if isinstance(snapshot_id, bool) or snapshot_id <= 0:
            raise HTTPException(status_code=404, detail="rating image not found")
        try:
            user_id = self._rhythm_user_id()
            if user_id is None:
                raise HTTPException(status_code=404, detail="rating image not found")
            snapshot = self.rhythm_persistence.get_snapshot(
                user_id=user_id,
                snapshot_id=snapshot_id,
            )
        except SQLAlchemyError:
            raise HTTPException(status_code=404, detail="rating image not found") from None
        if snapshot is None:
            raise HTTPException(status_code=404, detail="rating image not found")
        for link in getattr(snapshot, "image_links", ()) or ():
            image = getattr(link, "image", None)
            if image is None:
                continue
            try:
                return verify_rating_image(
                    image_root=self.settings.rhythm_image_dir,
                    image_path=image.image_path,
                    expected_sha256=image.image_sha256,
                    expected_byte_size=image.byte_size,
                )
            except (RatingImageVerificationError, OSError, RuntimeError, TypeError, ValueError):
                continue
        raise HTTPException(status_code=404, detail="rating image not found")

    def rhythm_views(self) -> tuple[list[TodoView], list[GameView], list[ResultView]]:
        with self.database.session() as session:
            todos = list(
                session.scalars(
                    select(RhythmTodo)
                    .where(RhythmTodo.status == RhythmTodoStatus.OPEN.value)
                    .options(joinedload(RhythmTodo.event))
                    .order_by(RhythmTodo.created_at.desc(), RhythmTodo.id.desc())
                ).all()
            )
            results = list(
                session.scalars(
                    select(RhythmResult)
                    .options(
                        joinedload(RhythmResult.event).selectinload(Event.attachments),
                        joinedload(RhythmResult.todo),
                    )
                    .order_by(RhythmResult.created_at.desc(), RhythmResult.id.desc())
                ).all()
            )
            todo_view_pairs = [
                (
                    todo,
                    TodoView(
                        id=todo.id,
                        title=todo.title,
                        occurred_label=_format_datetime(todo.event.occurred_at, self.timezone),
                    ),
                )
                for todo in todos
            ]
            todo_views = [view for _, view in todo_view_pairs]
            grouped = {
                game: tuple(
                    view for todo, view in todo_view_pairs if _value(todo.game) == game
                )
                for game in RHYTHM_GAME_LABELS
            }
            games = tuple(
                GameView(
                    key=game,
                    short=RHYTHM_GAME_SHORT_NAMES[game],
                    label=RHYTHM_GAME_LABELS[game],
                    todos=grouped[game],
                )
                for game in RHYTHM_GAME_LABELS
            )
            result_views: list[ResultView] = []
            for result in results:
                attachment_id = next(
                    (
                        attachment.id
                        for attachment in sorted(result.event.attachments, key=lambda item: item.id)
                        if _is_image_attachment(attachment)
                    ),
                    None,
                )
                result_views.append(
                    ResultView(
                        attachment_id=attachment_id,
                        game_label=RHYTHM_GAME_LABELS.get(_value(result.game), _value(result.game)),
                        note=result.note,
                        todo_title=result.todo.title if result.todo is not None else None,
                        occurred_label=_format_datetime(result.event.occurred_at, self.timezone),
                    )
                )
        return todo_views, list(games), result_views

    def rhythm_today_views(self) -> list[TodayResultView]:
        """Return today's rhythm results and their safe image references."""

        local_today = datetime.now(self.timezone).date()
        results = self.rhythm_service.list_results_today(
            timezone_name=self.settings.timezone,
            target_date=local_today,
        )
        event_ids = [result.event_id for result in results]
        image_ids_by_event: dict[int, list[int]] = {}
        if event_ids:
            with self.database.session() as session:
                attachments = session.scalars(
                    select(Attachment)
                    .where(Attachment.event_id.in_(event_ids))
                    .order_by(Attachment.event_id, Attachment.id)
                ).all()
                for attachment in attachments:
                    if _is_image_attachment(attachment):
                        image_ids_by_event.setdefault(attachment.event_id, []).append(
                            attachment.id
                        )
        return [
            TodayResultView(
                id=result.id,
                event_id=result.event_id,
                game_label=RHYTHM_GAME_LABELS.get(result.game.value, result.game.value),
                note=result.note,
                todo_title=result.todo_title,
                occurred_label=_format_datetime(
                    result.occurred_at or result.created_at,
                    self.timezone,
                ),
                attachment_ids=tuple(image_ids_by_event.get(result.event_id, [])),
            )
            for result in results
        ]

    def maimai_snapshot_views(
        self,
    ) -> tuple[
        list[Any],
        list[MaimaiNetFavoriteView],
        list[MaimaiMyBestView],
        str | None,
        str | None,
    ]:
        """Return manual favorites and the two separate official lists."""

        manual_favorites = [
            favorite
            for favorite in self.rhythm_service.list_favorites(RhythmGame.MAIMAI)
            if favorite.source == "manual"
        ]
        net_records = self.net_favorites_service.list_current()
        my_best_records = self.mybest_service.list_current()
        net_favorites = [
            MaimaiNetFavoriteView(
                rank=record.rank,
                category=record.category,
                title=record.title,
                artist=record.artist,
            )
            for record in net_records
        ]
        my_best = [
            MaimaiMyBestView(
                rank=record.rank,
                title=record.title,
                play_count=record.play_count,
            )
            for record in my_best_records
        ]
        net_fetched_label = (
            _format_datetime(net_records[0].fetched_at, self.timezone)
            if net_records
            else None
        )
        my_best_fetched_label = (
            _format_datetime(my_best_records[0].fetched_at, self.timezone)
            if my_best_records
            else None
        )
        return (
            manual_favorites,
            net_favorites,
            my_best,
            net_fetched_label,
            my_best_fetched_label,
        )

    def event_detail(self, event_id: int) -> EventDetailView:
        _, account_names = self._activity_views()
        with self.database.session() as session:
            statement = (
                select(Event)
                .where(Event.id == event_id)
                .options(
                    selectinload(Event.expenses).selectinload(Expense.receipt_items),
                    selectinload(Event.meals),
                    selectinload(Event.attachments),
                )
            )
            event = session.scalar(statement)
            if event is None:
                raise HTTPException(status_code=404, detail="event not found")

            expense = event.expenses[0] if event.expenses else None
            meal = event.meals[0] if event.meals else None
            image_attachments = tuple(
                AttachmentView(
                    id=attachment.id,
                    filename=Path(attachment.original_filename).name or "画像",
                )
                for attachment in sorted(event.attachments, key=lambda item: item.id)
                if _is_image_attachment(attachment)
            )
            receipt_items: tuple[ReceiptItemView, ...] = ()
            expense_view: ExpenseDetailView | None = None
            if expense is not None:
                receipt_items = tuple(
                    ReceiptItemView(
                        name=item.name,
                        quantity=item.quantity,
                        unit_price_label=_format_yen(item.unit_price)
                        if item.unit_price is not None
                        else "—",
                        amount_label=_format_yen(item.amount)
                        if item.amount is not None
                        else "—",
                    )
                    for item in sorted(
                        expense.receipt_items,
                        key=lambda item: (item.order_index, item.id),
                    )
                )
                expense_view = ExpenseDetailView(
                    merchant=expense.merchant_normalized
                    or expense.merchant_raw
                    or "店舗不明",
                    amount_label=_format_yen(expense.amount),
                    category_label=_category_label(expense.category, expense.subcategory),
                    description=expense.description,
                    confidence_label=_confidence_label(expense.confidence),
                )
            meal_view = None
            if meal is not None:
                meal_view = MealDetailView(
                    meal_type_label=MEAL_TYPE_LABELS.get(_value(meal.meal_type), "不明"),
                    restaurant=meal.restaurant_normalized or meal.restaurant_raw or "場所不明",
                    outside_label=OUTSIDE_HOME_LABELS.get(_value(meal.outside_home), "不明"),
                    description=meal.description,
                )
            title = (
                (expense.merchant_normalized or expense.merchant_raw)
                if expense is not None
                else None
            ) or (
                (meal.restaurant_normalized or meal.restaurant_raw)
                if meal is not None
                else None
            ) or event.raw_text or f"記録 #{event.id}"
            capture_hint = None
            quick_prefix = "web-quick:"
            if event.discord_channel_id.startswith(quick_prefix):
                quick_type = event.discord_channel_id[len(quick_prefix) :]
                quick_label = QUICK_CAPTURE_TYPE_LABELS.get(quick_type)
                if quick_label is not None:
                    capture_hint = f"すぐ記録（{quick_label}）"
            return EventDetailView(
                id=event.id,
                title=title,
                occurred_label=_format_datetime(event.occurred_at, self.timezone),
                status_label=EVENT_STATUS_LABELS.get(_value(event.status), _value(event.status)),
                capture_hint=capture_hint,
                copied_label="転記済み" if event.copied_to_planner_at is not None else "未転記",
                raw_text=event.raw_text,
                amount_label=_format_yen(expense.amount) if expense is not None else None,
                category_label=(
                    _category_label(expense.category, expense.subcategory)
                    if expense is not None
                    else None
                ),
                account_name=account_names.get(event.id),
                expense=expense_view,
                meal=meal_view,
                receipt_items=receipt_items,
                attachments=image_attachments,
            )

    def social_context(self) -> dict[str, Any]:
        """Read local social-draft choices without adding a write endpoint."""

        with self.database.session() as session:
            rows = session.execute(
                select(SocialDraft, Event)
                .join(Event, SocialDraft.event_id == Event.id)
                .options(selectinload(Event.attachments))
                .order_by(SocialDraft.updated_at.desc(), SocialDraft.id.desc())
            ).all()
            views: list[SocialDraftPageView] = []
            for draft, event in rows:
                media = tuple(
                    SocialDraftMediaView(
                        id=attachment.id,
                        filename=attachment.original_filename,
                        content_type=_normalize_content_type(attachment.content_type),
                    )
                    for attachment in sorted(event.attachments, key=lambda item: item.id)
                    if _is_image_attachment(attachment)
                    and _safe_attachment_path(self.settings.data_dir, attachment.local_path)
                    is not None
                )
                views.append(
                    SocialDraftPageView(
                        event_id=event.id,
                        raw_text=event.raw_text,
                        target=str(draft.target),
                        target_label=social_target_label(str(draft.target)),
                        state=str(draft.state),
                        state_label=social_state_label(str(draft.state)),
                        state_tone=SOCIAL_STATE_TONES.get(str(draft.state), "neutral"),
                        has_media=bool(draft.has_media),
                        media=media,
                        created_label=_format_datetime(draft.created_at, self.timezone),
                        updated_label=_format_datetime(draft.updated_at, self.timezone),
                        selected_label=(
                            _format_datetime(draft.selected_at, self.timezone)
                            if draft.selected_at is not None
                            else "未選択"
                        ),
                        published_label=(
                            _format_datetime(draft.published_at, self.timezone)
                            if draft.published_at is not None
                            else "未実施（外部投稿なし）"
                        ),
                        error_label=draft.error,
                    )
                )
        return {
            "page": "social",
            "social_drafts": tuple(views),
            "social_ready_count": sum(item.state == "ready" for item in views),
            "social_blocked_count": sum(item.state == "blocked" for item in views),
        }

    def quick_context(
        self,
        *,
        state: str | None = None,
        error: str | None = None,
        event_id: int | None = None,
        capture_type: str | None = None,
    ) -> dict[str, Any]:
        selected_type = (
            capture_type if capture_type in QUICK_CAPTURE_TYPE_KEYS else "normal"
        )
        quick_state: dict[str, Any] | None = None
        if state == "saved" and event_id is not None and event_id > 0:
            with self.database.session() as session:
                saved_event_exists = session.scalar(
                    select(Event.id).where(Event.id == event_id)
                )
            if saved_event_exists is not None:
                label = QUICK_CAPTURE_TYPE_LABELS[selected_type]
                quick_state = {
                    "tone": "success",
                    "title": "ローカルSQLiteへ保存しました",
                    "body": (
                        f"{label}の分類ヒント付きで原文を保留保存しました。"
                        "AI・Discord・外部サービスは呼び出していません。"
                    ),
                    "event_id": int(event_id),
                }
        elif state == "error":
            error_label = QUICK_ERROR_LABELS.get(error or "", QUICK_ERROR_LABELS["invalid_form"])
            quick_state = {
                "tone": "danger",
                "title": "保存できませんでした",
                "body": error_label,
                "event_id": None,
            }
        return {
            "page": "quick",
            "quick_types": QUICK_CAPTURE_TYPES,
            "quick_selected_type": selected_type,
            "quick_state": quick_state,
            "quick_max_length": QUICK_TEXT_MAX_LENGTH,
        }

    def dashboard_context(self) -> dict[str, Any]:
        selected, selected_date, start_utc, end_utc = _month_window(None, self.timezone)
        today = datetime.now(self.timezone).date()
        day_start_utc, day_end_utc = _day_window(today, self.timezone)
        budget = self.budgets.get_month(selected)
        pairs = self._confirmed_expense_pairs(start_utc, end_utc)
        today_pairs = self._confirmed_expense_pairs(day_start_utc, day_end_utc)
        _, account_names = self._activity_views()
        recent_pairs = self._confirmed_expense_pairs()
        recent_views = [
            self._event_row(expense, event, account_names) for expense, event in recent_pairs
        ]
        month_total = sum(expense.amount or 0 for expense, _ in pairs)
        dining_pairs = [
            (expense, event)
            for expense, event in pairs
            if expense.category == "食費" and expense.subcategory == "外食"
        ]
        category_amounts: dict[str, int] = {}
        for expense, _ in pairs:
            if expense.amount is not None:
                category = expense.category or "未分類"
                category_amounts[category] = category_amounts.get(category, 0) + expense.amount
        category_totals = []
        for name, total in sorted(category_amounts.items(), key=lambda item: (-item[1], item[0])):
            percent: int | float = 0
            if month_total:
                percent = round(total * 100 / month_total, 1)
                if isinstance(percent, float) and percent.is_integer():
                    percent = int(percent)
            category_totals.append(CategoryTotalView(name=name, total=total, percent=percent))
        meal_pairs = self._meal_pairs(start_utc, end_utc)
        with self.database.session() as session:
            open_rhythm_count = session.scalar(
                select(func.count(RhythmTodo.id)).where(
                    RhythmTodo.status == RhythmTodoStatus.OPEN.value
                )
            ) or 0
        accounts = self.account_views()
        task_groups = self._task_groups()
        routines, routines_available = self._routine_views()
        rhythm_statuses, rhythm_status_available = self.rhythm_status_views(include_history=False)
        recent_meals, _ = self.meal_views()
        career_parts = self._career_context_parts()
        return {
            "page": "dashboard",
            "month_label": _month_text(selected_date),
            "today_label": _format_local_date(today),
            "today_total": sum(expense.amount or 0 for expense, _ in today_pairs),
            "today_transaction_count": len(today_pairs),
            "month_total": month_total,
            "month_transaction_count": len(pairs),
            "dining_total": sum(expense.amount or 0 for expense, _ in dining_pairs),
            "dining_count": len(dining_pairs),
            "notable_count": sum(
                1 for expense, _ in pairs if expense.amount is not None and expense.amount >= 5_000
            ),
            "meal_count": len(meal_pairs),
            "open_rhythm_count": int(open_rhythm_count),
            "category_totals": category_totals,
            "account_balances": accounts,
            "account_summaries": self.account_summary_views(accounts),
            "recent_events": recent_views[:10],
            "recent_memos": self._memos(limit=5),
            "recent_meals": recent_meals[:5],
            "open_tasks": task_groups["open_tasks"],
            "overdue_tasks": task_groups["overdue_tasks"],
            "due_today_tasks": task_groups["due_today_tasks"],
            "upcoming_tasks": task_groups["upcoming_tasks"],
            "task_open_count": len(task_groups["open_tasks"]),
            "task_overdue_count": len(task_groups["overdue_tasks"]),
            "task_upcoming_count": len(task_groups["upcoming_tasks"]),
            "routines": routines,
            "routines_available": routines_available,
            "routine_completed_count": sum(1 for routine in routines if routine.completed_today),
            "rhythm_statuses": rhythm_statuses,
            "rhythm_status_available": rhythm_status_available,
            "selected_month": selected,
            "budget": budget,
            "budget_overall": budget.overall,
            "budget_category_count": len(budget.categories),
            "career_available": career_parts["career_available"],
            "career_active_count": career_parts["career_active_count"],
            "career_next_item": career_parts["career_next_item"],
        }

    def budgets_context(self, *, month: str | None) -> dict[str, Any]:
        selected, selected_date, _, _ = _month_window(month, self.timezone)
        budget: BudgetMonthView = self.budgets.get_month(selected)
        return {
            "page": "budgets",
            "selected_month": selected,
            "month_label": _month_text(selected_date),
            "budget": budget,
            "overall": budget.overall,
            "category_budgets": budget.categories,
        }

    def transactions_context(
        self,
        *,
        month: str | None,
        category: str,
        minimum: str | None,
        dining: str | None,
    ) -> dict[str, Any]:
        selected, _, start_utc, end_utc = _month_window(month, self.timezone)
        minimum_value = _parse_minimum(minimum)
        dining_only = dining in {"1", "true", "on"}
        pairs = self._confirmed_expense_pairs(
            start_utc,
            end_utc,
            category=category.strip() or None,
            minimum=minimum_value,
            dining_only=dining_only,
        )
        _, account_names = self._activity_views()
        events = [self._event_row(expense, event, account_names) for expense, event in pairs]
        return {
            "page": "transactions",
            "selected_month": selected,
            "selected_category": category.strip(),
            "minimum": minimum_value,
            "dining_only": dining_only,
            "categories": self.settings.categories,
            "events": events,
            "filtered_total": sum(expense.amount or 0 for expense, _ in pairs),
        }

    def accounts_context(self) -> dict[str, Any]:
        activities, _ = self._activity_views()
        return {"page": "accounts", "accounts": self.account_views(), "activities": activities}

    def memos_context(self) -> dict[str, Any]:
        return {"page": "memos", "memos": self._memos()}

    def meals_context(self) -> dict[str, Any]:
        meals, outside_count = self.meal_views()
        return {"page": "meals", "meals": meals, "outside_count": outside_count}

    def rhythm_context(self) -> dict[str, Any]:
        todos, games, results = self.rhythm_views()
        (
            manual_favorites,
            net_favorites,
            my_best,
            net_fetched_at_label,
            my_best_fetched_at_label,
        ) = self.maimai_snapshot_views()
        rhythm_statuses, rhythm_status_available = self.rhythm_status_views(include_history=True)
        return {
            "page": "rhythm",
            "todos": todos,
            "games": games,
            "results": results,
            "today_results": self.rhythm_today_views(),
            "timezone_name": self.settings.timezone,
            "manual_favorites": manual_favorites,
            "net_favorites": net_favorites,
            "net_favorites_fetched_at": net_fetched_at_label,
            "my_best": my_best,
            "my_best_fetched_at": my_best_fetched_at_label,
            "rhythm_statuses": rhythm_statuses,
            "rhythm_status_available": rhythm_status_available,
        }

    def tasks_context(self) -> dict[str, Any]:
        task_groups = self._task_groups()
        routines, routines_available = self._routine_views()
        deliveries, deliveries_available = self._reminder_delivery_views(
            task_groups["tasks"], routines
        )
        return {
            "page": "tasks",
            **task_groups,
            "open_count": len(task_groups["open_tasks"]),
            "overdue_count": len(task_groups["overdue_tasks"]),
            "completed_count": len(task_groups["completed_tasks"]),
            "routines": routines,
            "routines_available": routines_available,
            "routine_completed_count": sum(1 for routine in routines if routine.completed_today),
            "routine_pending_count": sum(
                1 for routine in routines if routine.enabled and not routine.completed_today
            ),
            "reminder_deliveries": deliveries,
            "reminder_deliveries_available": deliveries_available,
        }

    def career_context(self) -> dict[str, Any]:
        return {
            "page": "career",
            "timezone_name": self.settings.timezone,
            **self._career_context_parts(),
        }

    def guide_context(self) -> dict[str, Any]:
        from .command_guide import command_guide_sections

        web_url = f"http://{self.settings.web_host}:{self.settings.web_port}"
        sections = command_guide_sections(web_url)
        return {
            "page": "guide",
            "sections": tuple(
                GuideSectionView(
                    title=section.splitlines()[0] if section.splitlines() else "案内",
                    body=section,
                )
                for section in sections
            ),
        }


def create_web_app(
    settings: Settings,
    *,
    database: Database | None = None,
    finance: FinanceService | None = None,
    rhythm: RhythmService | None = None,
    career_service: CareerService | None = None,
    career: CareerService | None = None,
    task_service: TaskService | None = None,
    tasks: TaskService | None = None,
    routine_service: RoutineService | None = None,
    routines: RoutineService | None = None,
    inbox_service: InboxService | None = None,
    inbox: InboxService | None = None,
    mybest_service: MaimaiMyBestService | None = None,
    mybest: MaimaiMyBestService | None = None,
    net_favorites_service: MaimaiNetFavoriteService | None = None,
    net_favorites: MaimaiNetFavoriteService | None = None,
    budget_service: BudgetService | None = None,
    budgets: BudgetService | None = None,
) -> FastAPI:
    """Create the local read-only FastAPI application.

    Dependency construction is intentionally side-effect free.  ``run_web``
    performs the explicit normal-start initialization (additive table creation
    and default accounts); tests and embedding callers can pass a temp
    ``Database`` and already-created services directly.
    """

    resolved_database = database or Database(settings.database_url)
    resolved_finance = finance or FinanceService(resolved_database.session_factory)
    resolved_rhythm = rhythm or RhythmService(resolved_database.session_factory)
    resolved_mybest = mybest_service or mybest or MaimaiMyBestService(
        resolved_database.session_factory
    )
    resolved_net_favorites = (
        net_favorites_service
        or net_favorites
        or MaimaiNetFavoriteService(resolved_database.session_factory)
    )
    if task_service is not None and tasks is not None:
        raise ValueError("provide only one task service")
    if routine_service is not None and routines is not None:
        raise ValueError("provide only one routine service")
    if inbox_service is not None and inbox is not None:
        raise ValueError("provide only one inbox service")
    if budget_service is not None and budgets is not None:
        raise ValueError("provide only one budget service")
    if career_service is not None and career is not None:
        raise ValueError("provide only one career service")
    if mybest_service is not None and mybest is not None:
        raise ValueError("provide only one MY BEST service")
    if net_favorites_service is not None and net_favorites is not None:
        raise ValueError("provide only one official favorite service")
    resolved_tasks = task_service or tasks or TaskService(
        resolved_database.session_factory,
        timezone=settings.timezone_info(),
    )
    resolved_routines = routine_service or routines or RoutineService(
        resolved_database.session_factory,
        timezone=settings.timezone_info(),
    )
    resolved_inbox = inbox_service or inbox or InboxService(
        resolved_database.session_factory,
        settings,
        NullAnalyzer(),
    )
    resolved_budgets = budget_service or budgets or BudgetService(
        resolved_database.session_factory,
        timezone=settings.timezone_info(),
        allowed_categories=settings.categories,
    )
    resolved_career = career_service or career or CareerService(
        resolved_database.session_factory,
        timezone=settings.timezone_info(),
    )
    reader = _WebReader(
        settings,
        resolved_database,
        resolved_finance,
        career_service=resolved_career,
        task_service=resolved_tasks,
        routine_service=resolved_routines,
        inbox_service=resolved_inbox,
        budget_service=resolved_budgets,
        rhythm_service=resolved_rhythm,
        mybest_service=resolved_mybest,
        net_favorites_service=resolved_net_favorites,
    )
    archive_health = ArchiveHealthService(
        resolved_database.session_factory,
        data_dir=settings.data_dir,
    )
    archive_browser = LocalArchiveBrowser(
        resolved_database.session_factory,
        timezone=settings.timezone_info(),
    )
    package_dir = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(package_dir / "templates"))

    app = FastAPI(title="生活マイページ", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.database = resolved_database
    app.state.finance = resolved_finance
    app.state.rhythm = resolved_rhythm
    app.state.mybest = resolved_mybest
    app.state.mybest_service = app.state.mybest
    app.state.net_favorites = resolved_net_favorites
    app.state.net_favorites_service = app.state.net_favorites
    app.state.tasks = resolved_tasks
    app.state.task_service = resolved_tasks
    app.state.routines = resolved_routines
    app.state.routine_service = resolved_routines
    app.state.inbox = resolved_inbox
    app.state.inbox_service = resolved_inbox
    app.state.social_drafts = reader.social_drafts
    app.state.budgets = resolved_budgets
    app.state.budget_service = resolved_budgets
    app.state.career = resolved_career
    app.state.career_service = resolved_career
    app.state.archive_health = archive_health
    app.state.archive_browser = archive_browser
    quick_tokens = _QuickTokenRegistry()
    app.state.quick_tokens = quick_tokens
    app.mount("/static", StaticFiles(directory=str(package_dir / "static")), name="static")
    install_rhythm_api(app, settings=settings, database=resolved_database)
    install_rhythm_spending_event_api(
        app, settings=settings, database=resolved_database
    )
    install_health_api(app, settings=settings, database=resolved_database)

    @app.middleware("http")
    async def no_store_private_responses(request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'none'"
        )
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @app.get("/", name="dashboard")
    def dashboard(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=reader.dashboard_context(),
        )

    @app.get("/quick", name="quick")
    def quick(
        request: Request,
        state: str | None = None,
        error: str | None = None,
        event_id: int | None = None,
        capture_type: str | None = None,
    ):
        return templates.TemplateResponse(
            request=request,
            name="quick.html",
            context=reader.quick_context(
                state=state,
                error=error,
                event_id=event_id,
                capture_type=capture_type,
            )
            | {"quick_token": quick_tokens.issue()},
        )

    @app.post("/quick", name="quick_submit")
    async def quick_submit(request: Request):
        form: dict[str, str] = {}
        try:
            form = await _read_quick_form(request)
            capture_type = _quick_capture_type(form["capture_type"])
            text = _quick_text(form["text"])
            token = _quick_token(form["idempotency_token"])
            if not quick_tokens.accepts(token):
                raise _QuickCaptureError("invalid_token")
            event_id = reader.quick_capture(capture_type, text, token)
        except _QuickCaptureError as exc:
            capture_type = None
            try:
                capture_type = _quick_capture_type(form.get("capture_type"))
            except _QuickCaptureError:
                pass
            return RedirectResponse(
                _quick_redirect_url(
                    state="error",
                    capture_type=capture_type,
                    error=exc.code,
                ),
                status_code=303,
            )
        except (OSError, SQLAlchemyError, ValueError):
            return RedirectResponse(
                _quick_redirect_url(state="error", error="save_failed"),
                status_code=303,
            )
        return RedirectResponse(
            _quick_redirect_url(
                state="saved",
                capture_type=capture_type,
                event_id=event_id,
            ),
            status_code=303,
        )

    @app.get("/transactions", name="transactions")
    def transactions(
        request: Request,
        month: str | None = None,
        category: str = "",
        minimum: str | None = None,
        dining: str | None = None,
    ):
        return templates.TemplateResponse(
            request=request,
            name="transactions.html",
            context=reader.transactions_context(
                month=month,
                category=category,
                minimum=minimum,
                dining=dining,
            ),
        )

    @app.get("/budgets", name="budgets")
    def budgets_page(request: Request, month: str | None = None):
        return templates.TemplateResponse(
            request=request,
            name="budgets.html",
            context=reader.budgets_context(month=month),
        )

    @app.get("/events/{event_id}", name="event_detail")
    def event_detail(request: Request, event_id: int):
        return templates.TemplateResponse(
            request=request,
            name="event_detail.html",
            context={"page": "event_detail", "event": reader.event_detail(event_id)},
        )

    @app.get("/accounts", name="accounts")
    def accounts(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="accounts.html",
            context=reader.accounts_context(),
        )

    @app.get("/memos", name="memos")
    def memos(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="memos.html",
            context=reader.memos_context(),
        )

    @app.get("/social", name="social")
    def social(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="social.html",
            context=reader.social_context(),
        )

    @app.get("/meals", name="meals")
    def meals(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="meals.html",
            context=reader.meals_context(),
        )

    @app.get("/rhythm", name="rhythm")
    def rhythm_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="rhythm.html",
            context=reader.rhythm_context(),
        )

    @app.get("/tasks", name="tasks")
    def tasks(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="tasks.html",
            context=reader.tasks_context(),
        )

    @app.get("/career", name="career")
    def career_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="career.html",
            context=reader.career_context(),
        )

    @app.get("/guide", name="guide")
    def guide(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="guide.html",
            context=reader.guide_context(),
        )

    @app.get("/archive", name="archive")
    def archive_timeline(
        request: Request,
        q: str = "",
        status: str = "all",
        kind: str = "all",
        media: str = "all",
        limit: int = 50,
    ):
        bounded_limit = max(1, min(limit, 100))
        return templates.TemplateResponse(
            request=request,
            name="archive_timeline.html",
            context=(
                archive_health.timeline_context(limit=bounded_limit)
                | archive_browser.context(
                    query=q,
                    status=status,
                    kind=kind,
                    media=media,
                    limit=bounded_limit,
                )
                | {
                    "notion_hub_url": settings.notion_hub_url,
                    "notion_sync_enabled": settings.notion_sync_enabled,
                }
            ),
        )

    @app.get("/archive/health", name="archive_health")
    def archive_health_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="archive_health.html",
            context=archive_health.health_context(),
        )

    @app.get("/rhythm/images/{snapshot_id}", name="rhythm_image")
    def rhythm_image(snapshot_id: int):
        path = reader.rating_image(snapshot_id)
        media_type = "image/jpeg" if path.suffix.casefold() in {".jpg", ".jpeg"} else "image/png"
        response = FileResponse(path, media_type=media_type)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/attachments/{attachment_id}", name="attachment")
    def attachment(attachment_id: int):
        with resolved_database.session() as session:
            row = session.get(Attachment, attachment_id)
            if row is None or not _is_image_attachment(row):
                raise HTTPException(status_code=404, detail="attachment not found")
            path = _safe_attachment_path(settings.data_dir, row.local_path)
            if path is None:
                raise HTTPException(status_code=404, detail="attachment not found")
            response = FileResponse(path, media_type=_normalize_content_type(row.content_type))
            response.headers["Cache-Control"] = "no-store"
            return response

    @app.get("/healthz", name="healthz")
    def healthz():
        return JSONResponse({"status": "ok"})

    return app


def run_web(settings: Settings) -> None:
    """Initialize the local database and serve the app with Uvicorn."""

    import uvicorn

    database = Database(settings.database_url)
    database.init()
    finance = FinanceService(database.session_factory)
    finance.ensure_default_accounts()
    rhythm = RhythmService(database.session_factory)
    app = create_web_app(settings, database=database, finance=finance, rhythm=rhythm)
    uvicorn.run(app, host=settings.web_host, port=settings.web_port)
