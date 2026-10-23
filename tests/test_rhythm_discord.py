from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from life_inbox.rhythm_discord import (
    DiscordPyTransport,
    DiscordSendError,
    RhythmDiscordPublisher,
    format_collection_error_message,
    format_rating_message,
    format_spending_message,
)
from life_inbox.rhythm_models import DiscordDeliveryRow
from life_inbox.rhythm_persistence import RhythmPersistence


class FakeDiscordTransport:
    def __init__(self, failures: list[DiscordSendError] | None = None) -> None:
        self.failures = list(failures or [])
        self.sent: list[dict[str, object]] = []
        self.edited: list[dict[str, object]] = []
        self.next_id = 100

    async def _result(self, calls: list[dict[str, object]], payload: dict[str, object]):
        calls.append(payload)
        if self.failures:
            raise self.failures.pop(0)
        self.next_id += 1
        return str(self.next_id)

    async def send(self, *, channel_id: int, content: str, image_path: Path | None = None):
        return await self._result(
            self.sent,
            {"channel_id": channel_id, "content": content, "image_path": image_path},
        )

    async def edit(
        self,
        *,
        channel_id: int,
        message_id: str,
        content: str,
        image_path: Path | None = None,
    ):
        return await self._result(
            self.edited,
            {
                "channel_id": channel_id,
                "message_id": message_id,
                "content": content,
                "image_path": image_path,
            },
        )


@pytest.mark.asyncio
async def test_discord_transport_attaches_original_image_bytes_without_reencoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import discord

    original = b"\x89PNG\r\n\x1a\nexact-original-image-payload"
    image_path = tmp_path / "rating.png"
    image_path.write_bytes(original)
    sent: dict[str, object] = {}

    class RecordingFile:
        def __init__(self, path: Path) -> None:
            self.path = Path(path)
            self.payload = self.path.read_bytes()

    class Message:
        id = 123

    class Channel:
        async def send(self, **kwargs):
            sent.update(kwargs)
            return Message()

    class Bot:
        @staticmethod
        def get_channel(_channel_id: int):
            return Channel()

    monkeypatch.setattr(discord, "File", RecordingFile)

    message_id = await DiscordPyTransport(Bot()).send(
        channel_id=456,
        content="rating",
        image_path=image_path,
    )

    uploaded = sent["file"]
    assert isinstance(uploaded, RecordingFile)
    assert uploaded.path == image_path
    assert uploaded.payload == original
    assert message_id == "123"


@pytest.fixture
def publication_environment(db):
    persistence = RhythmPersistence(db.session_factory)
    user_id = persistence.ensure_user(
        display_name="Discordテスト",
        time_zone="Asia/Tokyo",
        discord_user_id="discord-publisher-user",
    )
    return persistence, user_id, db


def test_rating_message_normalizes_decimal_storage_scale():
    message = format_rating_message(
        game="chunithm",
        local_date=date(2026, 8, 30),
        rating="16.7200",
        previous_rating="16.7200",
        delta="0.0000",
        collected_label="10:32 JST",
    )

    assert "16.72 → 16.72" in message
    assert "前回比 0" in message
    assert "16.7200" not in message


def test_rating_message_marks_positive_delta():
    message = format_rating_message(
        game="maimai",
        local_date=date(2026, 8, 30),
        rating="15432",
        previous_rating="15420",
        delta="12.0000",
        collected_label="10:32 JST",
    )

    assert "15420 → 15432" in message
    assert "前回比 +12" in message


@pytest.mark.asyncio
async def test_rating_dry_run_has_no_discord_or_delivery_side_effect(publication_environment):
    persistence, user_id, db = publication_environment
    transport = FakeDiscordTransport()
    publisher = RhythmDiscordPublisher(persistence, transport, dry_run=True)

    result = await publisher.publish_rating(
        user_id=user_id,
        local_date=date(2026, 8, 29),
        game="chunithm",
        channel_id=111,
        semantic_fingerprint="a" * 64,
        image_sha256="b" * 64,
        image_path=Path("fixture.png"),
        rating=16.73,
        previous_rating=16.72,
        delta=0.01,
        collected_label="22:18",
    )

    assert result.dry_run is True
    assert result.posted is False
    assert "16.72 → 16.73" in result.planned_content
    assert transport.sent == []
    with db.session() as session:
        assert session.query(DiscordDeliveryRow).count() == 0


@pytest.mark.asyncio
async def test_rating_posts_once_and_skips_same_fingerprint_or_image(publication_environment):
    persistence, user_id, _ = publication_environment
    transport = FakeDiscordTransport()
    publisher = RhythmDiscordPublisher(persistence, transport, dry_run=False)
    kwargs = {
        "user_id": user_id,
        "local_date": date(2026, 8, 29),
        "game": "maimai",
        "channel_id": 222,
        "semantic_fingerprint": "a" * 64,
        "image_sha256": "b" * 64,
        "image_path": Path("fixture.png"),
        "rating": 15432,
        "previous_rating": 15420,
        "delta": 12,
        "collected_label": "22:18",
    }

    first = await publisher.publish_rating(**kwargs)
    duplicate_semantic = await publisher.publish_rating(**kwargs)
    duplicate_image = await publisher.publish_rating(
        **{**kwargs, "semantic_fingerprint": "c" * 64}
    )

    assert first.posted is True
    assert duplicate_semantic.duplicate is True
    assert duplicate_image.duplicate is True
    assert len(transport.sent) == 1
    assert transport.edited == []


@pytest.mark.asyncio
async def test_renderer_refresh_edits_same_semantic_rating_message(
    publication_environment,
):
    persistence, user_id, db = publication_environment
    transport = FakeDiscordTransport()
    publisher = RhythmDiscordPublisher(persistence, transport, dry_run=False)
    base = {
        "user_id": user_id,
        "local_date": date(2026, 8, 29),
        "game": "chunithm",
        "channel_id": 223,
        "image_path": Path("fixture.png"),
        "semantic_fingerprint": "a" * 64,
        "rating": 16.72,
        "previous_rating": 16.71,
        "delta": 0.01,
        "collected_label": "22:18",
    }

    first = await publisher.publish_rating(**base, image_sha256="b" * 64)
    ordinary_duplicate = await publisher.publish_rating(
        **base,
        image_sha256="c" * 64,
    )
    refreshed = await publisher.publish_rating(
        **base,
        image_sha256="c" * 64,
        allow_presentation_refresh=True,
    )

    assert first.posted is True
    assert ordinary_duplicate.duplicate is True
    assert refreshed.posted is True
    assert refreshed.edited is True
    assert len(transport.sent) == 1
    assert transport.edited[0]["message_id"] == first.message_id
    with db.session() as session:
        delivery = session.query(DiscordDeliveryRow).one()
        assert delivery.semantic_fingerprint == "a" * 64
        assert delivery.image_sha256 == "c" * 64
        assert delivery.discord_message_id == refreshed.message_id


@pytest.mark.asyncio
async def test_later_same_day_rating_edits_bot_message(publication_environment):
    persistence, user_id, _ = publication_environment
    transport = FakeDiscordTransport()
    publisher = RhythmDiscordPublisher(persistence, transport, dry_run=False)
    base = {
        "user_id": user_id,
        "local_date": date(2026, 8, 29),
        "game": "ongeki",
        "channel_id": 333,
        "image_path": Path("fixture.png"),
        "previous_rating": 16.44,
        "collected_label": "22:18",
    }
    first = await publisher.publish_rating(
        **base,
        semantic_fingerprint="a" * 64,
        image_sha256="b" * 64,
        rating=16.45,
        delta=0.01,
    )
    updated = await publisher.publish_rating(
        **base,
        semantic_fingerprint="c" * 64,
        image_sha256="d" * 64,
        rating=16.46,
        delta=0.02,
    )

    assert first.posted is True
    assert updated.posted is True
    assert updated.edited is True
    assert transport.edited[0]["message_id"] == first.message_id
    assert len(transport.sent) == 1


@pytest.mark.asyncio
async def test_deleted_previous_rating_message_falls_back_to_send(
    publication_environment,
):
    persistence, user_id, _ = publication_environment
    transport = FakeDiscordTransport()
    publisher = RhythmDiscordPublisher(
        persistence,
        transport,
        dry_run=False,
        max_attempts=1,
    )
    base = {
        "user_id": user_id,
        "local_date": date(2026, 8, 29),
        "game": "maimai",
        "channel_id": 334,
        "image_path": Path("fixture.png"),
        "previous_rating": 15420,
        "collected_label": "22:18",
    }
    first = await publisher.publish_rating(
        **base,
        semantic_fingerprint="a" * 64,
        image_sha256="b" * 64,
        rating=15432,
        delta=12,
    )
    transport.failures.append(
        DiscordSendError("discord_not_found", "削除済み", retryable=False)
    )

    updated = await publisher.publish_rating(
        **base,
        semantic_fingerprint="c" * 64,
        image_sha256="d" * 64,
        rating=15440,
        delta=20,
    )

    assert first.posted is True
    assert updated.posted is True
    assert updated.edited is False
    assert len(transport.edited) == 1
    assert len(transport.sent) == 2


@pytest.mark.asyncio
async def test_transient_failure_retries_and_only_then_marks_posted(
    publication_environment, monkeypatch
):
    persistence, user_id, db = publication_environment
    transport = FakeDiscordTransport(
        [
            DiscordSendError(
                "discord_rate_limited",
                "一時失敗",
                retryable=True,
                retry_after=0,
            )
        ]
    )
    publisher = RhythmDiscordPublisher(
        persistence, transport, dry_run=False, base_backoff_seconds=0
    )
    sleep_calls: list[float] = []

    async def fake_sleep(delay: float):
        sleep_calls.append(delay)

    monkeypatch.setattr("life_inbox.rhythm_discord.asyncio.sleep", fake_sleep)

    result = await publisher.publish_rating(
        user_id=user_id,
        local_date=date(2026, 8, 29),
        game="chunithm",
        channel_id=444,
        semantic_fingerprint="a" * 64,
        image_sha256="b" * 64,
        image_path=Path("fixture.png"),
        rating=16.73,
        previous_rating=16.72,
        delta=0.01,
        collected_label="22:18",
    )

    assert result.posted is True
    assert result.attempt_count == 2
    assert sleep_calls == [0.0]
    with db.session() as session:
        assert session.query(DiscordDeliveryRow).one().status == "posted"


@pytest.mark.asyncio
async def test_permanent_failure_is_not_marked_posted(publication_environment):
    persistence, user_id, db = publication_environment
    transport = FakeDiscordTransport(
        [DiscordSendError("discord_forbidden", "権限なし", retryable=False)]
    )
    publisher = RhythmDiscordPublisher(persistence, transport, dry_run=False)

    result = await publisher.publish_rating(
        user_id=user_id,
        local_date=date(2026, 8, 29),
        game="chunithm",
        channel_id=555,
        semantic_fingerprint="a" * 64,
        image_sha256="b" * 64,
        image_path=Path("fixture.png"),
        rating=16.73,
        previous_rating=16.72,
        delta=0.01,
        collected_label="22:18",
    )

    assert result.posted is False
    assert result.error_code == "discord_forbidden"
    with db.session() as session:
        delivery = session.query(DiscordDeliveryRow).one()
        assert delivery.status == "failed"
        assert delivery.discord_message_id is None


@pytest.mark.asyncio
async def test_spending_is_yen_only_zero_configurable_and_edits_after_revision(
    publication_environment
):
    persistence, user_id, _ = publication_environment
    transport = FakeDiscordTransport()
    publisher = RhythmDiscordPublisher(persistence, transport, dry_run=False)
    target = date(2026, 8, 29)
    zero = persistence.sync_spending(
        user_id=user_id,
        local_date=target,
        time_zone="Asia/Tokyo",
        chunithm_amount_yen=0,
        maimai_amount_yen=0,
        ongeki_amount_yen=0,
        total_amount_yen=0,
        revision=1,
    ).record

    skipped = await publisher.publish_spending(
        user_id=user_id,
        record=zero,
        channel_id=666,
        post_zero_days=False,
    )
    posted_zero = await publisher.publish_spending(
        user_id=user_id,
        record=zero,
        channel_id=666,
        post_zero_days=True,
    )
    updated_record = persistence.sync_spending(
        user_id=user_id,
        local_date=target,
        time_zone="Asia/Tokyo",
        chunithm_amount_yen=500,
        maimai_amount_yen=300,
        ongeki_amount_yen=400,
        total_amount_yen=1_200,
        revision=2,
    ).record
    updated = await publisher.publish_spending(
        user_id=user_id,
        record=updated_record,
        channel_id=666,
        post_zero_days=True,
    )

    assert skipped.posted is False
    assert posted_zero.posted is True
    assert updated.edited is True
    assert "合計 ¥1,200" in format_spending_message(updated_record)
    assert "ウニ ¥500" in updated.planned_content
    assert "cr" not in updated.planned_content.casefold()

    deleted_record = persistence.delete_spending(
        user_id=user_id,
        local_date=target,
        time_zone="Asia/Tokyo",
        revision=3,
    ).record
    deleted = await publisher.publish_spending(
        user_id=user_id,
        record=deleted_record,
        channel_id=666,
        post_zero_days=False,
    )
    assert deleted.posted is True
    assert deleted.edited is True
    assert "削除されました" in deleted.planned_content
    assert "合計 ¥0" in deleted.planned_content


@pytest.mark.asyncio
async def test_zero_or_deleted_spending_without_a_previous_post_is_not_sent(
    publication_environment,
):
    persistence, user_id, db = publication_environment
    transport = FakeDiscordTransport()
    publisher = RhythmDiscordPublisher(persistence, transport, dry_run=False)
    target = date(2026, 8, 29)
    deleted = persistence.delete_spending(
        user_id=user_id,
        local_date=target,
        time_zone="Asia/Tokyo",
        revision=1,
    ).record

    outcome = await publisher.publish_spending(
        user_id=user_id,
        record=deleted,
        channel_id=668,
        post_zero_days=False,
    )

    assert outcome.posted is False
    assert outcome.duplicate is True
    assert transport.sent == []
    with db.session() as session:
        assert session.query(DiscordDeliveryRow).count() == 0


@pytest.mark.asyncio
async def test_collection_error_is_sanitized_dry_run_then_daily_edit(
    publication_environment,
):
    persistence, user_id, db = publication_environment
    transport = FakeDiscordTransport()
    dry_publisher = RhythmDiscordPublisher(persistence, transport, dry_run=True)
    kwargs = {
        "user_id": user_id,
        "local_date": date(2026, 8, 29),
        "game": "maimai",
        "channel_id": 667,
        "state_label": "認証が必要です",
        "occurred_label": "2026-08-29 23:55",
        "action_label": "管理画面からmaimai NETへ再ログインしてください",
    }

    planned = await dry_publisher.publish_collection_error(**kwargs)

    assert planned.dry_run is True
    assert "【maimai取得失敗】" in planned.planned_content
    assert "パスワード" not in planned.planned_content
    with db.session() as session:
        assert session.query(DiscordDeliveryRow).count() == 0

    publisher = RhythmDiscordPublisher(persistence, transport, dry_run=False)
    first = await publisher.publish_collection_error(**kwargs)
    duplicate = await publisher.publish_collection_error(**kwargs)
    changed = await publisher.publish_collection_error(
        **{
            **kwargs,
            "state_label": "外部サービス障害",
            "action_label": "時間をおいて再試行してください",
        }
    )

    assert first.posted is True
    assert duplicate.duplicate is True
    assert changed.posted is True
    assert changed.edited is True
    assert format_collection_error_message(
        game="ongeki",
        state_label="通信失敗",
        occurred_label="2026-08-29 23:55",
        action_label="再試行してください",
    ).startswith("【オンゲキ取得失敗】")


def test_delivery_claim_blocks_second_instance_until_claim_expires(publication_environment):
    persistence, user_id, _ = publication_environment
    now = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

    first = persistence.claim_delivery(
        user_id=user_id,
        local_date=date(2026, 8, 29),
        delivery_kind="rating",
        game="maimai",
        channel_id=777,
        semantic_fingerprint="a" * 64,
        image_sha256="b" * 64,
        now=now,
        claim_seconds=30,
    )
    competing = persistence.claim_delivery(
        user_id=user_id,
        local_date=date(2026, 8, 29),
        delivery_kind="rating",
        game="maimai",
        channel_id=777,
        semantic_fingerprint="a" * 64,
        image_sha256="b" * 64,
        now=now,
        claim_seconds=30,
    )
    uncertain = persistence.claim_delivery(
        user_id=user_id,
        local_date=date(2026, 8, 29),
        delivery_kind="rating",
        game="maimai",
        channel_id=777,
        semantic_fingerprint="a" * 64,
        image_sha256="b" * 64,
        now=now + timedelta(seconds=31),
        claim_seconds=30,
    )

    assert first.claimed is True
    assert competing.in_progress is True
    assert uncertain.claimed is False
    assert uncertain.uncertain is True

    reclaimed = persistence.claim_delivery(
        user_id=user_id,
        local_date=date(2026, 8, 29),
        delivery_kind="rating",
        game="maimai",
        channel_id=777,
        semantic_fingerprint="a" * 64,
        image_sha256="b" * 64,
        now=now + timedelta(seconds=32),
        claim_seconds=30,
        force=True,
    )
    assert reclaimed.claimed is True
    assert reclaimed.attempt_count == 2


@pytest.mark.asyncio
async def test_unknown_delivery_outcome_is_not_automatically_retried(
    publication_environment,
):
    persistence, user_id, db = publication_environment
    transport = FakeDiscordTransport(
        [
            DiscordSendError(
                "discord_delivery_unknown",
                "送信結果を確認できません",
                retryable=False,
                outcome_unknown=True,
            )
        ]
    )
    publisher = RhythmDiscordPublisher(persistence, transport, dry_run=False)
    kwargs = {
        "user_id": user_id,
        "local_date": date(2026, 8, 29),
        "game": "chunithm",
        "channel_id": 778,
        "semantic_fingerprint": "a" * 64,
        "image_sha256": "b" * 64,
        "image_path": Path("fixture.png"),
        "rating": 16.73,
        "previous_rating": 16.72,
        "delta": 0.01,
        "collected_label": "22:18",
    }

    first = await publisher.publish_rating(**kwargs)
    repeated = await publisher.publish_rating(**kwargs)

    assert first.posted is False
    assert first.uncertain is True
    assert repeated.posted is False
    assert repeated.uncertain is True
    assert len(transport.sent) == 1
    with db.session() as session:
        delivery = session.query(DiscordDeliveryRow).one()
        assert delivery.status == "unknown"
        assert delivery.attempt_count == 1


@pytest.mark.asyncio
async def test_retryable_but_ambiguous_initial_send_is_still_not_retried(
    publication_environment,
):
    persistence, user_id, db = publication_environment
    transport = FakeDiscordTransport(
        [
            DiscordSendError(
                "discord_http_error",
                "Discord 5xx",
                retryable=True,
                outcome_unknown=True,
            )
        ]
    )
    publisher = RhythmDiscordPublisher(
        persistence,
        transport,
        dry_run=False,
        max_attempts=3,
        base_backoff_seconds=0,
    )

    outcome = await publisher.publish_rating(
        user_id=user_id,
        local_date=date(2026, 8, 29),
        game="chunithm",
        channel_id=779,
        semantic_fingerprint="a" * 64,
        image_sha256="b" * 64,
        image_path=Path("fixture.png"),
        rating=16.73,
        previous_rating=16.72,
        delta=0.01,
        collected_label="22:18",
    )

    assert outcome.posted is False
    assert outcome.uncertain is True
    assert len(transport.sent) == 1
    with db.session() as session:
        delivery = session.query(DiscordDeliveryRow).one()
        assert delivery.status == "unknown"


@pytest.mark.asyncio
async def test_ambiguous_edit_of_known_bot_message_can_retry_idempotently(
    publication_environment,
    monkeypatch,
):
    persistence, user_id, _ = publication_environment
    transport = FakeDiscordTransport()
    publisher = RhythmDiscordPublisher(
        persistence,
        transport,
        dry_run=False,
        max_attempts=3,
        base_backoff_seconds=0,
    )
    base = {
        "user_id": user_id,
        "local_date": date(2026, 8, 29),
        "game": "ongeki",
        "channel_id": 780,
        "image_path": Path("fixture.png"),
        "previous_rating": 16.44,
        "collected_label": "22:18",
    }
    first = await publisher.publish_rating(
        **base,
        semantic_fingerprint="a" * 64,
        image_sha256="b" * 64,
        rating=16.45,
        delta=0.01,
    )
    transport.failures.append(
        DiscordSendError(
            "discord_http_error",
            "Discord 5xx",
            retryable=True,
            outcome_unknown=True,
        )
    )

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("life_inbox.rhythm_discord.asyncio.sleep", no_sleep)
    updated = await publisher.publish_rating(
        **base,
        semantic_fingerprint="c" * 64,
        image_sha256="d" * 64,
        rating=16.46,
        delta=0.02,
    )

    assert first.posted is True
    assert updated.posted is True
    assert updated.edited is True
    assert len(transport.edited) == 2
