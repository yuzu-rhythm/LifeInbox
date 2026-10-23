from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from life_inbox.rhythm_collectors import (
    CHUNIRECClient,
    CHUNIRECConfig,
    CHUNIRECFailureReason,
    MaimaiBrowserEvent,
    MaimaiCollector,
    MaimaiConfig,
    MaimaiEventType,
    MaimaiState,
    OngekiBrowserEvent,
    OngekiCollector,
    OngekiConfig,
    OngekiDataSchemaError,
    PlaywrightMaimaiAdapter,
    PlaywrightOngekiAdapter,
    parse_chunirec_v2_rating_data,
    parse_maimai_data,
    parse_ongeki_data,
)

FIXTURES = Path(__file__).parent / "fixtures" / "rhythm_collectors"
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def maimai_config(tmp_path: Path) -> MaimaiConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    runtime = tmp_path / "maimai-bookmarklet.js"
    runtime.write_text("(() => true)();", encoding="utf-8")
    return MaimaiConfig(
        persistent_session_dir=tmp_path / "session",
        bookmarklet_runtime_path=runtime,
        expected_url="https://maimai.example/maimai-mobile/",
        full_data_selector="#full",
    )


def ongeki_config(tmp_path: Path) -> OngekiConfig:
    runtime = tmp_path / "ongeki-bookmarklet.js"
    runtime.write_text("(() => true)();", encoding="utf-8")
    return OngekiConfig(
        persistent_session_dir=tmp_path / "session",
        bookmarklet_runtime_path=runtime,
        official_url="https://ongeki.example/official/",
        score_log_url="https://ongeki.example/scorelog/",
        generator_url="https://generator.example/",
    )


def test_chunirec_v2_parser_validates_fixed_slots_and_candidate_envelopes() -> None:
    payload = fixture("chunirec_rating_data.json")
    parsed = parse_chunirec_v2_rating_data(payload)

    assert parsed.best_value == 15.7
    assert parsed.recent_value == 14.3
    assert len(parsed.best) == 2
    assert len(parsed.recent) == 1
    assert len(parsed.new_equivalent) == 1
    assert parsed.best[0].clear_status == "clear"

    malformed = dict(payload)
    malformed["best"] = {"value": 15.7, "entries": [None]}
    with pytest.raises(ValueError, match="exactly 30"):
        parse_chunirec_v2_rating_data(malformed)

    missing_candidate = dict(payload)
    del missing_candidate["best_candidate_sss"]
    with pytest.raises(ValueError, match="best_candidate_sss"):
        parse_chunirec_v2_rating_data(missing_candidate)

    too_many_candidates = dict(payload)
    too_many_candidates["best_candidate"] = {
        "entries": [
            {
                "id": f"candidate-{index}",
                "diff": "MAS",
                "score": 1_000_000,
            }
            for index in range(21)
        ]
    }
    with pytest.raises(ValueError, match="at most 20"):
        parse_chunirec_v2_rating_data(too_many_candidates)

    nested_row = dict(payload)
    nested_row["best"] = {
        "value": 15.7,
        "entries": [
            {
                "id": {"value": "not-a-v2-id"},
                "diff": "MAS",
            },
            *([None] * 29),
        ],
    }
    with pytest.raises(ValueError, match="id must be a non-empty string"):
        parse_chunirec_v2_rating_data(nested_row)


def test_chunirec_timeout_is_safe_and_distinct_from_generic_transport_failure() -> None:
    class TimeoutTransport:
        def request(self, method: str, url: str, *, params: dict[str, str]) -> object:
            raise TimeoutError("token=must-not-escape")

    result = CHUNIRECClient(
        TimeoutTransport(),
        CHUNIRECConfig(token="secret", user_id="user-1", region="jp"),
    ).fetch_snapshot()

    assert result.success is False
    assert result.failure_reason is CHUNIRECFailureReason.TIMEOUT
    assert "must-not-escape" not in repr(result)
    assert "secret" not in repr(result)


def test_chunirec_response_json_timeout_is_not_misclassified_as_malformed_json() -> None:
    class TimeoutResponse:
        status_code = 200
        headers: dict[str, str] = {}

        def json(self) -> object:
            raise TimeoutError("token=must-not-escape")

    class ResponseTransport:
        def request(self, method: str, url: str, *, params: dict[str, str]) -> object:
            return TimeoutResponse()

    result = CHUNIRECClient(
        ResponseTransport(),
        CHUNIRECConfig(token="secret", user_id="user-1", region="jp"),
    ).fetch_snapshot()

    assert result.success is False
    assert result.failure_reason is CHUNIRECFailureReason.TIMEOUT
    assert "must-not-escape" not in repr(result)
    assert "secret" not in repr(result)


def test_maimai_fixture_normalizes_frames_and_chart_metrics() -> None:
    snapshot = parse_maimai_data(fixture("maimai_rating_data.json"))

    assert snapshot.game.value == "maimai"
    assert snapshot.player_name == "Fixture Maimai Player"
    assert snapshot.rating == 14.32
    assert {entry.frame_membership for entry in snapshot.entries} == {
        "best",
        "recent",
        "new",
    }
    best = next(entry for entry in snapshot.entries if entry.song_identifier == "maimai-001")
    assert best.chart_type == "standard"
    assert best.chart_constant == 14.7
    assert best.score == 1_005_000
    assert best.achievement == 101.2
    assert best.technical_score == 2450
    assert best.p_score == 3
    assert best.song_rating == 14.82
    assert best.full_bell_status == "full_bell"


def test_maimai_fresh_result_has_initialized_stale_image_flag(tmp_path: Path) -> None:
    """Calling result before any event must be a valid event-driven outcome."""

    collector = MaimaiCollector(maimai_config(tmp_path))

    result = collector.result()

    assert result.success is False
    assert result.complete is False
    assert result.stale_image_rejected is False


def test_maimai_parser_deduplicates_named_frame_container_and_root_alias() -> None:
    row = {
        "songId": "maimai-one",
        "title": "One",
        "score": 1_000_000,
    }
    payload = {
        "playerName": "Player",
        "rating": 12.0,
        "frames": {"best_records": [row]},
        "best": [row],
    }

    snapshot = parse_maimai_data(payload)

    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].frame_membership == "best"


def test_maimai_data_and_png_events_are_separate_but_complete_is_explicit(tmp_path: Path) -> None:
    collector = MaimaiCollector(maimai_config(tmp_path))
    collector.process(
        [
            MaimaiBrowserEvent.url_observed("https://maimai.example/maimai-mobile/"),
            MaimaiBrowserEvent(
                MaimaiEventType.FULL_DATA_SELECTED,
                completed=True,
                selector="#full",
            ),
            MaimaiBrowserEvent.rating_data(fixture("maimai_rating_data.json")),
            MaimaiBrowserEvent.canvas_captured(PNG_BYTES),
        ]
    )

    result = collector.result()
    assert result.success is True
    assert result.complete is True
    assert result.ready_for_persistence is False
    assert result.snapshot is not None
    assert result.state is MaimaiState.IMAGE_SUCCESS
    assert any(e.kind.value == "data" for e in result.evidence)


def test_maimai_invalid_data_and_non_png_never_become_success(tmp_path: Path) -> None:
    collector = MaimaiCollector(maimai_config(tmp_path))
    failed_data = collector.observe(MaimaiBrowserEvent.rating_data({"rating": 12}))
    assert failed_data.success is False
    assert failed_data.state is MaimaiState.RATING_DATA_MISSING
    assert failed_data.manual_action_required is True

    second = MaimaiCollector(maimai_config(tmp_path / "second"))
    second.process(
        [
            MaimaiBrowserEvent(
                MaimaiEventType.FULL_DATA_SELECTED,
                completed=True,
                selector="#full",
            ),
            MaimaiBrowserEvent.blob_available(b"not-a-png"),
        ]
    )
    assert second.result().success is False


def test_maimai_image_candidate_does_not_replace_last_success_without_data(
    tmp_path: Path,
) -> None:
    old_image = tmp_path / "old.png"
    new_image = tmp_path / "new.png"
    old_image.write_bytes(PNG_BYTES)
    new_image.write_bytes(PNG_BYTES + b"new-candidate")
    collector = MaimaiCollector(
        maimai_config(tmp_path),
        last_successful_image=old_image,
    )
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

    result = collector.observe(MaimaiBrowserEvent.image_available(new_image))

    assert result.success is True
    assert result.complete is False
    assert result.last_successful_image_path == str(old_image)
    assert result.last_successful_image_sha256 == hashlib.sha256(PNG_BYTES).hexdigest()


def test_maimai_explicit_data_failure_after_image_cannot_remain_success(
    tmp_path: Path,
) -> None:
    image = tmp_path / "generated.png"
    image.write_bytes(PNG_BYTES)
    collector = MaimaiCollector(maimai_config(tmp_path))
    collector.process(
        [
            MaimaiBrowserEvent.url_observed("https://maimai.example/maimai-mobile/"),
            MaimaiBrowserEvent(
                MaimaiEventType.FULL_DATA_SELECTED,
                completed=True,
                selector="#full",
            ),
            MaimaiBrowserEvent.image_available(image),
        ]
    )

    result = collector.observe(
        MaimaiBrowserEvent(MaimaiEventType.RATING_DATA_FAILED, completed=False)
    )

    assert result.success is False
    assert result.state is MaimaiState.RATING_DATA_MISSING
    assert result.last_successful_image_path is None


def test_maimai_parser_rejects_rows_without_numeric_rating_or_chart_measure() -> None:
    with pytest.raises(ValueError, match="measure"):
        parse_maimai_data(
            {
                "playerName": "Player",
                "rating": 12.0,
                "best": [{"songId": "song-only", "title": "No score"}],
            }
        )

    with pytest.raises(ValueError, match="negative"):
        parse_maimai_data(
            {
                "playerName": "Player",
                "rating": 12.0,
                "best": [{"songId": "negative", "score": -1}],
            }
        )


def test_ongeki_scorelog_is_required_for_complete_result_and_same_path_new_bytes_are_allowed(
    tmp_path: Path,
) -> None:
    image = tmp_path / "generated.png"
    image.write_bytes(PNG_BYTES)
    collector = OngekiCollector(ongeki_config(tmp_path), last_successful_image=image)
    collector.process(
        [
            OngekiBrowserEvent.official_net_opened("https://ongeki.example/official/"),
            OngekiBrowserEvent.score_log_refresh_completed(),
            OngekiBrowserEvent.admin_confirmation(),
            OngekiBrowserEvent.generator_opened(),
            OngekiBrowserEvent.user_mode_selected("user-1", "all"),
            OngekiBrowserEvent.score_log_data(fixture("ongeki_scorelog.json")),
            OngekiBrowserEvent.image_generated(generation_id="generation-1"),
        ]
    )
    image.write_bytes(PNG_BYTES + b"new-rendered-bytes")
    result = collector.observe(
        OngekiBrowserEvent.image_saved(image, generation_id="generation-1")
    )

    assert result.success is True
    assert result.complete is True
    assert result.snapshot is not None
    assert result.snapshot.game.value == "ongeki"
    assert result.snapshot.entries[0].technical_score == 1820
    assert result.stale_image_rejected is False


def test_ongeki_scorelog_parser_rejects_incomplete_scorelog() -> None:
    with pytest.raises(OngekiDataSchemaError):
        parse_ongeki_data({"scoreLog": {"entries": []}, "playerName": "x"})


def test_ongeki_generator_player_data_is_normalized() -> None:
    snapshot = parse_ongeki_data(
        {
            "profile": {"name": "Generator Player", "rating": 16.45},
            "rating": {
                "best": [
                    {
                        "id": 123,
                        "title": "Best Song",
                        "diff": "MAS",
                        "level": 14.5,
                        "const": 14.5,
                        "score": 1_009_000,
                        "rating": 16.2,
                        "rank": "SSS+",
                        "update": "2026-08-30",
                        "lamps": {
                            "is_fullcombo": True,
                            "is_allbreak": False,
                            "is_fullbell": True,
                        },
                    }
                ],
                "new": [],
                "pscore": [
                    {
                        "id": 456,
                        "title": "P Score Song",
                        "diff": "LUN",
                        "level": 15,
                        "const": 15,
                        "score": 1_010_000,
                        "p_score": 12_345,
                        "p_rating": 16.1,
                        "rank": "SSS+",
                        "update": "2026-08-30",
                        "lamps": {},
                    }
                ],
            },
        }
    )

    assert snapshot.game.value == "ongeki"
    assert float(snapshot.rating) == 16.45
    assert [entry.frame_membership for entry in snapshot.entries] == ["best", "pscore"]
    assert snapshot.entries[0].technical_score == 1_009_000
    assert snapshot.entries[0].full_bell_status == "full_bell"
    assert snapshot.entries[1].p_score == 12_345
    assert snapshot.entries[1].song_rating == 16.1


def test_ongeki_data_event_before_refresh_is_not_accepted(tmp_path: Path) -> None:
    collector = OngekiCollector(ongeki_config(tmp_path))
    collector.observe(
        OngekiBrowserEvent.official_net_opened("https://ongeki.example/official/")
    )

    result = collector.observe(
        OngekiBrowserEvent.score_log_data(fixture("ongeki_scorelog.json"))
    )

    assert result.success is False
    assert result.state.value == "score_log_data_missing"
    assert result.score_log_data_confirmed is False


def test_ongeki_image_candidate_does_not_replace_last_success_without_scorelog(
    tmp_path: Path,
) -> None:
    old_image = tmp_path / "old.png"
    new_image = tmp_path / "new.png"
    old_image.write_bytes(PNG_BYTES)
    new_image.write_bytes(PNG_BYTES + b"new-candidate")
    collector = OngekiCollector(ongeki_config(tmp_path), last_successful_image=old_image)
    collector.process(
        [
            OngekiBrowserEvent.official_net_opened("https://ongeki.example/official/"),
            OngekiBrowserEvent.score_log_refresh_completed(),
            OngekiBrowserEvent.admin_confirmation(),
            OngekiBrowserEvent.generator_opened(),
            OngekiBrowserEvent.user_mode_selected("user-1", "all"),
            OngekiBrowserEvent.image_generated(generation_id="generation-1"),
        ]
    )

    result = collector.observe(
        OngekiBrowserEvent.image_saved(new_image, generation_id="generation-1")
    )

    assert result.success is True
    assert result.complete is False
    assert result.last_successful_image_path == str(old_image)
    assert result.last_successful_image_sha256 == hashlib.sha256(PNG_BYTES).hexdigest()


class RedirectingPage:
    def __init__(self, final_url: str, error: Exception | None = None) -> None:
        self.url = final_url
        self.error = error

    def goto(self, *_args: object, **_kwargs: object) -> None:
        if self.error is not None:
            raise self.error


def test_browser_adapters_report_final_url_and_timeout_without_page_details() -> None:
    maimai_event = PlaywrightMaimaiAdapter(
        RedirectingPage("https://maimai.example/login/?session=secret")
    ).open_url("https://maimai.example/maimai-mobile/")
    assert maimai_event.event_type.value == "url_observed"
    assert maimai_event.url == "https://maimai.example/login/"
    assert "secret" not in repr(maimai_event)

    class BrowserTimeoutError(Exception):
        pass

    ongeki_event = PlaywrightOngekiAdapter(
        RedirectingPage("https://ongeki.example/", BrowserTimeoutError("secret=hidden"))
    ).open_official_net("https://ongeki.example/official/")
    assert ongeki_event.event_type.value == "timeout"
    assert ongeki_event.url is None


def test_ongeki_adapter_reports_final_urls_for_scorelog_and_generator_navigation() -> None:
    page = RedirectingPage("https://ongeki.example/scorelog/?session=secret")
    adapter = PlaywrightOngekiAdapter(page)

    score_log_event = adapter.refresh_score_log("https://ongeki.example/scorelog/")
    assert score_log_event.url == "https://ongeki.example/scorelog/"

    page.url = "https://generator.example/?session=secret"
    generator_event = adapter.open_generator("https://generator.example/")
    assert generator_event.url == "https://generator.example/"
    assert "session" not in repr(score_log_event)
    assert "session" not in repr(generator_event)


def test_ongeki_collector_rejects_scorelog_or_generator_redirects(tmp_path: Path) -> None:
    config = ongeki_config(tmp_path)

    score_log_collector = OngekiCollector(config)
    score_log_collector.observe(OngekiBrowserEvent.official_net_opened(config.official_url))
    score_log_result = score_log_collector.observe(
        OngekiBrowserEvent.score_log_refresh_completed(
            url="https://ongeki.example/login/?session=secret"
        )
    )
    assert score_log_result.success is False
    assert score_log_result.state.value == "dom_mismatch"
    assert "secret" not in repr(score_log_result)

    generator_collector = OngekiCollector(config)
    generator_collector.process(
        [
            OngekiBrowserEvent.official_net_opened(config.official_url),
            OngekiBrowserEvent.score_log_refresh_completed(),
            OngekiBrowserEvent.admin_confirmation(),
        ]
    )
    generator_result = generator_collector.observe(
        OngekiBrowserEvent.generator_opened(
            "https://ongeki.example/login/?session=secret"
        )
    )
    assert generator_result.success is False
    assert generator_result.state.value == "dom_mismatch"
    assert "secret" not in repr(generator_result)


def test_browser_adapters_require_explicit_data_capture_hook() -> None:
    maimai_event = PlaywrightMaimaiAdapter().capture_rating_data()
    assert maimai_event.event_type.value == "rating_data_failed"
    assert maimai_event.completed is False

    ongeki_event = PlaywrightOngekiAdapter(
        data_capture=lambda: fixture("ongeki_scorelog.json")
    ).capture_score_log_data()
    assert ongeki_event.event_type.value == "score_log_data_available"
    assert ongeki_event.completed is True
    assert ongeki_event.payload is not None
