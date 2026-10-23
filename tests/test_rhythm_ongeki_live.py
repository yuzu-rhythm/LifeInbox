from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import fields
from pathlib import Path

import pytest

from life_inbox.rhythm_collectors import OngekiBrowserEvent, parse_ongeki_data
from life_inbox.rhythm_ongeki_live import (
    _INSTALL_JACKET_READINESS_SCRIPT,
    _ONGEKI_JACKET_BASE_PATH,
    _ONGEKI_JACKET_PLACEHOLDER_PATH,
    _ONGEKI_JACKET_PLACEHOLDER_PATHS,
    _ONGEKI_JACKET_SELECTOR,
    DEFAULT_ONGEKI_GENERATOR_URL,
    DEFAULT_ONGEKI_OFFICIAL_URL,
    DEFAULT_ONGEKI_SCORE_LOG_URL,
    OngekiLiveConfig,
    OngekiLiveDiagnostic,
    OngekiLiveDiagnosticCode,
    OngekiLiveFailureReason,
    OngekiLiveOutcome,
    OngekiLiveStage,
    _classify_ongeki_provider_message,
    _is_network_error,
    _jacket_readiness_status,
    _wait_for_new_result_image,
    collect_ongeki_events,
    decode_image_bytes,
    decode_image_data_uri,
    write_image_atomically,
)

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
# A deliberately tiny synthetic one-component JPEG with a complete SOF/SOS/EOI
# structure.  It contains no user image or third-party fixture data.
JPEG_BYTES = bytes.fromhex(
    "ffd8ffc0000b080001000101011100ffda0008010100003f0000ffd9"
)


def config(tmp_path: Path, **overrides: object) -> OngekiLiveConfig:
    values: dict[str, object] = {
        "session_dir": tmp_path / "ongeki-session",
        "output_path": tmp_path / "ongeki-overview.png",
        "user_id": "synthetic-user",
        "mode": "bestnew",
    }
    values.update(overrides)
    return OngekiLiveConfig(**values)


def score_log_payload() -> dict[str, object]:
    return {
        "playerName": "Synthetic Player",
        "rating": 13.75,
        "scoreLog": {
            "updatedAt": "2026-08-29T05:00:00Z",
            "entries": [
                {
                    "song_id": "synthetic-song",
                    "title": "Synthetic Song",
                    "difficulty": "MASTER",
                    "chart_type": "standard",
                    "level": "14",
                    "chart_constant": 14.0,
                    "score": 1_000_000,
                    "achievement": 100.5,
                    "technical_score": 1820,
                    "p_score": 2,
                    "song_rating": 13.75,
                    "clear_status": "CLEAR",
                    "genre": "ORIGINAL",
                }
            ],
        },
    }


def complete_events(image: bytes = PNG_BYTES) -> list[OngekiBrowserEvent]:
    return [
        OngekiBrowserEvent.official_net_opened(DEFAULT_ONGEKI_OFFICIAL_URL),
        OngekiBrowserEvent(
            "score_log_refresh_started",
            completed=True,
            url=DEFAULT_ONGEKI_SCORE_LOG_URL,
        ),
        OngekiBrowserEvent(
            "bookmarklet_loaded",
            completed=True,
        ),
        OngekiBrowserEvent.score_log_refresh_completed(url=DEFAULT_ONGEKI_SCORE_LOG_URL),
        OngekiBrowserEvent.admin_confirmation(),
        OngekiBrowserEvent.score_log_data(score_log_payload()),
        OngekiBrowserEvent.generator_opened(DEFAULT_ONGEKI_GENERATOR_URL),
        OngekiBrowserEvent.user_mode_selected("synthetic-user", "bestnew"),
        OngekiBrowserEvent.image_generated(generation_id="synthetic-generation"),
        OngekiBrowserEvent.image_saved(
            payload=image,
            generation_id="synthetic-generation",
        ),
    ]


def complete_manually_confirmed_events(
    image: bytes = PNG_BYTES,
) -> list[OngekiBrowserEvent]:
    """A human-run refresh is valid without persisting its secret script."""

    return [
        event
        for event in complete_events(image)
        if event.event_type.value != "bookmarklet_loaded"
    ]


def generator_captured_data_events(
    image: bytes = PNG_BYTES,
) -> list[OngekiBrowserEvent]:
    """Match the live generator order: parsePage data precedes image evidence."""

    events = complete_events(image)
    data_event = next(
        event
        for event in events
        if event.event_type.value == "score_log_data_available"
    )
    without_data = [event for event in events if event is not data_event]
    image_index = next(
        index
        for index, event in enumerate(without_data)
        if event.event_type.value == "image_generated"
    )
    without_data.insert(image_index, data_event)
    return without_data


def manual_external_refresh_events(
    image: bytes = PNG_BYTES,
) -> list[OngekiBrowserEvent]:
    """A user confirms a refresh performed in their own authenticated Chrome."""

    return [
        OngekiBrowserEvent.manual_score_log_refresh_confirmed(),
        OngekiBrowserEvent.generator_opened(DEFAULT_ONGEKI_GENERATOR_URL),
        OngekiBrowserEvent.user_mode_selected("synthetic-user", "bestnew"),
        OngekiBrowserEvent.score_log_data(score_log_payload()),
        OngekiBrowserEvent.image_generated(generation_id="manual-generation"),
        OngekiBrowserEvent.image_saved(
            payload=image,
            generation_id="manual-generation",
        ),
    ]


class SyntheticReadinessPage:
    """Minimal page seam returning fixed synthetic browser state only."""

    def __init__(self, value: object) -> None:
        self.value = value
        self.scripts: list[str] = []

    def evaluate(self, script: str) -> object:
        self.scripts.append(script)
        return self.value


class KeywordOnlyWaitPage:
    """Matches Playwright's keyword-only evaluate-argument contract."""

    def __init__(self) -> None:
        self.arg: object = None
        self.timeout: int | None = None

    def wait_for_function(
        self,
        expression: str,
        *,
        arg: object = None,
        timeout: int | None = None,
    ) -> None:
        assert "naturalWidth" in expression
        self.arg = arg
        self.timeout = timeout


def test_config_is_credential_free_and_validates_exact_https_urls(tmp_path: Path) -> None:
    live_config = config(tmp_path)

    assert live_config.session_directory.endswith("ongeki-session")
    assert live_config.user_id_selector == "#osl_id"
    assert live_config.mode_selector == "#display-content"
    assert live_config.generate_selector == "#generate"
    assert live_config.image_selector == "#result-img"
    assert live_config.copyright_mode is True
    assert "synthetic-user" not in repr(live_config)
    field_names = {item.name.casefold() for item in fields(OngekiLiveConfig)}
    assert not field_names & {
        "password",
        "sega_id",
        "sega_password",
        "credential",
        "credentials",
        "cookie",
        "token",
    }

    invalid_urls = (
        ("official_url", "http://ongeki-net.com/ongeki-net/"),
        ("official_url", "https://ongeki-net.com/ongeki-net/?session=secret"),
        ("score_log_url", "https://ongeki-net.com/ongeki-net/scoreLog"),
        ("generator_url", "https://reiwa.f5.si/newbestimg/ongeki/?user=secret"),
        ("generator_url", "http://reiwa.f5.si/newbestimg/ongeki/"),
    )
    for name, value in invalid_urls:
        with pytest.raises(ValueError, match="exact HTTPS URL"):
            config(tmp_path, **{name: value})


def test_non_copyright_jacket_guard_uses_explicit_image_readiness_without_sleep() -> None:
    assert _ONGEKI_JACKET_SELECTOR in _INSTALL_JACKET_READINESS_SCRIPT
    assert _ONGEKI_JACKET_BASE_PATH in _INSTALL_JACKET_READINESS_SCRIPT
    assert _ONGEKI_JACKET_PLACEHOLDER_PATH in _INSTALL_JACKET_READINESS_SCRIPT
    assert all(
        path in _INSTALL_JACKET_READINESS_SCRIPT
        for path in _ONGEKI_JACKET_PLACEHOLDER_PATHS
    )
    assert 'addEventListener("load"' in _INSTALL_JACKET_READINESS_SCRIPT
    assert 'addEventListener("error"' in _INSTALL_JACKET_READINESS_SCRIPT
    assert "image.complete" in _INSTALL_JACKET_READINESS_SCRIPT
    assert "image.naturalWidth > 0" in _INSTALL_JACKET_READINESS_SCRIPT
    assert "image.naturalHeight > 0" in _INSTALL_JACKET_READINESS_SCRIPT
    assert "captureDisplay" in _INSTALL_JACKET_READINESS_SCRIPT
    assert 'target.style.display = "none"' in _INSTALL_JACKET_READINESS_SCRIPT
    assert "setTimeout" not in _INSTALL_JACKET_READINESS_SCRIPT


def test_current_generator_fixture_normalizes_best_new_and_pscore_frames() -> None:
    payload = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "rhythm_collectors"
            / "ongeki_generator_player_data.json"
        ).read_text(encoding="utf-8")
    )

    snapshot = parse_ongeki_data(payload)

    assert snapshot.player_name == "Fixture Generator Player"
    assert snapshot.rating == 16.45
    assert {entry.frame_membership for entry in snapshot.entries} == {
        "best",
        "new",
        "pscore",
    }
    generator_best = next(
        entry for entry in snapshot.entries if entry.song_identifier == "123"
    )
    assert generator_best.technical_score == 1_009_000
    assert generator_best.full_combo_status == "full_combo"
    assert generator_best.full_bell_status == "full_bell"
    pscore = next(entry for entry in snapshot.entries if entry.song_identifier == "789")
    assert pscore.p_score == 12_345
    assert pscore.song_rating == 16.1


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ("User not found", OngekiLiveFailureReason.USER_NOT_FOUND),
        ("エラー: ユーザーが見つかりませんでした。", OngekiLiveFailureReason.USER_NOT_FOUND),
        ("Premium user only", OngekiLiveFailureReason.PREMIUM_MISSING),
        ("エラー: プレミアムコースに登録していません。", OngekiLiveFailureReason.PREMIUM_MISSING),
        (
            "エラー: OngekiScoreLogにスコアを登録していません。プライベートモードが有効です。",
            OngekiLiveFailureReason.PRIVATE_MODE,
        ),
        ("Score not fetched", OngekiLiveFailureReason.SCORE_LOG_DATA_MISSING),
        ("Failed to fetch", OngekiLiveFailureReason.PROVIDER_NETWORK_OUTAGE),
        ("unrelated provider text", None),
    ],
)
def test_current_generator_error_signals_are_fixed_and_distinct(
    message: str,
    reason: OngekiLiveFailureReason | None,
) -> None:
    assert _classify_ongeki_provider_message(message) is reason


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ConnectionError("network unavailable"), True),
        (RuntimeError("net::ERR_CONNECTION_RESET"), True),
        (RuntimeError("net::ERR_NAME_NOT_RESOLVED"), True),
        (RuntimeError("net::ERR_FAILED"), True),
        (ValueError("DOM mismatch"), False),
    ],
)
def test_network_exception_classifier_does_not_hide_dom_or_provider_failures(
    error: Exception,
    expected: bool,
) -> None:
    assert _is_network_error(error) is expected


def test_result_image_wait_uses_playwright_keyword_only_argument() -> None:
    page = KeywordOnlyWaitPage()

    _wait_for_new_result_image(
        page,
        selector="#result-img",
        previous_source="data:image/jpeg;base64,old",
        timeout_ms=12_345,
    )

    assert page.arg == ["#result-img", "data:image/jpeg;base64,old"]
    assert page.timeout == 12_345


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            {"status": "failed", "code": "jacket_image_load_failed"},
            ("failed", "jacket_image_load_failed"),
        ),
        ({"status": "waiting", "code": None}, ("waiting", None)),
        ({"status": "failed", "code": 42}, ("failed", None)),
        (None, (None, None)),
    ],
)
def test_jacket_readiness_status_is_fail_closed_for_synthetic_dom_state(
    value: object,
    expected: tuple[str | None, str | None],
) -> None:
    page = SyntheticReadinessPage(value)

    assert _jacket_readiness_status(page) == expected
    assert page.scripts


def test_jacket_readiness_failure_is_distinct_and_blocks_synthetic_flow(
    tmp_path: Path,
) -> None:
    result = collect_ongeki_events(
        config(tmp_path, copyright_mode=False),
        [*complete_events()[:-2], OngekiBrowserEvent.jacket_images_not_ready()],
    )

    assert result.success is False
    assert result.complete is False
    assert result.outcome is OngekiLiveOutcome.PROVIDER_FAILURE
    assert result.failure_reason is OngekiLiveFailureReason.JACKET_IMAGES_NOT_READY
    assert result.state.value == "jacket_images_not_ready"
    assert result.collection is not None
    assert result.collection.image_path is None
    assert result.events[-1].event_type.value == "jacket_images_not_ready"


def test_complete_png_and_jpeg_decoders_and_atomic_write(tmp_path: Path) -> None:
    png = decode_image_bytes(PNG_BYTES)
    jpeg = decode_image_bytes(JPEG_BYTES)

    assert (png.mime_type, png.width, png.height) == ("image/png", 1, 1)
    assert (jpeg.mime_type, jpeg.width, jpeg.height) == ("image/jpeg", 1, 1)
    assert decode_image_data_uri(
        "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
    ).sha256 == hashlib.sha256(PNG_BYTES).hexdigest()
    assert decode_image_data_uri(
        "data:image/jpeg;base64," + base64.b64encode(JPEG_BYTES).decode("ascii")
    ).mime_type == "image/jpeg"

    destination = tmp_path / "nested" / "generated.jpg"
    written = write_image_atomically(destination, jpeg)
    assert written == destination.resolve()
    assert written.read_bytes() == JPEG_BYTES
    assert not tuple(written.parent.glob(f".{written.name}.*.tmp"))

    with pytest.raises(ValueError):
        decode_image_bytes(JPEG_BYTES[:-2])
    with pytest.raises(ValueError):
        decode_image_data_uri("data:image/png;base64,bm90LWltYWdl")


def test_score_log_completion_without_known_dom_stays_manual_and_blocks_generator(
    tmp_path: Path,
) -> None:
    live_config = config(tmp_path)
    result = collect_ongeki_events(
        live_config,
        [
            OngekiBrowserEvent.official_net_opened(DEFAULT_ONGEKI_OFFICIAL_URL),
            OngekiBrowserEvent(
                "score_log_refresh_started",
                completed=True,
                url=DEFAULT_ONGEKI_SCORE_LOG_URL,
            ),
        ],
    )

    assert result.success is False
    assert result.outcome is OngekiLiveOutcome.MANUAL_ACTION_REQUIRED
    assert result.failure_reason is OngekiLiveFailureReason.MANUAL_CONFIRMATION_REQUIRED
    assert result.manual_action_required is True
    assert result.diagnostic == OngekiLiveDiagnostic(
        OngekiLiveStage.SCORE_LOG_CONFIRMATION,
        OngekiLiveDiagnosticCode.SCORE_LOG_CONFIRMATION_REQUIRED,
    )

    blocked = collect_ongeki_events(
        live_config,
        [
            OngekiBrowserEvent.official_net_opened(DEFAULT_ONGEKI_OFFICIAL_URL),
            OngekiBrowserEvent.score_log_refresh_completed(),
            OngekiBrowserEvent.generator_opened(DEFAULT_ONGEKI_GENERATOR_URL),
        ],
    )
    assert blocked.success is False
    assert blocked.outcome is OngekiLiveOutcome.MANUAL_ACTION_REQUIRED
    assert blocked.diagnostic.code is OngekiLiveDiagnosticCode.EVENT_ORDER_INVALID
    assert not any(
        event.event_type.value == "generator_opened" for event in blocked.events
    )


def test_generator_captured_scorelog_data_is_accepted_before_image_event(
    tmp_path: Path,
) -> None:
    result = collect_ongeki_events(
        config(tmp_path),
        generator_captured_data_events(),
    )

    assert result.success is True
    assert result.complete is True
    assert result.collection is not None
    assert result.collection.snapshot is not None
    assert result.collection.snapshot.player_name == "Synthetic Player"
    assert result.image is not None


def test_refresh_completed_embedded_scorelog_payload_counts_as_data_available(
    tmp_path: Path,
) -> None:
    events = complete_events()
    refresh_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type.value == "score_log_refresh_completed"
    )
    events[refresh_index] = OngekiBrowserEvent.score_log_refresh_completed(
        url=DEFAULT_ONGEKI_SCORE_LOG_URL,
        payload=score_log_payload(),
    )
    events = [
        event
        for event in events
        if event.event_type.value != "score_log_data_available"
    ]

    result = collect_ongeki_events(config(tmp_path), events)

    assert result.success is True
    assert result.collection is not None
    assert result.collection.snapshot is not None
    assert result.collection.snapshot.player_name == "Synthetic Player"


def test_manual_external_refresh_confirmation_is_explicit_and_complete(
    tmp_path: Path,
) -> None:
    result = collect_ongeki_events(
        config(tmp_path),
        manual_external_refresh_events(),
    )

    assert result.success is True
    assert result.complete is True
    assert result.collection is not None
    assert result.collection.snapshot is not None
    assert result.image is not None
    assert result.events[0].event_type.value == "manual_score_log_refresh_confirmed"
    assert not any(
        event.event_type.value == "official_net_opened"
        for event in result.events
    )


@pytest.mark.parametrize(
    ("image", "mime_type"),
    [(PNG_BYTES, "image/png"), (JPEG_BYTES, "image/jpeg")],
)
def test_complete_event_flow_normalizes_scorelog_after_new_image_artifact(
    tmp_path: Path,
    image: bytes,
    mime_type: str,
) -> None:
    result = collect_ongeki_events(config(tmp_path), complete_events(image))

    assert result.success is True
    assert result.complete is True
    assert result.outcome is OngekiLiveOutcome.SUCCESS
    assert result.collection is not None
    assert result.collection.snapshot is not None
    assert result.collection.snapshot.game.value == "ongeki"
    assert result.collection.snapshot.rating == 13.75
    assert result.image is not None
    assert result.image.mime_type == mime_type
    assert result.message == "ongeki_live:success:collector_normalization"


def test_explicit_manual_refresh_can_complete_without_saved_bookmarklet(
    tmp_path: Path,
) -> None:
    result = collect_ongeki_events(
        config(tmp_path, score_log_bookmarklet_path=None),
        complete_manually_confirmed_events(),
    )

    assert result.success is True
    assert result.collection is not None
    assert result.collection.snapshot is not None
    assert not any(
        event.event_type.value == "bookmarklet_loaded" for event in result.events
    )


def test_old_image_sha_is_not_accepted_as_a_new_generation(tmp_path: Path) -> None:
    old_image = tmp_path / "old-overview.png"
    old_image.write_bytes(PNG_BYTES)
    result = collect_ongeki_events(
        config(tmp_path),
        complete_events(PNG_BYTES),
        last_successful_image=old_image,
    )

    assert result.success is False
    assert result.outcome is OngekiLiveOutcome.PROVIDER_FAILURE
    assert result.failure_reason is OngekiLiveFailureReason.STALE_IMAGE
    assert result.collection is not None
    assert result.collection.stale_image_rejected is True
    assert result.diagnostic is None
    assert old_image.read_bytes() == PNG_BYTES


@pytest.mark.parametrize(
    ("event_type", "reason"),
    [
        ("user_not_found", OngekiLiveFailureReason.USER_NOT_FOUND),
        ("private_mode", OngekiLiveFailureReason.PRIVATE_MODE),
        ("premium_missing", OngekiLiveFailureReason.PREMIUM_MISSING),
        ("provider_network_outage", OngekiLiveFailureReason.PROVIDER_NETWORK_OUTAGE),
    ],
)
def test_known_provider_failures_remain_distinct(
    tmp_path: Path,
    event_type: str,
    reason: OngekiLiveFailureReason,
) -> None:
    result = collect_ongeki_events(
        config(tmp_path),
        [OngekiBrowserEvent(event_type, completed=True)],
    )

    assert result.success is False
    assert result.outcome is OngekiLiveOutcome.PROVIDER_FAILURE
    assert result.failure_reason is reason
    assert result.authentication_required is False
    assert result.manual_action_required is False


def test_diagnostics_are_fixed_stage_values_without_page_or_exception_text(
    tmp_path: Path,
) -> None:
    result = collect_ongeki_events(
        config(tmp_path),
        [
            OngekiBrowserEvent.generator_opened(
                "https://reiwa.f5.si/newbestimg/ongeki/?secret=page-text"
            )
        ],
    )

    assert result.success is False
    assert result.diagnostic is not None
    assert result.diagnostic.code is OngekiLiveDiagnosticCode.EVENT_ORDER_INVALID
    assert result.message == "ongeki_live:manual_action_required:generator"
    assert "secret=page-text" not in repr(result)
    assert "Exception" not in repr(result)
