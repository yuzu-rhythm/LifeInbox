import json

import pytest
from pydantic import ValidationError

from life_inbox.config import Settings


def test_safe_summary_masks_all_secrets():
    settings = Settings(
        _env_file=None,
        discord_bot_token="discord-secret",
        openai_api_key="openai-secret",
        database_url="postgresql://user:database-secret@example.invalid/app",
    )

    serialized = json.dumps(settings.safe_summary())
    assert "discord-secret" not in serialized
    assert "openai-secret" not in serialized
    assert "database-secret" not in serialized
    assert "***" in settings.safe_summary()["database_url"]


def test_categories_can_be_overridden_without_code_changes():
    settings = Settings(
        _env_file=None,
        category_names="食費,医療,未分類",
        food_subcategory_names="外食,自炊",
    )
    assert settings.categories == ("食費", "医療", "未分類")
    assert settings.food_subcategories == ("外食", "自炊")

    with pytest.raises(ValidationError, match="未分類"):
        Settings(_env_file=None, category_names="食費,交通")


def test_records_channel_is_optional_and_loaded_from_environment(monkeypatch):
    without_records = Settings(_env_file=None)
    assert without_records.records_channel_id is None
    assert without_records.safe_summary()["records_channel_id"] is None

    monkeypatch.setenv("RECORDS_CHANNEL_ID", "40")
    with_records = Settings(_env_file=None)
    assert with_records.records_channel_id == 40
    assert with_records.safe_summary()["records_channel_id"] == 40


def test_inbox_original_deletion_is_explicit_opt_in(monkeypatch):
    disabled = Settings(_env_file=None)
    assert disabled.inbox_delete_original_after_route is False
    assert disabled.safe_summary()["inbox_delete_original_after_route"] is False

    monkeypatch.setenv("INBOX_DELETE_ORIGINAL_AFTER_ROUTE", "true")
    enabled = Settings(_env_file=None)
    assert enabled.inbox_delete_original_after_route is True
    assert enabled.safe_summary()["inbox_delete_original_after_route"] is True


def test_notion_delivery_is_opt_in_requires_credentials_and_keeps_token_secret():
    defaults = Settings(_env_file=None)
    assert defaults.notion_sync_enabled is False
    assert defaults.notion_parent_type == "page_id"
    assert defaults.notion_sync_poll_seconds == 60

    with pytest.raises(ValidationError, match="NOTION_API_TOKEN"):
        Settings(_env_file=None, notion_sync_enabled=True)

    configured = Settings(
        _env_file=None,
        notion_sync_enabled=True,
        notion_api_token="synthetic-notion-token",
        notion_parent_page_id="synthetic-parent-id",
        notion_parent_type="data_source",
        notion_title_property_name="Name",
        notion_sync_poll_seconds=15,
    )
    summary = configured.safe_summary()
    assert configured.notion_parent_type == "data_source_id"
    assert summary["notion_configured"] is True
    assert summary["notion_parent_type"] == "data_source_id"
    assert summary["notion_sync_poll_seconds"] == 15
    assert "synthetic-notion-token" not in json.dumps(summary)

    with pytest.raises(ValidationError, match="page_id"):
        Settings(_env_file=None, notion_parent_type="database_id")


def test_bot_configuration_allows_local_fallback_without_openai_key():
    settings = Settings(
        _env_file=None,
        discord_bot_token="fixture-token",
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        openai_api_key=None,
    )

    settings.validate_for_bot()
    assert settings.safe_summary()["openai_configured"] is False


def test_optional_capture_channels_and_local_web_settings(monkeypatch):
    monkeypatch.setenv("CARD_CHANNEL_ID", "41")
    monkeypatch.setenv("ACCOUNT_CHANNEL_ID", "42")
    monkeypatch.setenv("MEMO_CHANNEL_ID", "43")
    monkeypatch.setenv("RHYTHM_CHANNEL_ID", "44")
    monkeypatch.setenv("MEAL_CHANNEL_ID", "45")
    monkeypatch.setenv("WEB_PORT", "9000")

    settings = Settings(_env_file=None)

    assert settings.card_channel_id == 41
    assert settings.account_channel_id == 42
    assert settings.memo_channel_id == 43
    assert settings.rhythm_channel_id == 44
    assert settings.meal_channel_id == 45
    assert settings.safe_summary()["web_url"] == "http://127.0.0.1:9000"


def test_reviewed_inbox_destination_channels_load_and_are_safe(monkeypatch):
    for name, value in (
        ("MONOLOGUE_CHANNEL_ID", "51"),
        ("DINING_OUT_CHANNEL_ID", "52"),
        ("MEMORIES_CHANNEL_ID", "53"),
        ("SOCIAL_DRAFT_CHANNEL_ID", "54"),
    ):
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)

    assert settings.monologue_channel_id == 51
    assert settings.dining_out_channel_id == 52
    assert settings.memories_channel_id == 53
    assert settings.social_draft_channel_id == 54
    summary = settings.safe_summary()
    assert summary["monologue_channel_id"] == 51
    assert summary["dining_out_channel_id"] == 52
    assert summary["memories_channel_id"] == 53
    assert summary["social_draft_channel_id"] == 54


def test_reviewed_inbox_destination_channel_ids_must_not_duplicate():
    with pytest.raises(ValidationError, match="重複"):
        Settings(_env_file=None, monologue_channel_id=51, social_draft_channel_id=51)


def test_progress_channel_is_optional_loaded_and_safe_summary_includes_it(monkeypatch):
    without_progress = Settings(_env_file=None)
    assert without_progress.progress_channel_id is None
    assert without_progress.safe_summary()["progress_channel_id"] is None

    monkeypatch.setenv("PROGRESS_CHANNEL_ID", "46")
    with_progress = Settings(_env_file=None)
    assert with_progress.progress_channel_id == 46
    assert with_progress.safe_summary()["progress_channel_id"] == 46


def test_discord_channel_ids_must_be_distinct_and_web_stays_local():
    with pytest.raises(ValidationError, match="重複"):
        Settings(_env_file=None, inbox_channel_id=30, card_channel_id=30)

    with pytest.raises(ValidationError, match="ローカルホスト"):
        Settings(_env_file=None, web_host="0.0.0.0")

    with pytest.raises(ValidationError, match="重複"):
        Settings(_env_file=None, rhythm_channel_id=31, progress_channel_id=31)


def test_public_web_url_is_optional_https_navigation_only():
    unset = Settings(_env_file=None)
    assert unset.public_web_url is None
    assert unset.safe_summary()["public_web_configured"] is False

    configured = Settings(
        _env_file=None,
        public_web_url="https://archive.example.test/",
    )
    assert configured.public_web_url == "https://archive.example.test"
    assert configured.safe_summary()["public_web_configured"] is True

    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(_env_file=None, public_web_url="http://archive.example.test")
    with pytest.raises(ValidationError, match="認証情報"):
        Settings(
            _env_file=None,
            public_web_url="https://user:password@archive.example.test",
        )


def test_rhythm_collection_defaults_are_opt_in_and_dry_run(monkeypatch):
    monkeypatch.delenv("RHYTHM_DAILY_POST_CRON", raising=False)
    monkeypatch.delenv("RHYTHM_SPENDING_DAILY_SUMMARY_CRON", raising=False)
    settings = Settings(_env_file=None)

    assert settings.rhythm_enabled is False
    assert settings.rhythm_scheduler_enabled is False
    assert settings.rhythm_discord_dry_run is True
    assert settings.rhythm_daily_post_cron == "0 10 * * *"
    assert settings.rhythm_time_zone == "Asia/Tokyo"
    assert settings.rhythm_spending_daily_summary_cron == "55 23 * * *"
    assert settings.safe_summary()["rhythm_api_configured"] is False


def test_rhythm_secrets_are_validated_and_never_in_safe_summary():
    device_hash = "a" * 64
    settings = Settings(
        _env_file=None,
        rhythm_device_sync_token_sha256=device_hash,
        chunirec_api_token="chunirec-secret",
        chunirec_user_name="private-user",
    )

    serialized = json.dumps(settings.safe_summary())
    assert device_hash not in serialized
    assert "chunirec-secret" not in serialized
    assert "private-user" not in serialized
    assert settings.safe_summary()["rhythm_api_configured"] is True
    assert settings.safe_summary()["chunirec_configured"] is True

    with pytest.raises(ValidationError, match="SHA-256"):
        Settings(_env_file=None, rhythm_device_sync_token_sha256="raw-token")


def test_ongeki_manual_browser_flow_does_not_require_bookmarklet_file():
    settings = Settings(
        _env_file=None,
        ongeki_score_log_user_id="fixture-public-user",
        ongeki_premium_enabled=True,
    )

    assert settings.ongeki_score_log_bookmarklet_path is None
    assert settings.safe_summary()["ongeki_browser_configured"] is True


def test_rhythm_daily_cron_and_provider_identity_are_strict():
    with pytest.raises(ValidationError, match="分と時"):
        Settings(_env_file=None, rhythm_daily_post_cron="*/5 * * * *")

    with pytest.raises(ValidationError, match="どちらか一方"):
        Settings(
            _env_file=None,
            chunirec_user_id="123",
            chunirec_user_name="duplicate-identity",
        )

    with pytest.raises(ValidationError, match="https"):
        Settings(_env_file=None, maimai_net_rating_url="http://example.invalid")


def test_sega_credentials_must_be_configured_as_a_pair_and_stay_secret():
    with pytest.raises(ValidationError, match="両方"):
        Settings(_env_file=None, sega_id="fixture-id")

    with pytest.raises(ValidationError, match="両方"):
        Settings(_env_file=None, sega_password="fixture-password")

    settings = Settings(
        _env_file=None,
        sega_id="fixture-id",
        sega_password="fixture-password",
    )
    summary = json.dumps(settings.safe_summary())
    assert settings.safe_summary()["sega_credentials_configured"] is True
    assert "fixture-id" not in summary
    assert "fixture-password" not in summary


def test_finance_digest_defaults_and_safe_summary(monkeypatch):
    defaults = Settings(_env_file=None)

    assert defaults.finance_digest_enabled is False
    assert defaults.finance_digest_dry_run is True
    assert defaults.finance_digest_channel_id is None
    assert defaults.finance_digest_cron == "45 23 * * *"
    assert defaults.finance_digest_time_zone == "Asia/Tokyo"
    assert defaults.finance_digest_post_zero_days is True
    assert defaults.finance_digest_reconcile_seconds == 60
    assert defaults.finance_digest_lookback_days == 7
    assert defaults.finance_digest_large_expense_yen == 5_000

    monkeypatch.setenv("FINANCE_DIGEST_ENABLED", "true")
    monkeypatch.setenv("FINANCE_DIGEST_DRY_RUN", "false")
    monkeypatch.setenv("FINANCE_DIGEST_CHANNEL_ID", "40")
    configured = Settings(_env_file=None)
    summary = configured.safe_summary()
    assert configured.finance_digest_enabled is True
    assert configured.finance_digest_dry_run is False
    assert configured.finance_digest_channel_id == 40
    assert summary["finance_digest_enabled"] is True
    assert summary["finance_digest_dry_run"] is False
    assert summary["finance_digest_channel_id"] == 40
    assert summary["finance_digest_cron"] == "45 23 * * *"
    assert summary["finance_digest_time_zone"] == "Asia/Tokyo"


def test_finance_digest_enabled_requires_channel_but_records_channel_is_valid():
    with pytest.raises(ValidationError, match="FINANCE_DIGEST_CHANNEL_ID"):
        Settings(_env_file=None, finance_digest_enabled=True)

    settings = Settings(
        _env_file=None,
        records_channel_id=40,
        finance_digest_enabled=True,
        finance_digest_channel_id=40,
    )
    assert settings.finance_digest_channel_id == settings.records_channel_id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("finance_digest_cron", "*/5 * * * *"),
        ("finance_digest_cron", "45 23 * * 1"),
        ("finance_digest_cron", "60 23 * * *"),
        ("finance_digest_time_zone", "Not/AZone"),
        ("finance_digest_reconcile_seconds", 4),
        ("finance_digest_reconcile_seconds", 3_601),
        ("finance_digest_lookback_days", 0),
        ("finance_digest_lookback_days", 32),
        ("finance_digest_large_expense_yen", 0),
        ("finance_digest_large_expense_yen", 10_000_001),
    ],
)
def test_finance_digest_limits_and_schedule_are_strict(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_ai_review_is_local_opt_in_and_requires_key_for_openai():
    defaults = Settings(_env_file=None)
    assert defaults.ai_review_channel_id is None
    assert defaults.ai_review_use_openai is False
    assert defaults.safe_summary()["ai_review_use_openai"] is False

    configured = Settings(
        _env_file=None,
        ai_review_channel_id=50,
        ai_review_use_openai=True,
        openai_api_key="test-key",
    )
    assert configured.ai_review_channel_id == 50
    assert configured.safe_summary()["ai_review_channel_id"] == 50
    assert "test-key" not in json.dumps(configured.safe_summary())

    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(_env_file=None, ai_review_use_openai=True)
