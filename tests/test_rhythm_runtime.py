from __future__ import annotations

import base64
import binascii
import hashlib
import json
import struct
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from life_inbox.config import Settings
from life_inbox.rhythm_chunirec_image_live import (
    CHUNIRECImageResult,
    CHUNIRECImageState,
)
from life_inbox.rhythm_collectors import (
    CHUNIREC_PROFILE_ENDPOINT,
    CHUNIRECResponse,
    MaimaiCollectionResult,
    MaimaiState,
    OngekiCollectionResult,
    OngekiState,
    ScreenshotCompletionEvent,
    parse_maimai_data,
    parse_ongeki_data,
)
from life_inbox.rhythm_discord import DiscordSendError, RhythmDiscordPublisher
from life_inbox.rhythm_maimai_live import (
    DEFAULT_RATING_URL,
    MaimaiLiveResult,
)
from life_inbox.rhythm_models import RatingImageRow
from life_inbox.rhythm_ongeki_live import OngekiLiveOutcome, OngekiLiveResult
from life_inbox.rhythm_persistence import RhythmPersistence
from life_inbox.rhythm_rating import RenderedImageMetadata
from life_inbox.rhythm_runtime import RhythmCollectionExecutor

FIXTURES = Path(__file__).parent / "fixtures" / "rhythm_collectors"
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
JPEG_BYTES = bytes.fromhex(
    "ffd8ffc0000b080001000101011100ffda0008010100003f0000ffd9"
)


class FixtureTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def request(self, method: str, url: str, *, params: dict[str, str]):
        self.calls.append((method, url, dict(params)))
        name = (
            "chunirec_profile.json"
            if url == CHUNIREC_PROFILE_ENDPOINT
            else "chunirec_rating_data.json"
        )
        payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        return CHUNIRECResponse(200, payload, {"X-Rate-Limit-Remaining": "58"})


class FixturePngBackend:
    def __init__(self) -> None:
        self.calls = 0

    def screenshot(self, _html: str, output_path: Path) -> ScreenshotCompletionEvent:
        self.calls += 1
        output_path.write_bytes(PNG_BYTES)
        return ScreenshotCompletionEvent(True, output_path=output_path, width=1, height=1)


class FixtureChunirecImageCollector:
    configs: list[object] = []

    def __init__(self, config) -> None:
        self.config = config
        self.configs.append(config)

    def collect(self, *, collected_at: datetime) -> CHUNIRECImageResult:
        output = Path(self.config.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(PNG_BYTES)
        return CHUNIRECImageResult(
            success=True,
            state=CHUNIRECImageState.SUCCESS,
            image=RenderedImageMetadata(
                path=output,
                sha256=hashlib.sha256(PNG_BYTES).hexdigest(),
                width=1,
                height=1,
                byte_size=len(PNG_BYTES),
                rendered_at=collected_at,
            ),
        )


def _png_with_text_marker(marker: bytes) -> bytes:
    chunk_type = b"tEXt"
    chunk = (
        struct.pack(">I", len(marker))
        + chunk_type
        + marker
        + struct.pack(">I", binascii.crc32(chunk_type + marker) & 0xFFFFFFFF)
    )
    return PNG_BYTES[:-12] + chunk + PNG_BYTES[-12:]


class ChangingFixturePngBackend(FixturePngBackend):
    def screenshot(self, _html: str, output_path: Path) -> ScreenshotCompletionEvent:
        self.calls += 1
        output_path.write_bytes(_png_with_text_marker(f"render={self.calls}".encode()))
        return ScreenshotCompletionEvent(True, output_path=output_path, width=1, height=1)


class NullDiscordTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        return "fixture-message"

    async def edit(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs["message_id"]


class FailOnceDiscordTransport(NullDiscordTransport):
    def __init__(self, *, uncertain: bool = False) -> None:
        super().__init__()
        self.uncertain = uncertain
        self.failed = False

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        if not self.failed:
            self.failed = True
            raise DiscordSendError(
                (
                    "discord_delivery_unknown"
                    if self.uncertain
                    else "discord_forbidden"
                ),
                "fixture failure",
                retryable=False,
                outcome_unknown=self.uncertain,
            )
        return "fixture-message"


class FixtureMaimaiLiveCollector:
    output_paths: list[Path] = []

    def __init__(self, config) -> None:
        self.config = config

    def collect(self, *, collected_at: datetime) -> MaimaiLiveResult:
        output = Path(self.config.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(PNG_BYTES)
        self.output_paths.append(output)
        snapshot = parse_maimai_data(
            {
                "player_name": "Runtime Maimai Fixture",
                "rating": 13_189,
                "frames": {
                    "new": [
                        {
                            "title": "New Song",
                            "difficulty": "master",
                            "chart_type": "DX",
                            "achievement": "100.5000",
                            "song_rating": 301,
                        }
                    ],
                    "best": [
                        {
                            "title": "Best Song",
                            "difficulty": "master",
                            "chart_type": "STD",
                            "achievement": "100.0000",
                            "song_rating": 299,
                        }
                    ],
                },
            },
            collected_at=collected_at,
        )
        digest = hashlib.sha256(PNG_BYTES).hexdigest()
        collection = MaimaiCollectionResult(
            state=MaimaiState.IMAGE_SUCCESS,
            success=True,
            image_path=str(output),
            image_sha256=digest,
            image_bytes=PNG_BYTES,
            snapshot=snapshot,
            data_observed=True,
        )
        return MaimaiLiveResult(
            success=True,
            state=MaimaiState.IMAGE_SUCCESS,
            collection=collection,
            image=RenderedImageMetadata(
                path=output,
                sha256=digest,
                width=1,
                height=1,
                byte_size=len(PNG_BYTES),
                rendered_at=collected_at,
            ),
        )


class FixtureOngekiLiveCollector:
    output_paths: list[Path] = []

    def __init__(self, config) -> None:
        self.config = config

    def collect(
        self,
        *,
        collected_at: datetime,
        score_log_confirmed: bool,
    ) -> OngekiLiveResult:
        del score_log_confirmed
        output = Path(self.config.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(JPEG_BYTES)
        self.output_paths.append(output)
        snapshot = parse_ongeki_data(
            {
                "profile": {"name": "Runtime Ongeki Fixture", "rating": 21.194},
                "rating": {
                    "best": [
                        {
                            "id": 1001,
                            "title": "Best Fixture Song",
                            "diff": "MAS",
                            "level": 14.8,
                            "const": 14.8,
                            "score": 1_009_852,
                            "rating": 17.435,
                            "rank": "SSS+",
                            "update": "2026-08-29 17:22:48",
                            "lamps": {
                                "is_fullbell": True,
                                "is_allbreak": True,
                            },
                        }
                    ],
                    "new": [
                        {
                            "id": 1002,
                            "title": "New Fixture Song",
                            "diff": "LUN",
                            "level": 15,
                            "const": 15,
                            "score": 1_004_674,
                            "rating": 17.011,
                            "rank": "SSS+",
                            "update": "2026-08-30",
                            "lamps": {"is_fullbell": True},
                        }
                    ],
                },
            },
            collected_at=collected_at,
        )
        digest = hashlib.sha256(JPEG_BYTES).hexdigest()
        collection = OngekiCollectionResult(
            state=OngekiState.IMAGE_SUCCESS,
            success=True,
            image_path=str(output),
            image_sha256=digest,
            image_bytes=JPEG_BYTES,
            snapshot=snapshot,
            score_log_data_confirmed=True,
        )
        return OngekiLiveResult(
            outcome=OngekiLiveOutcome.SUCCESS,
            success=True,
            state=OngekiState.IMAGE_SUCCESS,
            collection=collection,
            image=RenderedImageMetadata(
                path=output,
                sha256=digest,
                mime_type="image/jpeg",
                width=1,
                height=1,
                byte_size=len(JPEG_BYTES),
                rendered_at=collected_at,
            ),
        )


@pytest.fixture
def runtime_environment(db, tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        rhythm_image_dir=tmp_path / "images",
        rhythm_enabled=True,
        rhythm_scheduler_enabled=True,
        rhythm_discord_dry_run=True,
        discord_chunithm_channel_id=1001,
        chunirec_api_token="fixture-token-do-not-persist",
        chunirec_user_id="fixture-user",
        chunirec_region="jp2",
        chunirec_require_manual_refresh_confirmation=False,
    )
    persistence = RhythmPersistence(db.session_factory)
    user_id = persistence.ensure_user(
        display_name="Runtime Fixture",
        time_zone="Asia/Tokyo",
        discord_user_id="runtime-fixture-user",
    )
    transport = FixtureTransport()
    discord_transport = NullDiscordTransport()
    publisher = RhythmDiscordPublisher(persistence, discord_transport, dry_run=True)
    executor = RhythmCollectionExecutor(
        settings=settings,
        persistence=persistence,
        publisher=publisher,
        chunirec_transport=transport,
        png_backend=FixturePngBackend(),
        now=lambda: datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
    )
    return settings, persistence, user_id, transport, discord_transport, executor


@pytest.mark.asyncio
async def test_chunirec_fixture_runs_full_pipeline_without_discord(runtime_environment):
    _, persistence, user_id, transport, discord_transport, executor = runtime_environment
    run = persistence.queue_collection_runs(
        user_id=user_id,
        games=["chunithm"],
        trigger="fixture",
    )[0]

    await executor.execute(run)

    completed = persistence.get_run(run.id)
    snapshot = persistence.latest_snapshot(user_id=user_id, game="chunithm")
    assert completed is not None
    assert completed.state == "cancelled"
    assert completed.snapshot_id is not None
    assert "dry-run完了" in completed.progress_message
    assert "rating=15.25" in completed.progress_message
    assert "channel=1001" in completed.progress_message
    assert "image=" in completed.progress_message
    assert snapshot is not None
    assert float(snapshot.rating) == 15.25
    assert len(snapshot.entries) == 7
    assert snapshot.image_links
    assert Path(snapshot.image_links[0].image.image_path).is_file()
    serialized_raw = json.dumps(snapshot.raw_data, ensure_ascii=False)
    assert "fixture-token-do-not-persist" not in serialized_raw
    assert len(transport.calls) == 2
    assert discord_transport.calls == []


@pytest.mark.asyncio
async def test_chunirec_public_generator_image_is_preferred_over_custom_renderer(
    runtime_environment,
):
    settings, persistence, user_id, _, _, executor = runtime_environment
    settings.chunirec_user_id = None
    settings.chunirec_user_name = "fixture-user"
    executor.chunirec_image_collector_factory = FixtureChunirecImageCollector
    run = persistence.queue_collection_runs(
        user_id=user_id,
        games=["chunithm"],
        trigger="fixture",
    )[0]

    await executor.execute(run)

    snapshot = persistence.latest_snapshot(user_id=user_id, game="chunithm")
    assert snapshot is not None
    assert snapshot.image_links[0].image.renderer_version == (
        "chunirec-public-generator-v1"
    )
    assert executor.png_backend.calls == 0
    config = FixtureChunirecImageCollector.configs[-1]
    assert config.expected_player_name == "Fixture Player"
    assert str(config.expected_rating) == "15.25"


@pytest.mark.asyncio
async def test_chunirec_manual_confirmation_never_advances_by_time(
    runtime_environment,
):
    settings, persistence, user_id, transport, _, executor = runtime_environment
    settings.chunirec_require_manual_refresh_confirmation = True
    run = persistence.queue_collection_runs(
        user_id=user_id,
        games=["chunithm"],
        trigger="fixture",
    )[0]

    await executor.execute(run)
    waiting = persistence.get_run(run.id)
    assert waiting is not None
    assert waiting.state == "waitingForManualAction"
    assert waiting.manual_action == "confirmChunirecRefresh"
    assert transport.calls == []

    persistence.confirm_manual_action(run_id=run.id)
    await executor.execute(persistence.get_run(run.id))
    assert persistence.get_run(run.id).state == "cancelled"
    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_same_semantic_chunirec_snapshot_is_unchanged(runtime_environment):
    _, persistence, user_id, _, _, executor = runtime_environment
    first = persistence.queue_collection_runs(
        user_id=user_id, games=["chunithm"], trigger="fixture"
    )[0]
    await executor.execute(first)
    executor.now = lambda: datetime(2026, 8, 29, 12, 1, tzinfo=UTC)
    second = persistence.queue_collection_runs(
        user_id=user_id, games=["chunithm"], trigger="fixture"
    )[0]

    await executor.execute(second)

    assert persistence.get_run(second.id).state == "unchanged"
    snapshots, total = persistence.list_snapshots(user_id=user_id, game="chunithm")
    assert total == 2
    assert snapshots[0].semantic_fingerprint == snapshots[1].semantic_fingerprint
    assert snapshots[0].image_links[0].image_id == snapshots[1].image_links[0].image_id
    assert executor.png_backend.calls == 1


@pytest.mark.asyncio
async def test_renderer_version_change_refreshes_same_semantic_discord_image(
    runtime_environment,
):
    settings, persistence, user_id, _, discord_transport, executor = runtime_environment
    settings.rhythm_discord_dry_run = False
    executor.publisher = RhythmDiscordPublisher(
        persistence,
        discord_transport,
        dry_run=False,
    )
    executor.png_backend = ChangingFixturePngBackend()
    first = persistence.queue_collection_runs(
        user_id=user_id, games=["chunithm"], trigger="fixture"
    )[0]
    await executor.execute(first)
    first_snapshot = persistence.latest_snapshot(user_id=user_id, game="chunithm")
    first_image_id = first_snapshot.image_links[0].image_id

    session = persistence.session_factory()
    try:
        image = session.get(RatingImageRow, first_image_id)
        image.renderer_version = "chunithm-html-v1"
        session.commit()
    finally:
        session.close()

    executor.now = lambda: datetime(2026, 8, 29, 12, 1, tzinfo=UTC)
    second = persistence.queue_collection_runs(
        user_id=user_id, games=["chunithm"], trigger="fixture"
    )[0]
    await executor.execute(second)

    refreshed = persistence.get_run(second.id)
    snapshots, total = persistence.list_snapshots(user_id=user_id, game="chunithm")
    assert refreshed.state == "posted"
    assert total == 2
    assert snapshots[0].semantic_fingerprint == snapshots[1].semantic_fingerprint
    assert snapshots[0].image_links[0].image_id != first_image_id
    assert snapshots[0].image_links[0].image.renderer_version == "chunithm-html-v2"
    assert executor.png_backend.calls == 2
    assert len(discord_transport.calls) == 2
    assert "message_id" not in discord_transport.calls[0]
    assert discord_transport.calls[1]["message_id"] == "fixture-message"


@pytest.mark.asyncio
async def test_same_semantic_retries_after_explicit_discord_failure(runtime_environment):
    settings, persistence, user_id, _, _, executor = runtime_environment
    settings.rhythm_discord_dry_run = False
    discord_transport = FailOnceDiscordTransport()
    executor.publisher = RhythmDiscordPublisher(
        persistence,
        discord_transport,
        dry_run=False,
        max_attempts=1,
    )
    first = persistence.queue_collection_runs(
        user_id=user_id, games=["chunithm"], trigger="fixture"
    )[0]

    await executor.execute(first)
    assert persistence.get_run(first.id).state == "failed"

    executor.now = lambda: datetime(2026, 8, 29, 12, 1, tzinfo=UTC)
    second = persistence.queue_collection_runs(
        user_id=user_id, games=["chunithm"], trigger="fixture"
    )[0]
    await executor.execute(second)

    assert persistence.get_run(second.id).state == "posted"
    assert len(discord_transport.calls) == 2
    assert executor.png_backend.calls == 1


@pytest.mark.asyncio
async def test_same_semantic_never_retries_unknown_discord_outcome(runtime_environment):
    settings, persistence, user_id, _, _, executor = runtime_environment
    settings.rhythm_discord_dry_run = False
    discord_transport = FailOnceDiscordTransport(uncertain=True)
    executor.publisher = RhythmDiscordPublisher(
        persistence,
        discord_transport,
        dry_run=False,
        max_attempts=1,
    )
    first = persistence.queue_collection_runs(
        user_id=user_id, games=["chunithm"], trigger="fixture"
    )[0]

    await executor.execute(first)
    assert persistence.get_run(first.id).state == "failed"

    executor.now = lambda: datetime(2026, 8, 29, 12, 1, tzinfo=UTC)
    second = persistence.queue_collection_runs(
        user_id=user_id, games=["chunithm"], trigger="fixture"
    )[0]
    await executor.execute(second)

    blocked = persistence.get_run(second.id)
    assert blocked.state == "failed"
    assert blocked.last_error_code == "discord_delivery_requires_reconciliation"
    assert len(discord_transport.calls) == 1


@pytest.mark.asyncio
async def test_maimai_without_secret_or_session_waits_for_manual_authentication(
    runtime_environment,
    tmp_path,
):
    settings, persistence, user_id, _, _, executor = runtime_environment
    settings.maimai_net_rating_url = DEFAULT_RATING_URL
    settings.maimai_standard_course_enabled = True
    settings.rhythm_browser_session_dir = tmp_path / "missing-sessions"
    run = persistence.queue_collection_runs(
        user_id=user_id, games=["maimai"], trigger="fixture"
    )[0]

    await executor.execute(run)
    waiting = persistence.get_run(run.id)
    assert waiting.state == "waitingForManualAction"
    assert waiting.manual_action == "authenticate:maimai"
    assert persistence.latest_snapshot(user_id=user_id, game="maimai") is None


@pytest.mark.asyncio
async def test_maimai_external_image_uses_persistence_and_duplicate_suppression(
    runtime_environment,
    tmp_path,
):
    settings, persistence, user_id, _, discord_transport, executor = runtime_environment
    settings.maimai_net_rating_url = DEFAULT_RATING_URL
    settings.maimai_standard_course_enabled = True
    settings.rhythm_browser_session_dir = tmp_path / "sessions"
    settings.rhythm_browser_session_dir.mkdir(parents=True)
    settings.sega_id = SecretStr("fixture-id")
    settings.sega_password = SecretStr("fixture-password")
    settings.discord_maimai_channel_id = 1002
    FixtureMaimaiLiveCollector.output_paths = []
    executor.maimai_live_collector_factory = FixtureMaimaiLiveCollector

    first = persistence.queue_collection_runs(
        user_id=user_id, games=["maimai"], trigger="fixture"
    )[0]
    await executor.execute(first)

    first_run = persistence.get_run(first.id)
    first_snapshot = persistence.latest_snapshot(user_id=user_id, game="maimai")
    assert first_run.state == "cancelled"
    assert first_snapshot is not None
    assert int(first_snapshot.rating) == 13_189
    assert first_snapshot.image_links
    first_image_id = first_snapshot.image_links[0].image_id
    assert "channel=1002" in first_run.progress_message
    assert discord_transport.calls == []

    executor.now = lambda: datetime(2026, 8, 29, 12, 1, tzinfo=UTC)
    second = persistence.queue_collection_runs(
        user_id=user_id, games=["maimai"], trigger="fixture"
    )[0]
    await executor.execute(second)

    second_run = persistence.get_run(second.id)
    snapshots, total = persistence.list_snapshots(user_id=user_id, game="maimai")
    assert second_run.state == "unchanged"
    assert total == 2
    assert snapshots[0].semantic_fingerprint == snapshots[1].semantic_fingerprint
    assert snapshots[0].image_links[0].image_id == first_image_id
    assert snapshots[1].image_links[0].image_id == first_image_id
    assert FixtureMaimaiLiveCollector.output_paths[0].is_file()
    assert not FixtureMaimaiLiveCollector.output_paths[1].exists()


@pytest.mark.asyncio
async def test_ongeki_external_image_uses_persistence_and_duplicate_suppression(
    runtime_environment,
    tmp_path,
):
    settings, persistence, user_id, _, discord_transport, executor = runtime_environment
    settings.ongeki_score_log_user_id = "fixture-public-user"
    settings.ongeki_premium_enabled = True
    settings.rhythm_browser_session_dir = tmp_path / "sessions"
    settings.rhythm_browser_session_dir.mkdir(parents=True)
    settings.discord_ongeki_channel_id = 1003
    FixtureOngekiLiveCollector.output_paths = []
    executor.ongeki_live_collector_factory = FixtureOngekiLiveCollector

    first = persistence.queue_collection_runs(
        user_id=user_id, games=["ongeki"], trigger="fixture"
    )[0]
    await executor.execute(first)

    first_run = persistence.get_run(first.id)
    first_snapshot = persistence.latest_snapshot(user_id=user_id, game="ongeki")
    first_provider = persistence.provider_statuses(user_id=user_id)["ongeki"]
    assert first_run.state == "cancelled"
    assert first_snapshot is not None
    assert first_provider.provider == "official_net_scorelog_generator"
    assert first_provider.session_status == "success"
    assert float(first_snapshot.rating) == 21.194
    assert first_snapshot.image_links
    first_image_id = first_snapshot.image_links[0].image_id
    assert "channel=1003" in first_run.progress_message
    assert discord_transport.calls == []

    executor.now = lambda: datetime(2026, 8, 29, 12, 1, tzinfo=UTC)
    second = persistence.queue_collection_runs(
        user_id=user_id, games=["ongeki"], trigger="fixture"
    )[0]
    await executor.execute(second)

    second_run = persistence.get_run(second.id)
    snapshots, total = persistence.list_snapshots(user_id=user_id, game="ongeki")
    assert second_run.state == "unchanged"
    assert total == 2
    assert snapshots[0].semantic_fingerprint == snapshots[1].semantic_fingerprint
    assert snapshots[0].image_links[0].image_id == first_image_id
    assert snapshots[1].image_links[0].image_id == first_image_id
    assert FixtureOngekiLiveCollector.output_paths[0].is_file()
    assert not FixtureOngekiLiveCollector.output_paths[1].exists()
