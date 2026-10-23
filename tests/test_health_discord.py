from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from life_inbox.config import Settings
from life_inbox.db import Database
from life_inbox.health_discord import (
    HealthDiscordPublisher,
    format_sleep_message,
    format_workout_message,
)
from life_inbox.health_persistence import HealthPersistence
from life_inbox.health_scheduler import HealthScheduler, sleep_post_instant
from life_inbox.rhythm_persistence import RhythmPersistence


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edited: list[tuple[int, str, str]] = []

    async def send(self, *, channel_id: int, content: str, image_path=None) -> str:
        self.sent.append((channel_id, content))
        return str(1000 + len(self.sent))

    async def edit(
        self, *, channel_id: int, message_id: str, content: str, image_path=None
    ) -> str:
        self.edited.append((channel_id, message_id, content))
        return message_id


@pytest.fixture
def health_store(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'health-discord.db'}")
    database.init()
    health = HealthPersistence(database.session_factory)
    deliveries = RhythmPersistence(database.session_factory)
    user_id = health.ensure_user(
        display_name="テスト", time_zone="Asia/Tokyo", discord_user_id=123
    )
    return health, deliveries, user_id


def _sleep(health: HealthPersistence, user_id: int, *, revision: int = 1):
    return health.sync_sleep(
        user_id=user_id,
        local_date=date(2026, 8, 30),
        time_zone="Asia/Tokyo",
        sleep_start=datetime(2026, 8, 29, 23, 30, tzinfo=UTC),
        sleep_end=datetime(2026, 8, 30, 7, 30, tzinfo=UTC),
        asleep_minutes=420 + revision,
        awake_minutes=20,
        core_minutes=250,
        deep_minutes=70,
        rem_minutes=100,
        in_bed_minutes=480,
        source="appleWatch",
        revision=revision,
    ).record


def _workout(health: HealthPersistence, user_id: int, *, revision: int = 1):
    return health.sync_workout(
        user_id=user_id,
        workout_key=hashlib.sha256(b"workout").hexdigest(),
        time_zone="Asia/Tokyo",
        activity_type="walking",
        started_at=datetime(2026, 8, 30, 9, 12, tzinfo=UTC),
        ended_at=datetime(2026, 8, 30, 9, 54, tzinfo=UTC),
        duration_seconds=2_520,
        distance_meters=3_800,
        active_energy_kcal=210 + revision,
        average_heart_rate_bpm=112,
        source="appleWatch",
        revision=revision,
    ).record


def test_health_messages_are_summary_only_and_hide_heart_rate_by_default(health_store):
    health, _, user_id = health_store
    sleep = format_sleep_message(_sleep(health, user_id))
    workout = format_workout_message(
        _workout(health, user_id), include_heart_rate=False
    )

    assert "睡眠時間" in sleep
    assert "深い" in sleep
    assert "ウォーキング" in workout
    assert "3.80km" in workout
    assert "心拍" not in workout
    assert "latitude" not in workout.casefold()


@pytest.mark.asyncio
async def test_health_publisher_deduplicates_then_edits_new_revision(health_store):
    health, deliveries, user_id = health_store
    transport = FakeTransport()
    publisher = HealthDiscordPublisher(
        deliveries, transport, dry_run=False, include_heart_rate=False
    )
    first_record = _workout(health, user_id)

    first = await publisher.publish_workout(
        user_id=user_id, record=first_record, channel_id=555
    )
    duplicate = await publisher.publish_workout(
        user_id=user_id, record=first_record, channel_id=555
    )
    changed = await publisher.publish_workout(
        user_id=user_id,
        record=_workout(health, user_id, revision=2),
        channel_id=555,
    )

    assert first.posted is True
    assert duplicate.duplicate is True
    assert changed.posted is True
    assert changed.edited is True
    assert len(transport.sent) == 1
    assert len(transport.edited) == 1


@pytest.mark.asyncio
async def test_health_dry_run_never_calls_transport(health_store):
    health, deliveries, user_id = health_store
    transport = FakeTransport()
    publisher = HealthDiscordPublisher(
        deliveries, transport, dry_run=True, include_heart_rate=False
    )

    outcome = await publisher.publish_sleep(
        user_id=user_id, record=_sleep(health, user_id), channel_id=555
    )

    assert outcome.dry_run is True
    assert "睡眠時間" in outcome.planned_content
    assert transport.sent == []
    assert transport.edited == []


@pytest.mark.asyncio
async def test_sleep_scheduler_waits_for_explicit_wall_clock_then_dry_runs(health_store):
    health, deliveries, user_id = health_store
    _sleep(health, user_id)
    transport = FakeTransport()
    settings = Settings(
        _env_file=None,
        health_enabled=True,
        health_scheduler_enabled=True,
        health_discord_dry_run=True,
        discord_health_channel_id=555,
        health_sleep_post_cron="0 10 * * *",
        health_time_zone="Asia/Tokyo",
    )
    scheduler = HealthScheduler(
        settings=settings,
        persistence=health,
        publisher=HealthDiscordPublisher(
            deliveries, transport, dry_run=True, include_heart_rate=False
        ),
        user_id=user_id,
    )

    target = sleep_post_instant(date(2026, 8, 30), "0 10 * * *", "Asia/Tokyo")
    before = await scheduler.reconcile_once(now=target - timedelta(minutes=1))
    at_target = await scheduler.reconcile_once(now=target)

    assert before == []
    assert at_target == ["sleep:dry-run"]
    assert transport.sent == []
