from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select

from life_inbox.config import Settings
from life_inbox.db import Database
from life_inbox.rhythm_discord import (
    DiscordPyTransport,
    DiscordSendError,
    RhythmDiscordPublisher,
)
from life_inbox.rhythm_models import DiscordDeliveryRow
from life_inbox.rhythm_persistence import RhythmPersistence
from life_inbox.rhythm_spending import RhythmSpendingEventPersistence
from life_inbox.rhythm_spending_discord import (
    RhythmSpendingDiscordPublisher,
    spending_event_nonce,
)
from life_inbox.rhythm_spending_models import RhythmSpendingEventRow
from life_inbox.rhythm_spending_scheduler import RhythmSpendingScheduler
from life_inbox.web import create_web_app

RAW_TOKEN = "rhythm-spending-event-test-token"
AUTH = {"Authorization": f"Bearer {RAW_TOKEN}"}


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.sent_nonces: list[str | int | None] = []
        self.edited: list[tuple[int, str, str]] = []
        self.deleted: list[tuple[int, str]] = []
        self.failures: list[DiscordSendError] = []

    def _raise_if_needed(self) -> None:
        if self.failures:
            raise self.failures.pop(0)

    async def send(
        self, *, channel_id: int, content: str, image_path=None, nonce=None
    ) -> str:
        self._raise_if_needed()
        self.sent.append((channel_id, content))
        self.sent_nonces.append(nonce)
        return str(10_000 + len(self.sent))

    async def edit(
        self, *, channel_id: int, message_id: str, content: str, image_path=None
    ) -> str:
        self._raise_if_needed()
        self.edited.append((channel_id, message_id, content))
        return message_id

    async def delete(self, *, channel_id: int, message_id: str) -> None:
        self._raise_if_needed()
        self.deleted.append((channel_id, message_id))


@pytest.mark.asyncio
async def test_discord_transport_forwards_enforced_nonce_input():
    class Message:
        id = 12345

    class Channel:
        def __init__(self) -> None:
            self.kwargs = None

        async def send(self, **kwargs):
            self.kwargs = kwargs
            return Message()

    channel = Channel()

    class Bot:
        @staticmethod
        def get_channel(channel_id):
            return channel if channel_id == 999 else None

    message_id = await DiscordPyTransport(Bot()).send(
        channel_id=999,
        content="利用記録",
        nonce="stable-spending-nonce",
    )

    assert message_id == "12345"
    assert channel.kwargs["nonce"] == "stable-spending-nonce"


@pytest.fixture
def stores(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'events.db'}")
    database.init()
    rhythm = RhythmPersistence(database.session_factory)
    events = RhythmSpendingEventPersistence(database.session_factory)
    user_id = rhythm.ensure_user(
        display_name="テスト", time_zone="Asia/Tokyo", discord_user_id=123
    )
    return database, rhythm, events, user_id


@pytest.fixture
def event_api(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'api.db'}",
        data_dir=tmp_path,
        allowed_user_id=24680,
        rhythm_device_sync_token_sha256=hashlib.sha256(RAW_TOKEN.encode()).hexdigest(),
        rhythm_enabled=True,
        rhythm_spending_immediate_post_enabled=True,
        rhythm_spending_daily_summary_enabled=True,
        discord_credit_channel_id=999,
    )
    database = Database(settings.database_url)
    database.init()
    app = create_web_app(settings, database=database)
    with TestClient(app) as client:
        yield client, database


def payload(
    event_id: str,
    *,
    revision: int = 1,
    game: str = "maimai",
    amount: int = 600,
    source: str = "app",
    deleted: bool = False,
    occurred_at: str = "2026-08-30T18:42:00+09:00",
):
    return {
        "eventId": event_id,
        "revision": revision,
        "userId": "example-user",
        "game": game,
        "amountYen": amount,
        "occurredAt": occurred_at,
        "timeZone": "Asia/Tokyo",
        "deleted": deleted,
        "source": source,
    }


def sync_event(
    events: RhythmSpendingEventPersistence,
    user_id: int,
    event_id: str,
    *,
    revision: int = 1,
    game: str = "maimai",
    amount: int = 600,
    source: str = "app",
    deleted: bool = False,
    occurred_at: datetime | None = None,
):
    return events.sync_event(
        rhythm_user_id=user_id,
        event_id=str(UUID(event_id)),
        source_user_id="example-user",
        game=game,
        amount_yen=amount,
        occurred_at=occurred_at
        or datetime(2026, 8, 30, 18, 42, tzinfo=ZoneInfo("Asia/Tokyo")),
        time_zone="Asia/Tokyo",
        revision=revision,
        source=source,
        deleted=deleted,
    )


def test_event_api_is_idempotent_revisioned_and_returns_yen_totals(event_api):
    client, database = event_api
    event_id = str(uuid4()).upper()
    url = f"/api/v1/rhythm/spending/events/{event_id}"

    created = client.put(url, headers=AUTH, json=payload(event_id))
    repeated = client.put(url, headers=AUTH, json=payload(event_id))
    conflict = client.put(
        url, headers=AUTH, json=payload(event_id, amount=500)
    )
    updated = client.put(
        url, headers=AUTH, json=payload(event_id, revision=2, amount=500)
    )
    stale = client.put(url, headers=AUTH, json=payload(event_id))

    assert created.status_code == 200
    assert created.json()["data"]["amountYen"] == 600
    assert created.json()["dailyTotals"]["totalAmountYen"] == 600
    assert created.json()["data"]["discord"]["status"] == "pending"
    assert created.json()["discordDeliveryIsAsynchronous"] is True
    assert repeated.json()["idempotent"] is True
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "revision_conflict"
    assert updated.json()["data"]["amountYen"] == 500
    assert updated.json()["data"]["revision"] == 2
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_revision"
    with database.session() as session:
        assert session.scalar(select(func.count(RhythmSpendingEventRow.id))) == 1


def test_event_api_accepts_widget_shortcut_and_rejects_vending(event_api):
    client, _ = event_api
    widget_id = str(uuid4())
    shortcut_id = str(uuid4())
    widget = client.put(
        f"/api/v1/rhythm/spending/events/{widget_id}",
        headers=AUTH,
        json=payload(widget_id, source="widget", game="chunithm", amount=100),
    )
    shortcut = client.put(
        f"/api/v1/rhythm/spending/events/{shortcut_id}",
        headers=AUTH,
        json=payload(shortcut_id, source="shortcut", game="ongeki", amount=300),
    )
    vending_id = str(uuid4())
    vending = client.put(
        f"/api/v1/rhythm/spending/events/{vending_id}",
        headers=AUTH,
        json=payload(vending_id, game="vending", amount=150),
    )

    assert widget.status_code == 200
    assert shortcut.status_code == 200
    assert shortcut.json()["dailyTotals"] == {
        "localDate": "2026-08-30",
        "timeZone": "Asia/Tokyo",
        "chunithmAmountYen": 100,
        "maimaiAmountYen": 0,
        "ongekiAmountYen": 300,
        "totalAmountYen": 400,
        "activeEventCount": 2,
    }
    assert vending.status_code == 422
    assert vending.json()["error"]["code"] == "invalid_request"


def test_event_local_date_uses_asia_tokyo_boundary_and_auth(event_api):
    client, _ = event_api
    event_id = str(uuid4())
    unauthorized = client.put(
        f"/api/v1/rhythm/spending/events/{event_id}",
        json=payload(event_id),
    )
    response = client.put(
        f"/api/v1/rhythm/spending/events/{event_id}",
        headers=AUTH,
        json=payload(event_id, occurred_at="2026-08-30T00:05:00+09:00"),
    )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json()["data"]["localDate"] == "2026-08-30"
    assert response.json()["data"]["occurredAt"] == "2026-08-29T15:05:00Z"


def test_event_schema_contains_delivery_and_revision_contract(stores):
    database, _, _, _ = stores
    columns = {
        column["name"]
        for column in inspect(database.engine).get_columns("rhythm_spending_events")
    }
    assert {
        "event_id",
        "user_id",
        "game",
        "amount_yen",
        "occurred_at",
        "local_date",
        "time_zone",
        "revision",
        "source",
        "deleted_at",
        "discord_channel_id",
        "discord_message_id",
        "discord_status",
        "discord_last_error",
        "synced_at",
        "created_at",
        "updated_at",
    } <= columns


@pytest.mark.asyncio
async def test_each_event_posts_yen_edits_revision_and_deletes_message(stores):
    _, rhythm, events, user_id = stores
    event_id = str(uuid4())
    sync_event(events, user_id, event_id, game="maimai", amount=600)
    transport = FakeTransport()
    publisher = RhythmSpendingDiscordPublisher(
        event_store=events,
        delivery_store=rhythm,
        transport=transport,
        dry_run=False,
        base_backoff_seconds=0,
    )

    first = await publisher.publish_event(
        rhythm_user_id=user_id, event_id=event_id, channel_id=999
    )
    duplicate = await publisher.publish_event(
        rhythm_user_id=user_id, event_id=event_id, channel_id=999
    )
    sync_event(events, user_id, event_id, revision=2, game="maimai", amount=500)
    edited = await publisher.publish_event(
        rhythm_user_id=user_id, event_id=event_id, channel_id=999
    )
    sync_event(
        events,
        user_id,
        event_id,
        revision=3,
        game="maimai",
        amount=500,
        deleted=True,
    )
    deleted = await publisher.publish_event(
        rhythm_user_id=user_id, event_id=event_id, channel_id=999
    )

    assert first is not None and first.posted is True
    assert first.planned_content.startswith("【利用記録】")
    assert "マイ ¥600" in first.planned_content
    assert "本日の合計 ¥600" in first.planned_content
    assert "cr" not in first.planned_content.casefold()
    assert duplicate is not None and duplicate.duplicate is True
    assert edited is not None and edited.edited is True
    assert "マイ ¥500" in transport.edited[0][2]
    assert deleted is not None and deleted.deleted is True
    assert len(transport.sent) == 1
    assert len(transport.edited) == 1
    assert len(transport.deleted) == 1
    saved = events.get_event(rhythm_user_id=user_id, event_id=event_id)
    assert saved is not None and saved.discord_status == "posted"
    assert saved.deleted is True


@pytest.mark.asyncio
async def test_three_game_totals_and_transient_discord_failure_are_safe(stores):
    _, rhythm, events, user_id = stores
    ids = [str(uuid4()) for _ in range(3)]
    sync_event(events, user_id, ids[0], game="chunithm", amount=100)
    sync_event(events, user_id, ids[1], game="maimai", amount=600)
    sync_event(events, user_id, ids[2], game="ongeki", amount=300)
    totals = events.daily_totals(
        rhythm_user_id=user_id,
        local_date=date(2026, 8, 30),
        time_zone="Asia/Tokyo",
    )
    assert (
        totals.chunithm_amount_yen,
        totals.maimai_amount_yen,
        totals.ongeki_amount_yen,
        totals.total_amount_yen,
    ) == (100, 600, 300, 1_000)

    transport = FakeTransport()
    transport.failures.append(
        DiscordSendError(
            "discord_rate_limited",
            "一時的なレート制限です。",
            retryable=True,
            retry_after=0,
        )
    )
    publisher = RhythmSpendingDiscordPublisher(
        event_store=events,
        delivery_store=rhythm,
        transport=transport,
        dry_run=False,
        base_backoff_seconds=0,
    )
    result = await publisher.publish_event(
        rhythm_user_id=user_id, event_id=ids[0], channel_id=999
    )
    assert result is not None and result.posted is True
    saved = events.get_event(rhythm_user_id=user_id, event_id=ids[0])
    assert saved is not None and saved.discord_status == "posted"


@pytest.mark.asyncio
async def test_permanent_discord_failure_keeps_record_and_dry_run_never_posts(stores):
    _, rhythm, events, user_id = stores
    failed_id = str(uuid4())
    dry_id = str(uuid4())
    sync_event(events, user_id, failed_id, game="ongeki", amount=300)
    sync_event(events, user_id, dry_id, game="chunithm", amount=100)
    transport = FakeTransport()
    transport.failures.append(
        DiscordSendError(
            "discord_forbidden", "権限がありません。", retryable=False
        )
    )
    publisher = RhythmSpendingDiscordPublisher(
        event_store=events,
        delivery_store=rhythm,
        transport=transport,
        dry_run=False,
    )
    failed = await publisher.publish_event(
        rhythm_user_id=user_id, event_id=failed_id, channel_id=999
    )
    saved = events.get_event(rhythm_user_id=user_id, event_id=failed_id)
    assert failed is not None and failed.error_code == "discord_forbidden"
    assert saved is not None and saved.discord_status == "failed"
    assert saved.amount_yen == 300

    dry_transport = FakeTransport()
    dry_publisher = RhythmSpendingDiscordPublisher(
        event_store=events,
        delivery_store=rhythm,
        transport=dry_transport,
        dry_run=True,
    )
    preview = await dry_publisher.publish_event(
        rhythm_user_id=user_id, event_id=dry_id, channel_id=999
    )
    assert preview is not None and preview.dry_run is True
    assert preview.event_id == str(UUID(dry_id))
    assert preview.revision == 1
    assert preview.channel_id == 999
    assert preview.totals.total_amount_yen == 400
    assert dry_transport.sent == []
    pending = events.get_event(rhythm_user_id=user_id, event_id=dry_id)
    assert pending is not None and pending.discord_status == "pending"


@pytest.mark.asyncio
async def test_exhausted_temporary_failure_enters_retry_waiting(stores):
    _, rhythm, events, user_id = stores
    event_id = str(uuid4())
    sync_event(events, user_id, event_id, game="chunithm", amount=100)
    transport = FakeTransport()
    transport.failures.append(
        DiscordSendError(
            "discord_rate_limited",
            "一時的なレート制限です。",
            retryable=True,
            retry_after=2,
        )
    )
    publisher = RhythmSpendingDiscordPublisher(
        event_store=events,
        delivery_store=rhythm,
        transport=transport,
        dry_run=False,
        max_attempts=1,
    )

    outcome = await publisher.publish_event(
        rhythm_user_id=user_id, event_id=event_id, channel_id=999
    )
    saved = events.get_event(rhythm_user_id=user_id, event_id=event_id)

    assert outcome is not None and outcome.status == "retryWaiting"
    assert saved is not None and saved.discord_status == "retryWaiting"
    assert saved.discord_next_retry_at is not None


@pytest.mark.asyncio
async def test_unknown_new_send_is_retried_with_stable_enforced_nonce(stores):
    _, rhythm, events, user_id = stores
    event_id = str(uuid4())
    sync_event(events, user_id, event_id, game="maimai", amount=600)
    transport = FakeTransport()
    transport.failures.append(
        DiscordSendError(
            "discord_delivery_unknown",
            "一時的に応答を確認できません。",
            retryable=True,
            outcome_unknown=True,
        )
    )
    publisher = RhythmSpendingDiscordPublisher(
        event_store=events,
        delivery_store=rhythm,
        transport=transport,
        dry_run=False,
        base_backoff_seconds=0,
    )

    outcome = await publisher.publish_event(
        rhythm_user_id=user_id, event_id=event_id, channel_id=999
    )

    assert outcome is not None and outcome.posted is True
    assert transport.sent_nonces == [spending_event_nonce(event_id, 1)]
    assert len(transport.sent_nonces[0]) == 25


@pytest.mark.asyncio
async def test_update_daily_message_mode_sends_once_then_edits(stores):
    _, rhythm, events, user_id = stores
    first_id, second_id = str(uuid4()), str(uuid4())
    sync_event(events, user_id, first_id, game="chunithm", amount=100)
    sync_event(events, user_id, second_id, game="maimai", amount=600)
    transport = FakeTransport()
    event_publisher = RhythmSpendingDiscordPublisher(
        event_store=events,
        delivery_store=rhythm,
        transport=transport,
        dry_run=False,
    )
    settings = Settings(
        _env_file=None,
        rhythm_enabled=True,
        rhythm_spending_immediate_post_enabled=True,
        rhythm_spending_daily_summary_enabled=False,
        rhythm_spending_post_mode="updateDailyMessage",
        discord_credit_channel_id=999,
    )
    scheduler = RhythmSpendingScheduler(
        settings=settings,
        event_store=events,
        delivery_store=rhythm,
        event_publisher=event_publisher,
        daily_publisher=RhythmDiscordPublisher(rhythm, transport, dry_run=False),
        rhythm_user_id=user_id,
    )

    first = await scheduler.process_pending_once()
    sync_event(events, user_id, second_id, revision=2, game="maimai", amount=500)
    second = await scheduler.process_pending_once()

    assert len(first) == 1 and first[0].posted is True
    assert len(second) == 1 and second[0].edited is True
    assert len(transport.sent) == 1
    assert len(transport.edited) == 1
    assert "合計 ¥600" in transport.edited[0][2]
    first_record = events.get_event(rhythm_user_id=user_id, event_id=first_id)
    second_record = events.get_event(rhythm_user_id=user_id, event_id=second_id)
    assert first_record is not None and second_record is not None
    assert first_record.discord_message_id == second_record.discord_message_id


@pytest.mark.asyncio
async def test_daily_summary_and_zero_day_setting(stores):
    _, rhythm, events, user_id = stores
    event_id = str(uuid4())
    sync_event(events, user_id, event_id, game="maimai", amount=600)
    transport = FakeTransport()
    event_publisher = RhythmSpendingDiscordPublisher(
        event_store=events,
        delivery_store=rhythm,
        transport=transport,
        dry_run=False,
    )
    daily_publisher = RhythmDiscordPublisher(
        rhythm, transport, dry_run=False, base_backoff_seconds=0
    )
    settings = Settings(
        _env_file=None,
        rhythm_enabled=True,
        rhythm_spending_immediate_post_enabled=True,
        rhythm_spending_daily_summary_enabled=True,
        rhythm_spending_post_zero_days=False,
        discord_credit_channel_id=999,
    )
    scheduler = RhythmSpendingScheduler(
        settings=settings,
        event_store=events,
        delivery_store=rhythm,
        event_publisher=event_publisher,
        daily_publisher=daily_publisher,
        rhythm_user_id=user_id,
        now=lambda: datetime(2026, 8, 30, 14, 55, tzinfo=UTC),
    )

    summary = await scheduler.run_daily_summary_once(local_date=date(2026, 8, 30))
    empty = await scheduler.run_daily_summary_once(local_date=date(2026, 8, 29))

    assert summary is not None and summary.posted is True
    assert summary.planned_content.startswith("【2026年8月30日 音ゲー利用額】")
    assert "合計 ¥600" in summary.planned_content
    assert "マイ ¥600" in summary.planned_content
    assert "cr" not in summary.planned_content.casefold()
    assert empty is None
    with rhythm.session_factory() as session:
        assert session.scalar(select(func.count(DiscordDeliveryRow.id))) == 1


@pytest.mark.asyncio
async def test_zero_day_summary_can_be_enabled(stores):
    _, rhythm, events, user_id = stores
    transport = FakeTransport()
    settings = Settings(
        _env_file=None,
        rhythm_enabled=True,
        rhythm_spending_immediate_post_enabled=False,
        rhythm_spending_daily_summary_enabled=True,
        rhythm_post_zero_spending_days=True,
        discord_credit_channel_id=999,
    )
    scheduler = RhythmSpendingScheduler(
        settings=settings,
        event_store=events,
        delivery_store=rhythm,
        event_publisher=RhythmSpendingDiscordPublisher(
            event_store=events,
            delivery_store=rhythm,
            transport=transport,
            dry_run=False,
        ),
        daily_publisher=RhythmDiscordPublisher(
            rhythm, transport, dry_run=False, base_backoff_seconds=0
        ),
        rhythm_user_id=user_id,
        now=lambda: datetime(2026, 8, 30, 14, 55, tzinfo=UTC),
    )

    outcome = await scheduler.run_daily_summary_once(local_date=date(2026, 8, 30))

    assert outcome is not None and outcome.posted is True
    assert "合計 ¥0" in outcome.planned_content


def test_daily_summary_schedule_defaults_to_2355_jst(stores):
    _, rhythm, events, user_id = stores
    transport = FakeTransport()
    settings = Settings(
        _env_file=None,
        rhythm_enabled=True,
        rhythm_spending_daily_summary_enabled=True,
        discord_credit_channel_id=999,
    )
    scheduler = RhythmSpendingScheduler(
        settings=settings,
        event_store=events,
        delivery_store=rhythm,
        event_publisher=RhythmSpendingDiscordPublisher(
            event_store=events,
            delivery_store=rhythm,
            transport=transport,
            dry_run=True,
        ),
        daily_publisher=RhythmDiscordPublisher(rhythm, transport, dry_run=True),
        rhythm_user_id=user_id,
    )

    scheduled = scheduler._scheduled_today(
        datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    )

    assert settings.rhythm_spending_daily_summary_cron == "55 23 * * *"
    assert scheduled == datetime(2026, 8, 30, 14, 55, tzinfo=UTC)


@pytest.mark.asyncio
async def test_daily_only_mode_posts_no_immediate_message(stores):
    _, rhythm, events, user_id = stores
    event_id = str(uuid4())
    sync_event(events, user_id, event_id, game="maimai", amount=600)
    transport = FakeTransport()
    settings = Settings(
        _env_file=None,
        rhythm_enabled=True,
        rhythm_spending_immediate_post_enabled=True,
        rhythm_spending_daily_summary_enabled=True,
        rhythm_spending_post_mode="dailyOnly",
        discord_credit_channel_id=999,
    )
    scheduler = RhythmSpendingScheduler(
        settings=settings,
        event_store=events,
        delivery_store=rhythm,
        event_publisher=RhythmSpendingDiscordPublisher(
            event_store=events,
            delivery_store=rhythm,
            transport=transport,
            dry_run=False,
        ),
        daily_publisher=RhythmDiscordPublisher(rhythm, transport, dry_run=False),
        rhythm_user_id=user_id,
        now=lambda: datetime(2026, 8, 30, 14, 55, tzinfo=UTC),
    )

    immediate = await scheduler.process_pending_once()
    summary = await scheduler.run_daily_summary_once(local_date=date(2026, 8, 30))

    assert immediate == []
    assert summary is not None and summary.posted is True
    assert len(transport.sent) == 1
    assert transport.sent[0][1].startswith("【2026年8月30日 音ゲー利用額】")
    saved = events.get_event(rhythm_user_id=user_id, event_id=event_id)
    assert saved is not None and saved.discord_status == "posted"
