"""Authenticated, event-driven maimai NET full-data image collection.

The adapter loads the tool from its official distribution URL at runtime.  It
does not vendor the third-party source, persist credentials, or infer success
from elapsed time.  Success requires all of these observable events:

* the exact authenticated Rating target page is open;
* the official analyzer script exposes its full-data button;
* the expected confirmation opens a new window;
* the generated image is a complete decodable PNG; and
* normalized rating cards are present in the analyzer DOM.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .rhythm_collectors import (
    MaimaiBrowserEvent,
    MaimaiCollectionResult,
    MaimaiCollector,
    MaimaiConfig,
    MaimaiEventType,
    MaimaiFailureReason,
    MaimaiState,
)
from .rhythm_images import RatingImageVerificationError, png_dimensions
from .rhythm_rating import RenderedImageMetadata

OFFICIAL_ANALYZER_SCRIPT_URL = (
    "https://sgimera.github.io/mai_RatingAnalyzer/maidx_tools.js"
)
DEFAULT_RATING_URL = "https://maimaidx.jp/maimai-mobile/home/ratingTargetMusic/"
FULL_DATA_SELECTOR = 'button:has-text("\u5168\u30c7\u30fc\u30bf\u7248")'
FULL_DATA_ACCESSIBLE_NAME = (
    "\u5168\u30c7\u30fc\u30bf\u7248 \u5019\u88dc\u542b\u3081\u3066 \u5168\u90e8\u51fa\u3059"
)
EXPECTED_CONFIRM_PREFIX = (
    "Rating\u5bfe\u8c61\u697d\u66f2\u306e\u30c7\u30fc\u30bf\u3092\u753b\u50cf\u5316\u3057\u3066"
)


@dataclass(frozen=True, slots=True)
class MaimaiLiveConfig:
    """Runtime-only browser inputs; secret values are excluded from repr."""

    session_dir: str | Path
    output_path: str | Path
    rating_url: str = DEFAULT_RATING_URL
    analyzer_script_url: str = OFFICIAL_ANALYZER_SCRIPT_URL
    sega_id: str | None = field(default=None, repr=False)
    sega_password: str | None = field(default=None, repr=False)
    standard_course_enabled: bool = False
    headless: bool = True
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        rating = urlsplit(self.rating_url)
        if (
            rating.scheme != "https"
            or rating.hostname != "maimaidx.jp"
            or rating.path != "/maimai-mobile/home/ratingTargetMusic/"
            or rating.query
            or rating.fragment
        ):
            raise ValueError("maimai rating URL must be the exact official target page")
        if self.analyzer_script_url != OFFICIAL_ANALYZER_SCRIPT_URL:
            raise ValueError("maimai analyzer script must use the official distribution URL")
        if type(self.standard_course_enabled) is not bool or type(self.headless) is not bool:
            raise ValueError("maimai browser flags must be booleans")
        if type(self.timeout_seconds) is not int or not 10 <= self.timeout_seconds <= 300:
            raise ValueError("maimai browser timeout must be between 10 and 300 seconds")
        if bool(self.sega_id) != bool(self.sega_password):
            raise ValueError("SEGA ID and password must be configured together")
        object.__setattr__(self, "session_dir", str(Path(self.session_dir)))
        object.__setattr__(self, "output_path", str(Path(self.output_path)))


@dataclass(frozen=True, slots=True)
class MaimaiLiveResult:
    success: bool
    state: MaimaiState
    collection: MaimaiCollectionResult | None = None
    image: RenderedImageMetadata | None = None
    failure_reason: MaimaiFailureReason | None = None
    authentication_required: bool = False
    manual_action_required: bool = False
    message: str | None = None


class _LiveFailure(RuntimeError):
    def __init__(
        self,
        reason: MaimaiFailureReason,
        *,
        state: MaimaiState | None = None,
        authentication_required: bool = False,
        manual_action_required: bool = False,
        stage: str | None = None,
    ) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.state = state or MaimaiState.DOM_MISMATCH
        self.authentication_required = authentication_required
        self.manual_action_required = manual_action_required
        self.stage = stage


_ANALYZER_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?\Z")
_UNKNOWN_ANALYZER_VALUES = frozenset({"?", "???", "-", "—", "n/a"})


def _normalize_analyzer_number(value: object, *, field_name: str) -> str:
    """Normalize numeric text emitted by the current analyzer DOM."""

    if value is None or isinstance(value, bool):
        raise ValueError(f"{field_name} was not observed")
    text = unicodedata.normalize("NFKC", str(value)).strip()
    text = re.sub(r"^(?:rating|レーティング)\s*[:：]?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", "", text).replace(",", "").replace("，", "")
    if not _ANALYZER_NUMBER_PATTERN.fullmatch(text):
        raise ValueError(f"{field_name} was not numeric")
    return text


def _observed_analyzer_text(value: object) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def _atomic_write_png(path: str | Path, payload: bytes) -> Path:
    """Persist a verified analyzer PNG without exposing a partial output."""

    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return output


def _existing_image_sha256(path: str | Path | None) -> str | None:
    if path is None:
        return None
    try:
        candidate = Path(path)
        if candidate.is_file():
            return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        return None
    return None


def _clean_level(value: object) -> tuple[str | None, str | None]:
    text = _observed_analyzer_text(value)
    if not text:
        return None, None
    text = re.sub(r"^Lv\.?\s*", "", text, flags=re.IGNORECASE)
    if text.upper().endswith("AP"):
        return text[:-2].strip() or None, "AP"
    return text, None


def build_maimai_live_payload(
    *,
    player_name: object,
    rating: object,
    cards: object,
) -> dict[str, object]:
    """Build the narrow parser payload from explicitly observed analyzer DOM."""

    if not isinstance(player_name, str) or not player_name.strip():
        raise ValueError("maimai player name was not observed")
    normalized_player_name = unicodedata.normalize("NFKC", player_name).strip()
    rating_text = _normalize_analyzer_number(
        rating,
        field_name="maimai current rating",
    )
    if not isinstance(cards, list) or not cards:
        raise ValueError("maimai rating cards were not observed")
    frames: dict[str, list[dict[str, object]]] = {
        "new": [],
        "best": [],
        "candidate": [],
    }
    ranks = {name: 0 for name in frames}
    for raw in cards:
        if not isinstance(raw, dict):
            raise ValueError("maimai card is invalid")
        frame = str(raw.get("frame") or "").strip().casefold()
        if frame not in frames:
            continue
        title = _observed_analyzer_text(raw.get("title"))
        achievement = _observed_analyzer_text(raw.get("achievement"))
        song_rating = _observed_analyzer_text(raw.get("song_rating"))
        if frame == "candidate" and (
            not title
            or not achievement
            or song_rating.casefold() in _UNKNOWN_ANALYZER_VALUES
        ):
            # The current analyzer deliberately renders unresolved target
            # rates as ``???``. Candidate rows are optional; retaining them
            # as invalid data would block a complete BEST/new capture.
            continue
        if not title or not achievement or not song_rating:
            raise ValueError("maimai card is incomplete")
        level, full_combo = _clean_level(raw.get("level"))
        ranks[frame] += 1
        row: dict[str, object] = {
            "title": title,
            "difficulty": raw.get("difficulty"),
            "chart_type": raw.get("chart_type"),
            "level": level,
            "achievement": achievement,
            "song_rating": song_rating,
            "rank": ranks[frame],
        }
        song_identifier = _observed_analyzer_text(
            raw.get("song_id") or raw.get("songId") or raw.get("music_id")
        )
        if song_identifier:
            row["song_id"] = song_identifier
        if full_combo is not None:
            row["full_combo"] = full_combo
        frames[frame].append(row)
    if not frames["new"] or not frames["best"]:
        raise ValueError("maimai BEST/new frames were not observed")
    return {
        "player_name": normalized_player_name,
        "rating": rating_text,
        "frames": frames,
    }


def decode_png_data_uri(value: object) -> tuple[bytes, int, int]:
    """Decode and structurally validate one completed analyzer PNG URI."""

    prefix = "data:image/png;base64,"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise RatingImageVerificationError("maimai image is not a PNG data URI")
    try:
        payload = base64.b64decode(value[len(prefix) :], validate=True)
    except (ValueError, TypeError) as exc:
        raise RatingImageVerificationError("maimai image base64 is invalid") from exc
    width, height = png_dimensions(payload, require_complete=True)
    return payload, width, height


class PlaywrightMaimaiLiveCollector:
    """Run one isolated browser collection and return only detached evidence."""

    def __init__(
        self,
        config: MaimaiLiveConfig,
        *,
        last_successful_image: str | Path | None = None,
    ) -> None:
        self.config = config
        self.last_successful_image = last_successful_image

    @property
    def timeout_ms(self) -> int:
        return self.config.timeout_seconds * 1_000

    def _failure(self, exc: _LiveFailure) -> MaimaiLiveResult:
        stage = exc.stage or "provider"
        return MaimaiLiveResult(
            success=False,
            state=exc.state,
            failure_reason=exc.reason,
            authentication_required=exc.authentication_required,
            manual_action_required=exc.manual_action_required,
            message=f"maimai:{exc.reason.value}:{stage}",
        )

    def _authenticate(self, page: Any) -> None:
        page.goto(
            "https://maimaidx.jp/maimai-mobile/",
            wait_until="domcontentloaded",
            timeout=self.timeout_ms,
        )
        if page.locator('iframe[src*="recaptcha"], .g-recaptcha').count():
            raise _LiveFailure(
                MaimaiFailureReason.CAPTCHA,
                state=MaimaiState.CAPTCHA,
                authentication_required=True,
                manual_action_required=True,
            )
        login = page.locator('input[name="segaId"]')
        if login.count():
            if not self.config.sega_id or not self.config.sega_password:
                raise _LiveFailure(
                    MaimaiFailureReason.AUTH_REQUIRED,
                    state=MaimaiState.NOT_LOGGED_IN,
                    authentication_required=True,
                    manual_action_required=True,
                )
            login.fill(self.config.sega_id)
            page.locator('input[name="password"]').fill(self.config.sega_password)
            remember = page.locator('input[name="save_cookie"]')
            if remember.count() == 1:
                remember.set_checked(True, force=True)
            form = login.locator("xpath=ancestor::form[1]")
            with page.expect_navigation(
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            ):
                form.locator(
                    'button[type="submit"],input[type="submit"],input[type="image"]'
                ).first.click()
        if urlsplit(page.url).path == "/maimai-mobile/error/":
            raise _LiveFailure(
                MaimaiFailureReason.AUTH_REQUIRED,
                state=MaimaiState.NOT_LOGGED_IN,
                authentication_required=True,
                manual_action_required=True,
            )
        aime_inputs = page.locator('input[name="idx"]')
        if aime_inputs.count():
            course_required = self.config.standard_course_enabled
            body_text = page.locator("body").inner_text()
            course_available = "\u30b9\u30bf\u30f3\u30c0\u30fc\u30c9\u30b3\u30fc\u30b9" in body_text
            if course_required and not course_available:
                raise _LiveFailure(
                    MaimaiFailureReason.STANDARD_COURSE_MISSING,
                    state=MaimaiState.STANDARD_COURSE_MISSING,
                    manual_action_required=True,
                )
            if aime_inputs.count() != 1:
                raise _LiveFailure(
                    MaimaiFailureReason.MANUAL_ACTION_REQUIRED,
                    state=MaimaiState.AUTH_WAITING,
                    manual_action_required=True,
                )
            form = aime_inputs.first.locator("xpath=ancestor::form[1]")
            with page.expect_navigation(
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            ):
                form.locator(
                    'button[type="submit"],input[type="submit"],input[type="image"]'
                ).first.click()

    @staticmethod
    def _profile(page: Any) -> tuple[str, str]:
        name = page.locator("div.name_block")
        rating = page.locator("div.rating_block")
        if name.count() != 1 or rating.count() != 1:
            raise _LiveFailure(
                MaimaiFailureReason.DOM_MISMATCH,
                state=MaimaiState.DOM_MISMATCH,
            )
        return name.inner_text().strip(), rating.inner_text().strip()

    @staticmethod
    def _cards(page: Any) -> list[dict[str, object]]:
        return page.evaluate(
            """() => {
              const text = (element, selector) => {
                const node = element.querySelector(selector);
                return node ? node.textContent.trim() : null;
              };
              return [...document.querySelectorAll('#capture_big .music_best_waku_back')]
                .map((element) => {
                  const classes = [...element.classList];
                  const achievement = element.querySelector('.music_best_waku_achi');
                  const achievementClasses = achievement ? [...achievement.classList] : [];
                  return {
                    frame: classes.includes('curr') ? 'new'
                      : classes.includes('prev') ? 'best'
                      : classes.includes('target_big') ? 'candidate' : 'other',
                    title: text(element, '.music_best_waku_title_deco'),
                    difficulty: ['basic','advanced','expert','master','remaster']
                      .find((value) => classes.includes(value)) || null,
                    song_id: element.dataset.songId
                      || element.getAttribute('data-song-id') || null,
                    chart_type: achievementClasses.includes('dx') ? 'DX'
                      : achievementClasses.includes('st') ? 'STD' : null,
                    level: text(element, '.music_best_waku_level'),
                    achievement: text(element, '.music_best_waku_achi'),
                    song_rating: text(element, '.music_best_waku_rate'),
                  };
                });
            }"""
        )

    def collect(self, *, collected_at: datetime | None = None) -> MaimaiLiveResult:
        observed_at = collected_at or datetime.now(UTC)
        stage = "playwright_import"
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError:
            return self._failure(
                _LiveFailure(
                    MaimaiFailureReason.PLAYWRIGHT_UNAVAILABLE,
                    state=MaimaiState.DOM_MISMATCH,
                )
            )
        context = None
        try:
            stage = "session_directory"
            session_dir = Path(self.config.session_dir).resolve()
            session_dir.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as playwright:
                stage = "browser_launch"
                context = playwright.chromium.launch_persistent_context(
                    str(session_dir),
                    headless=self.config.headless,
                    accept_downloads=True,
                )
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(self.timeout_ms)
                page.set_default_navigation_timeout(self.timeout_ms)
                stage = "authentication"
                self._authenticate(page)
                stage = "target_page"
                page.goto(
                    self.config.rating_url,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )
                if urlsplit(page.url).path != urlsplit(self.config.rating_url).path:
                    raise _LiveFailure(
                        MaimaiFailureReason.WRONG_PAGE,
                        state=MaimaiState.WRONG_PAGE,
                    )
                # Read the current rating before the analyzer mutates its
                # PlayerBaseData object to an inherited-rating display value.
                stage = "profile"
                player_name, current_rating = self._profile(page)
                # Playwright returns only after the external script's load
                # event. The page-level timeout bounds network protection; it
                # is never used as a success signal.
                stage = "analyzer_load"
                page.add_script_tag(url=self.config.analyzer_script_url)
                stage = "full_data_button"
                full_button = page.get_by_role(
                    "button",
                    name=FULL_DATA_ACCESSIBLE_NAME,
                    exact=True,
                )
                full_button.wait_for(state="visible", timeout=self.timeout_ms)
                stage = "announcement"
                announcement = page.get_by_role("button", name="\u8aad\u3093\u3060 OK", exact=True)
                if announcement.count() == 1 and announcement.is_visible():
                    announcement.click()
                dialog_state: list[str] = []

                def handle_dialog(dialog: Any) -> None:
                    message = " ".join(dialog.message.split())
                    if dialog.type == "confirm" and message.startswith(EXPECTED_CONFIRM_PREFIX):
                        dialog_state.append("expected")
                        dialog.accept()
                    else:
                        dialog_state.append("unexpected")
                        dialog.dismiss()

                page.on("dialog", handle_dialog)
                # The analyzer calls window.open from the click handler. The
                # popup event itself is the success condition; the timeout is
                # only the bounded failure guard.
                stage = "full_data_popup"
                with page.expect_popup(timeout=self.timeout_ms) as popup_info:
                    full_button.click()
                popup = popup_info.value
                # The analyzer creates #capture_big as part of the confirmed
                # full-data action. It does not exist immediately after the
                # script load, so normalized rows are read only after the
                # explicit click/window.open event.
                stage = "rating_cards"
                cards = self._cards(page)
                stage = "normalized_payload"
                payload = build_maimai_live_payload(
                    player_name=player_name,
                    rating=current_rating,
                    cards=cards,
                )
                stage = "generated_image"
                image = popup.locator("img#best_pic_Best")
                image.wait_for(state="attached", timeout=self.timeout_ms)
                popup.wait_for_function(
                    """() => {
                      const image = document.querySelector('img#best_pic_Best');
                      return image && image.complete
                        && image.src.startsWith('data:image/png;base64,')
                        && image.naturalWidth > 0 && image.naturalHeight > 0;
                    }""",
                    timeout=self.timeout_ms,
                )
                if dialog_state != ["expected"]:
                    raise _LiveFailure(
                        MaimaiFailureReason.FULL_DATA_SELECTION_FAILURE,
                        state=MaimaiState.FULL_DATA_SELECTION_FAILURE,
                        manual_action_required=True,
                    )
                stage = "image_decode"
                png, width, height = decode_png_data_uri(image.get_attribute("src"))
                output = Path(self.config.output_path).resolve()
                baseline_path = self.last_successful_image
                if baseline_path is None and output.is_file():
                    baseline_path = output
                candidate_sha256 = hashlib.sha256(png).hexdigest()
                baseline_sha256 = _existing_image_sha256(baseline_path)
                if baseline_sha256 is not None and candidate_sha256 == baseline_sha256:
                    raise _LiveFailure(
                        MaimaiFailureReason.STALE_IMAGE,
                        state=MaimaiState.IMAGE_MISSING,
                        stage="image_compare",
                    )
                stage = "collector_normalization"
                collector = MaimaiCollector(
                    MaimaiConfig(
                        persistent_session_dir=session_dir,
                        bookmarklet_runtime_path=self.config.analyzer_script_url,
                        expected_url=self.config.rating_url,
                        full_data_selector=FULL_DATA_SELECTOR,
                        standard_course_required=True,
                    ),
                    last_successful_image=baseline_path,
                )
                collector.observe(MaimaiBrowserEvent.url_observed(self.config.rating_url))
                collector.observe(
                    MaimaiBrowserEvent(
                        MaimaiEventType.BOOKMARKLET_LOADED,
                        completed=True,
                    )
                )
                collector.observe(
                    MaimaiBrowserEvent(
                        MaimaiEventType.FULL_DATA_SELECTED,
                        completed=True,
                        selector=FULL_DATA_SELECTOR,
                    )
                )
                collector.observe(MaimaiBrowserEvent.rating_data(payload))
                result = collector.observe(MaimaiBrowserEvent.blob_available(png))
                if not result.complete or result.snapshot is None or result.image_sha256 is None:
                    raise _LiveFailure(
                        result.failure_reason or MaimaiFailureReason.IMAGE_MISSING,
                        state=result.state,
                        manual_action_required=result.manual_action_required,
                    )
                stage = "image_write"
                output = _atomic_write_png(output, png)
                result = collector.observe(MaimaiBrowserEvent.image_available(output))
                if not result.ready_for_persistence:
                    raise _LiveFailure(
                        result.failure_reason or MaimaiFailureReason.IMAGE_MISSING,
                        state=result.state,
                        manual_action_required=result.manual_action_required,
                    )
                metadata = RenderedImageMetadata(
                    path=output,
                    sha256=candidate_sha256,
                    width=width,
                    height=height,
                    byte_size=len(png),
                    rendered_at=observed_at,
                )
                return MaimaiLiveResult(
                    success=True,
                    state=MaimaiState.IMAGE_SUCCESS,
                    collection=result,
                    image=metadata,
                )
        except _LiveFailure as exc:
            if exc.stage is None:
                exc.stage = stage
            return self._failure(exc)
        except PlaywrightTimeoutError:
            return self._failure(
                _LiveFailure(
                    MaimaiFailureReason.TIMEOUT,
                    state=MaimaiState.TIMEOUT,
                    manual_action_required=True,
                    stage=stage,
                )
            )
        except RatingImageVerificationError:
            return self._failure(
                _LiveFailure(
                    MaimaiFailureReason.IMAGE_MISSING,
                    state=MaimaiState.IMAGE_MISSING,
                    stage=stage,
                )
            )
        except Exception as exc:
            # Provider/library messages may contain URLs, page text, or form
            # values. Never surface or persist the concrete exception here;
            # the class name and fixed internal stage are safe diagnostics.
            return self._failure(
                _LiveFailure(
                    MaimaiFailureReason.DOM_MISMATCH,
                    state=MaimaiState.DOM_MISMATCH,
                    stage=f"{stage}_{type(exc).__name__.casefold()}",
                )
            )
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass


__all__ = [
    "DEFAULT_RATING_URL",
    "FULL_DATA_ACCESSIBLE_NAME",
    "FULL_DATA_SELECTOR",
    "MaimaiLiveConfig",
    "MaimaiLiveResult",
    "OFFICIAL_ANALYZER_SCRIPT_URL",
    "PlaywrightMaimaiLiveCollector",
    "build_maimai_live_payload",
    "decode_png_data_uri",
]
