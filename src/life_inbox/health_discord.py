"""Discord formatting and idempotent publication for Apple Health summaries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .health_persistence import SleepSummaryRecord, WorkoutSummaryRecord
from .rhythm_discord import (
    DiscordPublishOutcome,
    DiscordSendError,
    DiscordTransport,
)
from .rhythm_persistence import RhythmPersistence

ACTIVITY_LABELS = {
    "walking": "ウォーキング",
    "running": "ランニング",
    "cycling": "サイクリング",
    "swimming": "スイミング",
    "hiking": "ハイキング",
    "traditionalStrengthTraining": "筋力トレーニング",
    "functionalStrengthTraining": "機能的筋力トレーニング",
    "highIntensityIntervalTraining": "高強度インターバル",
    "yoga": "ヨガ",
    "dance": "ダンス",
    "fitnessGaming": "フィットネスゲーム",
    "other": "その他のワークアウト",
}


def _duration(minutes: int) -> str:
    hours, remainder = divmod(minutes, 60)
    if hours and remainder:
        return f"{hours}時間{remainder}分"
    if hours:
        return f"{hours}時間"
    return f"{remainder}分"


def _local_time(value: datetime, timezone_name: str) -> str:
    if value.tzinfo is None:
        from datetime import UTC

        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo(timezone_name)).strftime("%H:%M")


def format_sleep_message(record: SleepSummaryRecord) -> str:
    day = record.local_date
    if record.deleted:
        return (
            f"【{day.year}年{day.month}月{day.day}日 睡眠】\n\n"
            "この睡眠記録はiPhone側で削除されました。"
        )
    lines = [
        f"【{day.year}年{day.month}月{day.day}日 睡眠】",
        "",
        f"睡眠時間 {_duration(record.asleep_minutes)}",
        f"就床 {_local_time(record.sleep_start, record.time_zone)} / "
        f"起床 {_local_time(record.sleep_end, record.time_zone)}",
    ]
    if record.core_minutes or record.deep_minutes or record.rem_minutes:
        lines.extend(
            [
                "",
                f"コア {_duration(record.core_minutes)} / "
                f"深い {_duration(record.deep_minutes)} / "
                f"REM {_duration(record.rem_minutes)}",
            ]
        )
    if record.awake_minutes:
        lines.append(f"覚醒 {_duration(record.awake_minutes)}")
    lines.extend(["", "取得元 Apple Watch / ヘルスケア"])
    return "\n".join(lines)


def format_workout_message(
    record: WorkoutSummaryRecord,
    *,
    include_heart_rate: bool,
) -> str:
    if record.deleted:
        return "【ワークアウト】\n\nこのワークアウト記録はiPhone側で削除されました。"
    activity = ACTIVITY_LABELS.get(record.activity_type, record.activity_type[:64])
    lines = [
        "【ワークアウト完了】",
        "",
        activity,
        f"時間 {_duration(round(record.duration_seconds / 60))}",
    ]
    if record.distance_meters is not None:
        lines.append(f"距離 {record.distance_meters / 1_000:.2f}km")
    if record.active_energy_kcal is not None:
        lines.append(f"消費カロリー {record.active_energy_kcal:,}kcal")
    if include_heart_rate and record.average_heart_rate_bpm is not None:
        lines.append(f"平均心拍数 {record.average_heart_rate_bpm}bpm")
    lines.extend(
        [
            f"開始 {_local_time(record.started_at, record.time_zone)} / "
            f"終了 {_local_time(record.ended_at, record.time_zone)}",
            "",
            "取得元 Apple Watch / ヘルスケア",
        ]
    )
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class HealthDryRunPlan:
    kind: str
    entity_key: str
    channel_id: int
    semantic_fingerprint: str
    content: str


class HealthDiscordPublisher:
    def __init__(
        self,
        persistence: RhythmPersistence,
        transport: DiscordTransport,
        *,
        dry_run: bool,
        include_heart_rate: bool,
        max_attempts: int = 3,
        base_backoff_seconds: float = 1.0,
    ) -> None:
        self.persistence = persistence
        self.transport = transport
        self.dry_run = dry_run
        self.include_heart_rate = include_heart_rate
        self.max_attempts = max_attempts
        self.base_backoff_seconds = base_backoff_seconds

    async def _send_with_retry(
        self,
        *,
        channel_id: int,
        content: str,
        existing_message_id: str | None,
    ) -> tuple[str, bool, int]:
        for attempt in range(1, self.max_attempts + 1):
            editing = existing_message_id is not None
            try:
                if editing:
                    message_id = await self.transport.edit(
                        channel_id=channel_id,
                        message_id=existing_message_id,
                        content=content,
                        image_path=None,
                    )
                    return message_id, True, attempt
                message_id = await self.transport.send(
                    channel_id=channel_id,
                    content=content,
                    image_path=None,
                )
                return message_id, False, attempt
            except DiscordSendError as exc:
                if editing and exc.code == "discord_not_found":
                    existing_message_id = None
                    continue
                if exc.outcome_unknown and not editing:
                    raise
                if not exc.retryable or attempt >= self.max_attempts:
                    raise
                delay = (
                    exc.retry_after
                    if exc.retry_after is not None
                    else self.base_backoff_seconds * (2 ** (attempt - 1))
                )
                await asyncio.sleep(max(0.0, delay))
        raise DiscordSendError(
            "delivery_exhausted", "Discord送信の再試行回数を超えました。", retryable=False
        )

    async def _publish(
        self,
        *,
        user_id: int,
        local_date: date,
        delivery_kind: str,
        entity_key: str | None,
        channel_id: int,
        semantic_fingerprint: str,
        content: str,
        now: datetime | None,
    ) -> DiscordPublishOutcome:
        if self.dry_run:
            return DiscordPublishOutcome(
                False, False, True, False, False, None, content, 0
            )
        claim = self.persistence.claim_delivery(
            user_id=user_id,
            local_date=local_date,
            delivery_kind=delivery_kind,
            game=entity_key,
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
            message_id, edited, attempts = await self._send_with_retry(
                channel_id=channel_id,
                content=content,
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

    async def publish_sleep(
        self,
        *,
        user_id: int,
        record: SleepSummaryRecord,
        channel_id: int,
        now: datetime | None = None,
    ) -> DiscordPublishOutcome:
        return await self._publish(
            user_id=user_id,
            local_date=record.local_date,
            delivery_kind="health_sleep",
            entity_key=None,
            channel_id=channel_id,
            semantic_fingerprint=record.semantic_fingerprint,
            content=format_sleep_message(record),
            now=now,
        )

    async def publish_workout(
        self,
        *,
        user_id: int,
        record: WorkoutSummaryRecord,
        channel_id: int,
        now: datetime | None = None,
    ) -> DiscordPublishOutcome:
        return await self._publish(
            user_id=user_id,
            local_date=record.local_date,
            delivery_kind="health_workout",
            entity_key=record.workout_key,
            channel_id=channel_id,
            semantic_fingerprint=record.semantic_fingerprint,
            content=format_workout_message(
                record, include_heart_rate=self.include_heart_rate
            ),
            now=now,
        )
