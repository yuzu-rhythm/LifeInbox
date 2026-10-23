"""Idempotent Discord timeline for recurring Life Inbox jobs.

The timeline is derived only from non-secret ``Settings`` fields.  Provisioning
is deliberately narrow: it finds one exact, existing channel in the configured
guild and only creates or edits a message owned by this Bot with our marker.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import discord

from .config import Settings

logger = logging.getLogger(__name__)

SCHEDULE_TIMELINE_CHANNEL_NAME = "定期更新スケジュール"
SCHEDULE_TIMELINE_VERSION = "life-inbox-schedule-timeline:v1"
_TIMELINE_MARKER_RE = re.compile(r"`life-inbox-schedule-timeline:v\d+`")


def _daily_time(cron: str) -> str:
    minute, hour, *_ = cron.split()
    return f"{int(hour):02d}:{int(minute):02d}"


def _channel(channel_id: int | None) -> str:
    return f"<#{channel_id}>" if channel_id is not None else "投稿先未設定"


def _state(*, enabled: bool, dry_run: bool = False, configured: bool = True) -> str:
    if not enabled:
        return "無効"
    if not configured:
        return "設定不足"
    return "有効（dry-run）" if dry_run else "有効（実処理）"


def schedule_timeline_message(settings: Settings) -> str:
    """Render a secret-free and deterministic snapshot of current scheduling."""

    finance_state = _state(
        enabled=settings.finance_digest_enabled,
        dry_run=settings.finance_digest_dry_run,
        configured=settings.finance_digest_channel_id is not None,
    )
    rating_channels_configured = all(
        channel_id is not None
        for channel_id in (
            settings.discord_chunithm_channel_id,
            settings.discord_maimai_channel_id,
            settings.discord_ongeki_channel_id,
        )
    )
    rating_state = _state(
        enabled=settings.rhythm_enabled and settings.rhythm_scheduler_enabled,
        dry_run=settings.rhythm_discord_dry_run,
        configured=rating_channels_configured,
    )
    spending_configured = settings.discord_credit_channel_id is not None
    spending_immediate_state = _state(
        enabled=(
            settings.rhythm_enabled
            and settings.rhythm_spending_immediate_post_enabled
        ),
        dry_run=settings.rhythm_discord_dry_run,
        configured=spending_configured,
    )
    spending_daily_state = _state(
        enabled=(
            settings.rhythm_enabled
            and settings.rhythm_spending_daily_summary_enabled
        ),
        dry_run=settings.rhythm_discord_dry_run,
        configured=spending_configured,
    )
    reminder_state = _state(
        enabled=settings.reminder_scheduler_enabled,
        configured=settings.reminder_channel_id is not None,
    )
    notion_configured = bool(
        settings.notion_api_token is not None and settings.notion_parent_page_id
    )
    notion_state = _state(
        enabled=settings.notion_sync_enabled,
        configured=notion_configured,
    )
    health_state = _state(
        enabled=settings.health_enabled and settings.health_scheduler_enabled,
        dry_run=settings.health_discord_dry_run,
        configured=settings.discord_health_channel_id is not None,
    )

    lines = [
        "【Bot定期更新タイムライン】",
        f"`{SCHEDULE_TIMELINE_VERSION}`",
        f"基準タイムゾーン: `{settings.timezone}`",
        "",
        "**随時**",
        "・#一般: 原文/添付をDBと #一般アーカイブへ保存 → 分類候補 → "
        "保存先コピー成功後だけ元投稿を削除",
        f"・期限通知: {settings.reminder_poll_seconds}秒ごとに確認 / "
        f"{reminder_state} / {_channel(settings.reminder_channel_id)}",
        f"・音ゲー利用額: iPhone同期後に即時反映 / {spending_immediate_state} / "
        f"方式 `{settings.rhythm_spending_post_mode}` / "
        f"{_channel(settings.discord_credit_channel_id)}",
        f"・Notion安全バックアップ: {settings.notion_sync_poll_seconds}秒ごとに確認 / "
        f"{notion_state}（安全ポリシー対象だけ）",
        "",
        f"**毎日 {_daily_time(settings.finance_digest_cron)} "
        f"({settings.finance_digest_time_zone})**",
        f"・家計・口座・クレカ日次まとめ / {finance_state} / "
        f"{_channel(settings.finance_digest_channel_id)}",
        "",
        f"**毎日 {_daily_time(settings.rhythm_daily_post_cron)} "
        f"({settings.rhythm_time_zone})**",
        f"・CHUNITHM・maimai・オンゲキのレート取得と更新時投稿 / {rating_state}",
        "  投稿先: "
        + " / ".join(
            (
                _channel(settings.discord_chunithm_channel_id),
                _channel(settings.discord_maimai_channel_id),
                _channel(settings.discord_ongeki_channel_id),
            )
        ),
        "",
        f"**毎日 {_daily_time(settings.rhythm_spending_daily_summary_cron)} "
        f"({settings.rhythm_time_zone})**",
        f"・音ゲー利用額の日次まとめ / {spending_daily_state} / "
        f"{_channel(settings.discord_credit_channel_id)}",
        "",
        f"**毎日 {_daily_time(settings.health_sleep_post_cron)} "
        f"({settings.health_time_zone})**",
        f"・睡眠・ワークアウトまとめ / {health_state} / "
        f"{_channel(settings.discord_health_channel_id)}",
        "",
        "**手動実行**",
        "・`/レート更新` または `/get`（全機種または機種別） / `/取得状況` / "
        "`/認証状況` / "
        "`/今日の利用額` / `/家計簿配信確認`",
        "",
        "時刻到達は処理開始条件です。外部ページのURL・DOM・通信結果・生成画像などの"
        "明示的な成功を確認できない取得は成功扱いにせず、認証・手動確認待ち・失敗を"
        "区別します。設定変更後はこの固定メッセージを再生成します。",
    ]
    content = "\n".join(lines)
    if len(content) > 2_000:
        raise ValueError("schedule timeline exceeds Discord message limit")
    return content


def _is_managed_timeline(content: str | None) -> bool:
    return bool(content and _TIMELINE_MARKER_RE.search(content))


def _same_id(expected: Any, actual: Any) -> bool:
    return expected is not None and actual is not None and str(expected) == str(actual)


async def _history_messages(channel: Any):
    history = channel.history(limit=None)
    if hasattr(history, "__aiter__"):
        async for message in history:
            yield message
        return
    for message in history:
        yield message


@dataclass(frozen=True, slots=True)
class ScheduleTimelineProvisionResult:
    channel_id: int
    message_id: int
    created: bool
    updated: bool
    pinned: bool
    pin_skipped: bool
    duplicate_managed_messages: int = 0


async def _provision_guild_timeline(
    guild: Any,
    *,
    bot_user_id: Any,
    settings: Settings,
) -> ScheduleTimelineProvisionResult:
    channels = [
        channel
        for channel in getattr(guild, "text_channels", ())
        if getattr(channel, "name", None) == SCHEDULE_TIMELINE_CHANNEL_NAME
        and callable(getattr(channel, "send", None))
    ]
    if not channels:
        raise RuntimeError("定期更新スケジュールチャンネルがありません")
    if len(channels) != 1:
        raise RuntimeError("同名の定期更新スケジュールチャンネルが複数あります")
    channel = channels[0]
    managed: list[Any] = []
    async for message in _history_messages(channel):
        author = getattr(message, "author", None)
        author_id = getattr(author, "id", getattr(message, "author_id", None))
        if _same_id(bot_user_id, author_id) and _is_managed_timeline(
            getattr(message, "content", None)
        ):
            managed.append(message)

    existing = max(managed, key=lambda item: int(getattr(item, "id", 0))) if managed else None
    desired = schedule_timeline_message(settings)
    created = existing is None
    updated = False
    if existing is None:
        message = await channel.send(desired)
    else:
        message = existing
        if getattr(existing, "content", None) != desired:
            edited = await existing.edit(content=desired)
            if edited is not None:
                message = edited
            updated = True

    pinned = False
    pin_skipped = bool(getattr(message, "pinned", False))
    if not pin_skipped:
        try:
            await message.pin()
            pinned = True
        except Exception as exc:  # pinning is useful but not required
            pin_skipped = True
            logger.warning("schedule timeline pin skipped (%s)", type(exc).__name__)

    return ScheduleTimelineProvisionResult(
        channel_id=int(getattr(channel, "id")),
        message_id=int(getattr(message, "id")),
        created=created,
        updated=updated,
        pinned=pinned,
        pin_skipped=pin_skipped,
        duplicate_managed_messages=max(0, len(managed) - 1),
    )


class _ScheduleTimelineClient(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.settings = settings
        self.result: ScheduleTimelineProvisionResult | None = None
        self.failure: Exception | None = None

    async def on_ready(self) -> None:
        try:
            if self.settings.discord_guild_id is None:
                raise RuntimeError("設定済みGuild以外は処理しません")
            guild = self.get_guild(self.settings.discord_guild_id)
            if guild is None:
                raise RuntimeError("生活サーバーを取得できませんでした")
            if self.user is None or getattr(self.user, "id", None) is None:
                raise RuntimeError("Botユーザーを確認できませんでした")
            self.result = await _provision_guild_timeline(
                guild,
                bot_user_id=self.user.id,
                settings=self.settings,
            )
        except Exception as exc:
            self.failure = exc
        finally:
            await self.close()


def provision_schedule_timeline(settings: Settings) -> ScheduleTimelineProvisionResult:
    """Create or update the one Bot-owned schedule timeline message."""

    settings.validate_for_bot()
    if settings.discord_bot_token is None:
        raise ValueError("DISCORD_BOT_TOKEN is required")
    client = _ScheduleTimelineClient(settings)
    client.run(settings.discord_bot_token.get_secret_value(), log_handler=None)
    if client.failure is not None:
        raise RuntimeError(
            "定期更新タイムラインをDiscordへ反映できませんでした"
        ) from client.failure
    if client.result is None:
        raise RuntimeError("定期更新タイムラインの結果を確認できませんでした")
    return client.result


__all__ = [
    "SCHEDULE_TIMELINE_CHANNEL_NAME",
    "SCHEDULE_TIMELINE_VERSION",
    "ScheduleTimelineProvisionResult",
    "provision_schedule_timeline",
    "schedule_timeline_message",
]
