"""Idempotent Discord publishers for rating images and daily spending."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from .rhythm_collectors.common import safe_message
from .rhythm_persistence import RhythmPersistence, SpendingRecord

GAME_LABELS = {
    "chunithm": "CHUNITHM",
    "maimai": "maimai",
    "ongeki": "オンゲキ",
}


class DiscordSendError(RuntimeError):
    def __init__(
        self,
        code: str,
        safe_message: str,
        *,
        retryable: bool,
        retry_after: float | None = None,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable
        self.retry_after = retry_after
        self.outcome_unknown = outcome_unknown


class DiscordTransport(Protocol):
    async def send(
        self,
        *,
        channel_id: int,
        content: str,
        image_path: Path | None = None,
        nonce: str | int | None = None,
    ) -> str: ...

    async def edit(
        self,
        *,
        channel_id: int,
        message_id: str,
        content: str,
        image_path: Path | None = None,
    ) -> str: ...

    async def delete(self, *, channel_id: int, message_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class DiscordPublishOutcome:
    posted: bool
    edited: bool
    dry_run: bool
    duplicate: bool
    in_progress: bool
    message_id: str | None
    planned_content: str
    attempt_count: int
    error_code: str | None = None
    uncertain: bool = False


def _discord_content(value: str) -> str:
    """Bound and redact generated content before it reaches Discord."""

    return (safe_message(value) or "Discord投稿内容はありません").replace("\x00", "")[:1_900]


def _display_number(value: int | float | str, *, signed: bool = False) -> str:
    """Render a numeric measure without storage-scale trailing zeroes."""

    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    if not number.is_finite():
        return str(value)
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-0"}:
        text = "0"
    if signed and number > 0:
        return f"+{text}"
    return text


def format_rating_message(
    *,
    game: str,
    local_date: date,
    rating: int | float | str,
    previous_rating: int | float | str | None,
    delta: int | float | str | None,
    collected_label: str,
) -> str:
    game_label = GAME_LABELS.get(game, game)
    previous = "初回" if previous_rating is None else _display_number(previous_rating)
    rating_text = _display_number(rating)
    delta_text = "—" if delta is None else _display_number(delta, signed=True)
    return _discord_content("\n".join(
        (
            f"【{local_date.isoformat()} {game_label} レート更新】",
            "",
            f"{previous} → {rating_text}",
            f"前回比 {delta_text}",
            f"取得時刻 {collected_label}",
        )
    ))


def spending_fingerprint(record: SpendingRecord) -> str:
    payload = ":".join(
        str(value)
        for value in (
            record.local_date.isoformat(),
            record.chunithm_amount_yen,
            record.maimai_amount_yen,
            record.ongeki_amount_yen,
            record.total_amount_yen,
            record.revision,
            record.deleted,
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def format_collection_error_message(
    *,
    game: str,
    state_label: str,
    occurred_label: str,
    action_label: str,
) -> str:
    game_label = GAME_LABELS.get(game, game)
    return _discord_content("\n".join(
        (
            f"【{game_label}取得失敗】",
            "",
            f"状態：{state_label}",
            f"発生：{occurred_label}",
            f"対応：{action_label}",
        )
    ))


def format_spending_message(record: SpendingRecord) -> str:
    lines = [
        f"【{record.local_date.year}年{record.local_date.month}月"
        f"{record.local_date.day}日 音ゲー利用額】",
        "",
    ]
    if record.deleted:
        lines.extend(("この日の利用額記録は削除されました。", ""))
    lines.extend(
        (
            f"合計 ¥{record.total_amount_yen:,}",
            "",
            f"ウニ ¥{record.chunithm_amount_yen:,}",
            f"マイ ¥{record.maimai_amount_yen:,}",
            f"ゲキ ¥{record.ongeki_amount_yen:,}",
        )
    )
    return _discord_content("\n".join(lines))


class RhythmDiscordPublisher:
    def __init__(
        self,
        persistence: RhythmPersistence,
        transport: DiscordTransport,
        *,
        dry_run: bool,
        max_attempts: int = 3,
        base_backoff_seconds: float = 1.0,
    ) -> None:
        self.persistence = persistence
        self.transport = transport
        self.dry_run = dry_run
        self.max_attempts = max_attempts
        self.base_backoff_seconds = base_backoff_seconds

    async def _deliver_with_retry(
        self,
        *,
        channel_id: int,
        content: str,
        image_path: Path | None,
        existing_message_id: str | None,
    ) -> tuple[str, bool, int]:
        for attempt in range(1, self.max_attempts + 1):
            operation_was_edit = existing_message_id is not None
            try:
                if existing_message_id is not None:
                    message_id = await self.transport.edit(
                        channel_id=channel_id,
                        message_id=existing_message_id,
                        content=content,
                        image_path=image_path,
                    )
                    return message_id, True, attempt
                message_id = await self.transport.send(
                    channel_id=channel_id,
                    content=content,
                    image_path=image_path,
                )
                return message_id, False, attempt
            except DiscordSendError as exc:
                if existing_message_id is not None and exc.code == "discord_not_found":
                    # The bot's previous message was removed.  Sending a new
                    # message is the only safe append fallback.
                    existing_message_id = None
                    try:
                        message_id = await self.transport.send(
                            channel_id=channel_id,
                            content=content,
                            image_path=image_path,
                        )
                        return message_id, False, attempt
                    except DiscordSendError as fallback_exc:
                        exc = fallback_exc
                        operation_was_edit = False
                # Creating a message is not idempotent. A network/5xx response
                # can be ambiguous after Discord accepted the message, so only
                # an explicit rejection such as 429 is safe to retry. Editing
                # a known Bot message remains idempotent and can be retried.
                if exc.outcome_unknown and not operation_was_edit:
                    raise
                if not exc.retryable or attempt >= self.max_attempts:
                    raise
                delay = (
                    exc.retry_after
                    if exc.retry_after is not None
                    else self.base_backoff_seconds * (2 ** (attempt - 1))
                )
                # This delay protects a failed request; it is never interpreted
                # as evidence that delivery succeeded.
                await asyncio.sleep(max(0.0, delay))
        raise DiscordSendError(
            "delivery_exhausted",
            "Discord送信の再試行回数を超えました。",
            retryable=False,
        )

    async def publish_rating(
        self,
        *,
        user_id: int,
        local_date: date,
        game: str,
        channel_id: int,
        semantic_fingerprint: str,
        image_sha256: str,
        image_path: Path,
        rating: int | float | str,
        previous_rating: int | float | str | None,
        delta: int | float | str | None,
        collected_label: str,
        now: datetime | None = None,
        force: bool = False,
        allow_presentation_refresh: bool = False,
    ) -> DiscordPublishOutcome:
        content = format_rating_message(
            game=game,
            local_date=local_date,
            rating=rating,
            previous_rating=previous_rating,
            delta=delta,
            collected_label=collected_label,
        )
        if self.dry_run:
            return DiscordPublishOutcome(
                False, False, True, False, False, None, content, 0
            )
        claim = self.persistence.claim_delivery(
            user_id=user_id,
            local_date=local_date,
            delivery_kind="rating",
            game=game,
            channel_id=channel_id,
            semantic_fingerprint=semantic_fingerprint,
            image_sha256=image_sha256,
            now=now,
            force=force,
            allow_presentation_refresh=allow_presentation_refresh,
        )
        if not claim.claimed:
            return DiscordPublishOutcome(
                False,
                False,
                False,
                claim.duplicate,
                claim.in_progress,
                claim.existing_message_id,
                content,
                claim.attempt_count,
                "delivery_outcome_unknown" if claim.uncertain else None,
                claim.uncertain,
            )
        try:
            message_id, edited, attempts = await self._deliver_with_retry(
                channel_id=channel_id,
                content=content,
                image_path=image_path,
                existing_message_id=claim.existing_message_id,
            )
        except DiscordSendError as exc:
            marker = (
                self.persistence.mark_delivery_unknown
                if exc.outcome_unknown
                else self.persistence.mark_delivery_failed
            )
            marker(
                delivery_id=claim.delivery_id,
                claim_attempt_count=claim.attempt_count,
                error_code=exc.code,
                safe_message=exc.safe_message,
                now=now,
            )
            return DiscordPublishOutcome(
                False,
                False,
                False,
                False,
                False,
                claim.existing_message_id,
                content,
                claim.attempt_count,
                exc.code,
                exc.outcome_unknown,
            )
        self.persistence.mark_delivery_succeeded(
            delivery_id=claim.delivery_id,
            claim_attempt_count=claim.attempt_count,
            discord_message_id=message_id,
            posted_at=now,
        )
        return DiscordPublishOutcome(
            True, edited, False, False, False, message_id, content, attempts
        )

    async def publish_collection_error(
        self,
        *,
        user_id: int,
        local_date: date,
        game: str,
        channel_id: int,
        state_label: str,
        occurred_label: str,
        action_label: str,
        now: datetime | None = None,
    ) -> DiscordPublishOutcome:
        """Post or update one sanitized per-game error notice for the day."""

        content = format_collection_error_message(
            game=game,
            state_label=state_label,
            occurred_label=occurred_label,
            action_label=action_label,
        )
        if self.dry_run:
            return DiscordPublishOutcome(
                False, False, True, False, False, None, content, 0
            )
        semantic_fingerprint = hashlib.sha256(
            f"{game}\0{state_label}\0{action_label}".encode()
        ).hexdigest()
        claim = self.persistence.claim_delivery(
            user_id=user_id,
            local_date=local_date,
            delivery_kind="collection_error",
            game=game,
            channel_id=channel_id,
            semantic_fingerprint=semantic_fingerprint,
            image_sha256=None,
            now=now,
        )
        if not claim.claimed:
            return DiscordPublishOutcome(
                False,
                False,
                False,
                claim.duplicate,
                claim.in_progress,
                claim.existing_message_id,
                content,
                claim.attempt_count,
                "delivery_outcome_unknown" if claim.uncertain else None,
                claim.uncertain,
            )
        try:
            message_id, edited, attempts = await self._deliver_with_retry(
                channel_id=channel_id,
                content=content,
                image_path=None,
                existing_message_id=claim.existing_message_id,
            )
        except DiscordSendError as exc:
            marker = (
                self.persistence.mark_delivery_unknown
                if exc.outcome_unknown
                else self.persistence.mark_delivery_failed
            )
            marker(
                delivery_id=claim.delivery_id,
                claim_attempt_count=claim.attempt_count,
                error_code=exc.code,
                safe_message=exc.safe_message,
                now=now,
            )
            return DiscordPublishOutcome(
                False,
                False,
                False,
                False,
                False,
                claim.existing_message_id,
                content,
                claim.attempt_count,
                exc.code,
                exc.outcome_unknown,
            )
        self.persistence.mark_delivery_succeeded(
            delivery_id=claim.delivery_id,
            claim_attempt_count=claim.attempt_count,
            discord_message_id=message_id,
            posted_at=now,
        )
        return DiscordPublishOutcome(
            True, edited, False, False, False, message_id, content, attempts
        )

    async def publish_spending(
        self,
        *,
        user_id: int,
        record: SpendingRecord,
        channel_id: int,
        post_zero_days: bool,
        now: datetime | None = None,
    ) -> DiscordPublishOutcome:
        content = format_spending_message(record)
        already_posted = self.persistence.has_posted_spending_delivery(
            user_id=user_id,
            local_date=record.local_date,
            channel_id=channel_id,
        )
        if (
            record.deleted or (record.total_amount_yen == 0 and not post_zero_days)
        ) and not already_posted:
            return DiscordPublishOutcome(
                False, False, self.dry_run, True, False, None, content, 0
            )
        if self.dry_run:
            return DiscordPublishOutcome(
                False, False, True, False, False, None, content, 0
            )
        claim = self.persistence.claim_delivery(
            user_id=user_id,
            local_date=record.local_date,
            delivery_kind="spending",
            game=None,
            channel_id=channel_id,
            semantic_fingerprint=spending_fingerprint(record),
            image_sha256=None,
            now=now,
        )
        if not claim.claimed:
            return DiscordPublishOutcome(
                False,
                False,
                False,
                claim.duplicate,
                claim.in_progress,
                claim.existing_message_id,
                content,
                claim.attempt_count,
                "delivery_outcome_unknown" if claim.uncertain else None,
                claim.uncertain,
            )
        try:
            message_id, edited, attempts = await self._deliver_with_retry(
                channel_id=channel_id,
                content=content,
                image_path=None,
                existing_message_id=claim.existing_message_id,
            )
        except DiscordSendError as exc:
            marker = (
                self.persistence.mark_delivery_unknown
                if exc.outcome_unknown
                else self.persistence.mark_delivery_failed
            )
            marker(
                delivery_id=claim.delivery_id,
                claim_attempt_count=claim.attempt_count,
                error_code=exc.code,
                safe_message=exc.safe_message,
                now=now,
            )
            return DiscordPublishOutcome(
                False,
                False,
                False,
                False,
                False,
                claim.existing_message_id,
                content,
                claim.attempt_count,
                exc.code,
                exc.outcome_unknown,
            )
        self.persistence.mark_delivery_succeeded(
            delivery_id=claim.delivery_id,
            claim_attempt_count=claim.attempt_count,
            discord_message_id=message_id,
            posted_at=now,
        )
        return DiscordPublishOutcome(
            True, edited, False, False, False, message_id, content, attempts
        )


class DiscordPyTransport:
    """Thin discord.py transport; no channel access occurs until publication."""

    def __init__(self, bot) -> None:
        self.bot = bot

    async def _channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception as exc:
                mapped = self._map_error(exc)
                if mapped.outcome_unknown:
                    # No send was attempted while resolving the channel, so a
                    # network failure is retryable and not externally ambiguous.
                    mapped = DiscordSendError(
                        "channel_unavailable",
                        "Discord投稿先チャンネルを一時的に取得できません。",
                        retryable=True,
                    )
                raise mapped from exc
        if not hasattr(channel, "send"):
            raise DiscordSendError(
                "invalid_channel",
                "Discord投稿先がテキストチャンネルではありません。",
                retryable=False,
            )
        return channel

    @staticmethod
    def _map_error(exc: Exception) -> DiscordSendError:
        import discord

        if isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError)):
            return DiscordSendError(
                "discord_delivery_unknown",
                "Discord送信の応答を確認できません。",
                retryable=True,
                outcome_unknown=True,
            )
        if isinstance(exc, discord.Forbidden):
            return DiscordSendError(
                "discord_forbidden", "Discordの投稿権限がありません。", retryable=False
            )
        if isinstance(exc, discord.NotFound):
            return DiscordSendError(
                "discord_not_found", "Discordメッセージが見つかりません。", retryable=False
            )
        if isinstance(exc, discord.HTTPException):
            status = getattr(exc, "status", 0)
            retry_after = getattr(exc, "retry_after", None)
            server_error = status >= 500
            return DiscordSendError(
                "discord_rate_limited" if status == 429 else "discord_http_error",
                "Discordへの送信に一時的に失敗しました。",
                retryable=status == 429 or server_error,
                retry_after=retry_after,
                outcome_unknown=server_error,
            )
        return DiscordSendError(
            "discord_send_failed", "Discordへの送信に失敗しました。", retryable=False
        )

    async def send(
        self,
        *,
        channel_id: int,
        content: str,
        image_path: Path | None = None,
        nonce: str | int | None = None,
    ) -> str:
        import discord

        channel = await self._channel(channel_id)
        try:
            kwargs: dict[str, object] = {
                "content": _discord_content(content),
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if nonce is not None:
                # discord.py 2.7 sends ``enforce_nonce=true`` whenever a nonce
                # is supplied. The API then returns the existing message for
                # a duplicate nonce instead of creating a second one.
                kwargs["nonce"] = nonce
            if image_path is not None:
                kwargs["file"] = discord.File(image_path)
            message = await channel.send(**kwargs)
        except Exception as exc:
            raise self._map_error(exc) from exc
        return str(message.id)

    async def edit(
        self,
        *,
        channel_id: int,
        message_id: str,
        content: str,
        image_path: Path | None = None,
    ) -> str:
        import discord

        channel = await self._channel(channel_id)
        try:
            message = await channel.fetch_message(int(message_id))
            attachments = [discord.File(image_path)] if image_path is not None else []
            edited = await message.edit(
                content=_discord_content(content),
                attachments=attachments,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            raise self._map_error(exc) from exc
        return str(edited.id)

    async def delete(self, *, channel_id: int, message_id: str) -> None:
        channel = await self._channel(channel_id)
        try:
            message = await channel.fetch_message(int(message_id))
            await message.delete()
        except Exception as exc:
            raise self._map_error(exc) from exc
