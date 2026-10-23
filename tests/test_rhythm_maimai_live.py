from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from life_inbox.rhythm_collectors import parse_maimai_data
from life_inbox.rhythm_images import RatingImageVerificationError
from life_inbox.rhythm_maimai_live import (
    DEFAULT_RATING_URL,
    MaimaiLiveConfig,
    build_maimai_live_payload,
    decode_png_data_uri,
)

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
FIXTURES = Path(__file__).parent / "fixtures" / "rhythm_collectors"


def _cards() -> list[dict[str, object]]:
    return [
        {
            "frame": "new",
            "title": "New Song",
            "difficulty": "master",
            "chart_type": "DX",
            "level": "14+",
            "achievement": "100.5000%",
            "song_rating": "301",
        },
        {
            "frame": "best",
            "title": "Best Song",
            "difficulty": "remaster",
            "chart_type": "STD",
            "level": "13+AP",
            "achievement": "100.0000%",
            "song_rating": "299",
        },
        {
            "frame": "candidate",
            "title": "Candidate Song",
            "difficulty": "master",
            "chart_type": "DX",
            "level": "13",
            "achievement": "99.5000%",
            "song_rating": "280",
        },
    ]


def test_live_payload_uses_pre_analyzer_rating_and_normalizes_frames() -> None:
    payload = build_maimai_live_payload(
        player_name="Fixture Player",
        rating="13189",
        cards=_cards(),
    )
    # A provider global observed after analyzer injection may contain an
    # inherited rating such as 13110. It is deliberately not an input here.
    snapshot = parse_maimai_data(payload)

    assert snapshot.rating == 13_189
    assert {entry.frame_membership for entry in snapshot.entries} == {
        "new",
        "best",
        "candidate",
    }
    best = next(entry for entry in snapshot.entries if entry.title == "Best Song")
    assert best.full_combo_status == "ap"


def test_current_analyzer_fixture_accepts_decimal_rating_and_skips_unknown_candidates() -> None:
    observed = json.loads(
        (FIXTURES / "maimai_analyzer_capture.json").read_text(encoding="utf-8")
    )

    payload = build_maimai_live_payload(**observed)
    snapshot = parse_maimai_data(payload)

    assert payload["rating"] == "14.32"
    assert snapshot.rating == 14.32
    assert {entry.song_identifier for entry in snapshot.entries} == {
        "analyzer-new-001",
        "analyzer-best-001",
        "analyzer-candidate-001",
    }
    best = next(entry for entry in snapshot.entries if entry.song_identifier == "analyzer-best-001")
    assert best.level == "13+"
    assert best.full_combo_status == "ap"


def test_live_config_requires_exact_official_page_and_hides_secrets(tmp_path: Path) -> None:
    config = MaimaiLiveConfig(
        session_dir=tmp_path / "session",
        output_path=tmp_path / "rating.png",
        sega_id="private-id",
        sega_password="private-password",
        standard_course_enabled=True,
    )

    assert config.rating_url == DEFAULT_RATING_URL
    assert "private-id" not in repr(config)
    assert "private-password" not in repr(config)

    with pytest.raises(ValueError, match="exact official target page"):
        MaimaiLiveConfig(
            session_dir=tmp_path / "session",
            output_path=tmp_path / "rating.png",
            rating_url="https://maimaidx.jp/maimai-mobile/",
        )


def test_png_data_uri_requires_a_complete_png() -> None:
    uri = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")

    payload, width, height = decode_png_data_uri(uri)

    assert payload == PNG_BYTES
    assert (width, height) == (1, 1)

    with pytest.raises(RatingImageVerificationError):
        decode_png_data_uri("data:image/png;base64,bm90LWEtcG5n")
