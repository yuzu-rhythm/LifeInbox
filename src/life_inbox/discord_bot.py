"""Thin discord.py adapter around the local InboxService."""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .ai_review import (
    AIReviewService,
    OpenAIReviewAdapter,
    ReviewProvider,
    ReviewProviderError,
    format_discord_review,
)
from .analyzer import NullAnalyzer, OpenAIAnalyzer
from .attachments import SUPPORTED_IMAGE_TYPES
from .budgets import BudgetMonthView, BudgetProgress, BudgetService
from .capture_context import CaptureContextService, format_capture_location
from .career import (
    ApplicationStage,
    CareerKind,
    CareerRecord,
    CareerService,
    CareerStatus,
)
from .channel_parsers import (
    AccountInstructionKind,
    RhythmInstruction,
    RhythmInstructionKind,
    parse_account_instruction,
    parse_rhythm_instruction,
    split_account_prefix,
)
from .command_guide import command_guide_messages
from .config import Settings
from .constants import AccountKind, RhythmGame
from .db import Database
from .finance import AccountBalance, FinanceService, SettlementAccountLink
from .finance_digest import FinanceDigestService, format_daily_digest
from .finance_digest_discord import FinanceDigestPublisher
from .finance_digest_scheduler import FinanceDigestScheduler
from .health_discord import HealthDiscordPublisher
from .health_persistence import HealthPersistence
from .health_scheduler import HealthScheduler
from .inbox_classifier import (
    CATEGORY_LABELS,
    InboxCandidate,
    InboxCategory,
    InboxClassification,
    InboxClassifier,
    InboxImageInput,
)
from .maimai import (
    MaimaiMyBestRecord,
    MaimaiMyBestService,
    MaimaiNetFavoriteRecord,
    MaimaiNetFavoriteService,
)
from .models import DiscordArchiveRecord, DiscordArchiveStatus, Event
from .notion_http import NotionHttpTransport
from .notion_sync import NotionSyncService
from .notion_worker import NotionSyncWorker
from .planner import local_datetime, render_inbox, render_month, render_planner, render_today
from .reminders import (
    ReminderNotification,
    ReminderScheduler,
    ReminderService,
    RoutineService,
    RoutineView,
)
from .rhythm import (
    RhythmFavoriteRecord,
    RhythmResultRecord,
    RhythmService,
    RhythmTodoResult,
)
from .rhythm_discord import (
    DiscordPyTransport,
    RhythmDiscordPublisher,
    format_spending_message,
)
from .rhythm_images import RatingImageVerificationError, verify_rating_image
from .rhythm_persistence import RhythmPersistence
from .rhythm_scheduler import RhythmScheduler
from .rhythm_spending import RhythmSpendingEventPersistence
from .rhythm_spending_capture import RhythmSpendingCaptureService
from .rhythm_spending_discord import RhythmSpendingDiscordPublisher
from .rhythm_spending_scheduler import RhythmSpendingScheduler
from .schemas import InboxRouteResult
from .scope import ScopeGuard
from .services import CaptureResult, InboxService, IncomingAttachment, IncomingMessage
from .social_drafts import SocialDraftResult, SocialDraftService
from .tasks import TaskService, TaskView

logger = logging.getLogger(__name__)


class _InboxCandidateAlreadyRouted(RuntimeError):
    """Signal that another candidate already completed this Event route."""


@dataclass(frozen=True, slots=True)
class DiscordArchiveDelivery:
    """Safe result of the archive-first Discord delivery gate."""

    event_id: int
    status: str
    succeeded: bool
    archive_message_id: str | None = None
    pending: bool = False
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class _DiscordArchiveAttempt:
    """Internal claim result for one persistent Discord archive row."""

    succeeded: bool = False
    send_allowed: bool = False
    pending: bool = False
    message_ids: tuple[str, ...] = ()
    archive_message_id: str | None = None
    error_code: str | None = None


HISTORY_DEFAULT_COUNT = 20
HISTORY_MAX_COUNT = 50
DEFAULT_CARD_ACCOUNT = "クレジットカード"
DEFAULT_BANK_ACCOUNT = "普通口座"
DISCORD_ARCHIVE_ATTEMPT_LEASE_SECONDS = 300

ACCOUNT_KIND_LABELS = {
    AccountKind.CASH: "現金",
    AccountKind.BANK: "銀行",
    AccountKind.CARD: "クレジットカード",
    AccountKind.E_MONEY: "電子マネー",
    AccountKind.INVESTMENT: "投資・NISA",
}

RHYTHM_GAME_LABELS = {
    RhythmGame.MAIMAI: "maimai",
    RhythmGame.ONGEKI: "オンゲキ",
    RhythmGame.CHUNITHM: "チュウニズム",
}

RHYTHM_GAME_CHOICES = [
    app_commands.Choice(name="maimai", value=RhythmGame.MAIMAI.value),
    app_commands.Choice(name="オンゲキ", value=RhythmGame.ONGEKI.value),
    app_commands.Choice(name="チュウニズム", value=RhythmGame.CHUNITHM.value),
]

RATING_GAME_CHOICES = [
    app_commands.Choice(name="ウニ（CHUNITHM）", value="chunithm"),
    app_commands.Choice(name="マイ（maimai）", value="maimai"),
    app_commands.Choice(name="ゲキ（オンゲキ）", value="ongeki"),
]

CAREER_KIND_CHOICES = [
    app_commands.Choice(name="求人", value=CareerKind.JOB.value),
    app_commands.Choice(name="企業", value=CareerKind.COMPANY.value),
    app_commands.Choice(name="イベント・説明会", value=CareerKind.EVENT.value),
    app_commands.Choice(name="応募", value=CareerKind.APPLICATION.value),
]

CAREER_RECORD_TYPE_CHOICES = [
    app_commands.Choice(name="求人・企業・イベント", value="opportunity"),
    app_commands.Choice(name="応募", value="application"),
]

CAREER_STAGE_CHOICES = [
    app_commands.Choice(name="保存", value=ApplicationStage.SAVED.value),
    app_commands.Choice(name="興味あり", value=ApplicationStage.INTERESTED.value),
    app_commands.Choice(name="応募済み", value=ApplicationStage.APPLIED.value),
    app_commands.Choice(name="書類選考", value=ApplicationStage.SCREENING.value),
    app_commands.Choice(name="面接", value=ApplicationStage.INTERVIEW.value),
    app_commands.Choice(name="最終面接", value=ApplicationStage.FINAL_INTERVIEW.value),
    app_commands.Choice(name="オファー", value=ApplicationStage.OFFER.value),
    app_commands.Choice(name="承諾", value=ApplicationStage.ACCEPTED.value),
    app_commands.Choice(name="不採用", value=ApplicationStage.REJECTED.value),
    app_commands.Choice(name="辞退", value=ApplicationStage.WITHDRAWN.value),
    app_commands.Choice(name="クローズ", value=ApplicationStage.CLOSED.value),
]

CAREER_STATUS_CHOICES = [
    app_commands.Choice(name="未処理", value=CareerStatus.OPEN.value),
    app_commands.Choice(name="興味あり", value=CareerStatus.INTERESTED.value),
    app_commands.Choice(name="応募済み", value=CareerStatus.APPLIED.value),
    app_commands.Choice(name="進行中", value=CareerStatus.IN_PROGRESS.value),
    app_commands.Choice(name="承諾", value=CareerStatus.ACCEPTED.value),
    app_commands.Choice(name="完了", value=CareerStatus.CLOSED.value),
    app_commands.Choice(name="不採用", value=CareerStatus.REJECTED.value),
    app_commands.Choice(name="辞退", value=CareerStatus.WITHDRAWN.value),
    app_commands.Choice(name="アーカイブ", value=CareerStatus.ARCHIVED.value),
]

# Keep this mapping Unicode-only and explicit.  The reaction handler narrows
# it to the candidates displayed for the specific source message, so a
# reaction that is valid for another category cannot route this message.
INBOX_CATEGORY_REACTIONS: dict[InboxCategory, str] = {
    InboxCategory.EXPENSES: "💰",
    InboxCategory.MEALS: "🍚",
    InboxCategory.MEMO: "📝",
    InboxCategory.SELF_TALK: "💭",
    InboxCategory.DINING_OUT: "🍽️",
    InboxCategory.MEMORIES: "📸",
    InboxCategory.SOCIAL_DRAFT: "📣",
    InboxCategory.TASK: "📌",
    InboxCategory.SHOPPING: "🛒",
    InboxCategory.JOB_HUNTING: "💼",
    InboxCategory.SAVED_LINK: "🔗",
    InboxCategory.RHYTHM_SPENDING: "🪙",
    InboxCategory.RHYTHM_PROGRESS: "🎵",
    InboxCategory.FINANCE_ACCOUNT: "🏦",
    InboxCategory.FINANCE_CARD: "💳",
}


def _reaction_name(emoji: Any) -> str | None:
    """Return a normalized Unicode reaction name, rejecting custom emoji IDs."""

    if isinstance(emoji, str):
        name = emoji
    else:
        if getattr(emoji, "id", None) is not None:
            return None
        is_custom_emoji = getattr(emoji, "is_custom_emoji", None)
        if callable(is_custom_emoji) and is_custom_emoji():
            return None
        name = getattr(emoji, "name", None)
    if not isinstance(name, str) or not name:
        return None
    # Discord clients may preserve or omit text/emoji variation selectors for
    # the same Unicode reaction. Treat those presentation selectors as the
    # same reviewed emoji while continuing to reject every custom emoji ID.
    return unicodedata.normalize("NFC", name).replace("\ufe0e", "").replace("\ufe0f", "")


def inbox_reaction_for_category(category: InboxCategory) -> str | None:
    """Return the reviewed Unicode reaction for one candidate category."""

    if not isinstance(category, InboxCategory):
        return None
    return INBOX_CATEGORY_REACTIONS.get(category)


def inbox_category_for_reaction(emoji: Any) -> InboxCategory | None:
    """Resolve only an exact, reviewed Unicode reaction to a category."""

    reaction = _reaction_name(emoji)
    if reaction is None:
        return None
    for category, candidate_reaction in INBOX_CATEGORY_REACTIONS.items():
        if _reaction_name(candidate_reaction) == reaction:
            return category
    return None


def _validate_rhythm_publisher(
    settings: Settings,
    publisher: RhythmDiscordPublisher,
    *,
    source: str,
) -> None:
    """Fail closed when an injected publisher disagrees with Bot settings."""

    if publisher.dry_run != settings.rhythm_discord_dry_run:
        raise ValueError(f"{source} publisher dry-run setting must match Bot settings")


def _validate_finance_digest_publisher(
    settings: Settings,
    publisher: FinanceDigestPublisher,
    *,
    source: str,
) -> None:
    """Fail closed when an injected finance publisher can bypass dry-run."""

    if publisher.dry_run != settings.finance_digest_dry_run:
        raise ValueError(
            f"{source} finance digest publisher dry-run setting must match Bot settings"
        )

TASK_SOURCE_LABELS = {
    "manual": "手動",
    "google_classroom": "Google Classroom",
    "notion": "Notion",
}


def _message_chunks(message: str, *, limit: int = 1_900) -> list[str]:
    """Split long Discord output without silently dropping Inbox records."""

    if not message:
        return ["（空）"]
    chunks: list[str] = []
    current = ""
    for line in message.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""
            chunks.append(line[:limit].rstrip("\n"))
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current.rstrip("\n"))
            current = ""
        current += line
    if current:
        chunks.append(current.rstrip("\n"))
    return chunks or ["（空）"]


def _archive_message_chunks(message: str, *, limit: int = 1_900) -> list[str]:
    """Split archive content while preserving the concatenated source bytes."""

    if not message:
        return ["（空）"]
    return [message[index : index + limit] for index in range(0, len(message), limit)]


def _web_base_url(settings: Settings) -> str:
    """Build the configured local Web origin for Discord-facing links."""

    return f"http://{settings.web_host}:{settings.web_port}"


def _event_web_url(settings: Settings, event_id: int) -> str:
    """Build one safe local Web detail URL for a positive Event ID."""

    if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
        raise ValueError("event_id must be a positive integer")
    return f"{_web_base_url(settings)}/events/{event_id}"


class NotionPromotionView(discord.ui.View):
    """Persistent, explicit local-queue promotion for one Event."""

    def __init__(self, bot: LifeInboxBot, event_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.event_id = event_id
        button = discord.ui.Button(
            label="📚 Notionへバックアップ",
            style=discord.ButtonStyle.secondary,
            custom_id=f"life-inbox:notion-promote:{event_id}",
        )
        button.callback = self._promote
        self.add_item(button)

    async def _promote(self, interaction: discord.Interaction) -> None:
        await self.bot._handle_notion_promotion(interaction, self.event_id)


class MemoWebView(discord.ui.View):
    """Persistent Web link and optional explicit Notion promotion for a memo."""

    def __init__(
        self,
        settings: Settings,
        event_id: int,
        *,
        bot: LifeInboxBot | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.event_id = event_id
        self.url = _event_web_url(settings, event_id)
        self.add_item(
            discord.ui.Button(
                label="Webで開く",
                style=discord.ButtonStyle.link,
                url=self.url,
            )
        )
        if bot is not None:
            button = discord.ui.Button(
                label="📚 Notionへバックアップ",
                style=discord.ButtonStyle.secondary,
                custom_id=f"life-inbox:notion-promote:{event_id}",
            )

            async def promote(interaction: discord.Interaction) -> None:
                await bot._handle_notion_promotion(interaction, event_id)

            button.callback = promote
            self.add_item(button)


class RoutineCompletionView(discord.ui.View):
    """Persistent, personal completion button for a routine reminder."""

    def __init__(
        self,
        routine_service: RoutineService,
        routine_id: int,
        settings: Settings,
    ) -> None:
        super().__init__(timeout=None)
        self.routine_service = routine_service
        self.routine_id = routine_id
        self.settings = settings
        button = discord.ui.Button(
            label="今日の完了",
            style=discord.ButtonStyle.success,
            custom_id=f"life-inbox:routine-complete:{routine_id}",
        )
        button.callback = self._complete
        self.add_item(button)

    def _allowed(self, interaction: discord.Interaction) -> bool:
        user = getattr(interaction, "user", None)
        return (
            str(getattr(user, "id", "")) == str(self.settings.allowed_user_id)
            and str(getattr(interaction, "guild_id", ""))
            == str(self.settings.discord_guild_id)
            and str(getattr(interaction, "channel_id", ""))
            == str(self.settings.reminder_channel_id)
        )

    async def _complete(self, interaction: discord.Interaction) -> None:
        if not self._allowed(interaction):
            await interaction.response.send_message(
                "この操作は許可されていません。", ephemeral=True
            )
            return
        try:
            routine = await asyncio.to_thread(
                self.routine_service.complete,
                self.routine_id,
            )
        except ValueError:
            await interaction.response.send_message(
                "日課を完了できませんでした。日課IDを確認してください。",
                ephemeral=True,
            )
            return
        button = self.children[0]
        if isinstance(button, discord.ui.Button):
            button.disabled = True
        message = getattr(interaction, "message", None)
        edit = getattr(message, "edit", None)
        if callable(edit):
            try:
                await edit(view=self)
            except Exception as exc:
                logger.info("routine completion button update skipped (%s)", type(exc).__name__)
        await interaction.response.send_message(
            f"日課 #{routine.id} を今日の完了として記録しました: {routine.title}",
            ephemeral=True,
        )


class SocialDraftView(discord.ui.View):
    """Persistent local-outbox target choices for one social draft Event."""

    def __init__(
        self,
        bot: LifeInboxBot,
        event_id: int,
        *,
        action_channel_id: Any = None,
    ) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.event_id = event_id
        self.action_channel_id = action_channel_id
        self.message: Any | None = None
        for target, label, style in (
            ("x", "X", discord.ButtonStyle.primary),
            ("instagram", "Instagram", discord.ButtonStyle.primary),
            ("both", "両方", discord.ButtonStyle.success),
            ("hold", "保留", discord.ButtonStyle.secondary),
        ):
            button = discord.ui.Button(
                label=label,
                style=style,
                custom_id=f"life-inbox:social-draft:{event_id}:{target}",
            )
            button.callback = self._choice_callback(target)
            self.add_item(button)

    def _choice_callback(self, target: str):
        async def callback(interaction: discord.Interaction) -> None:
            await self._choose(interaction, target)

        return callback

    def _allowed(self, interaction: discord.Interaction) -> bool:
        return self.bot._allows_social_draft_selection(
            interaction,
            self.event_id,
            action_channel_id=self.action_channel_id,
        )

    async def _choose(self, interaction: discord.Interaction, target: str) -> None:
        if not self._allowed(interaction):
            await _respond_text(interaction, "この操作は許可されていません。", ephemeral=True)
            return
        try:
            result = await asyncio.to_thread(
                self.bot.social_drafts.select_target,
                self.event_id,
                target,
                selected_by_user_id=interaction.user.id,
            )
        except ValueError:
            await _respond_text(
                interaction,
                "SNS下書きの選択を保存できませんでした。Event IDを確認してください。",
                ephemeral=True,
            )
            return
        except Exception as exc:
            logger.warning(
                "social draft selection failed for event_id=%s (%s)",
                self.event_id,
                type(exc).__name__,
            )
            await _respond_text(
                interaction,
                "SNS下書きの選択をローカル保存できませんでした。外部SNSへの投稿は行っていません。",
                ephemeral=True,
            )
            return
        await _respond_text(
            interaction,
            self.bot._format_social_selection_result(result),
            ephemeral=True,
        )


class InboxCandidateView(discord.ui.View):
    """One-shot candidate buttons for a raw free-form Inbox message."""

    def __init__(
        self,
        bot: LifeInboxBot,
        source_message: discord.Message,
        event_id: int,
        classification: InboxClassification,
    ) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.source_message = source_message
        self.event_id = event_id
        self.classification = classification
        self._state_lock = asyncio.Lock()
        self._processing = False
        self._completed = False
        self._cancelled = False
        self._candidate_categories = {
            candidate.category
            for candidate in classification.candidates
            if candidate.category is not InboxCategory.UNKNOWN
        }

        for candidate in classification.candidates:
            if candidate.category is InboxCategory.UNKNOWN:
                continue
            button = discord.ui.Button(
                label=CATEGORY_LABELS[candidate.category],
                style=discord.ButtonStyle.primary,
                custom_id=(
                    f"life-inbox:classify:{event_id}:{candidate.category.value}"
                ),
            )
            button.callback = self._choice_callback(candidate.category)
            self.add_item(button)

        keep_button = discord.ui.Button(
            label="保留",
            style=discord.ButtonStyle.secondary,
            custom_id=f"life-inbox:classify-keep:{event_id}",
        )
        keep_button.callback = self._keep_callback
        self.add_item(keep_button)

    def _choice_callback(self, category: InboxCategory):
        async def callback(interaction: discord.Interaction) -> None:
            await self._choose(interaction, category)

        return callback

    def _allowed(self, interaction: discord.Interaction) -> bool:
        bot_user = self.bot.user
        return (
            self.bot.guard.allows_inbox_interaction(interaction)
            and (
                bot_user is None
                or str(getattr(getattr(interaction, "user", None), "id", ""))
                != str(getattr(bot_user, "id", ""))
            )
        )

    def _category_for_reaction(self, emoji: Any) -> InboxCategory | None:
        category = inbox_category_for_reaction(emoji)
        if category is None or category not in self._candidate_categories:
            return None
        return category

    async def _respond(
        self,
        interaction: discord.Interaction,
        content: str,
        *,
        use_followup: bool = False,
    ) -> None:
        """Send an ephemeral result without retrying an expired interaction.

        A candidate route can take longer than Discord's initial response
        window. Callers that successfully deferred must therefore use the
        follow-up webhook even if a test double (or an adapter edge case) does
        not immediately report ``response.is_done()``. If the interaction
        token has already expired, neither an initial response nor a follow-up
        can repair it; swallowing that expected transport error keeps the
        callback from producing a noisy ``Unknown interaction`` traceback.
        """

        response = getattr(interaction, "response", None)
        followup = getattr(interaction, "followup", None)
        send = getattr(followup, "send", None)
        try:
            if use_followup:
                if callable(send):
                    await send(content, ephemeral=True)
                return
            if response is not None and not response.is_done():
                await response.send_message(content, ephemeral=True)
                return
            if callable(send):
                await send(content, ephemeral=True)
        except Exception as exc:
            logger.info("inbox interaction response skipped (%s)", type(exc).__name__)

    @staticmethod
    async def _defer_if_possible(
        interaction: discord.Interaction,
    ) -> tuple[bool, bool]:
        """Acknowledge a structured write before Discord's interaction deadline.

        Returns ``(can_continue, use_followup)``. A response object without a
        defer API is treated as a small local test adapter and may continue
        with an initial response. A real defer attempt that fails is
        different: the interaction may be expired, so the route is aborted
        before any destination write or source deletion.
        """

        response = getattr(interaction, "response", None)
        is_done = getattr(response, "is_done", None)
        defer = getattr(response, "defer", None)
        if response is None or not callable(is_done) or not callable(defer):
            return True, False
        try:
            if is_done():
                return True, True
            await defer(ephemeral=True, thinking=True)
        except Exception as exc:
            logger.info("inbox interaction acknowledgement skipped (%s)", type(exc).__name__)
            return False, False
        return True, True

    async def _set_disabled(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        message = getattr(self, "message", None)
        if message is None:
            message = getattr(self.source_message, "_inbox_candidate_message", None)
        edit = getattr(message, "edit", None)
        if callable(edit):
            try:
                await edit(view=self)
            except Exception as exc:
                logger.info("inbox candidate button update skipped (%s)", type(exc).__name__)

    async def _process_choice(self, category: InboxCategory) -> str:
        if category not in self._candidate_categories:
            return "この候補は利用できません。"
        async with self._state_lock:
            if self._completed or self._cancelled:
                return "この振り分けはすでに終了しています。"
            if self._processing:
                return "振り分け処理中です。少し待ってください。"
            self._processing = True
            try:
                destination_name, source_deleted = await self.bot._route_classified_inbox(
                    self.source_message,
                    self.event_id,
                    category,
                )
            except _InboxCandidateAlreadyRouted:
                self._completed = True
                self._processing = False
                await self._set_disabled()
                self.bot._forget_inbox_candidate_view(self.source_message, self)
                return "この振り分けはすでに終了しています。"
            except Exception as exc:
                self._processing = False
                logger.warning(
                    "inbox candidate route failed for event_id=%s (%s)",
                    self.event_id,
                    type(exc).__name__,
                )
                return (
                    "保存先への送信に失敗しました。元投稿は残しています。"
                    "後でもう一度お試しください。"
                )
            self._completed = True
            self._processing = False
            await self._set_disabled()
            self.bot._forget_inbox_candidate_view(self.source_message, self)
            if not self.bot.settings.inbox_delete_original_after_route:
                source_note = "元投稿は設定により保持しています。"
            elif source_deleted:
                source_note = "元投稿を削除しました。"
            else:
                source_note = "送信は成功しましたが、元投稿は削除できず残っています。"
            return (
                f"{CATEGORY_LABELS[category]}へ振り分けました（#{destination_name}）。"
                f"{source_note}"
            )

    async def _choose(
        self,
        interaction: discord.Interaction,
        category: InboxCategory,
    ) -> None:
        if not self._allowed(interaction):
            await self._respond(interaction, "この操作は許可されていません。")
            return
        can_continue, use_followup = await self._defer_if_possible(interaction)
        if not can_continue:
            # An expired interaction cannot be acknowledged or reported to.
            # Crucially, do not route after this point: the source remains
            # available for a fresh interaction/reaction retry.
            return
        await self._respond(
            interaction,
            await self._process_choice(category),
            use_followup=use_followup,
        )

    async def _choose_reaction(self, category: InboxCategory) -> str | None:
        """Run the same one-shot route used by candidate buttons."""

        if category not in self._candidate_categories:
            return None
        return await self._process_choice(category)

    async def _keep_callback(self, interaction: discord.Interaction) -> None:
        if not self._allowed(interaction):
            await self._respond(interaction, "この操作は許可されていません。")
            return
        can_continue, use_followup = await self._defer_if_possible(interaction)
        if not can_continue:
            return
        async with self._state_lock:
            if self._completed or self._cancelled:
                await self._respond(
                    interaction,
                    "この振り分けはすでに終了しています。",
                    use_followup=use_followup,
                )
                return
            if self._processing:
                await self._respond(
                    interaction,
                    "振り分け処理中です。少し待ってください。",
                    use_followup=use_followup,
                )
                return
            self._cancelled = True
            await self._set_disabled()
            self.bot._forget_inbox_candidate_view(self.source_message, self)
            await self._respond(
                interaction,
                "元投稿を保留しました。元投稿は削除しません。",
                use_followup=use_followup,
            )


async def _respond_text(
    interaction: discord.Interaction,
    message: str,
    *,
    ephemeral: bool = False,
    allowed_mentions: discord.AllowedMentions | None = None,
) -> None:
    chunks = _message_chunks(message)
    kwargs: dict[str, Any] = {"ephemeral": ephemeral}
    if allowed_mentions is not None:
        kwargs["allowed_mentions"] = allowed_mentions
    if interaction.response.is_done():
        await interaction.followup.send(chunks[0], **kwargs)
    else:
        await interaction.response.send_message(chunks[0], **kwargs)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, **kwargs)


def _format_datetime(value: datetime | None, timezone_name: str) -> str:
    if value is None:
        return "不明"
    return local_datetime(value, timezone_name).strftime("%Y-%m-%d %H:%M:%S")


def _confidence_text(confidence: float | None) -> str:
    if confidence is None:
        return "不明"
    return f"{_confidence_label(confidence)}（{confidence:.2f}）"


def _receipt_item_lines(expense: Any, *, prefix: str = "") -> list[str]:
    items = sorted(
        getattr(expense, "receipt_items", ()) or (),
        key=lambda item: (getattr(item, "order_index", 0), getattr(item, "id", 0) or 0),
    )
    if not items:
        return []
    lines = [f"{prefix}レシート明細:"]
    for index, item in enumerate(items, start=1):
        fields = [f"商品: {item.name or '名称不明'}"]
        if item.quantity is not None:
            fields.append(f"数量: {item.quantity}")
        if item.unit_price is not None:
            fields.append(f"単価: {item.unit_price:,}円")
        if item.amount is not None:
            fields.append(f"行合計（小計）: {item.amount:,}円")
        lines.append(f"{prefix}・{index}. " + " / ".join(fields))
    return lines


def _display_event(event: Event) -> str:
    lines = [f"Inbox #{event.id}（{event.status}）"]
    for expense in event.expenses:
        merchant = expense.merchant_normalized or expense.merchant_raw or "不明"
        amount = f"{expense.amount:,}円" if expense.amount is not None else "金額不明"
        category = expense.category or "未分類"
        if expense.subcategory:
            category += f" > {expense.subcategory}"
        lines.append(f"💰 {amount} / {merchant} / {category}")
        if expense.description:
            lines.append(f"内容: {expense.description}")
        if expense.confidence is not None:
            lines.append(f"信頼度: {_confidence_label(expense.confidence)}")
        lines.extend(_receipt_item_lines(expense))
    for meal in event.meals:
        restaurant = meal.restaurant_normalized or meal.restaurant_raw or "店名不明"
        lines.append(f"🍽️ {meal.meal_type} / {restaurant}")
        if meal.description:
            lines.append(f"食事: {meal.description}")
        if meal.confidence is not None:
            lines.append(f"信頼度: {_confidence_label(meal.confidence)}")
    if not event.expenses and not event.meals:
        lines.append("解析待ち（原文は保存済み）")
    return "\n".join(lines)


def _confidence_label(confidence: float) -> str:
    if confidence >= 0.9:
        return "高"
    if confidence >= 0.7:
        return "中"
    return "低"


def _display_event_detail(event: Event, *, timezone_name: str) -> str:
    """Render all persisted user-facing details for one Event."""

    lines = [
        f"Inbox #{event.id}",
        f"ステータス: {event.status}",
        f"発生日時（{timezone_name}）: {_format_datetime(event.occurred_at, timezone_name)}",
        "原文:",
        "```text",
        event.raw_text or "（なし）",
        "```",
    ]
    if event.copied_to_planner_at is None:
        lines.append("手帳転記: 未転記")
    else:
        copied_at = _format_datetime(event.copied_to_planner_at, timezone_name)
        lines.append(f"手帳転記: 転記済み（{copied_at}）")

    if event.expenses:
        lines.append("支出:")
        for expense in event.expenses:
            merchant = expense.merchant_normalized or expense.merchant_raw or "不明"
            lines.extend(
                [
                    f"金額: {expense.amount:,}円" if expense.amount is not None else "金額: 不明",
                    f"店舗: {merchant}",
                    f"カテゴリ: {expense.category or '未分類'}",
                    f"サブカテゴリ: {expense.subcategory or 'なし'}",
                    f"内容: {expense.description or 'なし'}",
                    f"信頼度: {_confidence_text(expense.confidence)}",
                ]
            )
            if expense.merchant_raw and expense.merchant_raw != merchant:
                lines.append(f"店舗原文: {expense.merchant_raw}")
            lines.extend(_receipt_item_lines(expense, prefix="  "))
    else:
        lines.append("支出: なし")

    if event.meals:
        lines.append("食事:")
        for meal in event.meals:
            restaurant = meal.restaurant_normalized or meal.restaurant_raw or "不明"
            lines.extend(
                [
                    f"区分: {meal.meal_type or '不明'}",
                    f"店舗: {restaurant}",
                    f"内容: {meal.description or 'なし'}",
                    f"自宅外: {meal.outside_home or '不明'}",
                    f"信頼度: {_confidence_text(meal.confidence)}",
                ]
            )
            if meal.restaurant_raw and meal.restaurant_raw != restaurant:
                lines.append(f"店舗原文: {meal.restaurant_raw}")

    if event.ledger_activity is not None:
        activity = event.ledger_activity
        amount = activity.effective_amount
        route = ""
        if activity.source_account is not None:
            route = activity.source_account.name
        if activity.destination_account is not None:
            route += (" → " if route else "") + activity.destination_account.name
        lines.extend(
            [
                "口座記録:",
                f"種類: {activity.kind}",
                f"金額: {amount:,}円" if amount is not None else "金額: 不明",
                f"口座: {route or '未指定'}",
                f"内容: {activity.description or 'なし'}",
            ]
        )

    if event.memo is not None:
        lines.extend(["一言メモ:", event.memo.body])

    if event.rhythm_todo is not None:
        todo = event.rhythm_todo
        lines.extend(
            [
                "音ゲーTODO:",
                f"ゲーム: {RHYTHM_GAME_LABELS.get(RhythmGame(todo.game), todo.game)}",
                f"内容: {todo.title}",
                f"状態: {todo.status}",
            ]
        )

    if event.rhythm_result is not None:
        result = event.rhythm_result
        lines.extend(
            [
                "音ゲーリザルト:",
                f"ゲーム: {RHYTHM_GAME_LABELS.get(RhythmGame(result.game), result.game)}",
                f"対応TODO: #{result.todo_id}" if result.todo_id else "対応TODO: なし",
                f"メモ: {result.note or 'なし'}",
            ]
        )

    return "\n".join(lines)


def _history_summary(event: Event, *, timezone_name: str) -> str:
    summaries: list[str] = []
    for expense in event.expenses:
        merchant = expense.merchant_normalized or expense.merchant_raw or "不明"
        amount = f"{expense.amount:,}円" if expense.amount is not None else "金額不明"
        category = expense.category or "未分類"
        if expense.subcategory:
            category += f" > {expense.subcategory}"
        summaries.append(f"{amount} / {merchant} / {category}")
    if not summaries:
        for meal in event.meals:
            restaurant = meal.restaurant_normalized or meal.restaurant_raw or "店名不明"
            summaries.append(f"食事 / {meal.meal_type or '不明'} / {restaurant}")
    if not summaries:
        if event.ledger_activity is not None:
            activity = event.ledger_activity
            amount = activity.effective_amount
            summaries.append(
                f"{activity.kind} / {amount:,}円"
                if amount is not None
                else f"{activity.kind} / 金額不明"
            )
        elif event.memo is not None:
            summaries.append(f"メモ / {event.memo.body}")
        elif event.rhythm_todo is not None:
            summaries.append(
                f"音ゲーTODO / {event.rhythm_todo.game} / {event.rhythm_todo.title}"
            )
        elif event.rhythm_result is not None:
            summaries.append(
                f"音ゲーリザルト / {event.rhythm_result.game} / "
                f"{event.rhythm_result.note or '画像記録'}"
            )
    if not summaries:
        summaries.append("内容不明")
    return (
        f"{_format_datetime(event.occurred_at, timezone_name)} #{event.id} "
        + " / ".join(summaries)
    )


def _display_history(
    events: list[Event], *, count: int, timezone_name: str
) -> str:
    selected = sorted(
        events,
        key=lambda event: (event.occurred_at or datetime.min, event.id or 0),
        reverse=True,
    )[:count]
    if not selected:
        return "確定記録はありません。"
    lines = [f"最近の確定記録（{len(selected)}件）"]
    for event in selected:
        lines.append(_history_summary(event, timezone_name=timezone_name))
        lines.append(f"詳細: /detail event_id:{event.id}")
    return "\n".join(lines)


def _display_accounts(accounts: list[AccountBalance]) -> str:
    if not accounts:
        return "口座はまだありません。"
    lines = ["口座・カード残高"]
    for account in accounts:
        label = ACCOUNT_KIND_LABELS.get(account.kind, account.kind.value)
        suffix = "（利用残高・負債）" if account.kind == AccountKind.CARD else ""
        lines.append(f"・{account.name} [{label}] {account.balance:,}円{suffix}")
    lines.append("詳しい入出金履歴: /mypage または /accounts")
    return "\n".join(lines)


def _display_settlement_links(
    accounts: list[AccountBalance], links: list[SettlementAccountLink]
) -> str:
    """Render every registered card, including cards without a settlement link."""

    cards = [account for account in accounts if account.kind == AccountKind.CARD]
    if not cards:
        return (
            "クレジットカード口座はまだありません。\n"
            "先に #口座 で `/account_add` を使ってカードを登録してください。"
        )
    links_by_card_id = {link.card_account_id: link for link in links}
    lines = [f"引落口座一覧（カード {len(cards)}件）"]
    for card in cards:
        link = links_by_card_id.get(card.id)
        settlement_name = link.settlement_account_name if link is not None else "未設定"
        lines.append(f"・{card.name} → {settlement_name}")
    lines.append("設定・変更: `/引落口座設定` / 解除: `/引落口座解除`")
    return "\n".join(lines)


def _display_budget_progress(label: str, progress: BudgetProgress) -> str:
    if progress.remaining >= 0:
        status = f"残り {progress.remaining:,}円"
    else:
        status = f"超過 {-progress.remaining:,}円"
    return (
        f"・{label}: 上限 {progress.amount:,}円 / 使用 {progress.spent:,}円 / "
        f"{status} / 使用率 {progress.percent:.1f}%"
    )


def _display_budget(snapshot: BudgetMonthView) -> str:
    """Render one detached budget snapshot for a private Discord response."""

    lines = [f"{snapshot.month}の予算"]
    if snapshot.overall is None and not snapshot.categories:
        return "\n".join(
            [
                *lines,
                "予算はまだ設定されていません。",
                "#口座で /budget_set amount:金額 を使って設定してください。",
            ]
        )
    if snapshot.overall is not None:
        lines.append(_display_budget_progress("全体", snapshot.overall))
    if snapshot.categories:
        lines.append("カテゴリ別:")
        lines.extend(
            _display_budget_progress(progress.category or "未分類", progress)
            for progress in snapshot.categories
        )
    return "\n".join(lines)


def _display_memos(memos: list[Any], *, timezone_name: str, count: int = 20) -> str:
    if not memos:
        return "一言メモはまだありません。"
    lines = [f"最近の一言メモ（最大{count}件）"]
    for memo in memos[:count]:
        timestamp = _format_datetime(memo.created_at, timezone_name)
        lines.append(f"・{timestamp} #{memo.event_id} {memo.body}")
    return "\n".join(lines)


def _filtered_expenses(
    events: list[Event],
    *,
    minimum: int = 0,
    category: str | None = None,
    dining_only: bool = False,
) -> list[tuple[Event, Any]]:
    rows: list[tuple[Event, Any]] = []
    for event in events:
        for expense in event.expenses:
            if expense.amount is None or expense.amount < minimum:
                continue
            if category and expense.category != category:
                continue
            if dining_only and not (
                expense.category == "食費" and expense.subcategory == "外食"
            ):
                continue
            rows.append((event, expense))
    return sorted(
        rows,
        key=lambda row: (row[0].occurred_at or datetime.min, row[0].id or 0),
        reverse=True,
    )


def _display_spending(
    events: list[Event],
    *,
    timezone_name: str,
    minimum: int = 0,
    category: str | None = None,
    dining_only: bool = False,
    count: int = 20,
) -> str:
    rows = _filtered_expenses(
        events,
        minimum=minimum,
        category=category,
        dining_only=dining_only,
    )
    if not rows:
        return "条件に合う確定支出はありません。"
    total = sum(expense.amount or 0 for _, expense in rows)
    title = "外食" if dining_only else "支出"
    lines = [f"{title}の記録: {len(rows)}件 / 合計 {total:,}円"]
    for event, expense in rows[:count]:
        merchant = expense.merchant_normalized or expense.merchant_raw or "内容不明"
        timestamp = _format_datetime(event.occurred_at, timezone_name)
        lines.append(
            f"・{timestamp} #{event.id} {expense.amount:,}円 / {merchant} / "
            f"{expense.category}{' > ' + expense.subcategory if expense.subcategory else ''}"
        )
    if len(rows) > count:
        lines.append(f"ほか {len(rows) - count}件は /mypage で確認できます。")
    return "\n".join(lines)


def _display_meals(events: list[Event], *, timezone_name: str, count: int = 20) -> str:
    rows = [
        (event, meal)
        for event in events
        for meal in event.meals
    ]
    rows.sort(
        key=lambda row: (row[0].occurred_at or datetime.min, row[0].id or 0),
        reverse=True,
    )
    if not rows:
        return "確定済みの食事記録はまだありません。"
    lines = [f"最近の食事（最大{count}件）"]
    for event, meal in rows[:count]:
        restaurant = meal.restaurant_normalized or meal.restaurant_raw or "場所不明"
        description = meal.description or event.raw_text or "内容不明"
        timestamp = _format_datetime(event.occurred_at, timezone_name)
        lines.append(
            f"・{timestamp} #{event.id} {meal.meal_type} / {restaurant} / {description}"
        )
    return "\n".join(lines)


def _display_rhythm(
    todos: list[RhythmTodoResult],
    results: list[RhythmResultRecord],
    *,
    timezone_name: str,
    count: int = 20,
) -> str:
    lines = [f"音ゲー成長ログ / 未完了TODO {len(todos)}件"]
    if todos:
        lines.append("【TODO】")
        for todo in todos[:count]:
            game = RHYTHM_GAME_LABELS.get(todo.game, todo.game.value)
            timestamp = _format_datetime(todo.created_at, timezone_name)
            lines.append(f"・#{todo.id} {game} / {todo.title} / {timestamp}")
    else:
        lines.append("未完了TODOはありません。")
    if results:
        lines.append("【最近のリザルト】")
        for result in results[:count]:
            game = RHYTHM_GAME_LABELS.get(result.game, result.game.value)
            linked = f"TODO #{result.todo_id}" if result.todo_id else "単独記録"
            timestamp = _format_datetime(result.created_at, timezone_name)
            lines.append(
                f"・Result #{result.id} {game} / {result.note or '画像記録'} / "
                f"{linked} / {timestamp}"
            )
    else:
        lines.append("リザルトはまだありません。")
    return "\n".join(lines)


def _display_favorites(favorites: list[RhythmFavoriteRecord]) -> str:
    """Render favorites without exposing ORM objects or provider internals."""

    if not favorites:
        return (
            "maimaiのお気に入りはまだありません。ローカルリストが空です。\n"
            "公式 maimai NET との同期は接続されていません。"
        )
    lines = [f"maimaiお気に入り（既存ローカル一覧・{len(favorites)}件）"]
    for favorite in favorites:
        detail = f" / メモ: {favorite.note}" if favorite.note else ""
        source_label = {
            "manual": "手動",
            "maimai_net": "maimai NET取込",
        }.get(favorite.source, "外部取込")
        lines.append(
            f"・{favorite.song_title}{detail} / 出典: {source_label}"
        )
    lines.append("公式NETお気に入りスナップショット: /maimai_net_favorites")
    lines.append("MY BEST（プレイ回数順）: /maimai_mybest")
    return "\n".join(lines)


def _display_maimai_net_favorites(
    favorites: list[MaimaiNetFavoriteRecord],
    *,
    timezone_name: str,
) -> str:
    """Render only the separate official NET favorites snapshot."""

    if not favorites:
        return (
            "公式 maimai NET のお気に入りスナップショットはまだありません。\n"
            "サニタイズ済みJSONをローカルから取り込んでください。"
        )
    fetched_at = _format_datetime(favorites[0].fetched_at, timezone_name)
    lines = [
        f"公式 maimai NET お気に入り（{len(favorites)}件）",
        f"取得日時: {fetched_at}（{timezone_name}）",
        "手動のお気に入り（/maimai_favorites）やMY BEST（/maimai_mybest）とは別の一覧です。",
    ]
    for favorite in favorites:
        lines.append(
            f"・{favorite.rank}位 {favorite.title} / {favorite.artist} / "
            f"カテゴリ: {favorite.category}"
        )
    return "\n".join(lines)


def _display_maimai_mybest(
    entries: list[MaimaiMyBestRecord],
    *,
    timezone_name: str,
) -> str:
    """Render the official play-count-ranked MY BEST snapshot."""

    if not entries:
        return (
            "maimai MY BESTはまだありません。\n"
            "サニタイズ済みJSONをローカルから取り込んでください。"
        )
    fetched_at = _format_datetime(entries[0].fetched_at, timezone_name)
    lines = [
        f"maimai MY BEST（プレイ回数順・{len(entries)}曲）",
        f"取得日時: {fetched_at}（{timezone_name}）",
        "これはお気に入りではなく、公式 maimai でらっくすNET のプレイ回数一覧です。",
        "順位 / 曲名 / プレイ回数",
    ]
    for entry in entries:
        lines.append(f"・{entry.rank}位 / {entry.title} / {entry.play_count}回")
    return "\n".join(lines)


def _safe_snowflake(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    normalized = str(value).strip()
    if not normalized.isdigit() or int(normalized) <= 0:
        return None
    return normalized


def _discord_jump_url(result: RhythmResultRecord, guild_id: int | None) -> str | None:
    """Build a jump URL only from positive numeric persisted identifiers."""

    guild = _safe_snowflake(guild_id)
    channel = _safe_snowflake(result.discord_channel_id)
    message = _safe_snowflake(result.discord_message_id)
    if guild is None or channel is None or message is None:
        return None
    return f"https://discord.com/channels/{guild}/{channel}/{message}"


def _display_progress_today(
    results: list[RhythmResultRecord],
    *,
    target_date: date,
    timezone_name: str,
    guild_id: int | None,
) -> str:
    if not results:
        return (
            f"{target_date.isoformat()}（{timezone_name}）の今日の進捗はまだありません。"
        )
    lines = [
        f"今日の進捗（{target_date.isoformat()} / {timezone_name}）",
        f"{len(results)}件のリザルト",
    ]
    for result in results:
        game = RHYTHM_GAME_LABELS.get(result.game, result.game.value)
        note = result.note or "画像記録"
        image_text = (
            f"画像{result.attachment_count}件"
            if result.attachment_count
            else "画像なし"
        )
        linked = f"TODO #{result.todo_id}（{result.todo_title}）" if result.todo_id else "単独記録"
        jump_url = _discord_jump_url(result, guild_id)
        jump = f" / [Discordで開く]({jump_url})" if jump_url else ""
        timestamp = _format_datetime(
            result.occurred_at or result.created_at,
            timezone_name,
        )
        lines.append(
            f"・Result #{result.id} / Event #{result.event_id} / {game} / {note} / "
            f"{image_text} / {linked} / {timestamp}{jump}"
        )
    return "\n".join(lines)


def _display_tasks(
    tasks: list[TaskView], *, timezone_name: str = "Asia/Tokyo"
) -> str:
    """Render the local open-task list without exposing provider internals."""

    if not tasks:
        return "未完了のタスクはありません。"
    lines = [f"未完了タスク（{len(tasks)}件）"]
    for task in tasks:
        if task.due_at is None:
            due = task.due_label
        else:
            local_due = local_datetime(task.due_at, timezone_name)
            if local_due.hour == 0 and local_due.minute == 0 and local_due.second == 0:
                due = task.due_label
            else:
                due = local_due.strftime("%Y-%m-%d %H:%M")
        overdue = " ⚠️期限超過" if task.is_overdue else ""
        source = TASK_SOURCE_LABELS.get(task.source, task.source)
        lines.append(
            f"・#{task.id} {task.title} / 期限: {due}{overdue} / "
            f"状態: {'未完了' if task.status == 'open' else task.status} / 出典: {source}"
        )
        if task.description:
            lines.append(f"  内容: {task.description}")
    return "\n".join(lines)


CAREER_KIND_LABELS = {
    CareerKind.COMPANY.value: "企業",
    CareerKind.JOB.value: "求人",
    CareerKind.EVENT.value: "イベント",
    CareerKind.APPLICATION.value: "応募",
}

CAREER_STATUS_LABELS = {
    CareerStatus.OPEN.value: "未処理",
    CareerStatus.INTERESTED.value: "興味あり",
    CareerStatus.APPLIED.value: "応募済み",
    CareerStatus.IN_PROGRESS.value: "進行中",
    CareerStatus.ACCEPTED.value: "承諾",
    CareerStatus.CLOSED.value: "完了",
    CareerStatus.REJECTED.value: "不採用",
    CareerStatus.WITHDRAWN.value: "辞退",
    CareerStatus.ARCHIVED.value: "アーカイブ",
}

CAREER_STAGE_LABELS = {
    ApplicationStage.SAVED.value: "保存",
    ApplicationStage.INTERESTED.value: "興味あり",
    ApplicationStage.APPLIED.value: "応募済み",
    ApplicationStage.SCREENING.value: "書類選考",
    ApplicationStage.INTERVIEW.value: "面接",
    ApplicationStage.FINAL_INTERVIEW.value: "最終面接",
    ApplicationStage.OFFER.value: "オファー",
    ApplicationStage.ACCEPTED.value: "承諾",
    ApplicationStage.REJECTED.value: "不採用",
    ApplicationStage.WITHDRAWN.value: "辞退",
    ApplicationStage.CLOSED.value: "クローズ",
}


def _career_deadline_label(record: CareerRecord, *, timezone_name: str) -> str:
    value = record.next_action_at or record.deadline_at
    if value is None:
        return "期限なし"
    local_value = local_datetime(value, timezone_name)
    if local_value.hour == 0 and local_value.minute == 0 and local_value.second == 0:
        return local_value.date().isoformat()
    return local_value.strftime("%Y-%m-%d %H:%M")


def _display_career(records: list[CareerRecord], *, timezone_name: str) -> str:
    """Render a bounded, personal-data-safe career list."""

    if not records:
        return "就活記録はまだありません。"
    lines = [f"就活一覧（{len(records)}件）"]
    for record in records:
        kind = CAREER_KIND_LABELS.get(record.kind, record.kind)
        status = CAREER_STATUS_LABELS.get(record.status, record.status)
        stage = (
            f" / 段階: {CAREER_STAGE_LABELS.get(record.stage, record.stage)}"
            if record.stage
            else ""
        )
        reminder = f" / LifeTask #{record.linked_task_id}" if record.linked_task_id else ""
        lines.append(
            f"・#{record.id} [{record.record_type}] {record.company} / {record.title}"
            f" / 種別: {kind} / 状態: {status}{stage}"
            f" / 期限: {_career_deadline_label(record, timezone_name=timezone_name)}{reminder}"
        )
        if record.next_action:
            lines.append(f"  次の行動: {record.next_action}")
        if record.location:
            lines.append(f"  場所: {record.location}")
        if record.url:
            lines.append(f"  URL: {record.url}")
        if record.notes:
            lines.append(f"  メモ: {record.notes}")
    return "\n".join(lines)


def _display_routines(routines: list[RoutineView]) -> str:
    """Render routine definitions and today's durable completion state."""

    if not routines:
        return "日課はまだありません。/日課追加 title:内容 time:HH:MM で追加できます。"
    lines = [f"日課一覧（{len(routines)}件）"]
    for routine in routines:
        enabled = "有効" if routine.enabled else "無効"
        completion = "今日: 完了済み" if routine.completed_today else "今日: 未完了"
        lines.append(
            f"・#{routine.id} {routine.title} / 時刻: {routine.reminder_time} / "
            f"状態: {enabled} / {completion}"
        )
    return "\n".join(lines)


def _rating_number(value: Any) -> str:
    if value is None:
        return "未取得"
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else f"{numeric:g}"


def _display_latest_rating(snapshot: Any, *, timezone_name: str) -> str:
    if snapshot is None:
        return "レートはまだ取得されていません。"
    game_label = RHYTHM_GAME_LABELS.get(RhythmGame(snapshot.game), snapshot.game)
    previous = _rating_number(snapshot.previous_rating)
    delta = _rating_number(snapshot.rating_delta)
    return "\n".join(
        (
            f"【{game_label} 最新レート】",
            f"現在: {_rating_number(snapshot.rating)}",
            f"前回: {previous}",
            f"差分: {delta}",
            f"取得: {_format_datetime(snapshot.collected_at, timezone_name)}",
            f"Snapshot #{snapshot.id}",
        )
    )


class EditEventModal(discord.ui.Modal):
    """Five-field combined edit form; store/description apply to both kinds."""

    def __init__(self, service: InboxService, guard: ScopeGuard, event: Event) -> None:
        super().__init__(title="Inboxを修正", custom_id=f"life_inbox:edit:{event.id}")
        expense = event.expenses[0] if event.expenses else None
        meal = event.meals[0] if event.meals else None
        self.service = service
        self.guard = guard
        self.event_id = event.id
        self.has_expense = expense is not None
        self.has_meal = meal is not None
        self.amount = discord.ui.TextInput(
            label="金額（円）",
            style=discord.TextStyle.short,
            required=False,
            default=str(expense.amount) if expense and expense.amount is not None else "",
            max_length=12,
        )
        self.store = discord.ui.TextInput(
            label="店舗（支出/食事共通）",
            style=discord.TextStyle.short,
            required=False,
            default=(expense.merchant_normalized if expense else None)
            or (meal.restaurant_normalized if meal else None)
            or "",
            max_length=100,
        )
        self.category = discord.ui.TextInput(
            label="カテゴリ",
            style=discord.TextStyle.short,
            required=False,
            default=expense.category if expense else "",
            max_length=30,
        )
        self.description = discord.ui.TextInput(
            label="内容（支出/食事共通）",
            style=discord.TextStyle.paragraph,
            required=False,
            default=(expense.description if expense else None)
            or (meal.description if meal else None)
            or "",
            max_length=500,
        )
        self.meal_type = discord.ui.TextInput(
            label="食事区分（朝/昼/夜/おやつ）",
            style=discord.TextStyle.short,
            required=False,
            default=meal.meal_type if meal else "",
            max_length=20,
        )
        for item in (self.amount, self.store, self.category, self.description, self.meal_type):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.guard.allows_event_interaction(interaction):
            await interaction.response.send_message(
                "この操作は許可されていません。", ephemeral=True
            )
            return
        try:
            changes: dict[str, Any] = {}
            wants_expense = self.has_expense or bool(
                self.amount.value.strip() or self.category.value.strip()
            )
            wants_meal = self.has_meal or bool(self.meal_type.value.strip())
            if wants_expense:
                changes.update(
                    amount=self.amount.value,
                    merchant=self.store.value,
                    category=self.category.value,
                    description=self.description.value,
                )
            if wants_meal:
                changes.update(
                    meal_type=self.meal_type.value,
                    restaurant=self.store.value,
                    meal_description=self.description.value,
                )
            event = await asyncio.to_thread(self.service.edit_event, self.event_id, **changes)
            await _respond_text(interaction, _display_event(event), ephemeral=True)
        except Exception as exc:
            logger.warning("edit interaction failed (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "修正できませんでした。金額や項目を確認してください。", ephemeral=True
            )


class EventActionView(discord.ui.View):
    """Restart-safe buttons with event-specific, stable custom IDs."""

    def __init__(self, service: InboxService, guard: ScopeGuard, event_id: int) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.guard = guard
        self.event_id = event_id
        for action, label, style in (
            ("confirm", "✅ 確定", discord.ButtonStyle.success),
            ("edit", "✏️ 修正", discord.ButtonStyle.primary),
            ("ignore", "🗑️ 無視", discord.ButtonStyle.danger),
        ):
            button = discord.ui.Button(
                label=label, style=style, custom_id=f"life_inbox:{action}:{event_id}"
            )

            async def callback(
                interaction: discord.Interaction, selected_action: str = action
            ) -> None:
                await self._handle(interaction, selected_action)

            button.callback = callback
            self.add_item(button)

    async def _handle(self, interaction: discord.Interaction, action: str) -> None:
        if not self.guard.allows_event_interaction(interaction):
            await interaction.response.send_message(
                "この操作は許可されていません。", ephemeral=True
            )
            return
        try:
            if action == "confirm":
                event = await asyncio.to_thread(self.service.confirm_event, self.event_id)
                message = "確定しました。\n" + _display_event(event)
            elif action == "ignore":
                event = await asyncio.to_thread(self.service.ignore_event, self.event_id)
                message = "無視にしました。原文はDBに残っています。"
            else:
                event = await asyncio.to_thread(self.service.get_event, self.event_id)
                if event is None:
                    raise ValueError("event not found")
                await interaction.response.send_modal(
                    EditEventModal(self.service, self.guard, event)
                )
                return
            await _respond_text(interaction, message, ephemeral=True)
        except Exception as exc:
            logger.warning("event action failed (%s)", type(exc).__name__)
            await interaction.response.send_message("操作に失敗しました。", ephemeral=True)


class LifeInboxCommands(commands.Cog):
    """Guild-scoped slash commands registered on the bot command tree."""

    def __init__(self, bot: LifeInboxBot) -> None:
        self.bot = bot

    async def _guard_command(
        self, interaction: discord.Interaction, *, read_only: bool = False
    ) -> bool:
        allowed = (
            self.bot.guard.allows_read_interaction(interaction)
            if read_only
            else self.bot.guard.allows_interaction(interaction)
        )
        if allowed:
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "この操作は許可されていません。", ephemeral=True
            )
        return False

    async def _guard_account_command(self, interaction: discord.Interaction) -> bool:
        if self.bot.guard.allows_account_interaction(interaction):
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "この操作は #口座 だけで使えます。", ephemeral=True
            )
        return False

    async def _guard_finance_read_command(self, interaction: discord.Interaction) -> bool:
        if self.bot.guard.allows_finance_read_interaction(interaction):
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "この閲覧コマンドは #生活-記録 など設定済みの生活チャンネルで使えます。",
                ephemeral=True,
            )
        return False

    async def _guard_finance_digest_read_command(
        self, interaction: discord.Interaction
    ) -> bool:
        """Allow digest reads in the normal records or configured digest channel."""

        if (
            self.bot.guard.allows_finance_read_interaction(interaction)
            or self.bot.finance_digest_guard.allows_read_interaction(interaction)
        ):
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "この閲覧コマンドは #生活-記録 など設定済みの生活チャンネルで使えます。",
                ephemeral=True,
            )
        return False

    async def _guard_ai_review_read_command(
        self, interaction: discord.Interaction
    ) -> bool:
        """Allow private reviews only in the records or dedicated review channel."""

        if self.bot.ai_review_guard.allows_read_interaction(interaction):
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "この閲覧コマンドは #ai-振り返り または #生活-記録 で使えます。",
                ephemeral=True,
            )
        return False

    async def _guard_rhythm_command(
        self, interaction: discord.Interaction, *, read_only: bool
    ) -> bool:
        if self.bot.guard.allows_rhythm_interaction(interaction, read_only=read_only):
            return True
        if not interaction.response.is_done():
            if read_only:
                destination = "#音ゲー、#生活-記録、#なんでも入力"
                if self.bot.settings.progress_channel_id is not None:
                    destination = "#音ゲー、#今日の進捗、#生活-記録、#なんでも入力"
            else:
                destination = "#音ゲー"
                if self.bot.settings.progress_channel_id is not None:
                    destination = "#音ゲー または #今日の進捗"
            await interaction.response.send_message(
                f"この操作は {destination} で使えます。", ephemeral=True
            )
        return False

    def _today(self) -> date:
        return datetime.now(ZoneInfo(self.bot.settings.timezone)).date()

    @app_commands.command(name="today", description="今日の確定支出・食事を表示")
    async def today(self, interaction: discord.Interaction) -> None:
        if not await self._guard_command(interaction, read_only=True):
            return
        target = self._today()
        events = await asyncio.to_thread(self.bot.service.list_confirmed)
        await _respond_text(
            interaction,
            render_today(
                events,
                target,
                timezone_name=self.bot.settings.timezone,
                categories=self.bot.settings.categories,
            ),
        )

    @app_commands.command(name="month", description="今月の支出概要を表示")
    async def month(self, interaction: discord.Interaction) -> None:
        if not await self._guard_command(interaction, read_only=True):
            return
        target = self._today()
        events = await asyncio.to_thread(self.bot.service.list_confirmed)
        await _respond_text(
            interaction,
            render_month(
                events,
                target.year,
                target.month,
                timezone_name=self.bot.settings.timezone,
                categories=self.bot.settings.categories,
            ),
        )

    @app_commands.command(name="uncopied", description="手帳へ未転記の確定記録を表示")
    async def uncopied(self, interaction: discord.Interaction) -> None:
        if not await self._guard_command(interaction, read_only=True):
            return
        events = await asyncio.to_thread(self.bot.service.list_uncopied)
        if not events:
            message = "未転記の確定記録はありません。"
        else:
            message = "\n".join(f"#{event.id} {_display_event(event)}" for event in events)
        await _respond_text(interaction, message)

    @app_commands.command(name="planner", description="手帳へ書き写しやすい今日のまとめ")
    @app_commands.describe(date_text="YYYY-MM-DD（省略時は今日）")
    async def planner(
        self,
        interaction: discord.Interaction,
        date_text: str | None = None,
    ) -> None:
        if not await self._guard_command(interaction, read_only=True):
            return
        try:
            target = date.fromisoformat(date_text) if date_text else self._today()
        except ValueError:
            await interaction.response.send_message(
                "日付はYYYY-MM-DDで指定してください。", ephemeral=True
            )
            return
        events = await asyncio.to_thread(self.bot.service.list_confirmed)
        await _respond_text(
            interaction,
            render_planner(
                events,
                target,
                timezone_name=self.bot.settings.timezone,
                categories=self.bot.settings.categories,
            ),
        )

    @app_commands.command(name="copied", description="確定記録を手帳へ転記済みにする")
    @app_commands.describe(
        event_id="対象のEvent ID",
        all_eligible="trueなら未転記の確定記録を全て対象",
    )
    async def copied(
        self,
        interaction: discord.Interaction,
        event_id: int | None = None,
        all_eligible: bool = False,
    ) -> None:
        if not await self._guard_command(interaction):
            return
        if event_id is None and not all_eligible:
            await interaction.response.send_message(
                "event_idを指定するか、all_eligible=trueを指定してください。",
                ephemeral=True,
            )
            return
        if all_eligible:
            marked = await asyncio.to_thread(self.bot.service.mark_all_uncopied)
        else:
            marked = await asyncio.to_thread(self.bot.service.mark_copied, [event_id])
        marked_text = ", ".join(f"#{item}" for item in marked) if marked else "対象なし"
        await _respond_text(interaction, f"転記済みにしました: {marked_text}")

    @app_commands.command(name="inbox", description="未整理のInboxを表示")
    async def inbox(self, interaction: discord.Interaction) -> None:
        if not await self._guard_command(interaction, read_only=True):
            return
        events = await asyncio.to_thread(self.bot.service.list_pending)
        message = render_inbox(events, timezone_name=self.bot.settings.timezone)
        await _respond_text(interaction, message)

    @app_commands.command(name="retry", description="保留中Eventの解析を再試行")
    @app_commands.describe(event_id="再試行するEvent ID")
    async def retry(self, interaction: discord.Interaction, event_id: int) -> None:
        if not self.bot.guard.allows_event_interaction(interaction):
            await interaction.response.send_message(
                "この操作は投稿用の生活チャンネルだけで使えます。", ephemeral=True
            )
            return
        try:
            event = await asyncio.to_thread(self.bot.service.retry_event, event_id)
            await _respond_text(interaction, _display_event(event), ephemeral=True)
        except Exception as exc:
            logger.warning("retry command failed (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "再試行できませんでした。pendingのIDを確認してください。",
                ephemeral=True,
            )

    @app_commands.command(name="history", description="最近の確定記録を表示")
    @app_commands.describe(count="表示件数（1〜50、既定20）")
    async def history(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, HISTORY_MAX_COUNT] = HISTORY_DEFAULT_COUNT,
    ) -> None:
        if not await self._guard_command(interaction, read_only=True):
            return
        if count < 1 or count > HISTORY_MAX_COUNT:
            await interaction.response.send_message(
                f"表示件数は1〜{HISTORY_MAX_COUNT}件で指定してください。", ephemeral=True
            )
            return
        events = await asyncio.to_thread(self.bot.service.list_confirmed)
        await _respond_text(
            interaction,
            _display_history(
                events,
                count=count,
                timezone_name=self.bot.settings.timezone,
            ),
        )

    @app_commands.command(name="detail", description="Eventの支出詳細を表示")
    @app_commands.describe(event_id="対象のEvent ID")
    async def detail(self, interaction: discord.Interaction, event_id: int) -> None:
        if not await self._guard_command(interaction, read_only=True):
            return
        try:
            event = await asyncio.to_thread(self.bot.service.get_event, event_id)
        except Exception as exc:
            logger.warning("detail command failed (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "詳細を表示できませんでした。Event IDを確認してください。", ephemeral=True
            )
            return
        if event is None:
            await interaction.response.send_message(
                f"Event #{event_id} は見つかりません。", ephemeral=True
            )
            return
        await _respond_text(
            interaction,
            _display_event_detail(event, timezone_name=self.bot.settings.timezone),
            ephemeral=True,
        )

    @app_commands.command(name="spending", description="目立つ支出やカテゴリ別支出を表示")
    @app_commands.describe(
        minimum="最低金額（省略時0円）",
        category="カテゴリ名（例: 食費）",
        dining="外食だけに絞る",
    )
    async def spending(
        self,
        interaction: discord.Interaction,
        minimum: int = 0,
        category: str | None = None,
        dining: bool = False,
    ) -> None:
        if not await self._guard_command(interaction, read_only=True):
            return
        if minimum < 0 or minimum > self.bot.settings.max_expense_yen:
            await interaction.response.send_message(
                "最低金額は0円以上で指定してください。", ephemeral=True
            )
            return
        if category and category not in self.bot.settings.categories:
            await interaction.response.send_message(
                "カテゴリは設定済みの名前から指定してください。", ephemeral=True
            )
            return
        events = await asyncio.to_thread(self.bot.service.list_confirmed)
        await _respond_text(
            interaction,
            _display_spending(
                events,
                timezone_name=self.bot.settings.timezone,
                minimum=minimum,
                category=category,
                dining_only=dining,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="budget_set", description="月の全体またはカテゴリ別予算を設定")
    @app_commands.describe(
        amount="予算額（円）",
        category="カテゴリ名（省略時は全体）",
        month="対象月 YYYY-MM（省略時は設定済みタイムゾーンの今月）",
    )
    async def budget_set(
        self,
        interaction: discord.Interaction,
        amount: int,
        category: str | None = None,
        month: str | None = None,
    ) -> None:
        if not await self._guard_account_command(interaction):
            return
        try:
            progress = await asyncio.to_thread(
                self.bot.budgets.set_budget,
                amount,
                month=month,
                category=category,
            )
        except ValueError as exc:
            logger.info("budget_set rejected (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "予算を設定できませんでした。金額・カテゴリ・月（YYYY-MM）を確認してください。",
                ephemeral=True,
            )
            return
        except Exception as exc:
            logger.warning("budget_set failed (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "予算を設定できませんでした。入力を確認してください。", ephemeral=True
            )
            return
        scope = progress.category or "全体"
        await interaction.response.send_message(
            f"予算を設定しました: {progress.month} / {scope} / {progress.amount:,}円",
            ephemeral=True,
        )

    @app_commands.command(name="budget", description="月の予算と確定支出を表示")
    @app_commands.describe(month="対象月 YYYY-MM（省略時は設定済みタイムゾーンの今月）")
    async def budget(
        self,
        interaction: discord.Interaction,
        month: str | None = None,
    ) -> None:
        if not await self._guard_finance_read_command(interaction):
            return
        try:
            snapshot = await asyncio.to_thread(self.bot.budgets.get_month, month)
        except ValueError as exc:
            logger.info("budget rejected (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "月はYYYY-MMで指定してください。", ephemeral=True
            )
            return
        except Exception as exc:
            logger.warning("budget failed (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "予算を表示できませんでした。", ephemeral=True
            )
            return
        await _respond_text(interaction, _display_budget(snapshot), ephemeral=True)

    @app_commands.command(name="dining", description="外食だけを合計付きで表示")
    async def dining(self, interaction: discord.Interaction) -> None:
        if not await self._guard_command(interaction, read_only=True):
            return
        events = await asyncio.to_thread(self.bot.service.list_confirmed)
        await _respond_text(
            interaction,
            _display_spending(
                events,
                timezone_name=self.bot.settings.timezone,
                dining_only=True,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="meals", description="最近の食事記録を表示")
    async def meals(self, interaction: discord.Interaction) -> None:
        if not await self._guard_command(interaction, read_only=True):
            return
        events = await asyncio.to_thread(self.bot.service.list_confirmed)
        await _respond_text(
            interaction,
            _display_meals(events, timezone_name=self.bot.settings.timezone),
            ephemeral=True,
        )

    @app_commands.command(name="accounts", description="複数口座・カード・NISA残高を表示")
    async def accounts(self, interaction: discord.Interaction) -> None:
        if not await self._guard_finance_read_command(interaction):
            return
        accounts = await asyncio.to_thread(self.bot.finance.list_accounts)
        await _respond_text(interaction, _display_accounts(accounts), ephemeral=True)

    @app_commands.command(name="memos", description="最近の一言メモを表示")
    async def memos(self, interaction: discord.Interaction) -> None:
        if not await self._guard_finance_read_command(interaction):
            return
        memos = await asyncio.to_thread(self.bot.finance.list_memos)
        await _respond_text(
            interaction,
            _display_memos(memos, timezone_name=self.bot.settings.timezone),
            ephemeral=True,
        )

    @app_commands.command(name="家計簿今日", description="今日の家計サマリーを表示")
    async def finance_digest_today(self, interaction: discord.Interaction) -> None:
        """Show today's snapshot without touching delivery state or Discord send."""

        if not await self._guard_finance_digest_read_command(interaction):
            return
        try:
            snapshot = await asyncio.to_thread(
                self.bot.finance_digest_service.build_snapshot
            )
            content = format_daily_digest(snapshot)
        except Exception as exc:
            logger.warning("finance digest today failed (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "今日の家計サマリーを表示できませんでした。", ephemeral=True
            )
            return
        await _respond_text(interaction, content, ephemeral=True)

    @app_commands.command(name="家計簿配信確認", description="家計サマリーの予定配信を確認")
    async def finance_digest_delivery_check(
        self, interaction: discord.Interaction
    ) -> None:
        """Preview a delivery through the service's read-only dry-run path."""

        if not await self._guard_finance_digest_read_command(interaction):
            return
        channel_id = self.bot.settings.finance_digest_channel_id
        if channel_id is None:
            await interaction.response.send_message(
                "家計サマリーの配信先が未設定です。FINANCE_DIGEST_CHANNEL_IDを設定してください。",
                ephemeral=True,
            )
            return

        publisher = self.bot.finance_digest_publisher
        digest_service = getattr(publisher, "service", self.bot.finance_digest_service)
        try:
            snapshot = await asyncio.to_thread(digest_service.build_snapshot)
            post_zero_days = getattr(
                publisher,
                "post_zero_days",
                self.bot.settings.finance_digest_post_zero_days,
            )
            is_empty_day = getattr(publisher, "_empty_day", None)
            if not post_zero_days and callable(is_empty_day) and is_empty_day(snapshot):
                action = "skip"
                fingerprint = snapshot.semantic_fingerprint
                content = "（0円日の設定により投稿なし）"
            else:
                plan = await asyncio.to_thread(
                    digest_service.prepare_delivery,
                    snapshot,
                    channel_id,
                    dry_run=True,
                )
                action = getattr(plan.action, "value", str(plan.action))
                fingerprint = plan.semantic_fingerprint
                content = plan.content or "（本文なし）"
        except Exception as exc:
            logger.warning("finance digest delivery preview failed (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "家計サマリーの配信予定を確認できませんでした。", ephemeral=True
            )
            return

        await _respond_text(
            interaction,
            "\n".join(
                (
                    "【家計簿配信確認（dry-run）】",
                    f"予定 action: {action}",
                    f"配信先チャンネルID: {channel_id}",
                    f"fingerprint: {str(fingerprint)[:12]}",
                    "実送信・delivery state変更はありません。",
                    "本文:",
                    content,
                )
            ),
            ephemeral=True,
        )

    @app_commands.command(name="振り返り", description="保存済みの構造化記録を安全に振り返る")
    @app_commands.describe(period="今日または今週")
    @app_commands.choices(
        period=[
            app_commands.Choice(name="今日", value="daily"),
            app_commands.Choice(name="今週", value="weekly"),
        ]
    )
    async def ai_review_summary(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await self._guard_ai_review_read_command(interaction):
            return
        period_value = period.value if period is not None else "daily"
        try:
            facts = await asyncio.to_thread(
                self.bot.ai_review.build_review,
                period_value,
                self._today(),
            )
            used_local_fallback = self.bot.ai_review_provider is None
            if self.bot.ai_review_provider is None:
                result = await asyncio.to_thread(self.bot.ai_review.render_local, facts)
            else:
                try:
                    result = await asyncio.to_thread(
                        self.bot.ai_review_provider.review,
                        facts,
                    )
                except ReviewProviderError:
                    used_local_fallback = True
                    result = await asyncio.to_thread(self.bot.ai_review.render_local, facts)
            content = format_discord_review(result, facts)
            if used_local_fallback:
                content += "\n※ローカル集計で作成（外部AIへの送信なし）"
        except Exception as exc:
            logger.warning("AI review command failed (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "振り返りを作成できませんでした。", ephemeral=True
            )
            return
        await _respond_text(interaction, content, ephemeral=True)

    @app_commands.command(name="rhythm", description="音ゲーTODOとリザルト履歴を表示")
    async def rhythm(self, interaction: discord.Interaction) -> None:
        if not await self._guard_rhythm_command(interaction, read_only=True):
            return
        todos, results = await asyncio.gather(
            asyncio.to_thread(self.bot.rhythm.list_open_todos),
            asyncio.to_thread(self.bot.rhythm.list_results),
        )
        await _respond_text(
            interaction,
            _display_rhythm(
                todos,
                results,
                timezone_name=self.bot.settings.timezone,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="favorite_add", description="音ゲーのお気に入り曲を追加")
    @app_commands.describe(
        game="ゲーム",
        song_title="曲名",
        note="任意メモ",
    )
    @app_commands.choices(game=RHYTHM_GAME_CHOICES)
    async def favorite_add(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str],
        song_title: str,
        note: str | None = None,
    ) -> None:
        if not await self._guard_rhythm_command(interaction, read_only=False):
            return
        game_value = getattr(game, "value", game)
        try:
            favorite = await asyncio.to_thread(
                self.bot.rhythm.add_favorite,
                game_value,
                song_title,
                note,
            )
        except ValueError as exc:
            logger.info("favorite_add rejected (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "お気に入りを追加できませんでした。ゲーム名・曲名・メモを確認してください。",
                ephemeral=True,
            )
            return
        detail = f" / メモ: {favorite.note}" if favorite.note else ""
        await interaction.response.send_message(
            f"お気に入りを保存しました: {RHYTHM_GAME_LABELS[favorite.game]} / "
            f"{favorite.song_title}{detail}",
            ephemeral=True,
        )

    @app_commands.command(name="maimai_favorites", description="maimaiのお気に入り曲を表示")
    async def maimai_favorites(self, interaction: discord.Interaction) -> None:
        if not await self._guard_rhythm_command(interaction, read_only=True):
            return
        favorites = await asyncio.to_thread(
            self.bot.rhythm.list_favorites,
            RhythmGame.MAIMAI,
        )
        await _respond_text(interaction, _display_favorites(favorites), ephemeral=True)

    @app_commands.command(
        name="maimai_net_favorites",
        description="公式maimai NETのお気に入り一覧を表示",
    )
    async def maimai_net_favorites(self, interaction: discord.Interaction) -> None:
        """Display the imported official NET favorites, not manual favorites."""

        if not await self._guard_rhythm_command(interaction, read_only=True):
            return
        favorites = await asyncio.to_thread(self.bot.net_favorites.list_current)
        await _respond_text(
            interaction,
            _display_maimai_net_favorites(
                favorites,
                timezone_name=self.bot.settings.timezone,
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="maimai_mybest",
        description="公式maimaiのMY BEST（プレイ回数順）を表示",
    )
    async def maimai_mybest(self, interaction: discord.Interaction) -> None:
        """Display the imported official play-count-ranked MY BEST list."""

        if not await self._guard_rhythm_command(interaction, read_only=True):
            return
        entries = await asyncio.to_thread(self.bot.mybest.list_current)
        await _respond_text(
            interaction,
            _display_maimai_mybest(
                entries,
                timezone_name=self.bot.settings.timezone,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="progress_today", description="今日の音ゲー進捗を表示")
    @app_commands.describe(game="ゲーム（省略時は全ゲーム）")
    @app_commands.choices(game=RHYTHM_GAME_CHOICES)
    async def progress_today(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await self._guard_rhythm_command(interaction, read_only=True):
            return
        target = self._today()
        game_value = getattr(game, "value", game) if game is not None else None
        try:
            results = await asyncio.to_thread(
                self.bot.rhythm.list_results_today,
                game_value,
                timezone_name=self.bot.settings.timezone,
                target_date=target,
            )
        except ValueError as exc:
            logger.info("progress_today rejected (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "今日の進捗を表示できませんでした。ゲーム指定を確認してください。",
                ephemeral=True,
            )
            return
        await _respond_text(
            interaction,
            _display_progress_today(
                results,
                target_date=target,
                timezone_name=self.bot.settings.timezone,
                guild_id=self.bot.settings.discord_guild_id,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="task_add", description="ローカルの生活タスクを追加")
    @app_commands.describe(
        title="タスクのタイトル",
        due="期限 YYYY-MM-DD（省略可）",
        description="任意の補足",
        due_time="期限時刻 HH:MM（dueを指定した場合だけ、省略可）",
    )
    async def task_add(
        self,
        interaction: discord.Interaction,
        title: str,
        due: str | None = None,
        description: str | None = None,
        due_time: str | None = None,
    ) -> None:
        await self._task_add_response(interaction, title, due, description, due_time)

    @app_commands.command(name="タスク追加", description="期限付きの生活タスクを追加")
    @app_commands.describe(
        title="タスクのタイトル",
        due="期限 YYYY-MM-DD（省略可）",
        description="任意の補足",
        due_time="期限時刻 HH:MM（dueを指定した場合だけ、省略可）",
    )
    async def task_add_japanese(
        self,
        interaction: discord.Interaction,
        title: str,
        due: str | None = None,
        description: str | None = None,
        due_time: str | None = None,
    ) -> None:
        await self._task_add_response(interaction, title, due, description, due_time)

    async def _task_add_response(
        self,
        interaction: discord.Interaction,
        title: str,
        due: str | None,
        description: str | None,
        due_time: str | None,
    ) -> None:
        if not await self._guard_command(interaction):
            return
        try:
            due_kwargs: dict[str, str | None] = {"due_date": due}
            if due_time is not None:
                normalized_time = due_time.strip()
                if due is None or len(normalized_time) != 5 or normalized_time[2] != ":":
                    raise ValueError("task due time requires YYYY-MM-DD and HH:MM")
                hour_text, minute_text = normalized_time.split(":", maxsplit=1)
                if not hour_text.isdigit() or not minute_text.isdigit():
                    raise ValueError("task due time must be HH:MM")
                hour, minute = int(hour_text), int(minute_text)
                if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                    raise ValueError("task due time is outside the valid range")
                due_kwargs = {"due_date": None, "due_at": f"{due}T{normalized_time}"}
            task = await asyncio.to_thread(
                self.bot.tasks.create_manual,
                title,
                description,
                **due_kwargs,
            )
        except ValueError as exc:
            logger.info("task_add rejected (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "タスクを追加できませんでした。タイトル、期限（YYYY-MM-DD）、"
                "時刻（HH:MM）を確認してください。",
                ephemeral=True,
            )
            return
        due_label = ""
        if task.due_at is not None:
            local_due = local_datetime(task.due_at, self.bot.settings.timezone)
            due_text = (
                task.due_label
                if local_due.hour == 0 and local_due.minute == 0 and local_due.second == 0
                else local_due.strftime("%Y-%m-%d %H:%M")
            )
            due_label = f" / 期限: {due_text}"
        await interaction.response.send_message(
            f"タスク #{task.id} を追加しました: {task.title}{due_label}",
            ephemeral=True,
        )

    @app_commands.command(name="tasks", description="未完了の生活タスクを表示")
    async def tasks(self, interaction: discord.Interaction) -> None:
        if not await self._guard_command(interaction, read_only=True):
            return
        task_list = await asyncio.to_thread(self.bot.tasks.list_open_tasks)
        await _respond_text(
            interaction,
            _display_tasks(task_list, timezone_name=self.bot.settings.timezone),
            ephemeral=True,
        )

    @app_commands.command(name="タスク一覧", description="未完了の生活タスクを表示")
    async def task_list_japanese(self, interaction: discord.Interaction) -> None:
        if not await self._guard_command(interaction, read_only=True):
            return
        task_list = await asyncio.to_thread(self.bot.tasks.list_open_tasks)
        await _respond_text(
            interaction,
            _display_tasks(task_list, timezone_name=self.bot.settings.timezone),
            ephemeral=True,
        )

    @app_commands.command(name="task_done", description="生活タスクを完了にする")
    @app_commands.describe(task_id="対象のタスクID")
    async def task_done(self, interaction: discord.Interaction, task_id: int) -> None:
        await self._task_done_response(interaction, task_id)

    @app_commands.command(name="タスク完了", description="生活タスクを完了にする")
    @app_commands.describe(task_id="対象のタスクID")
    async def task_done_japanese(
        self,
        interaction: discord.Interaction,
        task_id: int,
    ) -> None:
        await self._task_done_response(interaction, task_id)

    async def _task_done_response(
        self,
        interaction: discord.Interaction,
        task_id: int,
    ) -> None:
        if not await self._guard_command(interaction):
            return
        try:
            task = await asyncio.to_thread(self.bot.tasks.complete, task_id)
        except ValueError as exc:
            logger.info("task_done rejected (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "タスクを完了できませんでした。タスクIDを確認してください。",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"タスク #{task.id} を完了済みにしました: {task.title}",
            ephemeral=True,
        )

    @app_commands.command(name="就活追加", description="企業・求人・イベント・応募を追加")
    @app_commands.describe(
        company="会社名",
        title="求人名・職種・イベント名",
        kind="記録の種類",
        stage="応募段階（応募の場合）",
        deadline="明示的な期限 YYYY-MM-DD または YYYY-MM-DD HH:MM",
        next_action="次にすること",
        next_action_at="次にする日時 YYYY-MM-DD HH:MM",
        url="求人・イベントのURL（http/https）",
        notes="補足メモ",
        location="勤務地・会場など",
        status="企業・求人・イベントの状態",
        external_id="外部サービス側の識別子（任意）",
    )
    @app_commands.choices(
        kind=CAREER_KIND_CHOICES,
        stage=CAREER_STAGE_CHOICES,
        status=CAREER_STATUS_CHOICES,
    )
    async def career_add(
        self,
        interaction: discord.Interaction,
        company: str,
        title: str,
        kind: str = CareerKind.JOB.value,
        stage: str | None = None,
        deadline: str | None = None,
        next_action: str | None = None,
        next_action_at: str | None = None,
        url: str | None = None,
        notes: str | None = None,
        location: str | None = None,
        status: str | None = None,
        external_id: str | None = None,
    ) -> None:
        if not await self._guard_command(interaction):
            return
        try:
            if kind == CareerKind.APPLICATION.value:
                record = await asyncio.to_thread(
                    self.bot.career.create_application,
                    company,
                    title,
                    stage=stage,
                    status=status,
                    external_id=external_id,
                    deadline=deadline,
                    next_action=next_action,
                    next_action_at=next_action_at,
                    url=url,
                    notes=notes,
                    location=location,
                )
            else:
                opportunity_status = status
                if opportunity_status is None and stage in {
                    item.value for item in CareerStatus
                }:
                    opportunity_status = stage
                record = await asyncio.to_thread(
                    self.bot.career.create_opportunity,
                    company,
                    title,
                    kind=kind,
                    status=opportunity_status,
                    external_id=external_id,
                    deadline=deadline,
                    next_action=next_action,
                    next_action_at=next_action_at,
                    url=url,
                    notes=notes,
                    location=location,
                )
        except Exception as exc:
            logger.info("career_add rejected (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "就活記録を追加できませんでした。会社名・タイトル・期限・URLを確認してください。",
                ephemeral=True,
            )
            return
        kind_label = CAREER_KIND_LABELS.get(record.kind, record.kind)
        stage_label = (
            f" / 段階: {CAREER_STAGE_LABELS.get(record.stage, record.stage)}"
            if record.stage
            else ""
        )
        await interaction.response.send_message(
            f"就活記録 #{record.id} を追加しました: {record.company} / {record.title}"
            f" / 種別: {kind_label} / 状態: "
            f"{CAREER_STATUS_LABELS.get(record.status, record.status)}{stage_label}"
            f" / 期限: {_career_deadline_label(record, timezone_name=self.bot.settings.timezone)}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="就活一覧", description="就活記録と応募段階を表示")
    @app_commands.describe(
        kind="種類で絞り込み（省略時は全種類）",
        include_closed="完了・不採用・辞退も表示する",
    )
    @app_commands.choices(kind=CAREER_KIND_CHOICES)
    async def career_list(
        self,
        interaction: discord.Interaction,
        kind: str | None = None,
        include_closed: bool = False,
    ) -> None:
        if not await self._guard_command(interaction, read_only=True):
            return
        try:
            if kind == CareerKind.APPLICATION.value:
                records = await asyncio.to_thread(
                    self.bot.career.list_applications,
                    include_closed=include_closed,
                )
            elif kind is not None:
                records = await asyncio.to_thread(
                    self.bot.career.list_opportunities,
                    include_closed=include_closed,
                    kind=kind,
                )
            else:
                records = await asyncio.to_thread(
                    self.bot.career.list_records,
                    include_closed=include_closed,
                )
        except Exception as exc:
            logger.info("career_list failed (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "就活記録を表示できませんでした。", ephemeral=True
            )
            return
        await _respond_text(
            interaction,
            _display_career(records, timezone_name=self.bot.settings.timezone),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="就活更新", description="就活記録の状態・期限・次の行動を更新")
    @app_commands.describe(
        record_id="対象の記録ID",
        record_type="求人・企業・イベントか応募か",
        stage="応募段階（応募のとき）",
        status="状態（求人・企業・イベントのとき）",
        deadline="新しい明示的な期限 YYYY-MM-DD または YYYY-MM-DD HH:MM",
        next_action="次にすること",
        next_action_at="次にする日時 YYYY-MM-DD HH:MM",
        url="求人・イベントのURL（http/https）",
        notes="補足メモ",
    )
    @app_commands.choices(
        record_type=CAREER_RECORD_TYPE_CHOICES,
        stage=CAREER_STAGE_CHOICES,
        status=CAREER_STATUS_CHOICES,
    )
    async def career_update(
        self,
        interaction: discord.Interaction,
        record_id: int,
        record_type: str = "opportunity",
        stage: str | None = None,
        status: str | None = None,
        deadline: str | None = None,
        next_action: str | None = None,
        next_action_at: str | None = None,
        url: str | None = None,
        notes: str | None = None,
    ) -> None:
        if not await self._guard_command(interaction):
            return
        changes: dict[str, Any] = {}
        if stage is not None:
            changes["stage"] = stage
        if status is not None:
            changes["status"] = status
        if deadline is not None:
            changes["deadline"] = deadline
        if next_action is not None:
            changes["next_action"] = next_action
        if next_action_at is not None:
            changes["next_action_at"] = next_action_at
        if url is not None:
            changes["url"] = url
        if notes is not None:
            changes["notes"] = notes
        try:
            if record_type == "application":
                record = await asyncio.to_thread(
                    self.bot.career.update_application,
                    record_id,
                    **changes,
                )
            else:
                record = await asyncio.to_thread(
                    self.bot.career.update_opportunity,
                    record_id,
                    **changes,
                )
        except Exception as exc:
            logger.info("career_update rejected (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "就活記録を更新できませんでした。記録IDと入力値を確認してください。",
                ephemeral=True,
            )
            return
        stage_label = (
            f" / 段階: {CAREER_STAGE_LABELS.get(record.stage, record.stage)}"
            if record.stage
            else ""
        )
        await interaction.response.send_message(
            f"就活記録 #{record.id} を更新しました: {record.company} / {record.title}"
            f" / 状態: {CAREER_STATUS_LABELS.get(record.status, record.status)}{stage_label}"
            f" / 期限: {_career_deadline_label(record, timezone_name=self.bot.settings.timezone)}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="日課追加", description="毎日の生活習慣を追加")
    @app_commands.describe(title="日課のタイトル", time="通知時刻 HH:MM（24時間制）")
    async def routine_add(
        self,
        interaction: discord.Interaction,
        title: str,
        time: str,
    ) -> None:
        if not await self._guard_command(interaction):
            return
        try:
            routine = await asyncio.to_thread(self.bot.routines.create, title, time)
        except ValueError as exc:
            logger.info("routine_add rejected (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "日課を追加できませんでした。タイトルと時刻（HH:MM）を確認してください。",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"日課 #{routine.id} を追加しました: {routine.title} / 毎日 {routine.reminder_time}",
            ephemeral=True,
        )

    @app_commands.command(name="日課一覧", description="日課と今日の完了状態を表示")
    async def routine_list(self, interaction: discord.Interaction) -> None:
        if not await self._guard_command(interaction, read_only=True):
            return
        routines = await asyncio.to_thread(self.bot.routines.list_routines)
        await _respond_text(interaction, _display_routines(routines), ephemeral=True)

    @app_commands.command(name="日課完了", description="日課を今日の完了として記録")
    @app_commands.describe(routine_id="対象の日課ID")
    async def routine_complete(
        self,
        interaction: discord.Interaction,
        routine_id: int,
    ) -> None:
        if not await self._guard_command(interaction):
            return
        try:
            routine = await asyncio.to_thread(self.bot.routines.complete, routine_id)
        except ValueError as exc:
            logger.info("routine_complete rejected (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "日課を完了できませんでした。日課IDを確認してください。",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"日課 #{routine.id} を今日の完了として記録しました: {routine.title}",
            ephemeral=True,
        )

    @app_commands.command(name="account_add", description="口座・カード・NISAを追加")
    @app_commands.describe(
        name="表示名（例: 楽天カード、ゆうちょ、NISA）",
        kind="口座の種類",
        opening_balance="開始時点の残高。カードは利用残高を正数で入力",
    )
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="現金", value=AccountKind.CASH.value),
            app_commands.Choice(name="銀行口座", value=AccountKind.BANK.value),
            app_commands.Choice(name="クレジットカード", value=AccountKind.CARD.value),
            app_commands.Choice(name="電子マネー", value=AccountKind.E_MONEY.value),
            app_commands.Choice(name="投資・NISA", value=AccountKind.INVESTMENT.value),
        ]
    )
    async def account_add(
        self,
        interaction: discord.Interaction,
        name: str,
        kind: app_commands.Choice[str],
        opening_balance: int = 0,
    ) -> None:
        if not await self._guard_account_command(interaction):
            return
        try:
            account = await asyncio.to_thread(
                self.bot.finance.create_account,
                name,
                kind.value,
                opening_balance,
            )
        except ValueError as exc:
            logger.info("account_add rejected (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "追加できませんでした。同名口座や入力値を確認してください。",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"追加しました: {account.name} / {ACCOUNT_KIND_LABELS[account.kind]} / "
            f"開始残高 {account.opening_balance:,}円",
            ephemeral=True,
        )

    @app_commands.command(name="引落口座設定", description="クレジットカードの引落口座を設定")
    @app_commands.describe(
        card_name="登録済みクレジットカードの表示名",
        bank_name="登録済み銀行口座の表示名",
    )
    async def settlement_account_link(
        self,
        interaction: discord.Interaction,
        card_name: str,
        bank_name: str,
    ) -> None:
        if not await self._guard_account_command(interaction):
            return
        try:
            link = await asyncio.to_thread(
                self.bot.finance.link_settlement_account,
                card_name,
                bank_name,
            )
        except ValueError as exc:
            logger.info("settlement_account_link rejected (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "設定できませんでした。登録済みのクレジットカード表示名と"
                "銀行口座表示名を確認してください。",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"引落口座を設定しました: {link.card_account_name} → "
            f"{link.settlement_account_name}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="引落口座解除", description="クレジットカードの引落口座を解除")
    @app_commands.describe(card_name="登録済みクレジットカードの表示名")
    async def settlement_account_unlink(
        self,
        interaction: discord.Interaction,
        card_name: str,
    ) -> None:
        if not await self._guard_account_command(interaction):
            return
        try:
            removed = await asyncio.to_thread(
                self.bot.finance.unlink_settlement_account,
                card_name,
            )
        except ValueError as exc:
            logger.info("settlement_account_unlink rejected (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "解除できませんでした。登録済みクレジットカードの表示名を確認してください。",
                ephemeral=True,
            )
            return
        display_name = card_name.strip()
        if removed:
            message = f"引落口座を解除しました: {display_name}"
        else:
            message = f"{display_name} には引落口座が設定されていません。"
        await interaction.response.send_message(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="引落口座一覧", description="カードごとの引落口座を表示")
    async def settlement_account_list(self, interaction: discord.Interaction) -> None:
        if not await self._guard_account_command(interaction):
            return
        try:
            accounts = await asyncio.to_thread(self.bot.finance.list_accounts)
            links = await asyncio.to_thread(
                self.bot.finance.list_settlement_account_links
            )
        except Exception as exc:
            logger.info("settlement_account_list failed (%s)", type(exc).__name__)
            await interaction.response.send_message(
                "引落口座一覧を取得できませんでした。口座登録を確認してください。",
                ephemeral=True,
            )
            return
        await _respond_text(
            interaction,
            _display_settlement_links(accounts, links),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="transfer", description="口座間の振替・カード引落しを記録")
    @app_commands.describe(
        source="移動元口座名",
        destination="移動先口座名",
        amount="金額（円）",
        note="任意メモ",
    )
    async def transfer(
        self,
        interaction: discord.Interaction,
        source: str,
        destination: str,
        amount: int,
        note: str | None = None,
    ) -> None:
        if not await self._guard_account_command(interaction):
            return
        raw_text = f"[{source}] 振替 {amount} -> [{destination}]"
        if note:
            raw_text += f" {note}"
        captured = await asyncio.to_thread(
            self.bot.service.capture_raw,
            IncomingMessage(
                discord_message_id=f"interaction:{interaction.id}",
                discord_channel_id=interaction.channel_id or 0,
                discord_user_id=interaction.user.id,
                guild_id=interaction.guild_id,
                raw_text=raw_text,
                captured_at=getattr(interaction, "created_at", None),
            ),
        )
        try:
            activity = await asyncio.to_thread(
                self.bot.finance.record_transfer,
                captured.event_id,
                source,
                destination,
                amount,
                note,
            )
        except ValueError as exc:
            logger.info("transfer rejected event_id=%s (%s)", captured.event_id, type(exc).__name__)
            await interaction.response.send_message(
                "振替を記録できませんでした。口座名と金額を確認してください。"
                f" 原文はEvent #{captured.event_id}として保存済みです。",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"振替を記録しました: {activity.source_account_name} → "
            f"{activity.destination_account_name} / {activity.amount:,}円",
            ephemeral=True,
        )

    @app_commands.command(name="レート", description="ウニ・マイ・ゲキの最新レートを表示")
    @app_commands.describe(game="機種")
    @app_commands.choices(game=RATING_GAME_CHOICES)
    async def rating(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str],
    ) -> None:
        if not await self._guard_rhythm_command(interaction, read_only=True):
            return
        user_id = await asyncio.to_thread(self.bot.ensure_rating_user)
        snapshot = await asyncio.to_thread(
            self.bot.rating_store.latest_snapshot,
            user_id=user_id,
            game=game.value,
        )
        await _respond_text(
            interaction,
            _display_latest_rating(snapshot, timezone_name=self.bot.settings.rhythm_time_zone),
            ephemeral=True,
        )

    async def _run_rating_update(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await self._guard_rhythm_command(interaction, read_only=False):
            return
        if not self.bot.settings.rhythm_enabled:
            await interaction.response.send_message(
                "音ゲー収集はまだ無効です。設定後に再実行してください。", ephemeral=True
            )
            return
        scheduler = self.bot.rating_scheduler
        if scheduler is None:
            await interaction.response.send_message(
                "手動取得処理を準備できません。Botを再起動してから再実行してください。",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        user_id = await asyncio.to_thread(self.bot.ensure_rating_user)
        games = [game.value] if game is not None else [item.value for item in RATING_GAME_CHOICES]
        runs = await asyncio.to_thread(
            self.bot.rating_store.queue_collection_runs,
            user_id=user_id,
            games=games,
            trigger="discord",
            request_key=f"interaction:{interaction.id}",
        )
        processed_ids = set(await scheduler.run_manual_once(runs))
        current_runs = await asyncio.gather(
            *(asyncio.to_thread(self.bot.rating_store.get_run, run.id) for run in runs)
        )
        lines = ["手動取得を一度実行しました。"]
        for requested, current in zip(runs, current_runs, strict=True):
            if current is None:
                lines.append(f"{requested.game}: {requested.id} / 状態を確認できません")
                continue
            marker = "実行済み" if current.id in processed_ids else "再実行なし"
            lines.append(f"{current.game}: {current.state} / {current.id} / {marker}")
            if current.progress_message:
                lines.append(f"  {current.progress_message}")
        if not processed_ids:
            lines.append("処理中・手動確認待ち・完了済み、または別のBotが保持中のrunは再実行していません。")
        lines.append("進み具合は `/取得状況` で確認できます。")
        await _respond_text(interaction, "\n".join(lines), ephemeral=True)

    @app_commands.command(name="レート更新", description="レート取得を1機種または3機種で開始")
    @app_commands.describe(game="省略時は3機種すべて")
    @app_commands.choices(game=RATING_GAME_CHOICES)
    async def rating_update(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str] | None = None,
    ) -> None:
        await self._run_rating_update(interaction, game)

    @app_commands.command(name="get", description="最新レート画像を1機種または3機種で取得")
    @app_commands.describe(game="省略時は3機種すべて")
    @app_commands.choices(game=RATING_GAME_CHOICES)
    async def get_rating(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str] | None = None,
    ) -> None:
        """Short alias for the same event-driven manual collection flow."""

        await self._run_rating_update(interaction, game)

    @app_commands.command(
        name="取得確認",
        description="手動操作待ちの取得を明示確認して再開可能にする",
    )
    @app_commands.describe(run_id="/取得状況に表示された実行ID")
    async def confirm_collection_action(
        self,
        interaction: discord.Interaction,
        run_id: str,
    ) -> None:
        if not await self._guard_rhythm_command(interaction, read_only=False):
            return
        user_id = await asyncio.to_thread(self.bot.ensure_rating_user)
        run = await asyncio.to_thread(self.bot.rating_store.get_run, run_id)
        if run is None or run.user_id != user_id:
            await interaction.response.send_message(
                "その取得実行は見つかりません。", ephemeral=True
            )
            return
        try:
            confirmed = await asyncio.to_thread(
                self.bot.rating_store.confirm_manual_action,
                run_id=run_id,
            )
        except ValueError:
            await interaction.response.send_message(
                "この取得実行は手動確認待ちではありません。", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"明示確認を記録しました: {confirmed.game} / {confirmed.id}\n"
            "固定時間ではなく、次のキュー処理で取得イベントを確認して再開します。",
            ephemeral=True,
        )

    @app_commands.command(name="取得状況", description="3機種の取得状態とエラーを表示")
    async def collection_status(self, interaction: discord.Interaction) -> None:
        if not await self._guard_rhythm_command(interaction, read_only=True):
            return
        user_id = await asyncio.to_thread(self.bot.ensure_rating_user)
        runs = await asyncio.to_thread(self.bot.rating_store.latest_runs, user_id=user_id)
        failed_runs = await asyncio.to_thread(
            self.bot.rating_store.latest_failed_runs,
            user_id=user_id,
        )
        deliveries = await asyncio.to_thread(
            self.bot.rating_store.delivery_statuses,
            user_id=user_id,
        )
        lines = ["【レート取得状況】"]
        for choice in RATING_GAME_CHOICES:
            run = runs.get(choice.value)
            if run is None:
                lines.append(f"{choice.name}: 未実行")
                continue
            manual = f" / 手動操作: {run.manual_action}" if run.manual_action else ""
            error = f" / エラー: {run.last_error_code}" if run.last_error_code else ""
            failed = failed_runs.get(choice.value)
            failure_time = (
                " / 最終失敗: "
                + _format_datetime(
                    failed.finished_at,
                    self.bot.settings.rhythm_time_zone,
                )
                if failed is not None
                else ""
            )
            delivery = deliveries[choice.value]
            posted_time = (
                _format_datetime(
                    delivery.last_posted_at,
                    self.bot.settings.rhythm_time_zone,
                )
                if delivery.last_posted_at is not None
                else "なし"
            )
            lines.append(
                f"{choice.name}: {run.state} / {run.progress_message}{manual}{error}"
                f"{failure_time} / 最終投稿: {posted_time}"
                f" / 未完了配信: {delivery.pending_or_failed_count}"
            )
        await _respond_text(interaction, "\n".join(lines), ephemeral=True)

    @app_commands.command(name="今日の利用額", description="iPhone同期済みの音ゲー利用額を表示")
    @app_commands.describe(date_text="YYYY-MM-DD（省略時は今日）")
    async def today_rhythm_spending(
        self,
        interaction: discord.Interaction,
        date_text: str | None = None,
    ) -> None:
        if not await self._guard_rhythm_command(interaction, read_only=True):
            return
        try:
            target = date.fromisoformat(date_text) if date_text else datetime.now(
                ZoneInfo(self.bot.settings.rhythm_time_zone)
            ).date()
        except ValueError:
            await interaction.response.send_message(
                "日付はYYYY-MM-DDで指定してください。", ephemeral=True
            )
            return
        user_id = await asyncio.to_thread(self.bot.ensure_rating_user)
        record = await asyncio.to_thread(
            self.bot.rating_store.get_spending,
            user_id=user_id,
            local_date=target,
        )
        message = (
            "この日の利用額は未同期です。"
            if record is None
            else format_spending_message(record)
        )
        await _respond_text(interaction, message, ephemeral=True)

    @app_commands.command(name="再投稿", description="指定日の最新レート投稿を再実行")
    @app_commands.describe(date_text="YYYY-MM-DD", game="機種")
    @app_commands.choices(game=RATING_GAME_CHOICES)
    async def repost_rating(
        self,
        interaction: discord.Interaction,
        date_text: str,
        game: app_commands.Choice[str],
    ) -> None:
        if not await self._guard_rhythm_command(interaction, read_only=False):
            return
        if not self.bot.settings.rhythm_enabled:
            await interaction.response.send_message(
                "音ゲー収集は無効です。設定後に再実行してください。", ephemeral=True
            )
            return
        try:
            target = date.fromisoformat(date_text)
        except ValueError:
            await interaction.response.send_message(
                "日付はYYYY-MM-DDで指定してください。", ephemeral=True
            )
            return
        channel_id = self.bot.rating_channel_id(game.value)
        if channel_id is None:
            await interaction.response.send_message(
                "この機種の投稿先チャンネルが未設定です。", ephemeral=True
            )
            return
        timezone = ZoneInfo(self.bot.settings.rhythm_time_zone)
        collected_from = datetime.combine(target, datetime.min.time(), tzinfo=timezone)
        collected_to = collected_from + timedelta(days=1)
        user_id = await asyncio.to_thread(self.bot.ensure_rating_user)
        snapshots, _ = await asyncio.to_thread(
            self.bot.rating_store.list_snapshots,
            user_id=user_id,
            game=game.value,
            collected_from=collected_from,
            collected_to=collected_to,
            offset=0,
            limit=1,
        )
        if not snapshots or not snapshots[0].image_links:
            await interaction.response.send_message(
                "指定日のレート画像がありません。", ephemeral=True
            )
            return
        snapshot = snapshots[0]
        image = snapshot.image_links[0].image
        try:
            verified_image_path = verify_rating_image(
                image_root=self.bot.settings.rhythm_image_dir,
                image_path=image.image_path,
                expected_sha256=image.image_sha256,
                expected_byte_size=image.byte_size,
            )
        except RatingImageVerificationError:
            await interaction.response.send_message(
                "指定日のレート画像を安全に確認できませんでした。再生成してください。",
                ephemeral=True,
            )
            return
        outcome = await self.bot.rating_publisher.publish_rating(
            user_id=user_id,
            local_date=target,
            game=game.value,
            channel_id=channel_id,
            semantic_fingerprint=snapshot.semantic_fingerprint,
            image_sha256=image.image_sha256,
            image_path=verified_image_path,
            rating=_rating_number(snapshot.rating),
            previous_rating=(
                _rating_number(snapshot.previous_rating)
                if snapshot.previous_rating is not None
                else None
            ),
            delta=(
                float(snapshot.rating_delta) if snapshot.rating_delta is not None else None
            ),
            collected_label=_format_datetime(
                snapshot.collected_at, self.bot.settings.rhythm_time_zone
            ),
            force=True,
        )
        if outcome.dry_run:
            message = (
                "dry-runのため実投稿せず、次の内容を確認しました。\n"
                + outcome.planned_content
            )
        elif outcome.posted:
            message = "再投稿（または既存Botメッセージの更新）が完了しました。"
        else:
            message = f"再投稿できませんでした: {outcome.error_code or '処理中'}"
        await _respond_text(interaction, message, ephemeral=True)

    @app_commands.command(name="認証状況", description="3機種のAPI・ブラウザー認証状態を表示")
    async def authentication_status(self, interaction: discord.Interaction) -> None:
        if not await self._guard_rhythm_command(interaction, read_only=True):
            return
        user_id = await asyncio.to_thread(self.bot.ensure_rating_user)
        statuses = await asyncio.to_thread(
            self.bot.rating_store.provider_statuses, user_id=user_id
        )
        lines = ["【認証状況】"]
        for choice in RATING_GAME_CHOICES:
            status = statuses.get(choice.value)
            session_path = self.bot.settings.rhythm_browser_session_dir / choice.value
            if status is None:
                state = "未設定"
            else:
                state = status.session_status
                if status.last_error_code:
                    state += f" / {status.last_error_code}"
            session_label = "セッションあり（有効性未確認）" if session_path.exists() else "なし"
            lines.append(f"{choice.name}: {state} / ブラウザー: {session_label}")
        await _respond_text(interaction, "\n".join(lines), ephemeral=True)

    @app_commands.command(name="コマンド一覧", description="Botコマンド案内の場所を表示")
    async def command_list(self, interaction: discord.Interaction) -> None:
        if not await self._guard_command(interaction, read_only=True):
            return
        messages = command_guide_messages()
        await interaction.response.send_message(
            f"コマンド案内は #{self.bot.settings.command_guide_channel_name} にあります。"
            f"（全{len(messages)}パート）",
            ephemeral=True,
        )

    @app_commands.command(name="mypage", description="ローカル生活マイページのURLを表示")
    async def mypage(self, interaction: discord.Interaction) -> None:
        if not await self._guard_command(interaction, read_only=True):
            return
        await interaction.response.send_message(
            "生活マイページ: "
            f"{_web_base_url(self.bot.settings)}\n"
            "このMacでWebサーバーを起動している間だけ開けます。",
            ephemeral=True,
        )


class LifeInboxBot(commands.Bot):
    def __init__(
        self,
        settings: Settings,
        service: InboxService,
        *,
        inbox_classifier: InboxClassifier | None = None,
        finance: FinanceService | None = None,
        finance_digest: FinanceDigestService | None = None,
        finance_digest_service: FinanceDigestService | None = None,
        finance_digest_publisher: FinanceDigestPublisher | None = None,
        finance_digest_scheduler: FinanceDigestScheduler | None = None,
        rhythm: RhythmService | None = None,
        mybest: MaimaiMyBestService | None = None,
        mybest_service: MaimaiMyBestService | None = None,
        net_favorites: MaimaiNetFavoriteService | None = None,
        net_favorites_service: MaimaiNetFavoriteService | None = None,
        tasks: TaskService | None = None,
        task_service: TaskService | None = None,
        career: CareerService | None = None,
        career_service: CareerService | None = None,
        budget_service: BudgetService | None = None,
        budgets: BudgetService | None = None,
        rating_store: RhythmPersistence | None = None,
        rating_publisher: RhythmDiscordPublisher | None = None,
        rating_scheduler: RhythmScheduler | None = None,
        rhythm_spending_store: RhythmSpendingEventPersistence | None = None,
        rhythm_spending_publisher: RhythmSpendingDiscordPublisher | None = None,
        rhythm_spending_scheduler: RhythmSpendingScheduler | None = None,
        health_store: HealthPersistence | None = None,
        health_publisher: HealthDiscordPublisher | None = None,
        health_scheduler: HealthScheduler | None = None,
        routines: RoutineService | None = None,
        routine_service: RoutineService | None = None,
        reminders: ReminderService | None = None,
        reminder_service: ReminderService | None = None,
        reminder_scheduler: ReminderScheduler | None = None,
        ai_review_service: AIReviewService | None = None,
        ai_review_provider: ReviewProvider | None = None,
        social_drafts: SocialDraftService | None = None,
        notion_sync: NotionSyncService | None = None,
        notion_sync_service: NotionSyncService | None = None,
        notion_sync_worker: NotionSyncWorker | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.service = service
        self.social_drafts = social_drafts or SocialDraftService(service.session_factory)
        self.capture_context = CaptureContextService(service.session_factory)
        self.rhythm_spending_capture = RhythmSpendingCaptureService(
            service.session_factory
        )
        self._shortcut_dispatch_task: asyncio.Task[None] | None = None
        self._social_draft_view_event_ids: set[int] = set()
        # ``None`` preserves the established structured router for callers
        # that inject an InboxService directly. ``create_bot`` always injects
        # the free-form classifier, including its local-only fallback.
        self.inbox_classifier = inbox_classifier
        self._inbox_candidate_views: dict[str, InboxCandidateView] = {}
        self._inbox_candidate_route_locks: dict[int, asyncio.Lock] = {}
        self._inbox_candidate_route_results: dict[
            int, tuple[InboxCategory, str, bool]
        ] = {}
        self._discord_archive_locks: dict[str, asyncio.Lock] = {}
        self._auto_notion_enqueued_event_ids: set[int] = set()
        # TaskService's manual-task API is intentionally independent from
        # Events. Keep the in-process association so a destination-send retry
        # can reuse the task that was already committed for this candidate.
        self._inbox_candidate_task_ids: dict[int, int] = {}
        if notion_sync is not None and notion_sync_service is not None:
            raise ValueError("provide only one Notion sync service")
        self.notion_sync = (
            notion_sync
            or notion_sync_service
            or NotionSyncService(
                service.session_factory,
                settings,
                data_dir=settings.data_dir,
            )
        )
        self.notion_sync_service = self.notion_sync
        self.notion_sync_worker = notion_sync_worker
        if (
            self.notion_sync_worker is None
            and settings.notion_sync_enabled
            and getattr(self.notion_sync, "transport", None) is not None
        ):
            self.notion_sync_worker = NotionSyncWorker(
                self.notion_sync,
                enabled=True,
                poll_seconds=settings.notion_sync_poll_seconds,
            )
        self.finance = finance or FinanceService(service.session_factory)
        if finance_digest is not None and finance_digest_service is not None:
            raise ValueError("provide only one finance digest service")
        self.finance_digest = (
            finance_digest
            or finance_digest_service
            or FinanceDigestService(
                service.session_factory,
                timezone=settings.finance_digest_time_zone,
                large_expense_yen=settings.finance_digest_large_expense_yen,
            )
        )
        self.finance_digest_service = self.finance_digest
        if finance_digest_publisher is None:
            self.finance_digest_publisher = FinanceDigestPublisher(
                self.finance_digest,
                DiscordPyTransport(self),
                dry_run=settings.finance_digest_dry_run,
                post_zero_days=settings.finance_digest_post_zero_days,
            )
        else:
            _validate_finance_digest_publisher(
                settings,
                finance_digest_publisher,
                source="injected",
            )
            self.finance_digest_publisher = finance_digest_publisher
        if finance_digest_scheduler is not None:
            # Read the concrete attribute rather than asking an arbitrary test
            # double for a dynamic ``publisher`` attribute.  An actual
            # FinanceDigestScheduler (or a fake that explicitly stores one)
            # still gets the same fail-closed check.
            scheduler_attrs = getattr(finance_digest_scheduler, "__dict__", {})
            scheduler_publisher = scheduler_attrs.get("publisher")
            if scheduler_publisher is not None:
                _validate_finance_digest_publisher(
                    settings,
                    scheduler_publisher,
                    source="scheduler",
                )
        self.finance_digest_scheduler = (
            finance_digest_scheduler
            or FinanceDigestScheduler(
                self.finance_digest_publisher,
                enabled=settings.finance_digest_enabled,
                channel_id=settings.finance_digest_channel_id,
                cron=settings.finance_digest_cron,
                timezone_name=settings.finance_digest_time_zone,
                reconcile_seconds=settings.finance_digest_reconcile_seconds,
                lookback_days=settings.finance_digest_lookback_days,
            )
        )
        self.ai_review = ai_review_service or AIReviewService(
            service.session_factory,
            allowed_user_id=settings.allowed_user_id,
            timezone=settings.timezone,
        )
        if ai_review_provider is not None:
            self.ai_review_provider = ai_review_provider
        elif settings.ai_review_use_openai:
            if settings.openai_api_key is None:
                raise ValueError("AI review requires an OpenAI API key")
            self.ai_review_provider = OpenAIReviewAdapter(
                api_key=settings.openai_api_key.get_secret_value(),
                model=settings.openai_model,
            )
        else:
            self.ai_review_provider = None
        self.rhythm = rhythm or RhythmService(service.session_factory)
        if mybest is not None and mybest_service is not None:
            raise ValueError("provide only one MY BEST service")
        if net_favorites is not None and net_favorites_service is not None:
            raise ValueError("provide only one official favorite service")
        self.mybest = mybest or mybest_service or MaimaiMyBestService(service.session_factory)
        self.mybest_service = self.mybest
        self.net_favorites = (
            net_favorites
            or net_favorites_service
            or MaimaiNetFavoriteService(service.session_factory)
        )
        self.net_favorites_service = self.net_favorites
        if tasks is not None and task_service is not None:
            raise ValueError("provide only one task service")
        if budget_service is not None and budgets is not None:
            raise ValueError("provide only one budget service")
        self.tasks = tasks or task_service or TaskService(
            service.session_factory,
            timezone=settings.timezone_info(),
        )
        self.task_service = self.tasks
        if career is not None and career_service is not None:
            raise ValueError("provide only one career service")
        self.career = (
            career
            or career_service
            or getattr(service, "career", None)
            or CareerService(
                service.session_factory,
                timezone=settings.timezone_info(),
            )
        )
        self.career_service = self.career
        self.budgets = budget_service or budgets or BudgetService(
            service.session_factory,
            timezone=settings.timezone_info(),
            allowed_categories=settings.categories,
        )
        self.budget_service = self.budgets
        if routines is not None and routine_service is not None:
            raise ValueError("provide only one routine service")
        self.routines = routines or routine_service or RoutineService(
            service.session_factory,
            timezone=settings.timezone_info(),
        )
        self.routine_service = self.routines
        if reminders is not None and reminder_service is not None:
            raise ValueError("provide only one reminder service")
        self.reminders = reminders or reminder_service or ReminderService(
            service.session_factory,
            timezone=settings.timezone_info(),
            claim_timeout_seconds=max(300, settings.reminder_poll_seconds * 3),
        )
        self.reminder_service = self.reminders
        self.rating_store = rating_store or RhythmPersistence(service.session_factory)
        if rating_publisher is None:
            self.rating_publisher = RhythmDiscordPublisher(
                self.rating_store,
                DiscordPyTransport(self),
                dry_run=settings.rhythm_discord_dry_run,
            )
        else:
            _validate_rhythm_publisher(settings, rating_publisher, source="injected")
            self.rating_publisher = rating_publisher
        if rating_scheduler is not None:
            scheduler_settings = rating_scheduler.settings
            if any(
                getattr(scheduler_settings, field) != getattr(settings, field)
                for field in (
                    "rhythm_enabled",
                    "rhythm_scheduler_enabled",
                    "rhythm_discord_dry_run",
                )
            ):
                raise ValueError("injected rhythm scheduler settings must match Bot settings")
            _validate_rhythm_publisher(settings, rating_scheduler.publisher, source="scheduler")
            executor_publisher = getattr(rating_scheduler.executor, "publisher", None)
            if executor_publisher is not None:
                _validate_rhythm_publisher(settings, executor_publisher, source="executor")
        self.rating_scheduler = rating_scheduler
        self.rhythm_spending_store = (
            rhythm_spending_store
            or RhythmSpendingEventPersistence(service.session_factory)
        )
        self.rhythm_spending_publisher = (
            rhythm_spending_publisher
            or RhythmSpendingDiscordPublisher(
                event_store=self.rhythm_spending_store,
                delivery_store=self.rating_store,
                transport=DiscordPyTransport(self),
                dry_run=settings.rhythm_discord_dry_run,
            )
        )
        if (
            self.rhythm_spending_publisher.dry_run
            != settings.rhythm_discord_dry_run
        ):
            raise ValueError(
                "spending publisher dry-run setting must match Bot settings"
            )
        self.rhythm_spending_scheduler = rhythm_spending_scheduler
        self.health_store = health_store or HealthPersistence(service.session_factory)
        self.health_publisher = health_publisher or HealthDiscordPublisher(
            self.rating_store,
            DiscordPyTransport(self),
            dry_run=settings.health_discord_dry_run,
            include_heart_rate=settings.health_include_heart_rate,
        )
        self.health_scheduler = health_scheduler
        if reminder_scheduler is None:
            self.reminder_scheduler = ReminderScheduler(
                self.reminders,
                self._send_reminder,
                enabled=settings.reminder_scheduler_enabled,
                poll_interval_seconds=settings.reminder_poll_seconds,
            )
        else:
            self.reminder_scheduler = reminder_scheduler
        self._rating_user_id: int | None = None
        self.guard = ScopeGuard(
            settings.allowed_user_id,
            settings.discord_guild_id,
            settings.primary_inbox_channel_id,
            settings.records_channel_id,
            settings.card_channel_id,
            settings.account_channel_id,
            settings.memo_channel_id,
            settings.rhythm_channel_id,
            settings.meal_channel_id,
            settings.progress_channel_id,
            social_draft_channel_id=settings.social_draft_channel_id,
            legacy_channel_id=settings.legacy_inbox_channel_id,
        )
        # Keep the existing central guard unchanged while permitting the two
        # read-only digest commands in a separately configured digest channel.
        self.finance_digest_guard = ScopeGuard(
            settings.allowed_user_id,
            settings.discord_guild_id,
            settings.primary_inbox_channel_id,
            settings.finance_digest_channel_id,
            legacy_channel_id=settings.legacy_inbox_channel_id,
        )
        self.ai_review_guard = ScopeGuard(
            settings.allowed_user_id,
            settings.discord_guild_id,
            settings.ai_review_channel_id,
            settings.records_channel_id,
        )

    async def setup_hook(self) -> None:
        await asyncio.to_thread(self.finance.ensure_default_accounts)
        if (
            self.rating_scheduler is None
            and self.settings.rhythm_enabled
        ):
            from .rhythm_runtime import RhythmCollectionExecutor

            rating_user_id = await asyncio.to_thread(self.ensure_rating_user)
            executor = RhythmCollectionExecutor(
                settings=self.settings,
                persistence=self.rating_store,
                publisher=self.rating_publisher,
            )
            self.rating_scheduler = RhythmScheduler(
                settings=self.settings,
                persistence=self.rating_store,
                executor=executor,
                publisher=self.rating_publisher,
                user_id=rating_user_id,
            )
        if self.health_scheduler is None and self.settings.health_enabled:
            health_user_id = await asyncio.to_thread(self.ensure_health_user)
            self.health_scheduler = HealthScheduler(
                settings=self.settings,
                persistence=self.health_store,
                publisher=self.health_publisher,
                user_id=health_user_id,
            )
        if (
            self.rhythm_spending_scheduler is None
            and self.settings.rhythm_enabled
            and (
                self.settings.rhythm_spending_immediate_post_enabled
                or self.settings.rhythm_spending_daily_summary_enabled
            )
        ):
            rhythm_user_id = await asyncio.to_thread(self.ensure_rating_user)
            self.rhythm_spending_scheduler = RhythmSpendingScheduler(
                settings=self.settings,
                event_store=self.rhythm_spending_store,
                delivery_store=self.rating_store,
                event_publisher=self.rhythm_spending_publisher,
                daily_publisher=self.rating_publisher,
                rhythm_user_id=rhythm_user_id,
            )
        await self.add_cog(LifeInboxCommands(self))
        # Persistent views are restored for active records after a restart.
        pending = await asyncio.to_thread(self.service.list_pending)
        confirmed = await asyncio.to_thread(self.service.list_confirmed)
        for event in [*pending, *confirmed]:
            self.add_view(EventActionView(self.service, self.guard, event.id))
            self.add_view(NotionPromotionView(self, event.id))
        for draft in await asyncio.to_thread(self.social_drafts.list_active):
            self._restore_social_draft_view(draft)
        for routine in await asyncio.to_thread(
            self.routines.list_routines,
            include_disabled=True,
        ):
            self.add_view(RoutineCompletionView(self.routines, routine.id, self.settings))
        if self.settings.discord_guild_id is not None:
            guild = discord.Object(id=self.settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        if self.rating_scheduler is not None:
            self.rating_scheduler.start()
        if self.health_scheduler is not None:
            self.health_scheduler.start()
        if self.rhythm_spending_scheduler is not None:
            self.rhythm_spending_scheduler.start()
        if self.reminder_scheduler is not None:
            self.reminder_scheduler.start()
        if self.finance_digest_scheduler is not None:
            self.finance_digest_scheduler.start()
        if self.notion_sync_worker is not None:
            self.notion_sync_worker.start()
        if self.settings.general_inbox_channel_id is not None:
            self._shortcut_dispatch_task = asyncio.create_task(
                self._shortcut_dispatch_loop(),
                name="shortcut-inbox-dispatch",
            )

    def ensure_rating_user(self) -> int:
        if self._rating_user_id is None:
            self._rating_user_id = self.rating_store.ensure_user(
                display_name=self.settings.rhythm_player_display_name,
                time_zone=self.settings.rhythm_time_zone,
                discord_user_id=self.settings.allowed_user_id,
            )
        return self._rating_user_id

    def ensure_health_user(self) -> int:
        return self.health_store.ensure_user(
            display_name=self.settings.rhythm_player_display_name,
            time_zone=self.settings.health_time_zone,
            discord_user_id=self.settings.allowed_user_id,
        )

    def rating_channel_id(self, game: str) -> int | None:
        return {
            "chunithm": self.settings.discord_chunithm_channel_id,
            "maimai": self.settings.discord_maimai_channel_id,
            "ongeki": self.settings.discord_ongeki_channel_id,
        }.get(game)

    def _restore_social_draft_view(self, draft: SocialDraftResult) -> None:
        """Register one event-scoped persistent View after a Bot restart."""

        if draft.event_id in self._social_draft_view_event_ids:
            return
        view = SocialDraftView(
            self,
            draft.event_id,
            action_channel_id=draft.discord_action_channel_id,
        )
        try:
            self.add_view(view)
        except ValueError as exc:
            # A repeated setup hook must not register duplicate custom IDs.
            logger.info(
                "social draft persistent view registration skipped (%s)",
                type(exc).__name__,
            )
            return
        self._social_draft_view_event_ids.add(draft.event_id)

    def _allows_social_draft_selection(
        self,
        interaction: discord.Interaction,
        event_id: int,
        *,
        action_channel_id: Any = None,
    ) -> bool:
        """Check the configured human, guild, and action-card channel."""

        user = getattr(interaction, "user", None)
        bot_user = self.user
        if (
            bot_user is not None
            and str(getattr(user, "id", "")) == str(getattr(bot_user, "id", ""))
        ):
            return False
        return self.guard.allows_social_draft_interaction(
            interaction,
            action_channel_id=action_channel_id,
        )

    @staticmethod
    def _format_social_selection_result(result: SocialDraftResult) -> str:
        """Explain local selection and keep the external-publication boundary explicit."""

        if result.state == "blocked":
            return (
                f"Event #{result.event_id} の対象を{result.target_label}として"
                "ローカルoutboxへ保存しました。"
                "保存済み画像がないためブロック中です。"
                "外部SNSには投稿していません。"
            )
        if result.state == "held":
            return (
                f"Event #{result.event_id} をローカルoutboxで保留にしました。"
                "外部SNSには投稿していません。"
            )
        if result.state == "ready":
            return (
                f"Event #{result.event_id} の対象を{result.target_label}として"
                "ローカルoutboxへ保存しました。"
                "これは送信先の選択だけで、外部SNSには投稿していません。"
            )
        return (
            f"Event #{result.event_id} のSNS下書きをローカルに保存しました。"
            "外部SNSには投稿していません。"
        )

    def _allows_notion_promotion(self, interaction: discord.Interaction) -> bool:
        """Allow promotion only for the configured human in the configured guild."""

        user = getattr(interaction, "user", None)
        configured_user = self.settings.allowed_user_id
        configured_guild = self.settings.discord_guild_id
        bot_user = self.user
        return (
            configured_user is not None
            and str(getattr(user, "id", "")) == str(configured_user)
            and not getattr(user, "bot", False)
            and (
                bot_user is None
                or str(getattr(user, "id", "")) != str(getattr(bot_user, "id", ""))
            )
            and configured_guild is not None
            and str(getattr(interaction, "guild_id", "")) == str(configured_guild)
        )

    def _notion_delivery_note(self) -> str:
        if getattr(self.notion_sync, "transport", None) is None:
            return (
                "外部Notionへのライブ配信アダプタは未設定です。"
                "この操作ではローカルキューへの予約だけを保存しました。"
            )
        if not self.settings.notion_sync_enabled:
            return (
                "外部Notionへのライブ配信は設定で無効です。"
                "この操作ではローカルキューへの予約だけを保存しました。"
            )
        return (
            "外部Notionへの配信はバックグラウンド処理へ渡します。"
            "Discordの応答を待たせず、失敗時もローカル予約を保持します。"
        )

    def _format_notion_enqueue_result(self, result: Any) -> str:
        event_id = getattr(result, "event_id", "?")
        if not getattr(result, "eligible", False):
            return (
                f"Event #{event_id} のNotion保存予約を受け付けましたが、"
                "ポリシーにより対象外としてローカルキューに記録しました。"
                f"（理由: {getattr(result, 'reason_code', 'policy_rejected')}）"
                "外部Notionへは配信しません。"
            )
        if getattr(result, "created", False):
            action = "ローカルのNotion保存予約キューに登録しました"
        elif getattr(result, "changed", False):
            action = "既存のローカルNotion保存予約を更新しました"
        else:
            action = "既存のローカルNotion保存予約を確認しました（重複は作成していません）"
        return f"Event #{event_id} の{action}。{self._notion_delivery_note()}"

    async def _handle_notion_promotion(
        self,
        interaction: discord.Interaction,
        event_id: int,
    ) -> None:
        if not self._allows_notion_promotion(interaction):
            await _respond_text(interaction, "この操作は許可されていません。", ephemeral=True)
            return
        try:
            result = await asyncio.to_thread(self.notion_sync.enqueue_event, event_id)
        except ValueError:
            await _respond_text(
                interaction,
                "Notion保存予約を作成できませんでした。Event IDを確認してください。"
                "ローカルEventは変更していません。",
                ephemeral=True,
            )
        except Exception as exc:
            logger.warning(
                "notion promotion failed for event_id=%s (%s)",
                event_id,
                type(exc).__name__,
            )
            await _respond_text(
                interaction,
                "Notion保存予約を保存できませんでした。"
                "ローカルEventは変更していません。",
                ephemeral=True,
            )
        else:
            if self.notion_sync_worker is not None:
                self.notion_sync_worker.wake()
            await _respond_text(
                interaction,
                self._format_notion_enqueue_result(result),
                ephemeral=True,
            )

    async def close(self) -> None:
        shortcut_task = self._shortcut_dispatch_task
        self._shortcut_dispatch_task = None
        if shortcut_task is not None:
            shortcut_task.cancel()
            await asyncio.gather(shortcut_task, return_exceptions=True)
        if self.reminder_scheduler is not None:
            await self.reminder_scheduler.close()
        if self.rating_scheduler is not None:
            await self.rating_scheduler.close()
        if self.health_scheduler is not None:
            await self.health_scheduler.close()
        if self.rhythm_spending_scheduler is not None:
            await self.rhythm_spending_scheduler.close()
        if self.finance_digest_scheduler is not None:
            await self.finance_digest_scheduler.close()
        if self.notion_sync_worker is not None:
            await self.notion_sync_worker.close()
        await super().close()

    async def _shortcut_dispatch_loop(self) -> None:
        """Move authenticated Shortcut captures into the reviewed Discord Inbox."""

        while not self.is_closed():
            try:
                await self.process_shortcut_inbox_once()
            except Exception as exc:
                logger.warning("shortcut inbox dispatch loop failed (%s)", type(exc).__name__)
            await asyncio.sleep(2)

    async def _general_inbox_source_channel(self) -> Any:
        channel_id = self.settings.general_inbox_channel_id
        if channel_id is None:
            raise RuntimeError("general inbox channel is not configured")
        channel = self.get_channel(channel_id)
        if channel is None:
            fetch_channel = getattr(self, "fetch_channel", None)
            if callable(fetch_channel):
                channel = await fetch_channel(channel_id)
        if not self._is_messageable_text_channel(channel):
            raise RuntimeError("general inbox channel is unavailable")
        guild_id = getattr(getattr(channel, "guild", None), "id", None)
        if (
            self.settings.discord_guild_id is not None
            and guild_id is not None
            and str(guild_id) != str(self.settings.discord_guild_id)
        ):
            raise RuntimeError("general inbox channel is outside the configured guild")
        return channel

    @staticmethod
    def _shortcut_source_content(event: Event) -> str:
        source = (
            event.capture_context.source_label
            if event.capture_context is not None
            else "shortcut"
        )
        location = format_capture_location(event.capture_context)
        parts = [f"【iPhone {source}から】", event.raw_text or "（本文なし）"]
        if location:
            parts.extend(("", location))
        return "\n".join(parts)

    async def process_shortcut_inbox_once(self) -> list[int]:
        """Deliver a bounded set of durable Shortcut outbox rows exactly once."""

        claims = await asyncio.to_thread(self.capture_context.claim_shortcut_deliveries)
        completed: list[int] = []
        for claim in claims:
            try:
                event = await asyncio.to_thread(self.service.get_event, claim.event_id)
                if event is None:
                    raise RuntimeError("shortcut Event is unavailable")
                channel = await self._general_inbox_source_channel()
                delivery = await asyncio.to_thread(
                    self.capture_context.get_shortcut_delivery,
                    event.id,
                )
                source_message = None
                if delivery is not None and delivery.source_message_id:
                    fetch_message = getattr(channel, "fetch_message", None)
                    if callable(fetch_message):
                        try:
                            source_message = await fetch_message(int(delivery.source_message_id))
                        except Exception:
                            source_message = None
                if source_message is None:
                    send_kwargs: dict[str, Any] = {
                        "allowed_mentions": discord.AllowedMentions.none(),
                    }
                    files = self._classified_attachment_files(event)
                    if files:
                        send_kwargs["files"] = files
                    source_message = await channel.send(
                        self._shortcut_source_content(event),
                        **send_kwargs,
                    )
                    await asyncio.to_thread(
                        self.capture_context.record_shortcut_source,
                        event_id=event.id,
                        attempt_count=claim.attempt_count,
                        source_channel_id=getattr(channel, "id", ""),
                        source_message_id=getattr(source_message, "id", ""),
                    )
                archive = await self._archive_general_inbox_message(source_message, event.id)
                if not archive.succeeded:
                    raise RuntimeError("shortcut archive is not durable")
                if not await self._enqueue_notion_after_discord_archive(event.id):
                    raise RuntimeError("shortcut Notion backup queue is not durable")
                candidate_message = await self._show_inbox_classification(
                    source_message,
                    CaptureResult(event_id=event.id, duplicate=True),
                )
                await asyncio.to_thread(
                    self.capture_context.mark_shortcut_posted,
                    event_id=event.id,
                    attempt_count=claim.attempt_count,
                    source_channel_id=getattr(channel, "id", ""),
                    source_message_id=getattr(source_message, "id", ""),
                    candidate_message_id=getattr(candidate_message, "id", None),
                )
                completed.append(event.id)
            except Exception as exc:
                logger.warning(
                    "shortcut inbox delivery failed for event_id=%s (%s)",
                    claim.event_id,
                    type(exc).__name__,
                )
                await asyncio.to_thread(
                    self.capture_context.mark_shortcut_failed,
                    event_id=claim.event_id,
                    attempt_count=claim.attempt_count,
                    error_code=type(exc).__name__,
                    retryable=type(exc).__name__ not in {"Forbidden", "NotFound"},
                )
        return completed

    @staticmethod
    def _is_messageable_text_channel(channel: Any) -> bool:
        if channel is None or not callable(getattr(channel, "send", None)):
            return False
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return True
        channel_type = getattr(channel, "type", None)
        if channel_type is None:
            # Small fakes used by local tests have no Discord channel type.
            return True
        return channel_type in {
            discord.ChannelType.text,
            discord.ChannelType.public_thread,
            discord.ChannelType.private_thread,
            discord.ChannelType.news_thread,
        }

    async def _reminder_destination_channel(self) -> Any:
        """Resolve the reminder channel from cache, then Discord API."""

        channel_id = self.settings.reminder_channel_id
        if channel_id is None:
            raise RuntimeError("reminder destination channel is not configured")
        channel = self.get_channel(channel_id)
        if channel is None:
            fetch_channel = getattr(self, "fetch_channel", None)
            if callable(fetch_channel):
                try:
                    channel = await fetch_channel(channel_id)
                except Exception as exc:
                    logger.warning(
                        "reminder destination lookup failed (%s)",
                        type(exc).__name__,
                    )
                    raise RuntimeError("reminder destination channel is unavailable") from exc
        if not self._is_messageable_text_channel(channel):
            raise RuntimeError("reminder destination channel is unavailable")
        channel_guild_id = getattr(getattr(channel, "guild", None), "id", None)
        configured_guild_id = self.settings.discord_guild_id
        if (
            channel_guild_id is not None
            and configured_guild_id is not None
            and str(channel_guild_id) != str(configured_guild_id)
        ):
            raise RuntimeError("reminder destination channel is unavailable")
        return channel

    async def _send_reminder(self, notification: ReminderNotification) -> None:
        channel = await self._reminder_destination_channel()
        kwargs: dict[str, Any] = {
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if notification.subject_type == "routine" and notification.routine_id is not None:
            kwargs["view"] = RoutineCompletionView(
                self.routines,
                notification.routine_id,
                self.settings,
            )
        await channel.send(notification.content, **kwargs)

    async def _memo_destination_channel(self) -> Any:
        """Resolve the configured memo channel without exposing Discord errors."""

        channel_id = self.settings.memo_channel_id
        if channel_id is None:
            raise RuntimeError("memo destination channel is not configured")

        channel = self.get_channel(channel_id)
        if channel is None:
            fetch_channel = getattr(self, "fetch_channel", None)
            if callable(fetch_channel):
                try:
                    channel = await fetch_channel(channel_id)
                except Exception as exc:
                    logger.warning(
                        "memo destination lookup failed (%s)",
                        type(exc).__name__,
                    )
                    raise RuntimeError("memo destination channel is unavailable") from exc
        if channel is None or not callable(getattr(channel, "send", None)):
            raise RuntimeError("memo destination channel is unavailable")
        return channel

    async def _reply_to_message(
        self,
        message: discord.Message,
        content: str,
        *,
        event_id: int | None = None,
    ) -> bool:
        """Send a native reply and report whether Discord accepted it."""

        reply = getattr(message, "reply", None)
        if not callable(reply):
            raise RuntimeError("native message reply is unavailable")
        kwargs: dict[str, Any] = {
            "allowed_mentions": discord.AllowedMentions.none(),
            "mention_author": False,
        }
        if event_id is not None:
            kwargs["view"] = MemoWebView(self.settings, event_id, bot=self)
        await reply(content, **kwargs)
        return True

    async def _send_memo_reply(
        self,
        message: discord.Message,
        content: str,
        *,
        event_id: int | None = None,
    ) -> bool:
        """Send a safe native reply while keeping response bodies out of logs."""

        try:
            return await self._reply_to_message(message, content, event_id=event_id)
        except Exception as exc:
            logger.warning("memo acknowledgement failed (%s)", type(exc).__name__)
            channel = getattr(message, "channel", None)
            send = getattr(channel, "send", None)
            if not callable(send):
                return False
            fallback_content = (
                f"{content}\n元投稿への返信に失敗したため、元投稿は残しています。"
                if event_id is not None
                else content
            )
            fallback_kwargs: dict[str, Any] = {
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if event_id is not None:
                try:
                    fallback_kwargs["view"] = MemoWebView(
                        self.settings,
                        event_id,
                        bot=self,
                    )
                except Exception as view_exc:
                    logger.warning(
                        "memo fallback link construction failed (%s)",
                        type(view_exc).__name__,
                    )
            try:
                await send(fallback_content, **fallback_kwargs)
            except Exception as fallback_exc:
                logger.warning(
                    "memo fallback acknowledgement failed (%s)",
                    type(fallback_exc).__name__,
                )
            return False

    async def _send_memo_card(self, memo: Any) -> None:
        """Post one durable memo card to #一言メモ with its local Web link."""

        destination = await self._memo_destination_channel()
        view = MemoWebView(self.settings, memo.event_id, bot=self)
        await destination.send(
            f"一言メモ #{memo.event_id}\n{memo.body}\n\nWeb詳細: Bot稼働Mac専用",
            allowed_mentions=discord.AllowedMentions.none(),
            view=view,
        )

    async def _handle_social_draft_message(self, message: discord.Message) -> None:
        """Capture a direct #毎日の投稿 message and show its local choices."""

        captured = await self.service.capture_discord_message_raw(message)
        if captured.duplicate:
            return
        draft = await asyncio.to_thread(
            self.social_drafts.register_event,
            captured.event_id,
        )
        media_note = (
            "保存済み画像あり。"
            if draft.has_media
            else "保存済み画像なし（Instagram／両方は選択後もブロックされます）。"
        )
        response_text = (
            f"Event #{captured.event_id}としてSNS下書きをローカルSQLiteへ保存しました。\n"
            f"{media_note}下のボタンで X・Instagram・両方・保留を選択できます。\n"
            "選択はローカルoutboxへの記録だけで、外部SNSには投稿していません。"
        )
        if captured.attachment_errors:
            response_text += "\n添付の一部は保存できませんでした。動画は現在未対応です。"
        action_channel_id = getattr(getattr(message, "channel", None), "id", None)
        view = SocialDraftView(
            self,
            captured.event_id,
            action_channel_id=action_channel_id,
        )
        response = await message.channel.send(
            response_text,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = response
        try:
            await asyncio.to_thread(
                self.social_drafts.record_action_message,
                captured.event_id,
                channel_id=action_channel_id,
                message_id=getattr(response, "id", None),
            )
        except Exception as exc:
            # The Event and local choice row remain durable.  Without the
            # action mapping, restart restoration may require a new action
            # card, so never turn this into a deletion or external action.
            logger.warning(
                "social draft action mapping failed for event_id=%s (%s)",
                captured.event_id,
                type(exc).__name__,
            )

    async def _send_capture_candidate(
        self,
        message: discord.Message,
        result: Any,
        *,
        extra: str = "",
    ) -> bool:
        event = await asyncio.to_thread(self.service.get_event, result.event_id)
        if event is None:
            return False
        suffix = extra
        if result.attachment_errors:
            suffix += "\n添付の一部を保存できませんでした。原文は保存済みです。"
        if result.analysis_error:
            suffix += "\n解析に失敗したため保留中です。/retry で再試行できます。"
        chunks = _message_chunks(_display_event(event) + suffix)
        for index, chunk in enumerate(chunks):
            await message.channel.send(
                chunk,
                view=EventActionView(self.service, self.guard, event.id)
                if index == 0
                else None,
            )
        return True

    def _is_general_inbox_message(self, message: discord.Message) -> bool:
        """Return whether this message uses the confirmed #一般 intake path."""

        configured_id = self.settings.general_inbox_channel_id
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        return (
            configured_id is not None
            and channel_id is not None
            and str(configured_id) == str(channel_id)
        )

    async def _retry_general_attachments(
        self,
        message: discord.Message,
        event_id: int,
    ) -> list[str]:
        """Retry attachment reads for a duplicate raw Event before archiving.

        ``InboxService.capture_discord_message_raw`` intentionally returns
        early for a duplicate Discord message.  That is correct for normal
        duplicate delivery, but a process restart can otherwise hide an
        attachment that failed after the raw Event was committed.  Re-reading
        the source attachments here lets the existing idempotent attachment
        registrar repair that narrow case without changing the service API.
        """

        incoming: list[IncomingAttachment] = []
        errors: list[str] = []
        maximum = self.settings.attachment_max_bytes
        for attachment in getattr(message, "attachments", []) or []:
            try:
                size = getattr(attachment, "size", 0)
                if isinstance(size, int) and size > maximum:
                    raise ValueError("attachment exceeds configured size limit")
                data = await attachment.read()
                incoming.append(
                    IncomingAttachment(
                        discord_attachment_id=attachment.id,
                        original_filename=attachment.filename,
                        content_type=attachment.content_type or "",
                        data=data,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "duplicate general attachment capture failed for event_id=%s (%s)",
                    event_id,
                    type(exc).__name__,
                )
                errors.append(type(exc).__name__)

        register = getattr(self.service, "_register_attachments", None)
        if incoming and callable(register):
            try:
                errors.extend(
                    await asyncio.to_thread(register, event_id, incoming)
                )
            except Exception as exc:
                logger.warning(
                    "duplicate general attachment registration failed for event_id=%s (%s)",
                    event_id,
                    type(exc).__name__,
                )
                errors.append(type(exc).__name__)
        elif incoming:
            errors.append("AttachmentRegistrationUnavailable")
        return errors

    def _discord_archive_lock(self, event_id: int) -> asyncio.Lock:
        key = str(event_id)
        lock = self._discord_archive_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._discord_archive_locks[key] = lock
        return lock

    @staticmethod
    def _archive_message_ids(record: DiscordArchiveRecord) -> tuple[str, ...]:
        values = getattr(record, "archive_message_ids", None)
        if not isinstance(values, list):
            values = []
        ids = tuple(
            str(value).strip()
            for value in values
            if value is not None and str(value).strip()
        )
        if not ids and getattr(record, "archive_message_id", None):
            return (str(record.archive_message_id),)
        return ids

    def _load_discord_archive_record(
        self,
        event_id: int,
        source_message_id: str,
    ) -> DiscordArchiveRecord | None:
        session = self.service.session_factory()
        try:
            record = session.scalar(
                select(DiscordArchiveRecord).where(
                    DiscordArchiveRecord.event_id == event_id,
                    DiscordArchiveRecord.source_message_id == source_message_id,
                )
            )
            if record is not None:
                # The returned model is detached after this method closes the
                # session, so copy the JSON list before leaving the unit of
                # work.  No relationship or content field is needed here.
                record.archive_message_ids = list(self._archive_message_ids(record))
            return record
        finally:
            session.close()

    def _claim_discord_archive_attempt(
        self,
        *,
        event_id: int,
        source_message_id: str,
        source_channel_id: str,
        destination_channel_id: str,
    ) -> _DiscordArchiveAttempt:
        """Claim one durable archive row without holding a DB transaction over Discord I/O."""

        session = self.service.session_factory()
        try:
            record = session.scalar(
                select(DiscordArchiveRecord).where(DiscordArchiveRecord.event_id == event_id)
            )
            if record is None:
                record = DiscordArchiveRecord(
                    event_id=event_id,
                    source_message_id=source_message_id,
                    source_channel_id=source_channel_id,
                    destination_channel_id=destination_channel_id,
                    status=DiscordArchiveStatus.SENDING.value,
                    attempts=1,
                    last_attempt_at=datetime.now(UTC).replace(tzinfo=None),
                )
                session.add(record)
                try:
                    session.flush()
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    record = session.scalar(
                        select(DiscordArchiveRecord).where(
                            DiscordArchiveRecord.event_id == event_id
                        )
                    )
                    if record is None:
                        raise
                else:
                    return _DiscordArchiveAttempt(send_allowed=True)

            if (
                str(record.source_message_id) != source_message_id
                or str(record.source_channel_id) != source_channel_id
            ):
                raise RuntimeError("archive source mapping does not match")
            if str(record.destination_channel_id) != destination_channel_id:
                raise RuntimeError("archive destination mapping does not match")

            message_ids = self._archive_message_ids(record)
            archive_message_id = (
                str(record.archive_message_id)
                if record.archive_message_id is not None
                else (message_ids[0] if message_ids else None)
            )
            if (
                record.status == DiscordArchiveStatus.SUCCEEDED.value
                and archive_message_id is not None
            ):
                return _DiscordArchiveAttempt(
                    succeeded=True,
                    message_ids=message_ids,
                    archive_message_id=archive_message_id,
                )

            now = datetime.now(UTC).replace(tzinfo=None)
            if record.status == DiscordArchiveStatus.SENDING.value:
                last_attempt = record.last_attempt_at or record.updated_at
                if (
                    last_attempt is not None
                    and (now - last_attempt).total_seconds()
                    < DISCORD_ARCHIVE_ATTEMPT_LEASE_SECONDS
                ):
                    return _DiscordArchiveAttempt(
                        pending=True,
                        message_ids=message_ids,
                        archive_message_id=archive_message_id,
                    )

            record.status = DiscordArchiveStatus.SENDING.value
            record.attempts = int(record.attempts or 0) + 1
            record.last_attempt_at = now
            record.last_error_code = None
            record.last_error_message = None
            session.commit()
            return _DiscordArchiveAttempt(
                send_allowed=True,
                message_ids=message_ids,
                archive_message_id=archive_message_id,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _save_discord_archive_progress(self, event_id: int, message_ids: list[str]) -> None:
        session = self.service.session_factory()
        try:
            record = session.scalar(
                select(DiscordArchiveRecord).where(DiscordArchiveRecord.event_id == event_id)
            )
            if record is None:
                raise RuntimeError("archive mapping is unavailable")
            record.archive_message_ids = list(message_ids)
            record.archive_message_id = message_ids[0] if message_ids else None
            record.status = DiscordArchiveStatus.SENDING.value
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _finish_discord_archive(self, event_id: int, message_ids: list[str]) -> None:
        session = self.service.session_factory()
        try:
            record = session.scalar(
                select(DiscordArchiveRecord).where(DiscordArchiveRecord.event_id == event_id)
            )
            if record is None:
                raise RuntimeError("archive mapping is unavailable")
            record.archive_message_ids = list(message_ids)
            record.archive_message_id = message_ids[0] if message_ids else None
            record.status = DiscordArchiveStatus.SUCCEEDED.value
            record.archived_at = datetime.now(UTC).replace(tzinfo=None)
            record.last_error_code = None
            record.last_error_message = None
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _fail_discord_archive(self, event_id: int, error_code: str) -> None:
        session = self.service.session_factory()
        try:
            record = session.scalar(
                select(DiscordArchiveRecord).where(DiscordArchiveRecord.event_id == event_id)
            )
            if record is None:
                return
            record.status = DiscordArchiveStatus.FAILED.value
            record.last_error_code = str(error_code)[:64] or "ArchiveDeliveryFailed"
            # Keep this stable and content-free.  Provider/Discord exception
            # messages may contain URLs, message text, or credential fragments.
            record.last_error_message = "Discord archive delivery failed"
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _discord_archive_is_succeeded(
        self,
        event_id: int,
        source_message: discord.Message,
    ) -> bool:
        source_message_id = str(getattr(source_message, "id", ""))
        source_channel_id = str(getattr(getattr(source_message, "channel", None), "id", ""))
        if not source_message_id or not source_channel_id:
            return False
        session = self.service.session_factory()
        try:
            record = session.scalar(
                select(DiscordArchiveRecord).where(DiscordArchiveRecord.event_id == event_id)
            )
            return bool(
                record is not None
                and record.status == DiscordArchiveStatus.SUCCEEDED.value
                and str(record.source_message_id) == source_message_id
                and str(record.source_channel_id) == source_channel_id
                and bool(record.archive_message_id or self._archive_message_ids(record))
            )
        finally:
            session.close()

    async def _general_archive_channel(
        self,
        source_message: discord.Message,
        *,
        preferred_channel_id: str | None = None,
    ) -> Any:
        """Resolve the configured archive channel, with a unique exact-name fallback."""

        configured_id = preferred_channel_id or self.settings.general_archive_channel_id
        destination = None
        if configured_id is not None:
            lookup_id: Any = configured_id
            if isinstance(configured_id, str) and configured_id.isdigit():
                lookup_id = int(configured_id)
            destination = self.get_channel(lookup_id)
            if destination is None:
                fetch_channel = getattr(self, "fetch_channel", None)
                if callable(fetch_channel):
                    try:
                        destination = await fetch_channel(lookup_id)
                    except Exception as exc:
                        logger.warning(
                            "general archive channel lookup failed (%s)",
                            type(exc).__name__,
                        )
        else:
            destination = self._named_inbox_channel(
                source_message,
                (self.settings.general_archive_channel_name,),
            )
        if not self._is_messageable_text_channel(destination):
            raise RuntimeError("general archive channel is unavailable")

        configured_guild_id = self.settings.discord_guild_id
        destination_guild_id = getattr(getattr(destination, "guild", None), "id", None)
        if (
            configured_guild_id is not None
            and destination_guild_id is not None
            and str(destination_guild_id) != str(configured_guild_id)
        ):
            raise RuntimeError("general archive channel is unavailable")
        source_channel_id = getattr(getattr(source_message, "channel", None), "id", None)
        destination_channel_id = getattr(destination, "id", None)
        if (
            source_channel_id is not None
            and destination_channel_id is not None
            and str(source_channel_id) == str(destination_channel_id)
        ):
            raise RuntimeError("general archive channel must differ from #一般")
        if destination_channel_id is None or not str(destination_channel_id).strip():
            raise RuntimeError("general archive channel has no stable ID")
        return destination

    def _general_archive_content(self, source_message: discord.Message, event: Event) -> str:
        """Build an archive message while retaining the raw text verbatim."""

        guild_id = getattr(getattr(source_message, "guild", None), "id", None)
        channel_id = getattr(getattr(source_message, "channel", None), "id", None)
        message_id = getattr(source_message, "id", None)
        author_id = getattr(getattr(source_message, "author", None), "id", None)
        created_at = getattr(source_message, "created_at", None) or event.captured_at
        source_reference = self._source_message_reference(source_message)
        raw_text = event.raw_text or ""
        content = (
            "【#一般アーカイブ】\n"
            f"Event #{event.id}\n"
            f"出典: {source_reference}\n"
            f"Guild ID: {guild_id}\n"
            f"元チャンネル ID: {channel_id}\n"
            f"元メッセージ ID: {message_id}\n"
            f"投稿者 ID: {author_id}\n"
            f"投稿日時: {created_at.isoformat() if created_at is not None else 'unknown'}\n\n"
            "原文（改変なし）:\n"
            f"{raw_text if raw_text else '（本文なし）'}"
        )
        location = format_capture_location(event.capture_context)
        if location:
            content += f"\n\n明示共有された現在地:\n{location}"
        return content

    async def _archive_general_inbox_message(
        self,
        source_message: discord.Message,
        event_id: int,
    ) -> DiscordArchiveDelivery:
        """Persist an idempotent Discord archive copy before classification."""

        source_message_id = str(getattr(source_message, "id", ""))
        source_channel_id = str(getattr(getattr(source_message, "channel", None), "id", ""))
        if not source_message_id or not source_channel_id:
            return DiscordArchiveDelivery(
                event_id=event_id,
                status=DiscordArchiveStatus.FAILED.value,
                succeeded=False,
                error_code="ArchiveSourceIdentityMissing",
            )

        async with self._discord_archive_lock(event_id):
            try:
                existing = await asyncio.to_thread(
                    self._load_discord_archive_record,
                    event_id,
                    source_message_id,
                )
                if (
                    existing is not None
                    and existing.status == DiscordArchiveStatus.SUCCEEDED.value
                    and str(existing.source_channel_id) == source_channel_id
                    and (existing.archive_message_id or self._archive_message_ids(existing))
                ):
                    return DiscordArchiveDelivery(
                        event_id=event_id,
                        status=DiscordArchiveStatus.SUCCEEDED.value,
                        succeeded=True,
                        archive_message_id=(
                            existing.archive_message_id
                            or self._archive_message_ids(existing)[0]
                        ),
                    )
                destination = await self._general_archive_channel(
                    source_message,
                    preferred_channel_id=(
                        str(existing.destination_channel_id) if existing is not None else None
                    ),
                )
                destination_channel_id = str(destination.id)
                attempt = await asyncio.to_thread(
                    self._claim_discord_archive_attempt,
                    event_id=event_id,
                    source_message_id=source_message_id,
                    source_channel_id=source_channel_id,
                    destination_channel_id=destination_channel_id,
                )
                if attempt.succeeded:
                    return DiscordArchiveDelivery(
                        event_id=event_id,
                        status=DiscordArchiveStatus.SUCCEEDED.value,
                        succeeded=True,
                        archive_message_id=attempt.archive_message_id,
                    )
                if attempt.pending:
                    return DiscordArchiveDelivery(
                        event_id=event_id,
                        status=DiscordArchiveStatus.SENDING.value,
                        succeeded=False,
                        pending=True,
                        archive_message_id=attempt.archive_message_id,
                        error_code="ArchiveDeliveryInProgress",
                    )

                event = await asyncio.to_thread(self.service.get_event, event_id)
                if event is None:
                    raise RuntimeError("archive Event is unavailable")
                chunks = _archive_message_chunks(
                    self._general_archive_content(source_message, event)
                )
                message_ids = list(attempt.message_ids)
                if len(message_ids) > len(chunks):
                    raise RuntimeError("archive progress does not match source content")
                files: list[discord.File] = []
                try:
                    # Attachments belong to the first archive message.  When a
                    # prior chunk is already persisted, it was already sent;
                    # resume only the remaining text chunks.
                    if not message_ids:
                        files = self._classified_attachment_files(event)
                    for index in range(len(message_ids), len(chunks)):
                        kwargs: dict[str, Any] = {
                            "allowed_mentions": discord.AllowedMentions.none(),
                        }
                        if index == 0 and files:
                            kwargs["files"] = files
                        response = await destination.send(chunks[index], **kwargs)
                        response_id = getattr(response, "id", None)
                        if response_id is None or not str(response_id).strip():
                            raise RuntimeError("archive response did not contain a message ID")
                        message_ids.append(str(response_id).strip())
                        await asyncio.to_thread(
                            self._save_discord_archive_progress,
                            event_id,
                            message_ids,
                        )
                finally:
                    for file in files:
                        close = getattr(file, "close", None)
                        if callable(close):
                            close()
                await asyncio.to_thread(self._finish_discord_archive, event_id, message_ids)
                return DiscordArchiveDelivery(
                    event_id=event_id,
                    status=DiscordArchiveStatus.SUCCEEDED.value,
                    succeeded=True,
                    archive_message_id=message_ids[0] if message_ids else None,
                )
            except Exception as exc:
                error_code = type(exc).__name__
                logger.warning(
                    "general archive delivery failed for event_id=%s (%s)",
                    event_id,
                    error_code,
                )
                try:
                    await asyncio.to_thread(self._fail_discord_archive, event_id, error_code)
                except Exception as persist_exc:
                    logger.warning(
                        "general archive failure state could not be persisted for event_id=%s (%s)",
                        event_id,
                        type(persist_exc).__name__,
                    )
                return DiscordArchiveDelivery(
                    event_id=event_id,
                    status=DiscordArchiveStatus.FAILED.value,
                    succeeded=False,
                    error_code=error_code,
                )

    async def _enqueue_notion_after_discord_archive(self, event_id: int) -> bool:
        """Durably enqueue only policy-eligible records after archive success."""

        if not self.settings.notion_sync_enabled:
            return True
        if event_id in self._auto_notion_enqueued_event_ids:
            return True
        try:
            enqueue_if_eligible = getattr(self.notion_sync, "enqueue_event_if_eligible", None)
            if callable(enqueue_if_eligible):
                result = await asyncio.to_thread(enqueue_if_eligible, event_id)
            else:
                # Keep small injected fakes useful while applying the policy
                # preview when the concrete Notion service is available.
                event = await asyncio.to_thread(self.service.get_event, event_id)
                if event is None:
                    raise ValueError("event not found")
                plan = getattr(self.notion_sync, "plan_event", None)
                if callable(plan):
                    planned = await asyncio.to_thread(plan, event)
                    if not getattr(planned, "eligible", True):
                        return True
                result = await asyncio.to_thread(self.notion_sync.enqueue_event, event_id)
            if result is None or getattr(result, "eligible", True):
                if result is not None:
                    self._auto_notion_enqueued_event_ids.add(event_id)
                    if self.notion_sync_worker is not None:
                        try:
                            self.notion_sync_worker.wake()
                        except Exception as exc:
                            # Durable queueing has already completed; waking is
                            # an optimization and must not block classification.
                            logger.info(
                                "notion worker wake skipped (%s)", type(exc).__name__
                            )
                return True
            return True
        except Exception as exc:
            logger.warning(
                "automatic Notion enqueue failed for event_id=%s (%s)",
                event_id,
                type(exc).__name__,
            )
            return False

    async def _delete_routed_inbox_message(self, message: discord.Message) -> bool:
        """Delete exactly one successful inbox source message when explicitly enabled."""

        if not self.settings.inbox_delete_original_after_route:
            return False
        try:
            await message.delete()
            return True
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            logger.warning(
                "routed inbox source deletion failed (%s)",
                type(exc).__name__,
            )
        except Exception as exc:  # defensive test doubles and future adapters
            logger.error(
                "routed inbox source deletion failed (%s)",
                type(exc).__name__,
                exc_info=False,
            )
        return False

    async def _send_inbox_pending(
        self,
        message: discord.Message,
        captured: CaptureResult,
        reason: str,
    ) -> None:
        suffix = ""
        if captured.attachment_errors:
            suffix = "\n添付の一部を保存できませんでした。原文と保存済み添付は保持しています。"
        await message.channel.send(
            f"Event #{captured.event_id}として原文・添付を保存しました。{reason}{suffix}",
            view=EventActionView(self.service, self.guard, captured.event_id),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _register_inbox_candidate_view(
        self,
        source_message: discord.Message,
        view: InboxCandidateView,
    ) -> None:
        message_id = getattr(source_message, "id", None)
        if message_id is not None:
            self._inbox_candidate_views[str(message_id)] = view

    def _forget_inbox_candidate_view(
        self,
        source_message: discord.Message,
        view: InboxCandidateView,
    ) -> None:
        message_id = getattr(source_message, "id", None)
        if message_id is not None and self._inbox_candidate_views.get(str(message_id)) is view:
            self._inbox_candidate_views.pop(str(message_id), None)

    def _inbox_candidate_route_lock(self, event_id: int) -> asyncio.Lock:
        lock = self._inbox_candidate_route_locks.get(event_id)
        if lock is None:
            lock = asyncio.Lock()
            self._inbox_candidate_route_locks[event_id] = lock
        return lock

    async def _send_inbox_reaction_feedback(
        self,
        payload: discord.RawReactionActionEvent,
        content: str,
    ) -> None:
        """Best-effort private result for a reaction-based candidate choice.

        Reactions have no ephemeral-response API. Prefer the member from the
        gateway payload (which is normally present for a guild reaction), then
        the cached User. Do not fall back to a public source-channel message:
        the result contains routing state intended only for the authorized
        operator, and failure to deliver a DM must never affect the durable
        route or source-message deletion decision.
        """

        if not content:
            return
        actors: list[Any] = []
        member = getattr(payload, "member", None)
        if member is not None:
            actors.extend((member, getattr(member, "user", None)))
        user_id = getattr(payload, "user_id", None)
        if user_id is not None:
            try:
                cached_user = self.get_user(user_id)
            except Exception as exc:
                logger.info(
                    "inbox reaction feedback lookup skipped (%s)", type(exc).__name__
                )
            else:
                actors.append(cached_user)

        seen_ids: set[int] = set()
        for actor in actors:
            if actor is None:
                continue
            actor_id = getattr(actor, "id", None)
            if isinstance(actor_id, int):
                if actor_id in seen_ids:
                    continue
                seen_ids.add(actor_id)
            send = getattr(actor, "send", None)
            if not callable(send):
                continue
            try:
                await send(content)
            except Exception as exc:
                logger.info(
                    "inbox reaction feedback delivery skipped (%s)", type(exc).__name__
                )
            return

    async def _add_inbox_candidate_reactions(
        self,
        source_message: discord.Message,
        classification: InboxClassification,
    ) -> None:
        """Best-effort reaction affordances that never disable the buttons."""

        add_reaction = getattr(source_message, "add_reaction", None)
        if not callable(add_reaction):
            return
        added: set[str] = set()
        for candidate in classification.candidates:
            reaction = inbox_reaction_for_category(candidate.category)
            if reaction is None or reaction in added:
                continue
            added.add(reaction)
            try:
                await add_reaction(reaction)
            except Exception as exc:
                logger.info(
                    "inbox candidate reaction add skipped (%s)",
                    type(exc).__name__,
                )

    @staticmethod
    def _inbox_category_channel_config(
        settings: Settings,
        category: InboxCategory,
    ) -> tuple[int | None, tuple[str, ...]]:
        """Return the existing configured ID and reviewed name fallback."""

        configuration: dict[InboxCategory, tuple[int | None, tuple[str, ...]]] = {
            InboxCategory.EXPENSES: (settings.records_channel_id, ("生活-記録",)),
            InboxCategory.MEALS: (
                settings.meal_channel_id or settings.records_channel_id,
                ("食事", "生活-記録"),
            ),
            InboxCategory.MEMO: (settings.memo_channel_id, ("一言メモ",)),
            InboxCategory.SELF_TALK: (settings.monologue_channel_id, ("独り言",)),
            InboxCategory.DINING_OUT: (settings.dining_out_channel_id, ("外食",)),
            InboxCategory.MEMORIES: (settings.memories_channel_id, ("思い出",)),
            InboxCategory.SOCIAL_DRAFT: (
                settings.social_draft_channel_id,
                ("毎日の投稿",),
            ),
            InboxCategory.TASK: (
                settings.reminder_channel_id,
                ("日課・リマインダー",),
            ),
            InboxCategory.SHOPPING: (None, ("買い物リスト",)),
            InboxCategory.JOB_HUNTING: (None, ("就活情報",)),
            InboxCategory.SAVED_LINK: (None, ("神リンク",)),
            InboxCategory.RHYTHM_SPENDING: (
                settings.discord_credit_channel_id,
                ("音ゲー利用額", "音ゲー-利用額"),
            ),
            InboxCategory.RHYTHM_PROGRESS: (
                settings.progress_channel_id or settings.rhythm_channel_id,
                ("今日の進捗", "音ゲー"),
            ),
            InboxCategory.FINANCE_ACCOUNT: (settings.account_channel_id, ("口座",)),
            InboxCategory.FINANCE_CARD: (settings.card_channel_id, ("クレカ",)),
        }
        try:
            configured_id, names = configuration[category]
        except KeyError as exc:
            raise ValueError("unknown inbox destination category") from exc
        return configured_id, names

    def _named_inbox_channel(
        self,
        source_message: discord.Message,
        names: tuple[str, ...],
    ) -> Any | None:
        """Find only an exact reviewed channel name in the source guild."""

        source_guild = getattr(source_message, "guild", None)
        source_guild_id = getattr(source_guild, "id", None)
        channels = list(getattr(source_guild, "text_channels", []) or [])
        get_all_channels = getattr(self, "get_all_channels", None)
        if callable(get_all_channels):
            channels.extend(
                channel
                for channel in get_all_channels()
                if getattr(getattr(channel, "guild", None), "id", source_guild_id)
                == source_guild_id
            )
        seen: set[str] = set()
        matches: list[Any] = []
        for channel in channels:
            channel_id = getattr(channel, "id", None)
            if channel_id is not None:
                normalized_id = str(channel_id)
                if normalized_id in seen:
                    continue
                seen.add(normalized_id)
            if (
                getattr(channel, "name", None) in names
                and self._is_messageable_text_channel(channel)
            ):
                matches.append(channel)
        return matches[0] if len(matches) == 1 else None

    async def _inbox_destination_channel(
        self,
        source_message: discord.Message,
        category: InboxCategory,
    ) -> Any:
        configured_id, names = self._inbox_category_channel_config(self.settings, category)
        destination = None
        if configured_id is not None:
            destination = self.get_channel(configured_id)
            if destination is None:
                fetch_channel = getattr(self, "fetch_channel", None)
                if callable(fetch_channel):
                    try:
                        destination = await fetch_channel(configured_id)
                    except Exception as exc:
                        logger.warning(
                            "inbox destination lookup failed (%s)",
                            type(exc).__name__,
                        )
        if destination is None:
            destination = self._named_inbox_channel(source_message, names)
        if not self._is_messageable_text_channel(destination):
            raise RuntimeError("inbox destination channel is unavailable")

        configured_guild_id = self.settings.discord_guild_id
        destination_guild_id = getattr(getattr(destination, "guild", None), "id", None)
        if (
            configured_guild_id is not None
            and destination_guild_id is not None
            and str(destination_guild_id) != str(configured_guild_id)
        ):
            raise RuntimeError("inbox destination channel is unavailable")
        source_channel_id = getattr(getattr(source_message, "channel", None), "id", None)
        destination_channel_id = getattr(destination, "id", None)
        if (
            source_channel_id is not None
            and destination_channel_id is not None
            and str(source_channel_id) == str(destination_channel_id)
        ):
            raise RuntimeError("inbox destination must differ from the catch-all channel")
        return destination

    @staticmethod
    def _source_message_reference(message: discord.Message) -> str:
        guild_id = getattr(getattr(message, "guild", None), "id", None)
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        message_id = getattr(message, "id", None)
        values = (guild_id, channel_id, message_id)
        if all(str(value).isdigit() and int(value) > 0 for value in values):
            return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
        return f"Discord message ID: {message_id}"

    def _classified_attachment_files(self, event: Any) -> list[discord.File]:
        """Open only attachments previously stored under DATA_DIR/attachments."""

        attachments = list(getattr(event, "attachments", []) or [])
        if not attachments:
            return []
        attachment_root = (Path(self.settings.data_dir) / "attachments").resolve()
        files: list[discord.File] = []
        try:
            for attachment in attachments:
                path = Path(attachment.local_path).resolve()
                path.relative_to(attachment_root)
                if not path.is_file():
                    raise OSError("stored attachment is unavailable")
                filename = Path(attachment.original_filename).name or "attachment"
                files.append(discord.File(str(path), filename=filename[:255]))
        except Exception:
            for file in files:
                close = getattr(file, "close", None)
                if callable(close):
                    close()
            raise RuntimeError("stored inbox attachment is unavailable")
        return files

    async def _persist_classified_task(
        self,
        event: Event,
        route: Any,
    ) -> TaskView:
        """Persist or reuse one candidate-selected manual task.

        ``TaskService.create_manual`` predates the Event-backed inbox path and
        therefore has no source-event foreign key.  Keep the task ID in this
        Bot process so a destination-send retry cannot create a second manual
        task.  A task whose Event was already marked as routed is also checked
        by its validated title/deadline after a restart; ambiguous matches fail
        closed instead of guessing.
        """

        due_date = route.task_due_date
        due_at = None
        if route.task_due_date is not None and route.task_due_time is not None:
            due_at = datetime.combine(route.task_due_date, route.task_due_time)
            due_date = None

        def matches(task: TaskView) -> bool:
            if task.source != "manual" or task.title != route.task_title:
                return False
            if route.task_due_date is None:
                return task.due_at is None
            if task.due_at is None:
                return False
            local_due = local_datetime(task.due_at, self.settings.timezone)
            if local_due.date() != route.task_due_date:
                return False
            if route.task_due_time is None:
                return local_due.hour == 0 and local_due.minute == 0
            return (
                local_due.hour == route.task_due_time.hour
                and local_due.minute == route.task_due_time.minute
            )

        task_id = self._inbox_candidate_task_ids.get(event.id)
        if task_id is not None:
            task = await asyncio.to_thread(self.tasks.get, task_id)
            if task is not None and matches(task):
                if event.event_type != "life_task":
                    await asyncio.to_thread(
                        self.service.mark_event_routed,
                        event.id,
                        "life_task",
                    )
                return task
            self._inbox_candidate_task_ids.pop(event.id, None)

        if event.event_type == "life_task":
            matches_in_db = [
                task
                for task in await asyncio.to_thread(
                    self.tasks.list_tasks,
                    include_completed=True,
                )
                if matches(task)
            ]
            if len(matches_in_db) != 1:
                raise ValueError("routed life task could not be resolved safely")
            task = matches_in_db[0]
            self._inbox_candidate_task_ids[event.id] = task.id
            return task
        if event.event_type != "unknown":
            raise ValueError("event already has a different routed destination")

        task = await asyncio.to_thread(
            self.tasks.create_manual,
            route.task_title,
            due_date=due_date,
            due_at=due_at,
        )
        self._inbox_candidate_task_ids[event.id] = task.id
        # Keep the Event transition after the domain row exists.  If this
        # second local write fails, the source remains retryable and the
        # in-process task association above makes the next attempt idempotent.
        await asyncio.to_thread(
            self.service.mark_event_routed,
            event.id,
            "life_task",
        )
        return task

    async def _persist_classified_candidate(
        self,
        source_message: discord.Message,
        event: Event,
        category: InboxCategory,
    ) -> tuple[Event, Any] | None:
        """Run the existing domain write for a reviewed classifier choice."""

        if category in {InboxCategory.EXPENSES, InboxCategory.MEALS}:
            if category is InboxCategory.MEALS:
                analyzed = await asyncio.to_thread(
                    self.service.analyze_event,
                    event.id,
                    meal_intent=True,
                )
            else:
                analyzed = await asyncio.to_thread(self.service.analyze_event, event.id)
            if getattr(analyzed, "_analysis_failed", False):
                raise RuntimeError("classified event analysis did not complete")
            persisted = await asyncio.to_thread(self.service.get_event, event.id)
            if persisted is None:
                raise RuntimeError("classified event is unavailable after analysis")
            records = persisted.meals if category is InboxCategory.MEALS else persisted.expenses
            if not records:
                raise ValueError("classified input did not produce the selected domain record")
            return persisted, persisted

        if category is InboxCategory.MEMO:
            memo = await asyncio.to_thread(
                self.finance.record_memo,
                event.id,
                event.raw_text,
            )
            persisted = await asyncio.to_thread(self.service.get_event, event.id)
            if persisted is None:
                raise RuntimeError("classified memo Event is unavailable after save")
            return persisted, memo

        if category in {InboxCategory.TASK, InboxCategory.JOB_HUNTING}:
            expected_destination = (
                "life_task" if category is InboxCategory.TASK else "career"
            )
            outcome = await asyncio.to_thread(
                self.service.route_inbox_event,
                event.id,
            )
            route = outcome.route
            if category is InboxCategory.TASK and (
                outcome.routing_failed or route.destination != expected_destination
            ):
                # The classifier choice has already been explicitly reviewed by
                # the allowed Discord user.  A second model/rule pass may still
                # be unable to derive optional due-date fields from free-form
                # prose; that must not discard the user's explicit TASK choice.
                # Keep the fallback intentionally narrow: create a local,
                # deadline-free task from the durable raw Event and do not
                # apply it to career records or any other structured domain.
                task_title = " ".join((event.raw_text or "").split())[:255]
                if not task_title:
                    task_title = f"Discord Event #{event.id}"
                route = InboxRouteResult(
                    destination="life_task",
                    task_title=task_title,
                )
            elif outcome.routing_failed or route.destination != expected_destination:
                raise ValueError("classifier choice did not produce a safe structured route")
            if category is InboxCategory.TASK:
                record = await self._persist_classified_task(event, route)
            else:
                record = await asyncio.to_thread(
                    self.career.create_from_route,
                    event.id,
                    route,
                )
            persisted = await asyncio.to_thread(self.service.get_event, event.id)
            if persisted is None:
                raise RuntimeError("classified routed Event is unavailable after save")
            return persisted, record

        if category is InboxCategory.RHYTHM_PROGRESS:
            reference = getattr(source_message, "reference", None)
            reference_id = getattr(reference, "message_id", None)
            linked_todo = None
            if reference_id is not None:
                linked_todo = await asyncio.to_thread(
                    self.rhythm.get_todo_by_event_message_id,
                    reference_id,
                )
            instruction = self._progress_instruction(event.raw_text, linked_todo)
            if instruction.kind == RhythmInstructionKind.TODO:
                record = await asyncio.to_thread(
                    self.rhythm.create_todo,
                    event.id,
                    instruction.game,
                    instruction.text,
                )
            else:
                record = await asyncio.to_thread(
                    self.rhythm.record_result,
                    event.id,
                    instruction.game,
                    instruction.text or None,
                    linked_todo.id if linked_todo is not None else None,
                )
            persisted = await asyncio.to_thread(self.service.get_event, event.id)
            if persisted is None:
                raise RuntimeError("classified rhythm Event is unavailable after save")
            return persisted, record

        if category is InboxCategory.RHYTHM_SPENDING:
            captured = await asyncio.to_thread(
                self.rhythm_spending_capture.record_event,
                event.id,
                display_name=self.settings.rhythm_player_display_name,
                time_zone=self.settings.rhythm_time_zone,
                allowed_discord_user_id=self.settings.allowed_user_id,
            )
            persisted = await asyncio.to_thread(self.service.get_event, event.id)
            if persisted is None:
                raise RuntimeError("classified rhythm spending Event is unavailable after save")
            return persisted, captured.spending

        return None

    def _classified_structured_content(
        self,
        source_message: discord.Message,
        event: Event,
        category: InboxCategory,
        record: Any,
    ) -> tuple[str, discord.ui.View]:
        """Format a structured candidate result and its existing local actions."""

        if category is InboxCategory.MEMO:
            return (
                f"一言メモ #{record.event_id}\n"
                f"{record.body}\n\nWeb詳細: Bot稼働Mac専用",
                MemoWebView(self.settings, record.event_id, bot=self),
            )

        if category in {InboxCategory.EXPENSES, InboxCategory.MEALS}:
            body = _display_event(event)
        elif category is InboxCategory.TASK:
            if record.due_at is None:
                due_label = "期限なし"
            else:
                local_due = local_datetime(record.due_at, self.settings.timezone)
                due_label = (
                    local_due.strftime("%Y-%m-%d")
                    if local_due.hour == 0
                    and local_due.minute == 0
                    and local_due.second == 0
                    else local_due.strftime("%Y-%m-%d %H:%M")
                )
            body = f"タスク #{record.id} を保存しました: {record.title} / 期限: {due_label}"
        elif category is InboxCategory.JOB_HUNTING:
            kind_label = CAREER_KIND_LABELS.get(record.kind, record.kind)
            status_label = CAREER_STATUS_LABELS.get(record.status, record.status)
            stage_label = (
                f" / 段階: {CAREER_STAGE_LABELS.get(record.stage, record.stage)}"
                if record.stage
                else ""
            )
            body = (
                f"就活記録 #{record.id} を保存しました: {record.company} / {record.title}"
                f" / 種別: {kind_label} / 状態: {status_label}{stage_label}"
                f" / 期限: {_career_deadline_label(record, timezone_name=self.settings.timezone)}"
            )
            if record.linked_task_id is not None:
                body += f" / LifeTask #{record.linked_task_id}"
        elif category is InboxCategory.RHYTHM_PROGRESS:
            game_label = RHYTHM_GAME_LABELS.get(record.game, record.game.value)
            if isinstance(record, RhythmTodoResult):
                body = f"TODO #{record.id} を保存しました: {game_label} / {record.title}"
            elif record.todo_id is None:
                body = f"リザルト #{record.id} を保存しました: {game_label}"
            else:
                body = (
                    f"リザルト #{record.id} を保存し、TODO #{record.todo_id}を完了しました: "
                    f"{game_label}"
                )
        else:
            raise ValueError("unsupported structured classifier category")

        view: discord.ui.View
        if category in {InboxCategory.EXPENSES, InboxCategory.MEALS}:
            # Choosing a destination confirms the kind, not every extracted
            # amount/merchant/meal field. Keep the existing review actions so
            # the structured row cannot become an invisible permanent pending
            # record after the source inbox message is removed.
            view = EventActionView(self.service, self.guard, event.id)
        else:
            view = NotionPromotionView(self, event.id)

        source_label = (
            "#一般" if self._is_general_inbox_message(source_message) else "#なんでも入力"
        )
        content = (
            f"【{source_label}からの構造化保存】\n"
            f"Event #{event.id}\n"
            f"元投稿: {self._source_message_reference(source_message)}\n\n"
            f"{body}\n\n原文:\n{event.raw_text or '（本文なし）'}"
        )
        location = format_capture_location(event.capture_context)
        if location:
            content += f"\n\n{location}"
        return content, view

    async def _send_classified_copy(
        self,
        destination: Any,
        event: Event,
        content: str,
        view: discord.ui.View,
    ) -> Any | None:
        """Send one structured candidate card after its local write."""

        files = self._classified_attachment_files(event)
        try:
            chunks = _message_chunks(content)
            first_response = None
            for index, chunk in enumerate(chunks):
                kwargs: dict[str, Any] = {
                    "allowed_mentions": discord.AllowedMentions.none(),
                }
                if index == 0 and files:
                    kwargs["files"] = files
                if index == 0:
                    kwargs["view"] = view
                response = await destination.send(chunk, **kwargs)
                if first_response is None:
                    first_response = response
            return first_response
        finally:
            for file in files:
                close = getattr(file, "close", None)
                if callable(close):
                    close()

    async def _deliver_classified_rhythm_spending(
        self,
        *,
        source_message: discord.Message,
        event: Event,
        record: Any,
        destination: Any,
        is_general_inbox: bool,
    ) -> tuple[str, bool]:
        """Publish a reviewed yen record through the existing idempotent publisher."""

        if (
            not self.settings.rhythm_spending_immediate_post_enabled
            or self.settings.rhythm_spending_post_mode == "dailyOnly"
        ):
            raise RuntimeError("immediate rhythm spending publication is disabled")
        destination_channel_id = getattr(destination, "id", None)
        if destination_channel_id is None:
            raise RuntimeError("rhythm spending destination has no stable ID")
        channel_id = int(destination_channel_id)

        if is_general_inbox and not await self._enqueue_notion_after_discord_archive(
            event.id
        ):
            raise RuntimeError("Notion enqueue was not durable")

        if self.settings.rhythm_spending_post_mode == "updateDailyMessage":
            totals = await asyncio.to_thread(
                self.rhythm_spending_store.daily_totals,
                rhythm_user_id=record.rhythm_user_id,
                local_date=record.local_date,
                time_zone=record.time_zone,
            )
            outcome = await self.rhythm_spending_publisher.publish_daily_live(
                totals=totals,
                channel_id=channel_id,
            )
            success = bool(
                outcome.message_id
                and (outcome.posted or outcome.edited or outcome.duplicate)
                and not outcome.dry_run
                and not outcome.in_progress
                and not outcome.uncertain
            )
        else:
            outcome = await self.rhythm_spending_publisher.publish_event(
                rhythm_user_id=record.rhythm_user_id,
                event_id=record.event_id,
                channel_id=channel_id,
            )
            success = bool(
                outcome is not None
                and outcome.message_id
                and (outcome.posted or outcome.duplicate)
                and not outcome.dry_run
                and outcome.error_code is None
            )
        if not success:
            raise RuntimeError("rhythm spending Discord publication did not complete")

        try:
            await asyncio.to_thread(
                self.capture_context.record_route,
                event_id=event.id,
                category=InboxCategory.RHYTHM_SPENDING.value,
                destination_channel_id=channel_id,
                destination_message_id=outcome.message_id,
            )
        except Exception as exc:
            logger.warning(
                "route metadata save failed for event_id=%s (%s)",
                event.id,
                type(exc).__name__,
            )

        self._auto_notion_enqueued_event_ids.discard(event.id)
        if not await self._enqueue_notion_after_discord_archive(event.id):
            logger.warning(
                "routed Notion backup refresh failed for event_id=%s",
                event.id,
            )
        if is_general_inbox and not await asyncio.to_thread(
            self._discord_archive_is_succeeded,
            event.id,
            source_message,
        ):
            raise RuntimeError("general inbox archive is not durable")
        source_deleted = await self._delete_routed_inbox_message(source_message)
        return (
            getattr(destination, "name", None)
            or CATEGORY_LABELS[InboxCategory.RHYTHM_SPENDING],
            source_deleted,
        )

    async def _route_classified_inbox(
        self,
        source_message: discord.Message,
        event_id: int,
        category: InboxCategory,
    ) -> tuple[str, bool]:
        """Route one candidate at most once for this running Bot process."""

        async with self._inbox_candidate_route_lock(event_id):
            previous = self._inbox_candidate_route_results.get(event_id)
            if previous is not None:
                previous_category, destination_name, source_deleted = previous
                if previous_category is not category:
                    raise _InboxCandidateAlreadyRouted
                return destination_name, source_deleted

            result = await self._route_classified_inbox_once(
                source_message,
                event_id,
                category,
            )
            self._inbox_candidate_route_results[event_id] = (
                category,
                result[0],
                result[1],
            )
            return result

    async def _route_classified_inbox_once(
        self,
        source_message: discord.Message,
        event_id: int,
        category: InboxCategory,
    ) -> tuple[str, bool]:
        """Copy one durable Event to a reviewed channel before deleting source."""

        is_general_inbox = self._is_general_inbox_message(source_message)
        if is_general_inbox and not await asyncio.to_thread(
            self._discord_archive_is_succeeded,
            event_id,
            source_message,
        ):
            raise RuntimeError("general inbox archive is not durable")

        is_social_draft = category is InboxCategory.SOCIAL_DRAFT
        if is_social_draft:
            # Register before the destination send so a local outbox row still
            # exists when a Discord copy fails and the source remains retryable.
            await asyncio.to_thread(self.social_drafts.register_event, event_id)
        destination = await self._inbox_destination_channel(source_message, category)
        event = await asyncio.to_thread(self.service.get_event, event_id)
        if event is None:
            raise RuntimeError("inbox event is unavailable")

        structured = await self._persist_classified_candidate(
            source_message,
            event,
            category,
        )
        if structured is not None:
            persisted_event, record = structured
            if category is InboxCategory.RHYTHM_SPENDING:
                return await self._deliver_classified_rhythm_spending(
                    source_message=source_message,
                    event=persisted_event,
                    record=record,
                    destination=destination,
                    is_general_inbox=is_general_inbox,
                )
            content, view = self._classified_structured_content(
                source_message,
                persisted_event,
                category,
                record,
            )
            if is_general_inbox and not await self._enqueue_notion_after_discord_archive(
                event_id
            ):
                raise RuntimeError("Notion enqueue was not durable")
            response = await self._send_classified_copy(
                destination,
                persisted_event,
                content,
                view,
            )
            try:
                await asyncio.to_thread(
                    self.capture_context.record_route,
                    event_id=event_id,
                    category=category.value,
                    destination_channel_id=getattr(destination, "id", ""),
                    destination_message_id=getattr(response, "id", ""),
                )
            except Exception as exc:
                # The destination copy is already durable. Secondary metadata
                # must not turn that success into a retry and duplicate send.
                logger.warning(
                    "route metadata save failed for event_id=%s (%s)",
                    event_id,
                    type(exc).__name__,
                )
            self._auto_notion_enqueued_event_ids.discard(event_id)
            if not await self._enqueue_notion_after_discord_archive(event_id):
                logger.warning("routed Notion backup refresh failed for event_id=%s", event_id)
            if is_general_inbox and not await asyncio.to_thread(
                self._discord_archive_is_succeeded,
                event_id,
                source_message,
            ):
                raise RuntimeError("general inbox archive is not durable")
            source_deleted = await self._delete_routed_inbox_message(source_message)
            return (
                getattr(destination, "name", None) or CATEGORY_LABELS[category],
                source_deleted,
            )

        # A non-structured candidate is still a reviewed record. Persist its
        # category and confirmation state so Notion can index the Discord
        # content/time as a database row instead of leaving it as unknown.
        event = await asyncio.to_thread(
            self.service.mark_event_classified,
            event_id,
            category.value,
        )

        files = self._classified_attachment_files(event)
        raw_text = (event.raw_text or "").strip() or "（本文なし）"
        if is_social_draft:
            content = (
                f"【{'#一般' if is_general_inbox else '#なんでも入力'}からのSNS下書き】\n"
                f"Event #{event.id}\n"
                f"元投稿: {self._source_message_reference(source_message)}\n\n"
                f"{raw_text}\n\n"
                "ローカルoutboxに保存しました。下のボタンで X・Instagram・"
                "両方・保留を選択できます。"
                "外部SNSには投稿していません。"
            )
        else:
            content = (
                f"【{'#一般' if is_general_inbox else '#なんでも入力'}からの振り分け】\n"
                f"Event #{event.id}\n"
                f"元投稿: {self._source_message_reference(source_message)}\n\n"
                f"{raw_text}"
            )
        location = format_capture_location(event.capture_context)
        if location:
            content += f"\n\n{location}"
        chunks = _message_chunks(content)
        social_view = (
            SocialDraftView(
                self,
                event.id,
                action_channel_id=getattr(destination, "id", None),
            )
            if is_social_draft
            else None
        )
        if is_general_inbox and not await self._enqueue_notion_after_discord_archive(
            event_id
        ):
            raise RuntimeError("Notion enqueue was not durable")
        first_response = None
        try:
            for index, chunk in enumerate(chunks):
                kwargs: dict[str, Any] = {
                    "allowed_mentions": discord.AllowedMentions.none(),
                }
                if index == 0 and files:
                    kwargs["files"] = files
                if index == 0:
                    kwargs["view"] = social_view or NotionPromotionView(self, event.id)
                response = await destination.send(chunk, **kwargs)
                if first_response is None:
                    first_response = response
                if index == 0 and social_view is not None:
                    social_view.message = response
                    await asyncio.to_thread(
                        self.social_drafts.record_action_message,
                        event.id,
                        channel_id=getattr(destination, "id", None),
                        message_id=getattr(response, "id", None),
                    )
        finally:
            for file in files:
                close = getattr(file, "close", None)
                if callable(close):
                    close()
        try:
            await asyncio.to_thread(
                self.capture_context.record_route,
                event_id=event_id,
                category=category.value,
                destination_channel_id=getattr(destination, "id", ""),
                destination_message_id=getattr(first_response, "id", ""),
            )
        except Exception as exc:
            logger.warning(
                "route metadata save failed for event_id=%s (%s)",
                event_id,
                type(exc).__name__,
            )
        self._auto_notion_enqueued_event_ids.discard(event_id)
        if not await self._enqueue_notion_after_discord_archive(event_id):
            logger.warning("routed Notion backup refresh failed for event_id=%s", event_id)
        if is_general_inbox and not await asyncio.to_thread(
            self._discord_archive_is_succeeded,
            event_id,
            source_message,
        ):
            raise RuntimeError("general inbox archive is not durable")
        source_deleted = await self._delete_routed_inbox_message(source_message)
        return (
            getattr(destination, "name", None) or CATEGORY_LABELS[category],
            source_deleted,
        )

    async def _send_inbox_classification(
        self,
        message: discord.Message,
        captured: CaptureResult,
        classification: InboxClassification,
    ) -> Any | None:
        if classification.is_unknown:
            await self._send_inbox_pending(
                message,
                captured,
                "用途を安全に判定できなかったため保留中です。"
                "候補を明示して再投稿するか、生活マイページで確認してください。",
            )
            return None
        labels = " / ".join(
            (
                f"{reaction} {CATEGORY_LABELS[candidate.category]}"
                if (reaction := inbox_reaction_for_category(candidate.category))
                else CATEGORY_LABELS[candidate.category]
            )
            for candidate in classification.candidates
            if candidate.category is not InboxCategory.UNKNOWN
        )
        source_note = (
            "ローカルの決定的な候補です。"
            if classification.source != "openai"
            else "候補を確認して選択してください。"
        )
        view = InboxCandidateView(self, message, captured.event_id, classification)
        response = await message.channel.send(
            f"Event #{captured.event_id}として原文・添付を保存しました。\n"
            f"候補: {labels}\n{source_note}\n"
            "送信先へのコピー成功後にだけ、設定に応じて元投稿を削除します。",
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        # discord.py does not expose the sent message on every test double;
        # retain it where available so a successful choice can disable buttons.
        view.message = response
        self._register_inbox_candidate_view(message, view)
        await self._add_inbox_candidate_reactions(message, classification)
        return response

    def _classifier_images_for_event(self, event: Event) -> tuple[InboxImageInput, ...]:
        """Resolve only stored image attachments under the private data root."""

        attachment_root = (Path(self.settings.data_dir) / "attachments").resolve()
        images: list[InboxImageInput] = []
        for attachment in list(event.attachments or [])[:4]:
            content_type = (attachment.content_type or "").casefold().split(";", 1)[0]
            if content_type not in SUPPORTED_IMAGE_TYPES:
                continue
            try:
                path = Path(attachment.local_path).resolve()
                path.relative_to(attachment_root)
            except (OSError, ValueError):
                continue
            if path.is_file():
                images.append(InboxImageInput(path=path, content_type=content_type))
        return tuple(images)

    async def _route_planner_image(
        self,
        source_message: discord.Message,
        event: Event,
        classification: InboxClassification,
    ) -> Any:
        """Copy high-confidence handwritten items to their reviewed channels."""

        rows = await asyncio.to_thread(
            self.capture_context.upsert_planner_items,
            event.id,
            classification.planner_items,
        )
        if not rows:
            raise RuntimeError("planner image did not contain a durable item")

        location = format_capture_location(event.capture_context)
        posted = 0
        destinations: set[str] = set()
        for row in rows:
            if row.status == "posted":
                posted += 1
                if row.destination_channel_id:
                    destinations.add(row.destination_channel_id)
                continue
            try:
                category = InboxCategory(row.category)
                if category is InboxCategory.UNKNOWN or row.confidence < 0.70:
                    raise ValueError("planner item requires manual category review")
                destination = await self._inbox_destination_channel(source_message, category)
                content = (
                    "【手帳画像からLunaが読み取った記録】\n"
                    f"Event #{event.id} / 項目 {row.item_index + 1}\n"
                    f"分類: {CATEGORY_LABELS[category]}\n"
                    f"元投稿: {self._source_message_reference(source_message)}\n\n"
                    f"{row.raw_text}"
                )
                if location:
                    content += f"\n\n{location}"
                files = self._classified_attachment_files(event)
                try:
                    response = await destination.send(
                        content,
                        files=files,
                        view=NotionPromotionView(self, event.id),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                finally:
                    for file in files:
                        close = getattr(file, "close", None)
                        if callable(close):
                            close()
                await asyncio.to_thread(
                    self.capture_context.mark_planner_item_posted,
                    item_id=row.id,
                    channel_id=getattr(destination, "id", ""),
                    message_id=getattr(response, "id", ""),
                )
                posted += 1
                destinations.add(str(getattr(destination, "id", "")))
            except Exception as exc:
                await asyncio.to_thread(
                    self.capture_context.mark_planner_item_failed,
                    item_id=row.id,
                    error_code=type(exc).__name__,
                )
                logger.warning(
                    "planner item delivery failed for event_id=%s item=%s (%s)",
                    event.id,
                    row.item_index,
                    type(exc).__name__,
                )

        if posted != len(rows):
            return await source_message.channel.send(
                f"Event #{event.id}の手帳画像は{len(rows)}項目を読み取り、"
                f"{posted}項目を保存しました。曖昧または送信失敗の項目があるため、"
                "元投稿は残しています。",
                allowed_mentions=discord.AllowedMentions.none(),
            )

        # The original was already archived before image analysis. Refresh
        # the secondary Notion backup after the extracted items are durable.
        self._auto_notion_enqueued_event_ids.discard(event.id)
        if not await self._enqueue_notion_after_discord_archive(event.id):
            # Every extracted Discord copy is already durable. Notion is a
            # secondary database/archive and can be retried independently.
            logger.warning("planner Notion backup refresh failed for event_id=%s", event.id)
        source_deleted = await self._delete_routed_inbox_message(source_message)
        response = await source_message.channel.send(
            f"Event #{event.id}の手帳画像をLunaで{len(rows)}項目に分け、"
            f"{len(destinations)}個の用途別チャンネルへ保存しました。"
            + (
                "元投稿を削除しました。"
                if source_deleted
                else "元投稿は設定または権限により保持しています。"
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            await asyncio.to_thread(
                self.capture_context.record_route,
                event_id=event.id,
                category="planner_multi",
                destination_channel_id=getattr(source_message.channel, "id", ""),
                destination_message_id=getattr(response, "id", ""),
            )
        except Exception as exc:
            logger.warning(
                "planner route metadata save failed for event_id=%s (%s)",
                event.id,
                type(exc).__name__,
            )
        return response

    async def _show_inbox_classification(
        self,
        message: discord.Message,
        captured: CaptureResult,
        *,
        force_local: bool = False,
    ) -> Any | None:
        """Classify one already archived Event and show its candidate card."""

        classifier = self.inbox_classifier
        event = await asyncio.to_thread(self.service.get_event, captured.event_id)
        if event is None:
            raise RuntimeError("inbox Event is unavailable for classification")
        classifier_text = event.raw_text or message.content or ""
        images = self._classifier_images_for_event(event)
        if classifier is None:
            if not force_local:
                raise RuntimeError("inbox classifier is not configured")
            classification = InboxClassifier.local(
                classifier_text,
                attachment_count=len(getattr(message, "attachments", []) or []),
            )
        else:
            try:
                classification = await asyncio.to_thread(
                    classifier.classify,
                    classifier_text,
                    attachment_count=max(
                        len(getattr(message, "attachments", []) or []),
                        len(event.attachments or []),
                    ),
                    images=images,
                )
                if not isinstance(classification, InboxClassification):
                    raise TypeError("inbox classifier returned an invalid result")
            except Exception as exc:
                # The raw Event and Discord archive are already durable. An
                # unexpected classifier failure is deliberately converted to
                # a retryable unknown.
                logger.warning(
                    "inbox classifier failed for event_id=%s (%s)",
                    captured.event_id,
                    type(exc).__name__,
                )
                classification = InboxClassification(
                    candidates=(InboxCandidate(category=InboxCategory.UNKNOWN, confidence=1.0),),
                    source="classifier_error",
                    fallback_reason=type(exc).__name__,
                )
        if (
            classification.source == "openai"
            and classification.document_kind == "planner"
            and classification.planner_items
            and all(
                item.candidates
                and item.candidates[0].category is not InboxCategory.UNKNOWN
                and item.candidates[0].confidence >= 0.70
                for item in classification.planner_items
            )
        ):
            return await self._route_planner_image(message, event, classification)
        return await self._send_inbox_classification(message, captured, classification)

    async def _handle_inbox_message(self, message: discord.Message) -> None:
        """Capture the configured free-form Inbox raw-first, then route it."""

        captured = await self.service.capture_discord_message_raw(message)
        if self._is_general_inbox_message(message):
            attachment_errors = list(captured.attachment_errors)
            if captured.duplicate and not attachment_errors:
                existing = await asyncio.to_thread(
                    self._load_discord_archive_record,
                    captured.event_id,
                    str(getattr(message, "id", "")),
                )
                archive_succeeded = bool(
                    existing is not None
                    and existing.status == DiscordArchiveStatus.SUCCEEDED.value
                    and (existing.archive_message_id or self._archive_message_ids(existing))
                )
                if not archive_succeeded:
                    attachment_errors = await self._retry_general_attachments(
                        message,
                        captured.event_id,
                    )
            if attachment_errors:
                await self._send_inbox_pending(
                    message,
                    CaptureResult(
                        event_id=captured.event_id,
                        attachment_errors=attachment_errors,
                    ),
                    "添付の一部を完全に保存できなかったため、アーカイブと分類処理は実行せず保留中です。"
                    "内容を確認して新しいメッセージとして再投稿してください。",
                )
                return

            archive = await self._archive_general_inbox_message(
                message,
                captured.event_id,
            )
            if not archive.succeeded:
                reason = (
                    "Discordアーカイブ処理が別の実行中です。"
                    if archive.pending
                    else "Discordアーカイブへ保存できなかったため、分類処理は実行せず保留中です。"
                )
                await self._send_inbox_pending(
                    message,
                    CaptureResult(event_id=captured.event_id),
                    reason + "後でもう一度お試しください。元投稿は保持しています。",
                )
                return

            if not await self._enqueue_notion_after_discord_archive(captured.event_id):
                await self._send_inbox_pending(
                    message,
                    CaptureResult(event_id=captured.event_id),
                    "Notion二次アーカイブのローカルキューへ保存できなかったため、"
                    "分類処理は実行せず保留中です。後でもう一度お試しください。",
                )
                return

            # A duplicate gateway delivery after the candidate card was
            # already sent must not create a second card. A duplicate whose
            # first classification failed has no registered view and can be
            # retried after the durable archive gate above.
            message_id = str(getattr(message, "id", ""))
            if captured.duplicate and message_id in self._inbox_candidate_views:
                return
            await self._show_inbox_classification(
                message,
                captured,
                force_local=True,
            )
            return

        if captured.duplicate:
            return
        if captured.attachment_errors:
            await self._send_inbox_pending(
                message,
                captured,
                "添付の一部を完全に保存できなかったため、分類処理は実行せず保留中です。"
                "内容を確認して新しいメッセージとして再投稿してください。",
            )
            return
        if self.inbox_classifier is not None:
            await self._show_inbox_classification(message, captured)
            return
        try:
            outcome = await asyncio.to_thread(
                self.service.route_inbox_event,
                captured.event_id,
            )
        except Exception as exc:
            logger.error(
                "inbox route orchestration failed for event_id=%s (%s)",
                captured.event_id,
                type(exc).__name__,
                exc_info=False,
            )
            await self._send_inbox_pending(
                message,
                captured,
                "分類処理を利用できなかったため保留中です。"
                "生活マイページで編集するか、用途を明示して再投稿してください。",
            )
            return

        if outcome.routing_failed:
            await self._send_inbox_pending(
                message,
                captured,
                "分類サービスが完了しなかったため保留中です。"
                "生活マイページで編集するか、後で用途を明示して再投稿してください。",
            )
            return

        route = outcome.route
        if route.destination == "finance_or_meal":
            analysis_error: str | None = None
            domain_record_created = False
            try:
                event = await asyncio.to_thread(
                    self.service.analyze_event,
                    captured.event_id,
                )
                if getattr(event, "_analysis_failed", False):
                    analysis_error = "AIAnalysisFailed"
            except Exception as exc:
                logger.warning(
                    "routed finance analysis failed for event_id=%s (%s)",
                    captured.event_id,
                    type(exc).__name__,
                )
                analysis_error = type(exc).__name__
            if analysis_error is None:
                try:
                    persisted_event = await asyncio.to_thread(
                        self.service.get_event,
                        captured.event_id,
                    )
                    domain_record_created = bool(
                        persisted_event is not None
                        and (persisted_event.expenses or persisted_event.meals)
                    )
                except Exception as exc:
                    logger.warning(
                        "routed finance durability check failed for event_id=%s (%s)",
                        captured.event_id,
                        type(exc).__name__,
                    )
            acknowledged = await self._send_capture_candidate(
                message,
                CaptureResult(
                    event_id=captured.event_id,
                    attachment_errors=captured.attachment_errors,
                    analysis_error=analysis_error,
                ),
            )
            if (
                acknowledged
                and analysis_error is None
                and domain_record_created
            ):
                await self._delete_routed_inbox_message(message)
            return

        if route.destination == "memo":
            try:
                memo = await asyncio.to_thread(
                    self.finance.record_memo,
                    captured.event_id,
                    route.content,
                )
            except Exception as exc:
                logger.warning(
                    "routed memo rejected for event_id=%s (%s)",
                    captured.event_id,
                    type(exc).__name__,
                )
                await self._send_inbox_pending(
                    message,
                    captured,
                    "メモとして安全に保存できなかったため保留中です。"
                    "生活マイページで編集できます。",
                )
                return
            try:
                await self._send_memo_card(memo)
            except Exception as exc:
                logger.warning(
                    "routed memo card delivery failed (%s)",
                    type(exc).__name__,
                )
                response = (
                    f"一言メモをDBへ保存しました: #{memo.event_id}\n"
                    "#一言メモへのカード投稿に失敗したため、元投稿は残しています。"
                )
                try:
                    response += (
                        f"\nWeb: {_event_web_url(self.settings, memo.event_id)}"
                        "（Bot稼働Mac専用）"
                    )
                except Exception as url_exc:
                    logger.warning(
                        "routed memo Web URL construction failed (%s)",
                        type(url_exc).__name__,
                    )
                await self._send_memo_reply(message, response, event_id=memo.event_id)
                return

            response = (
                f"一言メモを保存しました: #{memo.event_id}\n"
                "保存先: #一言メモ\n"
                f"Web: {_event_web_url(self.settings, memo.event_id)}"
                "（Bot稼働Mac専用）"
            )
            acknowledged = await self._send_memo_reply(
                message,
                response,
                event_id=memo.event_id,
            )
            if acknowledged:
                await self._delete_routed_inbox_message(message)
            return

        if route.destination in {"rhythm_todo", "rhythm_result"}:
            reference = getattr(message, "reference", None)
            reference_id = getattr(reference, "message_id", None)
            linked_todo = None
            if reference_id is not None:
                linked_todo = await asyncio.to_thread(
                    self.rhythm.get_todo_by_event_message_id,
                    reference_id,
                )

            if route.destination == "rhythm_todo" and route.game is None:
                await self._send_inbox_pending(
                    message,
                    captured,
                    "音ゲーTODOのゲームを安全に判定できませんでした。"
                    "maimai・オンゲキ・チュウニズムのいずれかを明示してください。",
                )
                return
            if (
                route.destination == "rhythm_result"
                and route.game is None
                and linked_todo is None
            ):
                await self._send_inbox_pending(
                    message,
                    captured,
                    "リザルトのゲームを安全に判定できませんでした。"
                    "ゲーム名を明示するか、対象TODOへ返信してください。",
                )
                return

            try:
                if route.destination == "rhythm_todo":
                    rhythm_record = await asyncio.to_thread(
                        self.rhythm.create_todo,
                        captured.event_id,
                        route.game,
                        route.content,
                    )
                    game_label = RHYTHM_GAME_LABELS.get(
                        rhythm_record.game,
                        rhythm_record.game.value,
                    )
                    response = (
                        f"TODO #{rhythm_record.id} を保存しました: "
                        f"{game_label} / {rhythm_record.title}"
                    )
                else:
                    rhythm_record = await asyncio.to_thread(
                        self.rhythm.record_result,
                        captured.event_id,
                        route.game,
                        route.content,
                        linked_todo.id if linked_todo is not None else None,
                    )
                    game_label = RHYTHM_GAME_LABELS.get(
                        rhythm_record.game,
                        rhythm_record.game.value,
                    )
                    if rhythm_record.todo_id is None:
                        response = (
                            f"リザルト #{rhythm_record.id} を保存しました: {game_label}"
                        )
                    else:
                        response = (
                            f"リザルト #{rhythm_record.id} を保存し、"
                            f"TODO #{rhythm_record.todo_id}を完了しました: {game_label}"
                        )
            except Exception as exc:
                logger.warning(
                    "routed rhythm record rejected for event_id=%s (%s)",
                    captured.event_id,
                    type(exc).__name__,
                )
                await self._send_inbox_pending(
                    message,
                    captured,
                    "音ゲー記録として安全に保存できなかったため保留中です。"
                    "ゲーム名と内容を確認してください。",
                )
                return
            if captured.attachment_errors:
                response += "\n添付の一部は保存できませんでした。原文は保存済みです。"
            await message.channel.send(response)
            await self._delete_routed_inbox_message(message)
            return

        if route.destination == "career":
            try:
                record = await asyncio.to_thread(
                    self.career.create_from_route,
                    captured.event_id,
                    route,
                )
            except Exception as exc:
                logger.warning(
                    "routed career record rejected for event_id=%s (%s)",
                    captured.event_id,
                    type(exc).__name__,
                )
                await self._send_inbox_pending(
                    message,
                    captured,
                    "就活記録として安全に保存できなかったため保留中です。"
                    "会社名・タイトル・明示的な期限・URLを確認してください。",
                )
                return

            kind_label = CAREER_KIND_LABELS.get(record.kind, record.kind)
            status_label = CAREER_STATUS_LABELS.get(record.status, record.status)
            stage_label = (
                f" / 段階: {CAREER_STAGE_LABELS.get(record.stage, record.stage)}"
                if record.stage
                else ""
            )
            response = (
                f"就活記録 #{record.id} を保存しました: {record.company} / {record.title}"
                f" / 種別: {kind_label} / 状態: {status_label}{stage_label}"
                f" / 期限: {_career_deadline_label(record, timezone_name=self.settings.timezone)}"
            )
            if record.linked_task_id is not None:
                response += f" / LifeTask #{record.linked_task_id}"
            if captured.attachment_errors:
                response += "\n添付の一部を保存できませんでした。原文は保存済みです。"
            # A successful send is the acknowledgement gate.  If Discord
            # rejects it, the exception exits this handler before deletion and
            # the durable Event/career row remains retryable.
            await message.channel.send(
                response,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self._delete_routed_inbox_message(message)
            return

        if route.destination == "life_task":
            due_date = route.task_due_date
            due_at = None
            if route.task_due_date is not None and route.task_due_time is not None:
                due_at = datetime.combine(route.task_due_date, route.task_due_time)
                due_date = None
            try:
                task = await asyncio.to_thread(
                    self.tasks.create_manual,
                    route.task_title,
                    due_date=due_date,
                    due_at=due_at,
                )
                await asyncio.to_thread(
                    self.service.mark_event_routed,
                    captured.event_id,
                    "life_task",
                )
            except Exception as exc:
                logger.warning(
                    "routed life task rejected for event_id=%s (%s)",
                    captured.event_id,
                    type(exc).__name__,
                )
                await self._send_inbox_pending(
                    message,
                    captured,
                    "生活タスクとして安全に保存できなかったため保留中です。"
                    "タイトルと明示的な日時を確認してください。",
                )
                return
            due_label = "期限なし"
            if route.task_due_date is not None:
                due_label = route.task_due_date.isoformat()
                if route.task_due_time is not None:
                    due_label += f" {route.task_due_time.strftime('%H:%M')}"
            response = f"タスク #{task.id} を保存しました: {task.title} / 期限: {due_label}"
            if captured.attachment_errors:
                response += "\n添付の一部は保存できませんでした。原文は保存済みです。"
            await message.channel.send(response)
            await self._delete_routed_inbox_message(message)
            return

        await self._send_inbox_pending(
            message,
            captured,
            "用途を安全に判定できなかったため保留中です。"
            f"生活マイページで編集するか、`/retry event_id:{captured.event_id}`で"
            "支出・食事として再解析するか、`メモ:`・`タスク:`・`todo`・`result`のように"
            "用途を明示して再投稿できます。",
        )

    async def _handle_card_message(self, message: discord.Message) -> None:
        account_name, expense_text = split_account_prefix(message.content or "")
        if not expense_text and not getattr(message, "attachments", None):
            captured = await self.service.capture_discord_message_raw(message)
            if captured.duplicate:
                return
            await message.channel.send(
                f"Event #{captured.event_id}として原文は保存しました。"
                "利用内容を入力してください。例: `680 マック` または "
                "`[楽天カード] 1280 Amazon`"
            )
            return
        result = await self.service.capture_discord_message(message)
        if result.duplicate:
            return
        event = await asyncio.to_thread(self.service.get_event, result.event_id)
        extra = ""
        if event is not None and event.expenses:
            selected_account = account_name or DEFAULT_CARD_ACCOUNT
            try:
                await asyncio.to_thread(
                    self.finance.link_expense, event.id, selected_account
                )
                extra = f"\n支払元: {selected_account}"
            except ValueError:
                extra = (
                    f"\nカード『{selected_account}』へ紐づけられませんでした。"
                    "#口座 で /account_add を使って登録してください。"
                )
        await self._send_capture_candidate(message, result, extra=extra)

    async def _handle_account_message(self, message: discord.Message) -> None:
        captured = await self.service.capture_discord_message_raw(message)
        if captured.duplicate:
            return
        try:
            instruction = parse_account_instruction(
                message.content or "", maximum=self.settings.max_expense_yen
            )
            source = instruction.source_account or DEFAULT_BANK_ACCOUNT
            action_view: EventActionView | None = None
            if instruction.kind == AccountInstructionKind.INCOME:
                activity = await asyncio.to_thread(
                    self.finance.record_income,
                    captured.event_id,
                    source,
                    instruction.amount,
                    instruction.note,
                )
                response = (
                    f"入金を記録しました: {activity.destination_account_name} / "
                    f"{activity.amount:,}円"
                )
            elif instruction.kind == AccountInstructionKind.EXPENSE:
                await asyncio.to_thread(
                    self.service.edit_event,
                    captured.event_id,
                    amount=instruction.amount,
                    category="未分類",
                    description=instruction.note,
                )
                await asyncio.to_thread(self.service.confirm_event, captured.event_id)
                activity = await asyncio.to_thread(
                    self.finance.link_expense, captured.event_id, source
                )
                response = (
                    f"口座支出を記録しました: {activity.source_account_name} / "
                    f"{activity.amount:,}円"
                )
                action_view = EventActionView(
                    self.service, self.guard, captured.event_id
                )
            else:
                destination = instruction.destination_account
                if destination is None:
                    raise ValueError("transfer destination is required")
                activity = await asyncio.to_thread(
                    self.finance.record_transfer,
                    captured.event_id,
                    source,
                    destination,
                    instruction.amount,
                    instruction.note,
                )
                response = (
                    f"振替を記録しました: {activity.source_account_name} → "
                    f"{activity.destination_account_name} / {activity.amount:,}円"
                )
            if captured.attachment_errors:
                response += "\n添付の一部は保存できませんでした。原文は保存済みです。"
            await message.channel.send(response, view=action_view)
        except ValueError:
            await message.channel.send(
                f"Event #{captured.event_id}として原文は保存しましたが、"
                "口座操作を解釈できませんでした。\n"
                "例: `[ゆうちょ] 入金 250000 給料` / "
                "`[ゆうちょ] 振替 5000 -> [現金] ATM` / "
                "`[ゆうちょ] カード引落 68000 -> [楽天カード] 8月分`"
            )

    async def _handle_memo_message(self, message: discord.Message) -> None:
        captured = await self.service.capture_discord_message_raw(message)
        if captured.duplicate:
            return
        body = message.content or ""
        if not body.strip():
            await self._send_memo_reply(
                message,
                f"Event #{captured.event_id}として添付は保存しました。メモ本文も入力してください。"
            )
            return
        try:
            memo = await asyncio.to_thread(
                self.finance.record_memo, captured.event_id, body
            )
        except Exception as exc:
            logger.warning(
                "direct memo save failed for event_id=%s (%s)",
                captured.event_id,
                type(exc).__name__,
            )
            await self._send_memo_reply(
                message,
                f"Event #{captured.event_id}として原文は保存しましたが、メモ化できませんでした。"
            )
            return
        response = (
            f"一言メモを保存しました: #{memo.event_id}\n"
            f"Web: {_event_web_url(self.settings, memo.event_id)}"
            "（Bot稼働Mac専用）"
        )
        if captured.attachment_errors:
            response += "\n添付の一部を保存できませんでした。原文は保存済みです。"
        await self._send_memo_reply(message, response, event_id=memo.event_id)

    async def _handle_rhythm_message(self, message: discord.Message) -> None:
        captured = await self.service.capture_discord_message_raw(message)
        if captured.duplicate:
            return

        reference = getattr(message, "reference", None)
        reference_id = getattr(reference, "message_id", None)
        linked_todo = None
        if reference_id is not None:
            linked_todo = await asyncio.to_thread(
                self.rhythm.get_todo_by_event_message_id, reference_id
            )

        raw_text = message.content or ""
        explicit_todo = raw_text.strip().casefold().startswith(
            ("todo", "課題", "やること")
        )
        try:
            if linked_todo is not None and not explicit_todo:
                # A reply to a TODO is a result by default, so attaching only
                # a result screenshot is enough. Prefixing with a game name or
                # "result" remains supported for an optional note.
                prefixed = raw_text.strip()
                if not prefixed.casefold().startswith(("result", "リザルト", "結果")):
                    prefixed = f"result {prefixed}".strip()
                instruction = parse_rhythm_instruction(prefixed)
            else:
                instruction = parse_rhythm_instruction(raw_text)

            if instruction.kind == RhythmInstructionKind.TODO:
                todo = await asyncio.to_thread(
                    self.rhythm.create_todo,
                    captured.event_id,
                    instruction.game,
                    instruction.text,
                )
                game_label = RHYTHM_GAME_LABELS.get(todo.game, todo.game.value)
                response = f"TODO #{todo.id} を保存しました: {game_label} / {todo.title}"
            else:
                result = await asyncio.to_thread(
                    self.rhythm.record_result,
                    captured.event_id,
                    instruction.game,
                    instruction.text or None,
                    linked_todo.id if linked_todo is not None else None,
                )
                game_label = RHYTHM_GAME_LABELS.get(result.game, result.game.value)
                if result.todo_id is not None:
                    response = (
                        f"リザルト #{result.id} を保存し、TODO #{result.todo_id}を完了しました: "
                        f"{game_label}"
                    )
                else:
                    response = f"リザルト #{result.id} を保存しました: {game_label}"
            if captured.attachment_errors:
                response += "\n画像の一部は保存できませんでした。原文は保存済みです。"
            await message.channel.send(response)
        except ValueError:
            await message.channel.send(
                f"Event #{captured.event_id}として原文・画像は保存しましたが、"
                "音ゲー記録を解釈できませんでした。\n"
                "例: `todo maimai でらっくす譜面を詰める` / "
                "`課題 オンゲキ ABFBを詰める` / `result チュウニズム SSS+`。"
                "TODOメッセージへの画像返信ならゲーム名は省略できます。"
            )

    @staticmethod
    def _progress_instruction(
        raw_text: str,
        linked_todo: RhythmTodoResult | None,
    ) -> RhythmInstruction:
        """Parse progress posts without making plain text into TODOs."""

        raw = (raw_text or "").strip()
        lowered = raw.casefold()
        explicit_todo = lowered == "todo" or lowered.startswith(("todo ", "課題 ", "やること "))
        explicit_result = lowered == "result" or lowered.startswith(
            ("result ", "リザルト ", "結果 ")
        )
        if explicit_todo or explicit_result:
            return parse_rhythm_instruction(raw)
        if linked_todo is not None:
            # A reply to a TODO is a result even when it only contains an
            # optional note, or no text at all.
            return parse_rhythm_instruction(f"result {raw}".strip())
        parsed = parse_rhythm_instruction(raw)
        if parsed.game is None:
            raise ValueError("progress post needs an explicit game")
        return RhythmInstruction(
            kind=RhythmInstructionKind.RESULT,
            game=parsed.game,
            text=parsed.text,
        )

    @staticmethod
    def _saved_progress_image_count(event: Event | None) -> int:
        if event is None:
            return 0
        return sum(
            1
            for attachment in event.attachments
            if (attachment.content_type or "").lower().split(";", 1)[0].strip()
            in SUPPORTED_IMAGE_TYPES
        )

    async def _handle_rhythm_progress_message(self, message: discord.Message) -> None:
        """Capture progress-channel input raw-first, then record text-led results."""

        captured = await self.service.capture_discord_message_raw(message)
        if captured.duplicate:
            return

        event = await asyncio.to_thread(self.service.get_event, captured.event_id)
        image_count = self._saved_progress_image_count(event)
        if image_count < 1:
            extra = (
                " 添付の一部を保存できませんでした。"
                if captured.attachment_errors
                else ""
            )
            await message.channel.send(
                f"Event #{captured.event_id}として原文・添付情報は保存しましたが、"
                f"今日の進捗には画像を1枚以上添付してください。{extra}\n"
                "TODOへの画像返信、またはゲーム名付きで投稿してください。"
                "例: `maimai SSS+` / `オンゲキ AB` / `チュウニズム AJ`"
            )
            return

        reference = getattr(message, "reference", None)
        reference_id = getattr(reference, "message_id", None)
        linked_todo = None
        if reference_id is not None:
            linked_todo = await asyncio.to_thread(
                self.rhythm.get_todo_by_event_message_id,
                reference_id,
            )

        try:
            instruction = self._progress_instruction(message.content or "", linked_todo)
            if instruction.kind == RhythmInstructionKind.TODO:
                todo = await asyncio.to_thread(
                    self.rhythm.create_todo,
                    captured.event_id,
                    instruction.game,
                    instruction.text,
                )
                game_label = RHYTHM_GAME_LABELS.get(todo.game, todo.game.value)
                response = f"TODO #{todo.id} を保存しました: {game_label} / {todo.title}"
            else:
                result = await asyncio.to_thread(
                    self.rhythm.record_result,
                    captured.event_id,
                    instruction.game,
                    instruction.text or None,
                    linked_todo.id if linked_todo is not None else None,
                )
                game_label = RHYTHM_GAME_LABELS.get(result.game, result.game.value)
                if result.todo_id is not None:
                    response = (
                        f"リザルト #{result.id} を保存し、TODO #{result.todo_id}を完了しました: "
                        f"{game_label} / 画像{result.attachment_count}件"
                    )
                else:
                    response = (
                        f"リザルト #{result.id} を保存しました: {game_label} / "
                        f"画像{result.attachment_count}件"
                    )
            if captured.attachment_errors:
                response += "\n画像の一部は保存できませんでした。原文は保存済みです。"
            await message.channel.send(response)
        except ValueError:
            await message.channel.send(
                f"Event #{captured.event_id}として原文・画像は保存しましたが、"
                "今日の進捗として解釈できませんでした。\n"
                "ゲーム名を付けた画像投稿（`maimai SSS+` / `オンゲキ AB` / "
                "`チュウニズム AJ`）か、既存TODOへの画像返信にしてください。"
            )

    def _allows_inbox_reaction(self, payload: discord.RawReactionActionEvent) -> bool:
        user_id = getattr(payload, "user_id", None)
        if not self.guard.allows_inbox_ids(
            user_id=user_id,
            guild_id=getattr(payload, "guild_id", None),
            channel_id=getattr(payload, "channel_id", None),
        ):
            return False

        bot_user = self.user
        if bot_user is not None and str(user_id) == str(getattr(bot_user, "id", "")):
            return False
        member = getattr(payload, "member", None)
        cached_user = self.get_user(user_id) if user_id is not None else None
        for actor in (member, getattr(member, "user", None), cached_user):
            if actor is not None and getattr(actor, "bot", False):
                return False
        return True

    async def _restore_inbox_candidate_view_for_reaction(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> InboxCandidateView | None:
        """Rebuild one reviewed candidate after a Bot-process restart.

        The raw Event and Discord archive mapping are durable, while the
        source-message object and callback view are intentionally in memory.
        A reaction on an already displayed candidate may therefore be the
        first event seen by a restarted process.  Re-fetch the exact scoped
        source message, re-run only the bounded candidate classifier, and
        restore the persistent custom IDs.  A category that is not present in
        the rebuilt candidate set remains unusable.
        """

        if self.inbox_classifier is None:
            return None
        message_id = getattr(payload, "message_id", None)
        channel_id = getattr(payload, "channel_id", None)
        user_id = getattr(payload, "user_id", None)
        if message_id is None or channel_id is None or user_id is None:
            return None

        event = await asyncio.to_thread(
            self.service.get_event_by_discord_message_id,
            message_id,
        )
        if (
            event is None
            or event.status != "pending"
            or event.event_type != "unknown"
            or str(event.discord_channel_id) != str(channel_id)
            or str(event.discord_user_id) != str(user_id)
        ):
            return None

        channel = self.get_channel(channel_id)
        if channel is None:
            fetch_channel = getattr(self, "fetch_channel", None)
            if not callable(fetch_channel):
                return None
            try:
                channel = await fetch_channel(channel_id)
            except Exception as exc:
                logger.info(
                    "inbox candidate source lookup skipped (%s)",
                    type(exc).__name__,
                )
                return None
        fetch_message = getattr(channel, "fetch_message", None)
        if not callable(fetch_message):
            return None
        try:
            source_message = await fetch_message(message_id)
            classification = await asyncio.to_thread(
                self.inbox_classifier.classify,
                event.raw_text,
                attachment_count=len(event.attachments),
            )
        except Exception as exc:
            logger.info(
                "inbox candidate restoration skipped (%s)",
                type(exc).__name__,
            )
            return None
        if classification.is_unknown:
            return None

        view = InboxCandidateView(self, source_message, event.id, classification)
        self._register_inbox_candidate_view(source_message, view)
        # Register the event-specific custom IDs globally so an existing
        # candidate button also works after this reaction-based recovery.
        self.add_view(view)
        return view

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Use a displayed candidate's Unicode reaction as its button equivalent."""

        if not self._allows_inbox_reaction(payload):
            return
        view = self._inbox_candidate_views.get(str(getattr(payload, "message_id", "")))
        if view is None:
            view = await self._restore_inbox_candidate_view_for_reaction(payload)
            if view is None:
                return
        category = view._category_for_reaction(getattr(payload, "emoji", None))
        if category is None:
            return
        try:
            result = await view._choose_reaction(category)
        except Exception as exc:
            logger.error(
                "inbox reaction handling failed (%s)",
                type(exc).__name__,
                exc_info=False,
            )
            result = (
                "保存先への送信に失敗しました。元投稿は残しています。"
                "後でもう一度お試しください。"
            )
        if result is not None:
            await self._send_inbox_reaction_feedback(payload, result)

    async def on_message(self, message: discord.Message) -> None:
        role = self.guard.message_role(
            message, self_user_id=self.user.id if self.user else None
        )
        if role is None:
            return
        try:
            if role == "card":
                await self._handle_card_message(message)
            elif role == "account":
                await self._handle_account_message(message)
            elif role == "memo":
                await self._handle_memo_message(message)
            elif role == "social_draft":
                await self._handle_social_draft_message(message)
            elif role == "rhythm":
                await self._handle_rhythm_message(message)
            elif role == "rhythm_progress":
                await self._handle_rhythm_progress_message(message)
            elif role in {"inbox", "inbox_compat"}:
                await self._handle_inbox_message(message)
            elif role == "meal":
                result = await self.service.capture_discord_message(message)
                if not result.duplicate:
                    await self._send_capture_candidate(message, result)
        except Exception as exc:
            # Message/provider exceptions can carry URLs or user content.
            # Keep production logs to a stable class label without traceback.
            logger.error("message handling failed (%s)", type(exc).__name__, exc_info=False)


def create_bot(
    settings: Settings,
    *,
    service: InboxService | None = None,
    analyzer: Any | None = None,
    inbox_classifier: InboxClassifier | None = None,
    mybest: MaimaiMyBestService | None = None,
    net_favorites: MaimaiNetFavoriteService | None = None,
) -> LifeInboxBot:
    settings.validate_for_bot()
    if inbox_classifier is None:
        inbox_classifier = InboxClassifier(
            (
                settings.openai_inbox_classifier_api_key.get_secret_value()
                if settings.openai_inbox_classifier_api_key is not None
                else None
            ),
            model=settings.openai_inbox_classifier_model,
            enabled=settings.openai_inbox_classifier_enabled,
            timeout_seconds=settings.openai_inbox_classifier_timeout_seconds,
        )
    if service is None:
        database = Database(settings.database_url)
        database.init()
        if analyzer is None:
            analyzer = (
                OpenAIAnalyzer(
                    settings.openai_api_key.get_secret_value(),
                    settings.openai_model,
                    max_image_bytes=settings.attachment_max_bytes,
                    categories=settings.categories,
                    food_subcategories=settings.food_subcategories,
                )
                if settings.openai_api_key is not None
                else NullAnalyzer()
            )
        service = InboxService(database.session_factory, settings, analyzer)
    notion_sync: NotionSyncService | None = None
    notion_sync_worker: NotionSyncWorker | None = None
    if settings.notion_sync_enabled:
        notion_transport = NotionHttpTransport(settings)
        notion_sync = NotionSyncService(
            service.session_factory,
            settings,
            transport=notion_transport,
            data_dir=settings.data_dir,
        )
        notion_sync_worker = NotionSyncWorker(
            notion_sync,
            enabled=True,
            poll_seconds=settings.notion_sync_poll_seconds,
            close_transport_on_shutdown=True,
        )
    return LifeInboxBot(
        settings,
        service,
        inbox_classifier=inbox_classifier,
        mybest=mybest,
        net_favorites=net_favorites,
        notion_sync=notion_sync,
        notion_sync_worker=notion_sync_worker,
    )


def run_bot(settings: Settings) -> None:
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    bot = create_bot(settings)
    if settings.discord_bot_token is None:  # validate_for_bot already guards this
        raise ValueError("DISCORD_BOT_TOKEN is required")
    bot.run(settings.discord_bot_token.get_secret_value())
