"""Immediate and daily Discord publication for rhythm-spending events."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from .rhythm_collectors.common import safe_message
from .rhythm_discord import (
    DiscordPublishOutcome,
    DiscordSendError,
    DiscordTransport,
    RhythmDiscordPublisher,
)
from .rhythm_persistence import RhythmPersistence, SpendingRecord
from .rhythm_spending import (
    RhythmSpendingEventPersistence,
    SpendingDayTotals,
    SpendingEventRecord,
)

SPENDING_GAME_LABELS = {
    "chunithm": "ウニ",
    "maimai": "マイ",
    "ongeki": "ゲキ",
}


def _content(value: str) -> str:
    return (safe_message(value) or "利用記録はありません").replace("\x00", "")[:1_900]


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def format_spending_event_message(
    event: SpendingEventRecord, totals: SpendingDayTotals
) -> str:
    occurred = _aware_utc(event.occurred_at).astimezone(ZoneInfo(event.time_zone))
    label = SPENDING_GAME_LABELS[event.game]
    heading = "【利用記録の削除】" if event.deleted else "【利用記録】"
    event_line = (
        f"{label} ¥{event.amount_yen:,}（削除）"
        if event.deleted
        else f"{label} ¥{event.amount_yen:,}"
    )
    return _content(
        "\n".join(
            (
                heading,
                "",
                event_line,
                f"記録時刻 {occurred:%H:%M}",
                "",
                f"本日の合計 ¥{totals.total_amount_yen:,}",
                f"ウニ ¥{totals.chunithm_amount_yen:,} / "
                f"マイ ¥{totals.maimai_amount_yen:,} / "
                f"ゲキ ¥{totals.ongeki_amount_yen:,}",
            )
        )
    )


def format_live_daily_message(totals: SpendingDayTotals) -> str:
    return _content(
        "\n".join(
            (
                "【本日の音ゲー利用額】",
                "",
                f"合計 ¥{totals.total_amount_yen:,}",
                "",
                f"ウニ ¥{totals.chunithm_amount_yen:,}",
                f"マイ ¥{totals.maimai_amount_yen:,}",
                f"ゲキ ¥{totals.ongeki_amount_yen:,}",
            )
        )
    )


def totals_fingerprint(totals: SpendingDayTotals, *, purpose: str) -> str:
    payload = "\0".join(
        (
            purpose,
            totals.local_date.isoformat(),
            str(totals.chunithm_amount_yen),
            str(totals.maimai_amount_yen),
            str(totals.ongeki_amount_yen),
            str(totals.total_amount_yen),
            str(totals.active_event_count),
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def spending_event_nonce(event_id: str, revision: int) -> str:
    """Return a stable, Discord-sized nonce for one event revision."""

    return hashlib.sha256(f"{event_id}\0{revision}".encode()).hexdigest()[:25]


def spending_record_from_totals(totals: SpendingDayTotals) -> SpendingRecord:
    return SpendingRecord(
        id=0,
        user_id=totals.rhythm_user_id,
        local_date=totals.local_date,
        time_zone=totals.time_zone,
        chunithm_amount_yen=totals.chunithm_amount_yen,
        maimai_amount_yen=totals.maimai_amount_yen,
        ongeki_amount_yen=totals.ongeki_amount_yen,
        total_amount_yen=totals.total_amount_yen,
        # Daily Discord semantics depend on displayed yen totals, not an
        # arbitrary per-event revision number.
        revision=0,
        synced_at=totals.updated_at or datetime.now(UTC).replace(tzinfo=None),
        deleted=False,
    )


@dataclass(frozen=True, slots=True)
class SpendingEventPublishOutcome:
    event_id: str
    revision: int
    channel_id: int
    operation: str
    status: str
    posted: bool
    edited: bool
    deleted: bool
    dry_run: bool
    duplicate: bool
    message_id: str | None
    planned_content: str
    totals: SpendingDayTotals
    error_code: str | None = None


class RhythmSpendingDiscordPublisher:
    def __init__(
        self,
        *,
        event_store: RhythmSpendingEventPersistence,
        delivery_store: RhythmPersistence,
        transport: DiscordTransport,
        dry_run: bool,
        max_attempts: int = 3,
        base_backoff_seconds: float = 1.0,
    ) -> None:
        self.event_store = event_store
        self.delivery_store = delivery_store
        self.transport = transport
        self.dry_run = dry_run
        self.max_attempts = max_attempts
        self.base_backoff_seconds = base_backoff_seconds

    def preview_event(
        self, *, rhythm_user_id: int, event_id: str, channel_id: int
    ) -> SpendingEventPublishOutcome | None:
        event = self.event_store.get_event(
            rhythm_user_id=rhythm_user_id, event_id=event_id
        )
        if event is None:
            return None
        totals = self.event_store.daily_totals(
            rhythm_user_id=rhythm_user_id,
            local_date=event.local_date,
            time_zone=event.time_zone,
        )
        operation = (
            "deleting"
            if event.deleted and event.discord_message_id
            else "editing"
            if event.discord_message_id
            else "sending"
        )
        return SpendingEventPublishOutcome(
            event_id=event.event_id,
            revision=event.revision,
            channel_id=channel_id,
            operation=operation,
            status=event.discord_status,
            posted=False,
            edited=False,
            deleted=False,
            dry_run=True,
            duplicate=event.discord_status == "posted",
            message_id=event.discord_message_id,
            planned_content=format_spending_event_message(event, totals),
            totals=totals,
        )

    async def _operate_event(
        self,
        *,
        operation: str,
        channel_id: int,
        message_id: str | None,
        content: str,
        nonce: str,
    ) -> tuple[str | None, bool, bool]:
        for attempt in range(1, self.max_attempts + 1):
            try:
                if operation == "sending":
                    return (
                        await self.transport.send(
                            channel_id=channel_id,
                            content=content,
                            image_path=None,
                            nonce=nonce,
                        ),
                        False,
                        False,
                    )
                if operation == "editing":
                    if message_id is None:
                        raise DiscordSendError(
                            "discord_message_missing",
                            "編集対象のDiscordメッセージIDがありません。",
                            retryable=False,
                        )
                    try:
                        edited_id = await self.transport.edit(
                            channel_id=channel_id,
                            message_id=message_id,
                            content=content,
                            image_path=None,
                        )
                        return edited_id, True, False
                    except DiscordSendError as exc:
                        if exc.code != "discord_not_found":
                            raise
                        # A user may have manually removed the old message.
                        return (
                            await self.transport.send(
                                channel_id=channel_id,
                                content=content,
                                image_path=None,
                                nonce=nonce,
                            ),
                            False,
                            False,
                        )
                if operation == "deleting":
                    if message_id is None:
                        return None, False, True
                    try:
                        await self.transport.delete(
                            channel_id=channel_id, message_id=message_id
                        )
                    except DiscordSendError as exc:
                        if exc.code != "discord_not_found":
                            raise
                    return message_id, False, True
                raise RuntimeError("unsupported spending Discord operation")
            except DiscordSendError as exc:
                # New sends use Discord's enforced nonce, so retrying this
                # exact event revision cannot create a second message.
                if not exc.retryable or attempt >= self.max_attempts:
                    raise
                delay = (
                    exc.retry_after
                    if exc.retry_after is not None
                    else self.base_backoff_seconds * (2 ** (attempt - 1))
                )
                await asyncio.sleep(max(0.0, delay))
        raise DiscordSendError(
            "delivery_exhausted",
            "Discord送信の再試行回数を超えました。",
            retryable=False,
        )

    async def publish_event(
        self,
        *,
        rhythm_user_id: int,
        event_id: str,
        channel_id: int,
        now: datetime | None = None,
    ) -> SpendingEventPublishOutcome | None:
        if self.dry_run:
            return self.preview_event(
                rhythm_user_id=rhythm_user_id,
                event_id=event_id,
                channel_id=channel_id,
            )
        claim = self.event_store.claim_event(
            rhythm_user_id=rhythm_user_id,
            event_id=event_id,
            channel_id=channel_id,
            now=now,
        )
        if claim is None:
            current = self.event_store.get_event(
                rhythm_user_id=rhythm_user_id, event_id=event_id
            )
            if current is None:
                return None
            totals = self.event_store.daily_totals(
                rhythm_user_id=rhythm_user_id,
                local_date=current.local_date,
                time_zone=current.time_zone,
            )
            return SpendingEventPublishOutcome(
                event_id=current.event_id,
                revision=current.revision,
                channel_id=channel_id,
                operation="none",
                status=current.discord_status,
                posted=False,
                edited=False,
                deleted=current.deleted,
                dry_run=False,
                duplicate=True,
                message_id=current.discord_message_id,
                planned_content=format_spending_event_message(current, totals),
                totals=totals,
            )
        event = claim.record
        totals = self.event_store.daily_totals(
            rhythm_user_id=rhythm_user_id,
            local_date=event.local_date,
            time_zone=event.time_zone,
        )
        content = format_spending_event_message(event, totals)
        try:
            message_id, edited, deleted = await self._operate_event(
                operation=claim.operation,
                channel_id=channel_id,
                message_id=event.discord_message_id,
                content=content,
                nonce=spending_event_nonce(event.event_id, claim.claim_revision),
            )
        except DiscordSendError as exc:
            failed = self.event_store.fail_delivery(
                event_id=event.event_id,
                claim_revision=claim.claim_revision,
                attempt_count=claim.attempt_count,
                operation=claim.operation,
                error_message=f"{exc.code}: {exc.safe_message}",
                retryable=exc.retryable,
                retry_after_seconds=exc.retry_after,
                now=now,
            )
            return SpendingEventPublishOutcome(
                event_id=event.event_id,
                revision=event.revision,
                channel_id=channel_id,
                operation=claim.operation,
                status=failed.discord_status,
                posted=False,
                edited=False,
                deleted=False,
                dry_run=False,
                duplicate=False,
                message_id=event.discord_message_id,
                planned_content=content,
                totals=totals,
                error_code=exc.code,
            )
        finished = self.event_store.finish_delivery(
            event_id=event.event_id,
            claim_revision=claim.claim_revision,
            attempt_count=claim.attempt_count,
            operation=claim.operation,
            channel_id=channel_id,
            message_id=message_id,
            now=now,
        )
        return SpendingEventPublishOutcome(
            event_id=event.event_id,
            revision=event.revision,
            channel_id=channel_id,
            operation=claim.operation,
            status=finished.discord_status,
            posted=True,
            edited=edited,
            deleted=deleted,
            dry_run=False,
            duplicate=False,
            message_id=message_id,
            planned_content=content,
            totals=totals,
        )

    async def publish_daily_live(
        self,
        *,
        totals: SpendingDayTotals,
        channel_id: int,
        now: datetime | None = None,
    ) -> DiscordPublishOutcome:
        content = format_live_daily_message(totals)
        if self.dry_run:
            return DiscordPublishOutcome(
                False, False, True, False, False, None, content, 0
            )
        # A delete arriving before any message was ever posted is already
        # externally complete.
        if totals.active_event_count == 0 and not self.delivery_store.has_posted_spending_delivery(
            user_id=totals.rhythm_user_id,
            local_date=totals.local_date,
            channel_id=channel_id,
            delivery_kind="spending_live",
        ):
            self.event_store.mark_day_delivery(
                rhythm_user_id=totals.rhythm_user_id,
                local_date=totals.local_date,
                channel_id=channel_id,
                message_id=None,
                now=now,
            )
            return DiscordPublishOutcome(
                False, False, False, True, False, None, content, 0
            )
        claim = self.delivery_store.claim_delivery(
            user_id=totals.rhythm_user_id,
            local_date=totals.local_date,
            delivery_kind="spending_live",
            game=None,
            channel_id=channel_id,
            semantic_fingerprint=totals_fingerprint(totals, purpose="live"),
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
            if totals.active_event_count == 0 and claim.existing_message_id is not None:
                try:
                    await self.transport.delete(
                        channel_id=channel_id,
                        message_id=claim.existing_message_id,
                    )
                except DiscordSendError as exc:
                    if exc.code != "discord_not_found":
                        raise
                message_id = claim.existing_message_id
                edited = False
            elif claim.existing_message_id is not None:
                try:
                    message_id = await self.transport.edit(
                        channel_id=channel_id,
                        message_id=claim.existing_message_id,
                        content=content,
                        image_path=None,
                    )
                    edited = True
                except DiscordSendError as exc:
                    if exc.code != "discord_not_found":
                        raise
                    message_id = await self.transport.send(
                        channel_id=channel_id,
                        content=content,
                        image_path=None,
                    )
                    edited = False
            else:
                message_id = await self.transport.send(
                    channel_id=channel_id,
                    content=content,
                    image_path=None,
                )
                edited = False
        except DiscordSendError as exc:
            marker = (
                self.delivery_store.mark_delivery_unknown
                if exc.outcome_unknown and claim.existing_message_id is None
                else self.delivery_store.mark_delivery_failed
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
        self.delivery_store.mark_delivery_succeeded(
            delivery_id=claim.delivery_id,
            claim_attempt_count=claim.attempt_count,
            discord_message_id=message_id,
            posted_at=now,
        )
        self.event_store.mark_day_delivery(
            rhythm_user_id=totals.rhythm_user_id,
            local_date=totals.local_date,
            channel_id=channel_id,
            message_id=message_id,
            now=now,
        )
        return DiscordPublishOutcome(
            True, edited, False, False, False, message_id, content, claim.attempt_count
        )


async def publish_daily_summary(
    *,
    publisher: RhythmDiscordPublisher,
    totals: SpendingDayTotals,
    channel_id: int,
    post_zero_days: bool,
    now: datetime | None = None,
) -> DiscordPublishOutcome:
    return await publisher.publish_spending(
        user_id=totals.rhythm_user_id,
        record=spending_record_from_totals(totals),
        channel_id=channel_id,
        post_zero_days=post_zero_days,
        now=now,
    )


SpendingPostMode = Literal["eachEvent", "updateDailyMessage", "dailyOnly"]
