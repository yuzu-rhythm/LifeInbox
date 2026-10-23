from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from life_inbox.rhythm_collectors import (
    CHUNIREC_PROFILE_ENDPOINT,
    CHUNIREC_RATING_DATA_ENDPOINT,
    CHUNIRECClient,
    CHUNIRECCollector,
    CHUNIRECConfig,
    CHUNIRECEvent,
    CHUNIRECEventType,
    CHUNIRECFailureReason,
    CHUNIRECResponse,
    CHUNITHMRenderer,
    EvidenceKind,
    MaimaiBrowserEvent,
    MaimaiCollector,
    MaimaiConfig,
    MaimaiEventType,
    MaimaiFailureReason,
    MaimaiState,
    OngekiBrowserEvent,
    OngekiCollector,
    OngekiConfig,
    OngekiEventType,
    OngekiFailureReason,
    OngekiState,
    PlaywrightPngBackend,
    RenderFailureCode,
    RetryPolicy,
    ScreenshotCompletionEvent,
    parse_chunirec_v2_profile,
    parse_profile,
    parse_rating_data,
)
from life_inbox.rhythm_rating import Game, RatingEntry, RatingSnapshot

FIXTURES = Path(__file__).parent / "fixtures" / "rhythm_collectors"
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FixtureTransport:
    def __init__(
        self,
        *,
        profile: CHUNIRECResponse | None = None,
        rating_data: CHUNIRECResponse | None = None,
    ) -> None:
        self.responses = {
            CHUNIREC_PROFILE_ENDPOINT: profile
            or CHUNIRECResponse(200, fixture("chunirec_profile.json")),
            CHUNIREC_RATING_DATA_ENDPOINT: rating_data
            or CHUNIRECResponse(200, fixture("chunirec_rating_data.json")),
        }
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str],
    ) -> CHUNIRECResponse:
        self.calls.append((method, url, dict(params)))
        return self.responses[url]


def client(
    transport: FixtureTransport | None = None,
    *,
    token: str | None = "fixture-token",
    user_id: str | None = "user-123",
    user_name: str | None = None,
) -> tuple[CHUNIRECClient, FixtureTransport]:
    actual_transport = transport or FixtureTransport()
    return (
        CHUNIRECClient(
            actual_transport,
            CHUNIRECConfig(
                token=token,
                region="jp",
                user_id=user_id,
                user_name=user_name,
            ),
        ),
        actual_transport,
    )


def test_chunirec_parses_profile_rating_frames_and_safe_query() -> None:
    api, transport = client()

    result = api.fetch_snapshot(collected_at=datetime(2026, 8, 29, 3, tzinfo=UTC))

    assert result.success is True
    assert result.snapshot is not None
    assert result.snapshot.player_name == "Fixture Player"
    assert result.snapshot.rating == 15.25
    assert result.profile is not None
    assert result.rating_data is not None
    assert len(result.rating_data.best) == 2
    assert len(result.rating_data.recent) == 1
    assert len(result.rating_data.new_equivalent) == 1
    assert len(result.rating_data.best_candidate) == 1
    assert len(result.rating_data.best_candidate_sss) == 1
    assert len(result.rating_data.best_outside) == 1
    assert len(result.snapshot.entries) == 7
    assert {entry.frame_membership for entry in result.snapshot.entries} == {
        "best",
        "recent",
        "new_equivalent",
        "best_candidate",
        "best_candidate_sss",
        "best_outside",
    }
    best = next(
        entry
        for entry in result.snapshot.entries
        if entry.song_identifier == "song-best-001"
    )
    assert best.title == "Best Song <safe>"
    assert best.difficulty == "master"
    assert best.level == "14+"
    assert best.chart_constant == 14.7
    assert best.score == 1_005_000
    assert best.rating == 15.7
    assert best.clear_status == "clear"
    assert best.full_combo_status == "fc"
    assert best.all_justice_status == "aj"
    assert {"clear", "fc", "aj", "fullchain"}.issubset(best.completion_statuses)
    assert best.genre == "Original"
    assert best.source_updated_at == datetime(2026, 8, 28, 1, tzinfo=UTC)

    assert len(transport.calls) == 2
    for method, url, params in transport.calls:
        assert method == "GET"
        assert url in {CHUNIREC_PROFILE_ENDPOINT, CHUNIREC_RATING_DATA_ENDPOINT}
        assert set(params) == {"token", "region", "user_id"}
        assert "user_name" not in params


def test_chunirec_v2_profile_accepts_documented_unpadded_timestamp() -> None:
    profile = parse_chunirec_v2_profile(
        {
            "player_name": "Fixture Player",
            "rating": 1.23,
            "updated_at": "2022-4-14T18:58:26+0900",
        }
    )

    assert profile.updated_at == datetime(2022, 4, 14, 9, 58, 26, tzinfo=UTC)


def test_chunirec_accepts_live_profile_decimal_string_without_relaxing_rows() -> None:
    profile = fixture("chunirec_profile.json")
    profile["rating"] = "15.25"
    api, transport = client(
        FixtureTransport(profile=CHUNIRECResponse(200, profile))
    )

    result = api.fetch_snapshot()

    assert result.success is True
    assert result.snapshot is not None
    assert result.snapshot.rating == 15.25
    assert len(transport.calls) == 2


@pytest.mark.parametrize("rating", ["not-a-number", "NaN", "-1", {"value": 15.25}])
def test_chunirec_rejects_invalid_profile_rating_values(rating: object) -> None:
    profile = fixture("chunirec_profile.json")
    profile["rating"] = rating
    api, transport = client(
        FixtureTransport(profile=CHUNIRECResponse(200, profile))
    )

    result = api.fetch_snapshot()

    assert result.success is False
    assert result.failure_reason is CHUNIRECFailureReason.INVALID_SCHEMA
    assert len(transport.calls) == 1


def test_chunirec_parser_ignores_null_fixed_size_slots() -> None:
    parsed = parse_rating_data(
        {
            "best": [None, {"id": "one", "title": "One"}, None],
            "recent": [None],
            "new": [None],
            "best_candidate": [None],
            "best_candidate_sss": [None],
            "best_outside": [None],
        }
    )

    assert [entry.song_identifier for entry in parsed.entries] == ["one"]


def test_chunirec_profile_and_rating_parsers_keep_sanitized_raw_data() -> None:
    profile = parse_profile({"player_name": "Player", "rating": "12.5", "token": "secret"})
    assert profile.player_name == "Player"
    assert profile.total_rating == 12.5
    assert profile.raw_data["token"] == "[redacted]"


@pytest.mark.parametrize(
    ("token", "user_id", "user_name", "reason"),
    [
        (None, "user-123", None, CHUNIRECFailureReason.MISSING_TOKEN),
        ("fixture-token", None, None, CHUNIRECFailureReason.INVALID_IDENTITY),
        ("fixture-token", "user-123", "name", CHUNIRECFailureReason.INVALID_IDENTITY),
    ],
)
def test_chunirec_missing_token_or_invalid_identity_never_calls_transport(
    token: str | None,
    user_id: str | None,
    user_name: str | None,
    reason: CHUNIRECFailureReason,
) -> None:
    api, transport = client(token=token, user_id=user_id, user_name=user_name)

    result = api.fetch_snapshot()

    assert result.success is False
    assert result.failure_reason is reason
    assert transport.calls == []


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (403, CHUNIRECFailureReason.AUTHENTICATION_REQUIRED),
        (404, CHUNIRECFailureReason.NOT_FOUND),
        (429, CHUNIRECFailureReason.RATE_LIMITED),
        (503, CHUNIRECFailureReason.PROVIDER_OUTAGE),
    ],
)
def test_chunirec_http_failures_are_classified(
    status: int,
    reason: CHUNIRECFailureReason,
) -> None:
    response = CHUNIRECResponse(
        status,
        {"error": "fixture"},
        {
            "X-Rate-Limit-Limit": "60",
            "X-Rate-Limit-Remaining": "0",
            "X-Rate-Limit-Reset": "1798599600",
        },
    )
    api, transport = client(FixtureTransport(profile=response))

    result = api.fetch_snapshot()

    assert result.success is False
    assert result.failure_reason is reason
    assert result.status_code == status
    assert len(transport.calls) == 1
    assert result.rate_limit is not None
    assert result.rate_limit.limit == 60
    assert result.rate_limit.remaining == 0
    assert result.rate_limit.reset == 1_798_599_600
    assert result.rate_limit.limited is True


def test_chunirec_malformed_json_and_schema_failures_do_not_raise() -> None:
    malformed_api, malformed_transport = client(
        FixtureTransport(profile=CHUNIRECResponse(200, "not-json"))
    )
    malformed = malformed_api.fetch_snapshot()
    assert malformed.success is False
    assert malformed.failure_reason is CHUNIRECFailureReason.MALFORMED_JSON
    assert len(malformed_transport.calls) == 1

    schema_api, schema_transport = client(
        FixtureTransport(
            profile=CHUNIRECResponse(200, ["not", "an", "object"]),
            rating_data=CHUNIRECResponse(200, fixture("chunirec_rating_data.json")),
        )
    )
    schema = schema_api.fetch_snapshot()
    assert schema.success is False
    assert schema.failure_reason is CHUNIRECFailureReason.INVALID_SCHEMA
    assert len(schema_transport.calls) == 1


def test_chunirec_refresh_confirmation_blocks_both_api_calls() -> None:
    api, transport = client()
    collector = CHUNIRECCollector(api)

    waiting = collector.collect()
    assert waiting.success is False
    assert waiting.waiting_for_manual_action is True
    assert waiting.state.value == "waitingForManualAction"
    assert transport.calls == []

    collector.handle_event(CHUNIRECEvent(CHUNIRECEventType.REFRESH_CONFIRMED))
    assert transport.calls == []
    confirmed = collector.collect(event=CHUNIRECEvent.refresh_confirmed())
    assert confirmed.success is True
    assert len(transport.calls) == 2


class FakePngBackend:
    def __init__(self, *, completion: bool = True) -> None:
        self.completion = completion
        self.documents: list[str] = []

    def screenshot(self, html_document: str, output_path: Path) -> ScreenshotCompletionEvent:
        self.documents.append(html_document)
        if not self.completion:
            return ScreenshotCompletionEvent(
                False,
                failure_reason=RenderFailureCode.SCREENSHOT_NOT_COMPLETED,
            )
        output_path.write_bytes(PNG_BYTES)
        return ScreenshotCompletionEvent(True, output_path=output_path, width=1, height=1)


def large_chunithm_snapshot() -> RatingSnapshot:
    entries: list[RatingEntry] = []
    for frame, count in (("best", 30), ("recent", 10), ("new_equivalent", 10)):
        for position in range(1, count + 1):
            entries.append(
                RatingEntry(
                    game=Game.CHUNITHM,
                    song_identifier=f"{frame}-{position:02d}",
                    title=f"{frame.title()} chart {position:02d} with a deliberately long title",
                    frame_membership=frame,
                    difficulty="MASTER",
                    level="14+",
                    chart_constant=14.7,
                    rank=position,
                    score=1_000_000 - position + 1,
                    achievement=100.0,
                    rating=15.0,
                    song_rating=15.0,
                    completion_statuses=("clear", "fc"),
                )
            )
    return RatingSnapshot(
        game=Game.CHUNITHM,
        entries=entries,
        rating=16.72,
        source="chunirec",
        player_name="Fixture Player",
        source_updated_at=datetime(2026, 8, 29, 2, 30, tzinfo=UTC),
        collected_at=datetime(2026, 8, 29, 3, tzinfo=UTC),
    )


def test_chunithm_renderer_escapes_html_and_returns_verified_png_metadata(tmp_path: Path) -> None:
    api, _ = client()
    collected = api.fetch_snapshot()
    assert collected.snapshot is not None
    previous = collected.snapshot
    renderer_backend = FakePngBackend()
    renderer = CHUNITHMRenderer(renderer_backend, previous_snapshot=previous)
    output = tmp_path / "rating.png"

    document = renderer.build_html(collected.snapshot, previous_rating=15.0)
    rendered = renderer.render(collected.snapshot, output, previous_rating=15.0)

    assert "CHUNITHM" in document
    assert "Fixture Player" in document
    assert "Current rating" in document
    assert "Previous rating" in document
    assert "Delta" in document
    assert "BEST" in document
    assert "RECENT" in document
    assert "NEW" in document
    assert "CANDIDATES / OTHER" in document
    assert "Candidate SSS" in document
    assert "Outside" in document
    assert "Best Song &lt;safe&gt;" in document
    assert "<safe>" not in document
    assert "<img" not in document.lower()
    assert "http://" not in document
    assert "https://" not in document
    assert document.count('class="song-card accent-') == len(collected.snapshot.entries)
    assert rendered.success is True
    assert rendered.metadata is not None
    assert rendered.metadata.image_path == str(output)
    assert rendered.metadata.image_sha256 == hashlib.sha256(PNG_BYTES).hexdigest()
    assert rendered.metadata.byte_size == len(PNG_BYTES)
    assert rendered.metadata.width == 1
    assert rendered.metadata.height == 1
    assert renderer_backend.documents


def test_chunithm_renderer_large_snapshot_uses_bounded_landscape_grids() -> None:
    document = CHUNITHMRenderer(FakePngBackend()).build_html(
        large_chunithm_snapshot(),
        previous_rating=16.5,
    )

    assert 'max-width: 1800px;' in document
    assert 'grid-template-columns: repeat(5, minmax(0, 1fr));' in document
    assert 'grid-template-columns: repeat(12, minmax(0, 1fr));' in document
    assert 'height: 166px;' in document
    assert '-webkit-line-clamp: 2;' in document
    assert 'overflow: hidden;' in document
    assert 'data-frame="best" data-entry-count="30"' in document
    assert 'data-frame="new_recent" data-entry-count="20"' in document
    assert 'data-frame="recent" data-entry-count="10"' in document
    assert 'data-frame="new_equivalent" data-entry-count="10"' in document
    assert document.count('class="song-card accent-') == 50
    assert "Best chart 01 with a deliberately long title" in document
    assert "SCORE" in document
    assert "ACHIEVEMENT" in document
    assert "CHART" in document
    assert "RATING" in document
    assert "1,000,000" in document
    assert "<img" not in document.lower()
    assert "url(" not in document.lower()


def test_chunithm_renderer_escapes_and_redacts_all_card_text() -> None:
    secret = "fixture-token-123"
    snapshot = RatingSnapshot(
        game=Game.CHUNITHM,
        entries=[
            RatingEntry(
                game=Game.CHUNITHM,
                song_identifier="safe-song",
                title=f"Title <b>{secret}</b>",
                frame_membership="best",
                difficulty="MASTER",
                level="14",
                score=1_000_000,
                chart_constant=14.0,
                rating=14.0,
                completion_statuses=(secret,),
            )
        ],
        rating=14.0,
        source=f"source <script>{secret}",
        player_name=f"Player <script>{secret}",
        source_updated_at=datetime(2026, 8, 29, 2, 30, tzinfo=UTC),
        collected_at=datetime(2026, 8, 29, 3, tzinfo=UTC),
    )

    document = CHUNITHMRenderer(FakePngBackend(), secrets=(secret,)).build_html(snapshot)

    assert secret not in document
    assert "&lt;script&gt;" in document
    assert "Title &lt;b&gt;[redacted]&lt;/b&gt;" in document
    assert "Player &lt;script&gt;[redacted]" in document
    assert "<script>" not in document
    assert "<img" not in document.lower()


def test_chunithm_renderer_playwright_large_snapshot_dimensions(tmp_path: Path) -> None:
    pytest.importorskip("playwright.sync_api")
    browser_cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    if not any(path.is_file() for path in browser_cache.glob("chromium-*/**/*")):
        pytest.skip("Playwright Chromium is not installed")

    output = tmp_path / "large-rating.png"
    rendered = CHUNITHMRenderer(PlaywrightPngBackend()).render(
        large_chunithm_snapshot(),
        output,
        previous_rating=16.5,
    )

    assert rendered.success is True, rendered.message
    assert rendered.metadata is not None
    assert rendered.metadata.width == 1_800
    assert rendered.metadata.height is not None
    assert rendered.metadata.height <= 1_800
    assert rendered.metadata.width > rendered.metadata.height
    assert rendered.metadata.byte_size == output.stat().st_size


def test_chunithm_renderer_failure_is_distinct_from_collection_failure(tmp_path: Path) -> None:
    api, _ = client()
    collected = api.fetch_snapshot()
    assert collected.snapshot is not None
    renderer = CHUNITHMRenderer(FakePngBackend(completion=False))

    rendered = renderer.render(collected.snapshot, tmp_path / "rating.png")

    assert rendered.success is False
    assert rendered.failure_reason is not None
    assert rendered.message == "rendering_failure:screenshot_not_completed"
    assert not (tmp_path / "rating.png").exists()
    assert collected.failure_reason is None


def test_chunithm_renderer_does_not_promote_an_unmodified_existing_output(
    tmp_path: Path,
) -> None:
    api, _ = client()
    collected = api.fetch_snapshot()
    assert collected.snapshot is not None
    output = tmp_path / "rating.png"
    output.write_bytes(PNG_BYTES)

    class NoopBackend:
        def screenshot(
            self,
            _html_document: str,
            _output_path: Path,
        ) -> ScreenshotCompletionEvent:
            return ScreenshotCompletionEvent(True, output_path=output, width=1, height=1)

    rendered = CHUNITHMRenderer(NoopBackend()).render(collected.snapshot, output)

    assert rendered.success is False
    assert rendered.message == "rendering_failure:output_not_refreshed"
    assert output.read_bytes() == PNG_BYTES


def test_playwright_backend_is_lazy_and_classifies_missing_dependency(tmp_path: Path) -> None:
    backend = PlaywrightPngBackend()

    event = backend.screenshot("<html></html>", tmp_path / "rating.png")

    # The current project venv intentionally has no Playwright.  If a caller
    # supplies it later, the event still has to be an explicit completion.
    assert isinstance(event, ScreenshotCompletionEvent)
    if not event.completed:
        assert event.failure_reason is RenderFailureCode.PLAYWRIGHT_UNAVAILABLE


def maimai_config(tmp_path: Path, *, selector: str | None = "#full") -> MaimaiConfig:
    runtime = tmp_path / "maimai-bookmarklet.js"
    runtime.write_text("(() => true)();", encoding="utf-8")
    return MaimaiConfig(
        persistent_session_dir=tmp_path / "maimai-session",
        bookmarklet_runtime_path=runtime,
        expected_url="https://maimai.example/maimai-mobile/",
        full_data_selector=selector,
    )


def maimai_ready(collector) -> None:
    collector.process(
        [
            MaimaiBrowserEvent.url_observed("https://maimai.example/maimai-mobile/"),
            MaimaiBrowserEvent(
                MaimaiEventType.FULL_DATA_SELECTED,
                completed=True,
                selector="#full",
            ),
        ]
    )


@pytest.mark.parametrize(
    ("event_type", "state", "reason"),
    [
        (
            MaimaiEventType.NOT_LOGGED_IN,
            MaimaiState.NOT_LOGGED_IN,
            MaimaiFailureReason.NOT_LOGGED_IN,
        ),
        (
            MaimaiEventType.AUTH_WAITING,
            MaimaiState.AUTH_WAITING,
            MaimaiFailureReason.AUTH_WAITING,
        ),
        (MaimaiEventType.CAPTCHA, MaimaiState.CAPTCHA, MaimaiFailureReason.CAPTCHA),
        (MaimaiEventType.TERMS, MaimaiState.TERMS, MaimaiFailureReason.TERMS),
        (
            MaimaiEventType.WRONG_PAGE,
            MaimaiState.WRONG_PAGE,
            MaimaiFailureReason.WRONG_PAGE,
        ),
        (
            MaimaiEventType.STANDARD_COURSE_MISSING,
            MaimaiState.STANDARD_COURSE_MISSING,
            MaimaiFailureReason.STANDARD_COURSE_MISSING,
        ),
        (
            MaimaiEventType.DOM_MISMATCH,
            MaimaiState.DOM_MISMATCH,
            MaimaiFailureReason.DOM_MISMATCH,
        ),
    ],
)
def test_maimai_explicit_failure_states(
    tmp_path: Path,
    event_type: MaimaiEventType,
    state: MaimaiState,
    reason: MaimaiFailureReason,
) -> None:
    collector = MaimaiCollector(maimai_config(tmp_path))

    result = collector.observe(MaimaiBrowserEvent(event_type, completed=True))

    assert result.success is False
    assert result.state is state
    assert result.failure_reason is reason


def test_maimai_popup_download_blob_canvas_image_events_are_explicit(tmp_path: Path) -> None:
    collector = MaimaiCollector(maimai_config(tmp_path))
    maimai_ready(collector)
    waiting = collector.process(
        [
            MaimaiBrowserEvent.popup_opened(),
            MaimaiBrowserEvent.download_started(),
        ]
    )
    assert waiting.state is MaimaiState.IMAGE_WAITING
    assert waiting.success is False

    complete = collector.observe(MaimaiBrowserEvent.blob_available(PNG_BYTES))

    assert complete.success is True
    assert complete.state is MaimaiState.IMAGE_SUCCESS
    assert complete.image_sha256 == hashlib.sha256(PNG_BYTES).hexdigest()
    assert {item.kind for item in complete.evidence} == {
        EvidenceKind.URL,
        EvidenceKind.DOWNLOAD,
        EvidenceKind.POPUP,
        EvidenceKind.BLOB,
    }


@pytest.mark.parametrize("event_factory", ["canvas", "image"])
def test_maimai_canvas_and_image_completion_events_are_explicit(
    tmp_path: Path,
    event_factory: str,
) -> None:
    collector = MaimaiCollector(maimai_config(tmp_path))
    maimai_ready(collector)
    if event_factory == "canvas":
        event = MaimaiBrowserEvent.canvas_captured(PNG_BYTES)
    else:
        image_path = tmp_path / "maimai.png"
        image_path.write_bytes(PNG_BYTES)
        event = MaimaiBrowserEvent.image_available(image_path)

    result = collector.observe(event)

    assert result.success is True
    assert result.state is MaimaiState.IMAGE_SUCCESS
    assert result.evidence[-1].kind in {EvidenceKind.CANVAS, EvidenceKind.IMAGE}


def test_maimai_timeout_alone_never_becomes_success(tmp_path: Path) -> None:
    collector = MaimaiCollector(maimai_config(tmp_path))

    result = collector.observe(MaimaiBrowserEvent(MaimaiEventType.TIMEOUT, completed=False))

    assert result.success is False
    assert result.state is MaimaiState.TIMEOUT
    assert result.failure_reason is MaimaiFailureReason.TIMEOUT
    still_failed = collector.observe(MaimaiBrowserEvent.blob_available(PNG_BYTES))
    assert still_failed.success is False
    assert still_failed.state is MaimaiState.TIMEOUT


@pytest.mark.parametrize(
    "gate",
    [
        MaimaiEventType.NOT_LOGGED_IN,
        MaimaiEventType.CAPTCHA,
        MaimaiEventType.TERMS,
        MaimaiEventType.STANDARD_COURSE_MISSING,
        MaimaiEventType.WRONG_PAGE,
        MaimaiEventType.DOM_MISMATCH,
    ],
)
def test_maimai_failure_gate_cannot_be_overwritten_by_later_image(
    tmp_path: Path,
    gate: MaimaiEventType,
) -> None:
    collector = MaimaiCollector(maimai_config(tmp_path))
    failed = collector.observe(MaimaiBrowserEvent(gate, completed=True))

    later = collector.process(
        [
            MaimaiBrowserEvent(
                MaimaiEventType.FULL_DATA_SELECTED,
                completed=True,
                selector="#full",
            ),
            MaimaiBrowserEvent.blob_available(PNG_BYTES),
        ]
    )

    assert failed.success is False
    assert later.success is False
    assert later.state is failed.state


def test_maimai_image_without_explicit_full_data_selection_is_rejected(
    tmp_path: Path,
) -> None:
    collector = MaimaiCollector(maimai_config(tmp_path))

    result = collector.observe(MaimaiBrowserEvent.blob_available(PNG_BYTES))

    assert result.success is False
    assert result.state is MaimaiState.IMAGE_MISSING


class MaimaiAdapterSpy:
    def __init__(self) -> None:
        self.selection_calls = 0

    def select_full_data(self, selector: str) -> MaimaiBrowserEvent:
        self.selection_calls += 1
        return MaimaiBrowserEvent(
            MaimaiEventType.FULL_DATA_SELECTED,
            completed=True,
            selector=selector,
        )

    def execute_bookmarklet(self, source: str) -> MaimaiBrowserEvent:
        return MaimaiBrowserEvent(MaimaiEventType.BOOKMARKLET_LOADED, completed=True)


def test_maimai_without_safe_selector_requires_manual_action_and_does_not_click(
    tmp_path: Path,
) -> None:
    adapter = MaimaiAdapterSpy()
    collector = MaimaiCollector(maimai_config(tmp_path, selector=None), adapter=adapter)

    result = collector.select_full_data()

    assert result.success is False
    assert result.state is MaimaiState.FULL_DATA_SELECTION_FAILURE
    assert result.failure_reason is MaimaiFailureReason.MANUAL_ACTION_REQUIRED
    assert result.manual_action_required is True
    assert adapter.selection_calls == 0


def test_maimai_missing_or_credential_bearing_runtime_file_is_classified(tmp_path: Path) -> None:
    missing = MaimaiConfig(
        persistent_session_dir=tmp_path / "session",
        bookmarklet_runtime_path=tmp_path / "missing.js",
        expected_url="https://maimai.example/",
    )
    missing_result = MaimaiCollector(missing).load_bookmarklet()
    assert missing_result.state is MaimaiState.BOOKMARKLET_LOAD_FAILURE

    credential_file = tmp_path / "credential.js"
    credential_file.write_text("const password = 'should-not-run';", encoding="utf-8")
    credential_config = MaimaiConfig(
        persistent_session_dir=tmp_path / "session-2",
        bookmarklet_runtime_path=credential_file,
        expected_url="https://maimai.example/",
    )
    credential_result = MaimaiCollector(credential_config).load_bookmarklet()
    assert credential_result.state is MaimaiState.BOOKMARKLET_LOAD_FAILURE


def ongeki_config(tmp_path: Path) -> OngekiConfig:
    runtime = tmp_path / "ongeki-bookmarklet.js"
    runtime.write_text("(() => true)();", encoding="utf-8")
    return OngekiConfig(
        persistent_session_dir=tmp_path / "ongeki-session",
        bookmarklet_runtime_path=runtime,
        official_url="https://ongeki.example/official/",
        score_log_url="https://ongeki.example/scorelog/",
        generator_url="https://generator.example/",
    )


def ongeki_flow(collector: OngekiCollector) -> None:
    collector.process(
        [
            OngekiBrowserEvent.official_net_opened("https://ongeki.example/official/"),
            OngekiBrowserEvent.score_log_refresh_completed(),
            OngekiBrowserEvent.score_log_data(fixture("ongeki_scorelog.json")),
            OngekiBrowserEvent.admin_confirmation(),
            OngekiBrowserEvent.generator_opened(),
            OngekiBrowserEvent.user_mode_selected("user-123", "all"),
        ]
    )


def test_ongeki_cannot_open_generator_before_scorelog_completion_and_confirmation() -> None:
    class Adapter:
        def __init__(self) -> None:
            self.open_calls = 0

        def open_generator(self, url: str) -> OngekiBrowserEvent:
            self.open_calls += 1
            return OngekiBrowserEvent.generator_opened()

    config = OngekiConfig("session", "runtime.js", generator_url="https://generator.example/")
    adapter = Adapter()
    collector = OngekiCollector(config, adapter=adapter)

    blocked = collector.open_generator()
    assert blocked.success is False
    assert blocked.failure_reason is OngekiFailureReason.MANUAL_CONFIRMATION_REQUIRED
    assert collector.generator_allowed is False
    assert adapter.open_calls == 0

    collector.observe(OngekiBrowserEvent.official_net_opened(config.official_url))
    collector.observe(OngekiBrowserEvent.score_log_refresh_completed())
    assert collector.state is OngekiState.ADMIN_CONFIRMATION_REQUIRED
    blocked_again = collector.open_generator()
    assert blocked_again.failure_reason is OngekiFailureReason.MANUAL_CONFIRMATION_REQUIRED
    assert adapter.open_calls == 0

    collector.confirm_administrator()
    assert collector.generator_allowed is True
    opened = collector.open_generator()
    assert opened.state is OngekiState.GENERATOR
    assert adapter.open_calls == 1


def test_ongeki_manual_external_refresh_confirmation_allows_generator_without_fake_url(
    tmp_path: Path,
) -> None:
    collector = OngekiCollector(ongeki_config(tmp_path))

    confirmed = collector.observe(
        OngekiBrowserEvent.manual_score_log_refresh_confirmed()
    )

    assert confirmed.state is OngekiState.GENERATOR_READY
    assert collector.generator_allowed is True
    assert not any(
        event.event_type is OngekiEventType.OFFICIAL_NET_OPENED
        for event in collector.events
    )
    opened = collector.observe(
        OngekiBrowserEvent.generator_opened("https://generator.example/")
    )
    assert opened.state is OngekiState.GENERATOR


@pytest.mark.parametrize(
    ("event_type", "state", "reason"),
    [
        (
            OngekiEventType.USER_NOT_FOUND,
            OngekiState.USER_NOT_FOUND,
            OngekiFailureReason.USER_NOT_FOUND,
        ),
        (
            OngekiEventType.PRIVATE_MODE,
            OngekiState.PRIVATE_MODE,
            OngekiFailureReason.PRIVATE_MODE,
        ),
        (
            OngekiEventType.PREMIUM_MISSING,
            OngekiState.PREMIUM_MISSING,
            OngekiFailureReason.PREMIUM_MISSING,
        ),
        (
            OngekiEventType.PROVIDER_NETWORK_OUTAGE,
            OngekiState.PROVIDER_NETWORK_OUTAGE,
            OngekiFailureReason.PROVIDER_NETWORK_OUTAGE,
        ),
        (
            OngekiEventType.IMAGE_MISSING,
            OngekiState.IMAGE_MISSING,
            OngekiFailureReason.IMAGE_MISSING,
        ),
        (
            OngekiEventType.JACKET_IMAGES_NOT_READY,
            OngekiState.JACKET_IMAGES_NOT_READY,
            OngekiFailureReason.JACKET_IMAGES_NOT_READY,
        ),
        (
            OngekiEventType.DOM_MISMATCH,
            OngekiState.DOM_MISMATCH,
            OngekiFailureReason.DOM_MISMATCH,
        ),
    ],
)
def test_ongeki_explicit_failure_classifications(
    tmp_path: Path,
    event_type: OngekiEventType,
    state: OngekiState,
    reason: OngekiFailureReason,
) -> None:
    result = OngekiCollector(ongeki_config(tmp_path)).observe(
        OngekiBrowserEvent(event_type, completed=True)
    )

    assert result.success is False
    assert result.state is state
    assert result.failure_reason is reason


def test_ongeki_explicit_image_success_and_stale_old_image_rejection(tmp_path: Path) -> None:
    old_image = tmp_path / "old.png"
    new_image = tmp_path / "new.png"
    old_image.write_bytes(PNG_BYTES)
    new_image.write_bytes(PNG_BYTES + b"new")
    collector = OngekiCollector(ongeki_config(tmp_path), last_successful_image=old_image)
    ongeki_flow(collector)

    collector.observe(OngekiBrowserEvent.image_generated(generation_id="generation-1"))
    stale = collector.observe(
        OngekiBrowserEvent.image_saved(old_image, generation_id="generation-1")
    )
    assert stale.success is False
    assert stale.state is OngekiState.IMAGE_MISSING
    assert stale.failure_reason is OngekiFailureReason.STALE_IMAGE
    assert stale.stale_image_rejected is True
    assert old_image.read_bytes() == PNG_BYTES
    assert stale.last_successful_image_path == str(old_image)

    collector.observe(OngekiBrowserEvent.image_generated(generation_id="generation-2"))
    success = collector.observe(
        OngekiBrowserEvent.image_saved(new_image, generation_id="generation-2")
    )
    assert success.success is True
    assert success.state is OngekiState.IMAGE_SUCCESS
    assert success.image_path == str(new_image)
    assert success.image_sha256 == hashlib.sha256(new_image.read_bytes()).hexdigest()
    assert success.last_successful_image_path == str(new_image)


def test_ongeki_old_image_without_new_generation_is_not_a_success(tmp_path: Path) -> None:
    old_image = tmp_path / "old.png"
    old_image.write_bytes(PNG_BYTES)
    collector = OngekiCollector(ongeki_config(tmp_path), last_successful_image=old_image)
    ongeki_flow(collector)

    result = collector.observe(OngekiBrowserEvent.image_saved(old_image))

    assert result.success is False
    assert result.state is OngekiState.IMAGE_MISSING
    assert result.last_successful_image_path == str(old_image)


@pytest.mark.parametrize(
    "gate",
    [
        OngekiEventType.USER_NOT_FOUND,
        OngekiEventType.PRIVATE_MODE,
        OngekiEventType.PREMIUM_MISSING,
        OngekiEventType.PROVIDER_NETWORK_OUTAGE,
        OngekiEventType.DOM_MISMATCH,
    ],
)
def test_ongeki_failure_gate_cannot_be_overwritten_by_later_image(
    tmp_path: Path,
    gate: OngekiEventType,
) -> None:
    image = tmp_path / "new.png"
    image.write_bytes(PNG_BYTES)
    collector = OngekiCollector(ongeki_config(tmp_path))
    failed = collector.observe(OngekiBrowserEvent(gate, completed=True))

    later = collector.process(
        [
            OngekiBrowserEvent.image_generated(generation_id="generation-after-failure"),
            OngekiBrowserEvent.image_saved(
                image,
                generation_id="generation-after-failure",
            ),
        ]
    )

    assert failed.success is False
    assert later.success is False
    assert later.state is failed.state


def test_ongeki_image_generation_requires_matching_nonempty_generation_id(
    tmp_path: Path,
) -> None:
    image = tmp_path / "new.png"
    image.write_bytes(PNG_BYTES)
    collector = OngekiCollector(ongeki_config(tmp_path))
    ongeki_flow(collector)

    missing_id = collector.observe(OngekiBrowserEvent.image_generated())
    mismatched = collector.process(
        [
            OngekiBrowserEvent.image_generated(generation_id="generation-1"),
            OngekiBrowserEvent.image_saved(image, generation_id="generation-2"),
        ]
    )

    assert missing_id.success is False
    assert missing_id.state is OngekiState.IMAGE_MISSING
    assert mismatched.success is False
    assert mismatched.state is OngekiState.IMAGE_MISSING


def test_ongeki_retry_policy_only_returns_decisions_and_schedule() -> None:
    policy = RetryPolicy(
        max_attempts=4,
        initial_delay_seconds=2,
        multiplier=2,
        max_delay_seconds=10,
    )

    transient = policy.classify("temporary network timeout", attempt=1)
    final = policy.classify("temporary network timeout", attempt=4)
    permanent = policy.classify("invalid schema", attempt=1)

    assert policy.schedule() == (2.0, 4.0, 8.0)
    assert transient.retry is True
    assert transient.delay_seconds == 2.0
    assert final.retry is False
    assert permanent.retry is False
    assert permanent.transient is False


def test_ongeki_missing_runtime_file_is_classified(tmp_path: Path) -> None:
    config = OngekiConfig(tmp_path / "session", tmp_path / "missing.js")

    result = OngekiCollector(config).load_bookmarklet()

    assert result.success is False
    assert result.state is OngekiState.BOOKMARKLET_LOAD_FAILURE
    assert result.failure_reason is OngekiFailureReason.BOOKMARKLET_LOAD_FAILURE


def test_provider_secret_marker_never_appears_in_public_results_or_rendered_html() -> None:
    secret = "fixture-secret-marker-7c4e"
    profile = {
        "player_name": secret,
        "rating": 15.0,
        "token": secret,
    }
    rating_data = {
        "best": {
            "value": 15.0,
            "entries": [
                {
                    "id": "safe-id",
                    "title": secret,
                    "diff": "MASTER",
                    "score": 1_000_000,
                    "rating": 15.0,
                },
                *([None] * 29),
            ],
        },
        "recent": {"value": 0.0, "entries": [None] * 10},
        "best_candidate": {"entries": []},
        "best_candidate_sss": {"entries": []},
        "best_outside": {"entries": []},
    }

    api, transport = client(
        FixtureTransport(
            profile=CHUNIRECResponse(200, profile),
            rating_data=CHUNIRECResponse(200, rating_data),
        ),
        token=secret,
    )
    result = api.fetch_snapshot()
    assert result.success is True
    assert result.snapshot is not None
    renderer_html = CHUNITHMRenderer(secrets=(secret,)).build_html(result.snapshot)
    public_values = (
        repr(api.config),
        repr(result),
        str(result),
        repr(result.snapshot),
        repr(result.snapshot.raw_data),
        renderer_html,
    )
    assert all(secret not in value for value in public_values)
    assert transport.calls[0][2]["token"] == secret

    class RaisingTransport:
        def request(self, method: str, url: str, *, params: dict[str, str]) -> object:
            raise RuntimeError(f"transport url={url}?token={secret}")

    failed = CHUNIRECClient(
        RaisingTransport(),
        CHUNIRECConfig(token=secret, user_id="user-123"),
    ).fetch_snapshot()
    assert secret not in repr(failed)
    assert secret not in str(failed)
