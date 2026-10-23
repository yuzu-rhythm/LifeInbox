"""Environment-backed configuration with secret-safe diagnostics."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from .constants import ABSOLUTE_MAX_EXPENSE_YEN, DEFAULT_CATEGORIES, FOOD_SUBCATEGORIES


class Settings(BaseSettings):
    """Application settings.

    ``.env.local`` is intentionally supported for normal CLI/bot use. Tests
    can pass ``_env_file=None`` to avoid reading any local environment file.
    The secret field is a ``SecretStr`` and is never included in diagnostics.
    """

    model_config = SettingsConfigDict(
        # Later files take precedence. Keep machine-local secrets ahead of a
        # shared, non-secret .env when both files exist.
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    discord_bot_token: SecretStr | None = Field(default=None, validation_alias="DISCORD_BOT_TOKEN")
    discord_guild_id: int | None = Field(default=None, gt=0, validation_alias="DISCORD_GUILD_ID")
    allowed_user_id: int | None = Field(default=None, gt=0, validation_alias="ALLOWED_USER_ID")
    # ``GENERAL_INBOX_CHANNEL_ID`` is the confirmed primary free-form inbox.
    # ``INBOX_CHANNEL_ID`` remains a deliberately separate compatibility
    # setting for older installations that still use #なんでも入力.
    general_inbox_channel_id: int | None = Field(
        default=None,
        gt=0,
        validation_alias="GENERAL_INBOX_CHANNEL_ID",
    )
    general_archive_channel_id: int | None = Field(
        default=None,
        gt=0,
        validation_alias="GENERAL_ARCHIVE_CHANNEL_ID",
    )
    general_archive_channel_name: str = Field(
        default="一般アーカイブ",
        min_length=1,
        max_length=100,
        validation_alias="GENERAL_ARCHIVE_CHANNEL_NAME",
    )
    inbox_channel_id: int | None = Field(default=None, gt=0, validation_alias="INBOX_CHANNEL_ID")
    records_channel_id: int | None = Field(
        default=None, gt=0, validation_alias="RECORDS_CHANNEL_ID"
    )
    card_channel_id: int | None = Field(default=None, gt=0, validation_alias="CARD_CHANNEL_ID")
    account_channel_id: int | None = Field(
        default=None, gt=0, validation_alias="ACCOUNT_CHANNEL_ID"
    )
    memo_channel_id: int | None = Field(default=None, gt=0, validation_alias="MEMO_CHANNEL_ID")
    monologue_channel_id: int | None = Field(
        default=None, gt=0, validation_alias="MONOLOGUE_CHANNEL_ID"
    )
    dining_out_channel_id: int | None = Field(
        default=None, gt=0, validation_alias="DINING_OUT_CHANNEL_ID"
    )
    memories_channel_id: int | None = Field(
        default=None, gt=0, validation_alias="MEMORIES_CHANNEL_ID"
    )
    social_draft_channel_id: int | None = Field(
        default=None, gt=0, validation_alias="SOCIAL_DRAFT_CHANNEL_ID"
    )
    rhythm_channel_id: int | None = Field(
        default=None, gt=0, validation_alias="RHYTHM_CHANNEL_ID"
    )
    progress_channel_id: int | None = Field(
        default=None, gt=0, validation_alias="PROGRESS_CHANNEL_ID"
    )
    meal_channel_id: int | None = Field(default=None, gt=0, validation_alias="MEAL_CHANNEL_ID")
    reminder_channel_id: int | None = Field(
        default=None, gt=0, validation_alias="REMINDER_CHANNEL_ID"
    )
    reminder_scheduler_enabled: bool = Field(
        default=False, validation_alias="REMINDER_SCHEDULER_ENABLED"
    )
    reminder_poll_seconds: int = Field(
        default=60,
        ge=5,
        le=3_600,
        validation_alias=AliasChoices(
            "REMINDER_POLL_SECONDS", "REMINDER_POLL_INTERVAL_SECONDS"
        ),
    )
    # The household-finance digest is opt-in and starts in dry-run mode.  Its
    # channel is intentionally independent from the other channel roles: the
    # records channel is a valid place to publish the digest as well.
    finance_digest_enabled: bool = Field(
        default=False, validation_alias="FINANCE_DIGEST_ENABLED"
    )
    finance_digest_dry_run: bool = Field(
        default=True, validation_alias="FINANCE_DIGEST_DRY_RUN"
    )
    finance_digest_channel_id: int | None = Field(
        default=None, gt=0, validation_alias="FINANCE_DIGEST_CHANNEL_ID"
    )
    finance_digest_cron: str = Field(
        default="45 23 * * *", validation_alias="FINANCE_DIGEST_CRON"
    )
    finance_digest_time_zone: str = Field(
        default="Asia/Tokyo", validation_alias="FINANCE_DIGEST_TIME_ZONE"
    )
    finance_digest_post_zero_days: bool = Field(
        default=True, validation_alias="FINANCE_DIGEST_POST_ZERO_DAYS"
    )
    finance_digest_reconcile_seconds: int = Field(
        default=60,
        ge=5,
        le=3_600,
        validation_alias="FINANCE_DIGEST_RECONCILE_SECONDS",
    )
    finance_digest_lookback_days: int = Field(
        default=7,
        ge=1,
        le=31,
        validation_alias="FINANCE_DIGEST_LOOKBACK_DAYS",
    )
    finance_digest_large_expense_yen: int = Field(
        default=5_000, validation_alias="FINANCE_DIGEST_LARGE_EXPENSE_YEN"
    )
    discord_chunithm_channel_id: int | None = Field(
        default=None, gt=0, validation_alias="DISCORD_CHUNITHM_CHANNEL_ID"
    )
    discord_maimai_channel_id: int | None = Field(
        default=None, gt=0, validation_alias="DISCORD_MAIMAI_CHANNEL_ID"
    )
    discord_ongeki_channel_id: int | None = Field(
        default=None, gt=0, validation_alias="DISCORD_ONGEKI_CHANNEL_ID"
    )
    discord_credit_channel_id: int | None = Field(
        default=None, gt=0, validation_alias="DISCORD_CREDIT_CHANNEL_ID"
    )
    discord_error_channel_id: int | None = Field(
        default=None, gt=0, validation_alias="DISCORD_ERROR_CHANNEL_ID"
    )
    discord_health_channel_id: int | None = Field(
        default=None, gt=0, validation_alias="DISCORD_HEALTH_CHANNEL_ID"
    )
    command_guide_channel_name: str = Field(
        default="bot-コマンド一覧",
        min_length=1,
        max_length=100,
        validation_alias="COMMAND_GUIDE_CHANNEL_NAME",
    )
    ai_review_channel_id: int | None = Field(
        default=None, gt=0, validation_alias="AI_REVIEW_CHANNEL_ID"
    )
    ai_review_use_openai: bool = Field(
        default=False, validation_alias="AI_REVIEW_USE_OPENAI"
    )
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_model: str = Field(
        default="gpt-5.6-luna",
        min_length=1,
        max_length=128,
        validation_alias="OPENAI_MODEL",
    )
    # The free-form Inbox classifier has a separate optional key so enabling
    # classification never broadens the credentials used by other analyzers.
    openai_inbox_classifier_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="OPENAI_INBOX_CLASSIFIER_API_KEY",
    )
    openai_inbox_classifier_enabled: bool = Field(
        default=True,
        validation_alias="OPENAI_INBOX_CLASSIFIER_ENABLED",
    )
    openai_inbox_classifier_model: str = Field(
        default="gpt-5.6-luna",
        min_length=1,
        max_length=128,
        validation_alias="OPENAI_INBOX_CLASSIFIER_MODEL",
    )
    openai_inbox_classifier_timeout_seconds: int = Field(
        default=8,
        ge=1,
        le=60,
        validation_alias="OPENAI_INBOX_CLASSIFIER_TIMEOUT_SECONDS",
    )
    database_url: str = Field(default="sqlite:///data/app.db", validation_alias="DATABASE_URL")
    data_dir: Path = Field(default=Path("data"), validation_alias="DATA_DIR")
    timezone: str = Field(default="Asia/Tokyo", validation_alias="TIMEZONE")
    web_host: str = Field(default="127.0.0.1", validation_alias="WEB_HOST")
    web_port: int = Field(default=8765, ge=1, le=65535, validation_alias="WEB_PORT")
    public_web_url: str | None = Field(
        default=None,
        max_length=2_048,
        validation_alias="PUBLIC_WEB_URL",
    )
    auto_confirm: bool = Field(default=False, validation_alias="AUTO_CONFIRM")
    inbox_delete_original_after_route: bool = Field(
        default=False,
        validation_alias="INBOX_DELETE_ORIGINAL_AFTER_ROUTE",
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    attachment_max_bytes: int = Field(
        default=10 * 1024 * 1024, validation_alias="ATTACHMENT_MAX_BYTES"
    )
    max_expense_yen: int = Field(default=1_000_000, validation_alias="MAX_EXPENSE_YEN")
    category_names: str = Field(default=",".join(DEFAULT_CATEGORIES), validation_alias="CATEGORIES")
    food_subcategory_names: str = Field(
        default=",".join(FOOD_SUBCATEGORIES), validation_alias="FOOD_SUBCATEGORIES"
    )

    # Notion is a secondary archive only.  The transport is injected by an
    # explicit caller, so these settings do not cause network activity during
    # Discord capture or normal application startup.
    notion_sync_enabled: bool = Field(default=False, validation_alias="NOTION_SYNC_ENABLED")
    notion_api_token: SecretStr | None = Field(default=None, validation_alias="NOTION_API_TOKEN")
    notion_parent_page_id: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices(
            "NOTION_PARENT_PAGE_ID",
            "NOTION_PARENT_ID",
            "NOTION_PARENT_DATABASE_ID",
        ),
    )
    notion_parent_type: str = Field(default="page_id", validation_alias="NOTION_PARENT_TYPE")
    notion_title_property_name: str = Field(
        default="Name",
        min_length=1,
        max_length=100,
        validation_alias="NOTION_TITLE_PROPERTY_NAME",
    )
    notion_hub_url: str | None = Field(
        default=None,
        max_length=2_048,
        validation_alias="NOTION_HUB_URL",
    )
    notion_api_base_url: str = Field(
        default="https://api.notion.com",
        min_length=1,
        max_length=2_048,
        validation_alias="NOTION_API_BASE_URL",
    )
    notion_request_timeout_seconds: int = Field(
        default=20,
        ge=1,
        le=120,
        validation_alias="NOTION_REQUEST_TIMEOUT_SECONDS",
    )
    notion_sync_poll_seconds: int = Field(
        default=60,
        ge=1,
        le=3_600,
        validation_alias="NOTION_SYNC_POLL_SECONDS",
    )

    # Rhythm collection is opt-in.  A fresh or upgraded installation must not
    # contact providers or post to Discord merely because the process starts.
    rhythm_enabled: bool = Field(default=False, validation_alias="RHYTHM_ENABLED")
    rhythm_scheduler_enabled: bool = Field(
        default=False, validation_alias="RHYTHM_SCHEDULER_ENABLED"
    )
    rhythm_discord_dry_run: bool = Field(
        default=True, validation_alias="RHYTHM_DISCORD_DRY_RUN"
    )
    rhythm_daily_post_cron: str = Field(
        default="0 10 * * *", validation_alias="RHYTHM_DAILY_POST_CRON"
    )
    rhythm_time_zone: str = Field(
        default="Asia/Tokyo", validation_alias="RHYTHM_TIME_ZONE"
    )
    rhythm_post_zero_spending_days: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "RHYTHM_SPENDING_POST_ZERO_DAYS",
            "RHYTHM_POST_ZERO_SPENDING_DAYS",
        ),
    )
    rhythm_spending_immediate_post_enabled: bool = Field(
        default=False,
        validation_alias="RHYTHM_SPENDING_IMMEDIATE_POST_ENABLED",
    )
    rhythm_spending_daily_summary_enabled: bool = Field(
        default=False,
        validation_alias="RHYTHM_SPENDING_DAILY_SUMMARY_ENABLED",
    )
    rhythm_spending_daily_summary_cron: str = Field(
        default="55 23 * * *",
        validation_alias="RHYTHM_SPENDING_DAILY_SUMMARY_CRON",
    )
    rhythm_spending_post_mode: str = Field(
        default="eachEvent", validation_alias="RHYTHM_SPENDING_POST_MODE"
    )
    rhythm_spending_reconcile_seconds: int = Field(
        default=5,
        ge=1,
        le=3_600,
        validation_alias="RHYTHM_SPENDING_RECONCILE_SECONDS",
    )
    rhythm_player_display_name: str = Field(
        default="プレイヤー",
        min_length=1,
        max_length=100,
        validation_alias="RHYTHM_PLAYER_DISPLAY_NAME",
    )
    rhythm_browser_session_dir: Path = Field(
        default=Path("data/rhythm/sessions"),
        validation_alias="RHYTHM_BROWSER_SESSION_DIR",
    )
    rhythm_image_dir: Path = Field(
        default=Path("data/rhythm/images"), validation_alias="RHYTHM_IMAGE_DIR"
    )
    rhythm_device_sync_token_sha256: SecretStr | None = Field(
        default=None, validation_alias="RHYTHM_DEVICE_SYNC_TOKEN_SHA256"
    )
    rhythm_api_rate_limit_per_minute: int = Field(
        default=60,
        ge=1,
        le=600,
        validation_alias="RHYTHM_API_RATE_LIMIT_PER_MINUTE",
    )
    # The public device-sync ingress is a separate process from the local/IAP
    # website.  Its opt-in and request limits are intentionally separate from
    # the broader rhythm API settings so a deployment cannot accidentally
    # expose the read-only surface along with the write endpoint.
    device_sync_enabled: bool = Field(default=False, validation_alias="DEVICE_SYNC_ENABLED")
    device_sync_max_body_bytes: int = Field(
        default=16 * 1024,
        ge=1_024,
        le=1_048_576,
        validation_alias="DEVICE_SYNC_MAX_BODY_BYTES",
    )
    device_sync_rate_limit_per_minute: int = Field(
        default=60,
        ge=1,
        le=600,
        validation_alias="DEVICE_SYNC_RATE_LIMIT_PER_MINUTE",
    )

    # Apple Health summaries are opt-in and never post during a fresh install.
    # The authenticated API reuses the existing dedicated iPhone device token;
    # the Discord Bot token is never sent to an Apple device.
    health_enabled: bool = Field(default=False, validation_alias="HEALTH_ENABLED")
    health_scheduler_enabled: bool = Field(
        default=False, validation_alias="HEALTH_SCHEDULER_ENABLED"
    )
    health_discord_dry_run: bool = Field(
        default=True, validation_alias="HEALTH_DISCORD_DRY_RUN"
    )
    health_sleep_post_cron: str = Field(
        default="0 10 * * *", validation_alias="HEALTH_SLEEP_POST_CRON"
    )
    health_time_zone: str = Field(
        default="Asia/Tokyo", validation_alias="HEALTH_TIME_ZONE"
    )
    health_reconcile_seconds: int = Field(
        default=60, ge=5, le=3_600, validation_alias="HEALTH_RECONCILE_SECONDS"
    )
    health_lookback_days: int = Field(
        default=7, ge=1, le=31, validation_alias="HEALTH_LOOKBACK_DAYS"
    )
    health_include_heart_rate: bool = Field(
        default=False, validation_alias="HEALTH_INCLUDE_HEART_RATE"
    )

    chunirec_api_token: SecretStr | None = Field(
        default=None, validation_alias="CHUNIREC_API_TOKEN"
    )
    chunirec_user_id: str | None = Field(
        default=None, max_length=128, validation_alias="CHUNIREC_USER_ID"
    )
    chunirec_user_name: str | None = Field(
        default=None, max_length=128, validation_alias="CHUNIREC_USER_NAME"
    )
    chunirec_region: str = Field(
        default="jp2", min_length=1, max_length=16, validation_alias="CHUNIREC_REGION"
    )
    chunirec_require_manual_refresh_confirmation: bool = Field(
        default=True,
        validation_alias="CHUNIREC_REQUIRE_MANUAL_REFRESH_CONFIRMATION",
    )

    # SEGA credentials remain server-side secrets.  They are shared by the
    # maimai/ONGEKI browser providers but are never included in diagnostics or
    # persisted in the rhythm tables.
    sega_id: SecretStr | None = Field(default=None, validation_alias="SEGA_ID")
    sega_password: SecretStr | None = Field(
        default=None,
        validation_alias="SEGA_PASSWORD",
    )

    maimai_net_rating_url: str | None = Field(
        default=None, max_length=2_048, validation_alias="MAIMAI_NET_RATING_URL"
    )
    maimai_analyzer_script_url: str = Field(
        default="https://sgimera.github.io/mai_RatingAnalyzer/maidx_tools.js",
        max_length=2_048,
        validation_alias="MAIMAI_ANALYZER_SCRIPT_URL",
    )
    maimai_analyzer_bookmarklet_path: Path | None = Field(
        default=None, validation_alias="MAIMAI_ANALYZER_BOOKMARKLET_PATH"
    )
    maimai_full_data_selector: str | None = Field(
        default=None, max_length=512, validation_alias="MAIMAI_FULL_DATA_SELECTOR"
    )
    maimai_standard_course_enabled: bool = Field(
        default=False, validation_alias="MAIMAI_STANDARD_COURSE_ENABLED"
    )
    rhythm_browser_headless: bool = Field(
        default=True,
        validation_alias="RHYTHM_BROWSER_HEADLESS",
    )
    rhythm_browser_timeout_seconds: int = Field(
        default=120,
        ge=10,
        le=300,
        validation_alias="RHYTHM_BROWSER_TIMEOUT_SECONDS",
    )

    ongeki_official_net_url: str | None = Field(
        default="https://ongeki-net.com/ongeki-mobile/home/",
        max_length=2_048,
        validation_alias="ONGEKI_OFFICIAL_NET_URL",
    )
    ongeki_score_log_bookmarklet_path: Path | None = Field(
        default=None, validation_alias="ONGEKI_SCORE_LOG_BOOKMARKLET_PATH"
    )
    ongeki_score_log_user_id: str | None = Field(
        default=None, max_length=128, validation_alias="ONGEKI_SCORE_LOG_USER_ID"
    )
    ongeki_image_generator_url: str = Field(
        default="https://reiwa.f5.si/newbestimg/ongeki/",
        max_length=2_048,
        validation_alias="ONGEKI_IMAGE_GENERATOR_URL",
    )
    ongeki_image_mode: str = Field(
        default="bestnew",
        min_length=1,
        max_length=64,
        validation_alias="ONGEKI_IMAGE_MODE",
    )
    ongeki_copyright_mode: bool = Field(
        default=True,
        validation_alias="ONGEKI_COPYRIGHT_MODE",
    )
    ongeki_premium_enabled: bool = Field(
        default=False, validation_alias="ONGEKI_PREMIUM_ENABLED"
    )

    @staticmethod
    def _normalize_name_list(value: str, *, setting_name: str) -> str:
        values = tuple(part.strip() for part in value.split(",") if part.strip())
        if not values:
            raise ValueError(f"{setting_name} must contain at least one value")
        if len(values) != len(set(values)):
            raise ValueError(f"{setting_name} must not contain duplicates")
        if any(len(item) > 64 for item in values):
            raise ValueError(f"{setting_name} values must be at most 64 characters")
        return ",".join(values)

    @field_validator("category_names")
    @classmethod
    def validate_category_names(cls, value: str) -> str:
        normalized = cls._normalize_name_list(value, setting_name="CATEGORIES")
        if "未分類" not in normalized.split(","):
            raise ValueError("CATEGORIES must include 未分類")
        return normalized

    @field_validator("food_subcategory_names")
    @classmethod
    def validate_food_subcategory_names(cls, value: str) -> str:
        return cls._normalize_name_list(value, setting_name="FOOD_SUBCATEGORIES")

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"TIMEZONE must be a valid IANA timezone, got {value!r}") from exc
        return value

    @field_validator("rhythm_time_zone")
    @classmethod
    def validate_rhythm_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"RHYTHM_TIME_ZONE must be a valid IANA timezone, got {value!r}"
            ) from exc
        return value

    @field_validator("health_time_zone")
    @classmethod
    def validate_health_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"HEALTH_TIME_ZONE must be a valid IANA timezone, got {value!r}"
            ) from exc
        return value

    @field_validator("health_sleep_post_cron")
    @classmethod
    def validate_health_sleep_cron(cls, value: str) -> str:
        fields = value.split()
        if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
            raise ValueError(
                "HEALTH_SLEEP_POST_CRONは日次の '分 時 * * *' 形式で指定してください"
            )
        try:
            minute, hour = (int(fields[0]), int(fields[1]))
        except ValueError as exc:
            raise ValueError(
                "HEALTH_SLEEP_POST_CRONの分と時は整数で指定してください"
            ) from exc
        if not 0 <= minute <= 59 or not 0 <= hour <= 23:
            raise ValueError("HEALTH_SLEEP_POST_CRONの時刻が範囲外です")
        return f"{minute} {hour} * * *"

    @field_validator("rhythm_daily_post_cron")
    @classmethod
    def validate_daily_cron(cls, value: str) -> str:
        fields = value.split()
        if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
            raise ValueError(
                "RHYTHM_DAILY_POST_CRONは日次の '分 時 * * *' 形式で指定してください"
            )
        try:
            minute, hour = (int(fields[0]), int(fields[1]))
        except ValueError as exc:
            raise ValueError(
                "RHYTHM_DAILY_POST_CRONの分と時は整数で指定してください"
            ) from exc
        if not 0 <= minute <= 59 or not 0 <= hour <= 23:
            raise ValueError("RHYTHM_DAILY_POST_CRONの時刻が範囲外です")
        return f"{minute} {hour} * * *"

    @field_validator("rhythm_spending_daily_summary_cron")
    @classmethod
    def validate_spending_daily_cron(cls, value: str) -> str:
        fields = value.split()
        if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
            raise ValueError(
                "RHYTHM_SPENDING_DAILY_SUMMARY_CRONは日次の '分 時 * * *' 形式で指定してください"
            )
        try:
            minute, hour = (int(fields[0]), int(fields[1]))
        except ValueError as exc:
            raise ValueError(
                "RHYTHM_SPENDING_DAILY_SUMMARY_CRONの分と時は整数で指定してください"
            ) from exc
        if not 0 <= minute <= 59 or not 0 <= hour <= 23:
            raise ValueError("RHYTHM_SPENDING_DAILY_SUMMARY_CRONの時刻が範囲外です")
        return f"{minute} {hour} * * *"

    @field_validator("rhythm_spending_post_mode")
    @classmethod
    def validate_spending_post_mode(cls, value: str) -> str:
        if value not in {"eachEvent", "updateDailyMessage", "dailyOnly"}:
            raise ValueError(
                "RHYTHM_SPENDING_POST_MODEはeachEvent、updateDailyMessage、dailyOnlyのいずれかです"
            )
        return value

    @field_validator("finance_digest_time_zone")
    @classmethod
    def validate_finance_digest_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                "FINANCE_DIGEST_TIME_ZONE must be a valid IANA timezone, "
                f"got {value!r}"
            ) from exc
        return value

    @field_validator("finance_digest_cron")
    @classmethod
    def validate_finance_digest_cron(cls, value: str) -> str:
        fields = value.split()
        if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
            raise ValueError(
                "FINANCE_DIGEST_CRONは日次の '分 時 * * *' 形式で指定してください"
            )
        try:
            minute, hour = (int(fields[0]), int(fields[1]))
        except ValueError as exc:
            raise ValueError(
                "FINANCE_DIGEST_CRONの分と時は整数で指定してください"
            ) from exc
        if not 0 <= minute <= 59 or not 0 <= hour <= 23:
            raise ValueError("FINANCE_DIGEST_CRONの時刻が範囲外です")
        return f"{minute} {hour} * * *"

    @field_validator("finance_digest_large_expense_yen")
    @classmethod
    def validate_finance_digest_large_expense(cls, value: int) -> int:
        if type(value) is not int or not 0 < value <= ABSOLUTE_MAX_EXPENSE_YEN:
            raise ValueError(
                "FINANCE_DIGEST_LARGE_EXPENSE_YEN must be between 1 and "
                f"{ABSOLUTE_MAX_EXPENSE_YEN:,}"
            )
        return value

    @field_validator("rhythm_device_sync_token_sha256")
    @classmethod
    def validate_device_token_hash(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        plain = value.get_secret_value().strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", plain):
            raise ValueError(
                "RHYTHM_DEVICE_SYNC_TOKEN_SHA256は64桁のSHA-256 hexで指定してください"
            )
        return SecretStr(plain)

    @field_validator(
        "maimai_net_rating_url",
        "maimai_analyzer_script_url",
        "ongeki_official_net_url",
        "ongeki_image_generator_url",
    )
    @classmethod
    def validate_provider_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized.startswith("https://"):
            raise ValueError("外部プロバイダーURLはhttps://で指定してください")
        return normalized

    @field_validator("command_guide_channel_name")
    @classmethod
    def validate_command_guide_channel_name(cls, value: str) -> str:
        normalized = value.strip().replace(" ", "-")
        if not normalized or any(character in normalized for character in "#/\\"):
            raise ValueError("COMMAND_GUIDE_CHANNEL_NAMEが不正です")
        return normalized

    @field_validator("general_archive_channel_name")
    @classmethod
    def validate_general_archive_channel_name(cls, value: str) -> str:
        normalized = value.strip().replace(" ", "-")
        if not normalized or any(character in normalized for character in "#/\\"):
            raise ValueError("GENERAL_ARCHIVE_CHANNEL_NAMEが不正です")
        return normalized

    @model_validator(mode="after")
    def validate_chunirec_identity(self) -> Settings:
        if self.chunirec_user_id and self.chunirec_user_name:
            raise ValueError("CHUNIREC_USER_IDとCHUNIREC_USER_NAMEはどちらか一方を指定してください")
        return self

    @model_validator(mode="after")
    def validate_sega_credentials_pair(self) -> Settings:
        if (self.sega_id is None) != (self.sega_password is None):
            raise ValueError("SEGA_IDとSEGA_PASSWORDは両方を同時に設定してください")
        return self

    @model_validator(mode="before")
    @classmethod
    def accept_reminder_poll_interval_keyword(cls, values: Any) -> Any:
        """Keep the longer setting name usable for programmatic callers."""

        if isinstance(values, dict):
            values = values.copy()
            if (
                "reminder_poll_interval_seconds" in values
                and "reminder_poll_seconds" not in values
            ):
                values["reminder_poll_seconds"] = values["reminder_poll_interval_seconds"]
        return values

    @field_validator("attachment_max_bytes")
    @classmethod
    def validate_attachment_size(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("ATTACHMENT_MAX_BYTES must be greater than zero")
        return value

    @field_validator("ongeki_image_mode")
    @classmethod
    def validate_ongeki_image_mode(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"bestnew", "pscore"}:
            raise ValueError("ONGEKI_IMAGE_MODE must be bestnew or pscore")
        return normalized

    @field_validator("max_expense_yen")
    @classmethod
    def validate_max_expense(cls, value: int) -> int:
        if value <= 0 or value > ABSOLUTE_MAX_EXPENSE_YEN:
            raise ValueError(f"MAX_EXPENSE_YEN must be between 1 and {ABSOLUTE_MAX_EXPENSE_YEN:,}")
        return value

    @field_validator("web_host")
    @classmethod
    def validate_web_host(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("WEB_HOSTは安全のためローカルホストだけ指定できます")
        return value

    @field_validator("public_web_url")
    @classmethod
    def validate_public_web_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("PUBLIC_WEB_URLはHTTPS URLを指定してください")
        if parsed.username or parsed.password:
            raise ValueError("PUBLIC_WEB_URLへ認証情報を含めないでください")
        return normalized

    @field_validator("notion_hub_url")
    @classmethod
    def validate_notion_hub_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip()
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or parsed.hostname not in {
            "notion.so",
            "www.notion.so",
            "app.notion.com",
        }:
            raise ValueError("NOTION_HUB_URLはNotionのHTTPS URLを指定してください")
        if parsed.username or parsed.password:
            raise ValueError("NOTION_HUB_URLへ認証情報を含めないでください")
        return normalized

    @field_validator("notion_parent_type")
    @classmethod
    def validate_notion_parent_type(cls, value: str) -> str:
        normalized = value.strip().casefold()
        aliases = {"page": "page_id", "data_source": "data_source_id"}
        normalized = aliases.get(normalized, normalized)
        if normalized not in {"page_id", "data_source_id"}:
            raise ValueError("NOTION_PARENT_TYPEはpage_idまたはdata_source_idを指定してください")
        return normalized

    @model_validator(mode="after")
    def validate_distinct_discord_channels(self) -> Settings:
        configured = {
            "GENERAL_INBOX_CHANNEL_ID": self.general_inbox_channel_id,
            "GENERAL_ARCHIVE_CHANNEL_ID": self.general_archive_channel_id,
            "INBOX_CHANNEL_ID": self.inbox_channel_id,
            "RECORDS_CHANNEL_ID": self.records_channel_id,
            "CARD_CHANNEL_ID": self.card_channel_id,
            "ACCOUNT_CHANNEL_ID": self.account_channel_id,
            "MEMO_CHANNEL_ID": self.memo_channel_id,
            "MONOLOGUE_CHANNEL_ID": self.monologue_channel_id,
            "DINING_OUT_CHANNEL_ID": self.dining_out_channel_id,
            "MEMORIES_CHANNEL_ID": self.memories_channel_id,
            "SOCIAL_DRAFT_CHANNEL_ID": self.social_draft_channel_id,
            "RHYTHM_CHANNEL_ID": self.rhythm_channel_id,
            "PROGRESS_CHANNEL_ID": self.progress_channel_id,
            "MEAL_CHANNEL_ID": self.meal_channel_id,
            "DISCORD_CHUNITHM_CHANNEL_ID": self.discord_chunithm_channel_id,
            "DISCORD_MAIMAI_CHANNEL_ID": self.discord_maimai_channel_id,
            "DISCORD_ONGEKI_CHANNEL_ID": self.discord_ongeki_channel_id,
            "DISCORD_CREDIT_CHANNEL_ID": self.discord_credit_channel_id,
            "DISCORD_ERROR_CHANNEL_ID": self.discord_error_channel_id,
            "DISCORD_HEALTH_CHANNEL_ID": self.discord_health_channel_id,
            "AI_REVIEW_CHANNEL_ID": self.ai_review_channel_id,
        }
        # When both names explicitly point at the same channel, the old
        # setting is simply an alias for the new primary role.  Otherwise the
        # compatibility channel is a real additional role and must also stay
        # distinct from the archive and reviewed destinations.
        values = [value for value in configured.values() if value is not None]
        if (
            self.general_inbox_channel_id is not None
            and self.inbox_channel_id == self.general_inbox_channel_id
        ):
            values.remove(self.inbox_channel_id)
        if len(values) != len(set(values)):
            raise ValueError("Discordの用途別チャンネルIDは重複させないでください")
        if self.reminder_scheduler_enabled and self.reminder_channel_id is None:
            raise ValueError(
                "REMINDER_SCHEDULER_ENABLED=trueにはREMINDER_CHANNEL_IDが必要です"
            )
        if self.finance_digest_enabled and self.finance_digest_channel_id is None:
            raise ValueError(
                "FINANCE_DIGEST_ENABLED=trueにはFINANCE_DIGEST_CHANNEL_IDが必要です"
            )
        if self.ai_review_use_openai and self.openai_api_key is None:
            raise ValueError("AI_REVIEW_USE_OPENAI=trueにはOPENAI_API_KEYが必要です")
        if self.health_scheduler_enabled and not self.health_enabled:
            raise ValueError(
                "HEALTH_SCHEDULER_ENABLED=trueにはHEALTH_ENABLED=trueが必要です"
            )
        if self.health_scheduler_enabled and self.discord_health_channel_id is None:
            raise ValueError(
                "HEALTH_SCHEDULER_ENABLED=trueにはDISCORD_HEALTH_CHANNEL_IDが必要です"
            )
        if self.device_sync_enabled and self.rhythm_device_sync_token_sha256 is None:
            raise ValueError(
                "DEVICE_SYNC_ENABLED=trueにはRHYTHM_DEVICE_SYNC_TOKEN_SHA256が必要です"
            )
        if self.notion_sync_enabled:
            token = (
                self.notion_api_token.get_secret_value()
                if self.notion_api_token is not None
                else ""
            )
            if not token or not self.notion_parent_page_id:
                raise ValueError(
                    "NOTION_SYNC_ENABLED=trueにはNOTION_API_TOKENとNOTION_PARENT_IDが必要です"
                )
        return self

    @property
    def reminder_poll_interval_seconds(self) -> int:
        """Compatibility alias for callers that prefer the explicit name."""

        return self.reminder_poll_seconds

    @property
    def rhythm_spending_post_zero_days(self) -> bool:
        """Preferred spelling for the backward-compatible zero-day setting."""

        return self.rhythm_post_zero_spending_days

    def timezone_info(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(self.category_names.split(","))

    @property
    def food_subcategories(self) -> tuple[str, ...]:
        return tuple(self.food_subcategory_names.split(","))

    @property
    def primary_inbox_channel_id(self) -> int | None:
        """Return the confirmed #一般 inbox, with an old-setting fallback."""

        return self.general_inbox_channel_id or self.inbox_channel_id

    @property
    def legacy_inbox_channel_id(self) -> int | None:
        """Return the optional #なんでも入力 compatibility channel."""

        if self.general_inbox_channel_id is None:
            return None
        if self.inbox_channel_id is None or self.inbox_channel_id == self.general_inbox_channel_id:
            return None
        return self.inbox_channel_id

    def validate_for_bot(self) -> None:
        missing: list[str] = []
        for name, value in (
            ("DISCORD_BOT_TOKEN", self.discord_bot_token),
            ("DISCORD_GUILD_ID", self.discord_guild_id),
            ("ALLOWED_USER_ID", self.allowed_user_id),
            ("GENERAL_INBOX_CHANNEL_ID or INBOX_CHANNEL_ID", self.primary_inbox_channel_id),
        ):
            plain_value = value.get_secret_value() if isinstance(value, SecretStr) else value
            if plain_value is None or plain_value == "":
                missing.append(name)
        if missing:
            raise ValueError("Bot起動に必要な環境変数が未設定です: " + ", ".join(missing))

    def safe_summary(self) -> dict[str, Any]:
        """Return configuration values that are safe to print in diagnostics."""

        return {
            "database_url": make_url(self.database_url).render_as_string(hide_password=True),
            "data_dir": str(self.data_dir),
            "timezone": self.timezone,
            "general_inbox_channel_id": self.general_inbox_channel_id,
            "general_archive_channel_id": self.general_archive_channel_id,
            "general_archive_channel_name": self.general_archive_channel_name,
            "inbox_channel_id": self.inbox_channel_id,
            "primary_inbox_channel_id": self.primary_inbox_channel_id,
            "legacy_inbox_channel_id": self.legacy_inbox_channel_id,
            "records_channel_id": self.records_channel_id,
            "card_channel_id": self.card_channel_id,
            "account_channel_id": self.account_channel_id,
            "memo_channel_id": self.memo_channel_id,
            "monologue_channel_id": self.monologue_channel_id,
            "dining_out_channel_id": self.dining_out_channel_id,
            "memories_channel_id": self.memories_channel_id,
            "social_draft_channel_id": self.social_draft_channel_id,
            "rhythm_channel_id": self.rhythm_channel_id,
            "progress_channel_id": self.progress_channel_id,
            "meal_channel_id": self.meal_channel_id,
            "reminder_channel_id": self.reminder_channel_id,
            "reminder_scheduler_enabled": self.reminder_scheduler_enabled,
            "reminder_poll_seconds": self.reminder_poll_seconds,
            "finance_digest_enabled": self.finance_digest_enabled,
            "finance_digest_dry_run": self.finance_digest_dry_run,
            "finance_digest_channel_id": self.finance_digest_channel_id,
            "finance_digest_cron": self.finance_digest_cron,
            "finance_digest_time_zone": self.finance_digest_time_zone,
            "finance_digest_post_zero_days": self.finance_digest_post_zero_days,
            "finance_digest_reconcile_seconds": self.finance_digest_reconcile_seconds,
            "finance_digest_lookback_days": self.finance_digest_lookback_days,
            "finance_digest_large_expense_yen": self.finance_digest_large_expense_yen,
            "discord_chunithm_channel_id": self.discord_chunithm_channel_id,
            "discord_maimai_channel_id": self.discord_maimai_channel_id,
            "discord_ongeki_channel_id": self.discord_ongeki_channel_id,
            "discord_credit_channel_id": self.discord_credit_channel_id,
            "discord_error_channel_id": self.discord_error_channel_id,
            "discord_health_channel_id": self.discord_health_channel_id,
            "command_guide_channel_name": self.command_guide_channel_name,
            "ai_review_channel_id": self.ai_review_channel_id,
            "ai_review_use_openai": self.ai_review_use_openai,
            "web_url": f"http://{self.web_host}:{self.web_port}",
            "public_web_configured": self.public_web_url is not None,
            "openai_model": self.openai_model,
            "openai_inbox_classifier_enabled": self.openai_inbox_classifier_enabled,
            "openai_inbox_classifier_model": self.openai_inbox_classifier_model,
            "openai_inbox_classifier_timeout_seconds": (
                self.openai_inbox_classifier_timeout_seconds
            ),
            "auto_confirm": self.auto_confirm,
            "inbox_delete_original_after_route": self.inbox_delete_original_after_route,
            "log_level": self.log_level,
            "attachment_max_bytes": self.attachment_max_bytes,
            "max_expense_yen": self.max_expense_yen,
            "categories": list(self.categories),
            "food_subcategories": list(self.food_subcategories),
            "notion_sync_enabled": self.notion_sync_enabled,
            "notion_configured": bool(
                self.notion_api_token
                and self.notion_api_token.get_secret_value()
                and self.notion_parent_page_id
            ),
            "notion_hub_configured": self.notion_hub_url is not None,
            "notion_parent_type": self.notion_parent_type,
            "notion_title_property_name": self.notion_title_property_name,
            "notion_api_base_url": self.notion_api_base_url,
            "notion_request_timeout_seconds": self.notion_request_timeout_seconds,
            "notion_sync_poll_seconds": self.notion_sync_poll_seconds,
            "rhythm_enabled": self.rhythm_enabled,
            "rhythm_scheduler_enabled": self.rhythm_scheduler_enabled,
            "rhythm_discord_dry_run": self.rhythm_discord_dry_run,
            "rhythm_daily_post_cron": self.rhythm_daily_post_cron,
            "rhythm_time_zone": self.rhythm_time_zone,
            "rhythm_post_zero_spending_days": self.rhythm_post_zero_spending_days,
            "rhythm_spending_immediate_post_enabled": (
                self.rhythm_spending_immediate_post_enabled
            ),
            "rhythm_spending_daily_summary_enabled": (
                self.rhythm_spending_daily_summary_enabled
            ),
            "rhythm_spending_daily_summary_cron": (
                self.rhythm_spending_daily_summary_cron
            ),
            "rhythm_spending_post_mode": self.rhythm_spending_post_mode,
            "rhythm_spending_reconcile_seconds": (
                self.rhythm_spending_reconcile_seconds
            ),
            "rhythm_api_configured": self.rhythm_device_sync_token_sha256 is not None,
            "device_sync_enabled": self.device_sync_enabled,
            "device_sync_max_body_bytes": self.device_sync_max_body_bytes,
            "device_sync_rate_limit_per_minute": self.device_sync_rate_limit_per_minute,
            "device_sync_api_configured": bool(
                self.device_sync_enabled and self.rhythm_device_sync_token_sha256 is not None
            ),
            "health_enabled": self.health_enabled,
            "health_scheduler_enabled": self.health_scheduler_enabled,
            "health_discord_dry_run": self.health_discord_dry_run,
            "health_sleep_post_cron": self.health_sleep_post_cron,
            "health_time_zone": self.health_time_zone,
            "health_reconcile_seconds": self.health_reconcile_seconds,
            "health_lookback_days": self.health_lookback_days,
            "health_include_heart_rate": self.health_include_heart_rate,
            "health_api_configured": self.rhythm_device_sync_token_sha256 is not None,
            "chunirec_configured": self.chunirec_api_token is not None
            and bool(self.chunirec_user_id or self.chunirec_user_name),
            "maimai_browser_configured": bool(
                self.maimai_net_rating_url
                and self.maimai_analyzer_script_url
                and self.maimai_standard_course_enabled
            ),
            "sega_credentials_configured": bool(self.sega_id and self.sega_password),
            "rhythm_browser_headless": self.rhythm_browser_headless,
            "rhythm_browser_timeout_seconds": self.rhythm_browser_timeout_seconds,
            "ongeki_browser_configured": bool(
                self.ongeki_official_net_url
                and self.ongeki_score_log_user_id
                and self.ongeki_image_generator_url
                and self.ongeki_premium_enabled
            ),
            "discord_configured": all(
                value is not None
                for value in (
                    self.discord_bot_token,
                    self.discord_guild_id,
                    self.allowed_user_id,
                    self.primary_inbox_channel_id,
                )
            ),
            "openai_configured": self.openai_api_key is not None,
            "openai_inbox_classifier_configured": (
                self.openai_inbox_classifier_api_key is not None
            ),
        }
