"""Copyright-safe CHUNITHM rating HTML and optional PNG rendering."""

from __future__ import annotations

import hashlib
import html
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from life_inbox.rhythm_rating import (
    CollectionReason,
    Game,
    RatingSnapshot,
    RenderedImageMetadata,
    RenderedImageResult,
)

from ..rhythm_images import RatingImageVerificationError, png_dimensions
from .common import redact_text


class RenderFailureCode(StrEnum):
    """Renderer-only failure classifications, kept separate from collection."""

    WRONG_GAME = "wrong_game"
    OUTPUT_PATH_REQUIRED = "output_path_required"
    PLAYWRIGHT_UNAVAILABLE = "playwright_unavailable"
    SCREENSHOT_FAILED = "screenshot_failed"
    SCREENSHOT_NOT_COMPLETED = "screenshot_not_completed"
    OUTPUT_MISSING = "output_missing"
    OUTPUT_NOT_REFRESHED = "output_not_refreshed"
    OUTPUT_NOT_PNG = "output_not_png"
    OUTPUT_PATH_MISMATCH = "output_path_mismatch"
    INVALID_HTML = "invalid_html"


@dataclass(frozen=True, slots=True)
class ScreenshotCompletionEvent:
    """Explicit completion signal emitted by a PNG backend."""

    completed: bool
    output_path: str | Path | None = None
    width: int | None = None
    height: int | None = None
    failure_reason: RenderFailureCode | str | None = None

    def __post_init__(self) -> None:
        if type(self.completed) is not bool:
            raise ValueError("completed must be a boolean")
        if self.output_path is not None:
            object.__setattr__(self, "output_path", str(self.output_path))
        if self.failure_reason is not None:
            try:
                reason = (
                    self.failure_reason
                    if isinstance(self.failure_reason, RenderFailureCode)
                    else RenderFailureCode(str(self.failure_reason).strip().casefold())
                )
            except ValueError as exc:
                raise ValueError("unsupported rendering failure reason") from exc
            object.__setattr__(self, "failure_reason", reason)


class PNGBackend(Protocol):
    """Injected screenshot seam used by the bot-owned renderer."""

    def screenshot(self, html_document: str, output_path: Path) -> ScreenshotCompletionEvent:
        """Write a PNG and return an explicit completion event."""


class PlaywrightPngBackend:
    """Lazy optional Playwright implementation.

    Importing this class or the package never imports Playwright.  The import
    occurs only when a caller explicitly requests a screenshot.
    """

    def __init__(self, *, width: int = 1_800, height: int = 1_200) -> None:
        self.width = width
        self.height = height

    def screenshot(self, html_document: str, output_path: Path) -> ScreenshotCompletionEvent:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return ScreenshotCompletionEvent(
                False,
                failure_reason=RenderFailureCode.PLAYWRIGHT_UNAVAILABLE,
            )
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={"width": self.width, "height": self.height})
                    page.set_content(html_document, wait_until="load")
                    page.screenshot(path=str(output_path), full_page=True, type="png")
                finally:
                    browser.close()
        except Exception:
            return ScreenshotCompletionEvent(
                False,
                failure_reason=RenderFailureCode.SCREENSHOT_FAILED,
            )
        if not output_path.is_file():
            return ScreenshotCompletionEvent(
                False,
                failure_reason=RenderFailureCode.OUTPUT_MISSING,
            )
        return ScreenshotCompletionEvent(True, output_path=output_path)


def _display_text(value: object, secrets: Sequence[str]) -> str:
    return html.escape(redact_text(str(value), secrets), quote=True)


def _display_upper(value: object, secrets: Sequence[str]) -> str:
    return html.escape(redact_text(str(value), secrets).upper(), quote=True)


def _measure_text(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _score_text(value: object) -> str:
    """Format integer-like scores for a compact card without changing data."""

    if isinstance(value, bool):
        return _measure_text(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}"
    return _measure_text(value)


def _numeric(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _delta_text(current: object, previous: object) -> str:
    current_number = _numeric(current)
    previous_number = _numeric(previous)
    if current_number is None or previous_number is None:
        return "—"
    delta = current_number - previous_number
    return f"{delta:+.4f}".rstrip("0").rstrip(".")


def _timestamp_text(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _frame_label(frame: str | None) -> str:
    labels = {
        "best": "BEST",
        "recent": "RECENT",
        "new": "NEW",
        "new_equivalent": "NEW",
        "best_candidate": "BEST CANDIDATE",
        "best_candidate_sss": "BEST CANDIDATE SSS",
        "best_outside": "BEST OUTSIDE",
        "other": "OTHER",
    }
    return labels.get((frame or "other").casefold(), frame or "OTHER")


_FRAME_ORDER = (
    "best",
    "recent",
    "new_equivalent",
    "best_candidate",
    "best_candidate_sss",
    "best_outside",
    "other",
)
_MAIN_FRAME_KEYS = ("best", "recent", "new_equivalent")
_ACCENT_COUNT = 6


def _frame_key(frame: object) -> str:
    key = str(frame or "other").strip().casefold()
    return "new_equivalent" if key == "new" else key


def _group_entries(entries: Sequence[object]) -> dict[str, list[object]]:
    """Group entries once so frame order does not depend on semantic sorting."""

    groups: dict[str, list[object]] = {key: [] for key in _FRAME_ORDER}
    for entry in entries:
        key = _frame_key(getattr(entry, "frame_membership", None))
        groups.setdefault(key, []).append(entry)
    for grouped in groups.values():
        if any(
            type(getattr(entry, "rank", None)) is int
            and getattr(entry, "rank", 0) > 0
            for entry in grouped
        ):
            grouped.sort(
                key=lambda entry: (
                    getattr(entry, "rank", None)
                    if type(getattr(entry, "rank", None)) is int
                    and getattr(entry, "rank", 0) > 0
                    else float("inf")
                )
            )
    return groups


def _accent_index(entry: object) -> int:
    """Return a stable CSS-only accent based on the normalized entry identity."""

    seed = getattr(entry, "song_identifier", None) or getattr(entry, "title", None)
    digest = hashlib.sha256(str(seed or "untitled").encode("utf-8")).digest()
    return digest[0] % _ACCENT_COUNT


def _status_badges(entry: object, secrets: Sequence[str]) -> str:
    statuses = tuple(
        dict.fromkeys(
            str(status)
            for status in (getattr(entry, "completion_statuses", ()) or ())
            if str(status)
        )
    )
    if not statuses:
        return '<span class="status-chip status-muted">No status</span>'
    return "".join(
        '<span class="status-chip">'
        + _display_upper(status, secrets)
        + "</span>"
        for status in statuses
    )


def _stat_markup(label: str, value: object, secrets: Sequence[str]) -> str:
    return (
        '<div class="stat"><span class="stat-label">'
        + label
        + '</span><strong class="stat-value">'
        + _display_text(value, secrets)
        + "</strong></div>"
    )


def _song_card(
    entry: object,
    secrets: Sequence[str],
    *,
    position: int,
    frame_label: str | None = None,
) -> str:
    """Build one fixed-height, data-only card from a normalized RatingEntry."""

    rank_value = getattr(entry, "rank", None)
    rank = rank_value if type(rank_value) is int and rank_value > 0 else position
    title = getattr(entry, "title", None) or getattr(entry, "song_identifier", None) or "Untitled"
    difficulty = getattr(entry, "difficulty", None) or "—"
    level = getattr(entry, "level", None) or "—"
    song_rating = getattr(entry, "song_rating", None)
    if song_rating is None:
        song_rating = getattr(entry, "rating", None)
    frame_markup = ""
    if frame_label is not None:
        frame_markup = (
            '<span class="card-frame">'
            + _display_text(frame_label, secrets)
            + "</span>"
        )
    return (
        f'<article class="song-card accent-{_accent_index(entry)}">'
        '<div class="card-top">'
        f'<span class="card-rank">{_display_text(f"#{rank:02d}", secrets)}</span>'
        f'<span class="card-difficulty">{_display_upper(difficulty, secrets)}</span>'
        + frame_markup
        + "</div>"
        f'<h3>{_display_text(title, secrets)}</h3>'
        '<p class="chart-line"><span class="chart-level-label">LEVEL</span>'
        f'<span>{_display_text(level, secrets)}</span></p>'
        '<div class="card-stats">'
        + _stat_markup("SCORE", _score_text(getattr(entry, "score", None)), secrets)
        + _stat_markup("ACHIEVEMENT", _measure_text(getattr(entry, "achievement", None)), secrets)
        + _stat_markup("CHART", _measure_text(getattr(entry, "chart_constant", None)), secrets)
        + _stat_markup("RATING", _measure_text(song_rating), secrets)
        + '</div><div class="statuses" aria-label="Status">'
        + _status_badges(entry, secrets)
        + "</div></article>"
    )


def _card_grid(
    entries: Sequence[object],
    secrets: Sequence[str],
    *,
    frame_label: str | None = None,
) -> str:
    if not entries:
        label = frame_label or "rating"
        return '<p class="empty">No ' + _display_text(label, secrets) + " entries</p>"
    return '<div class="card-grid">' + "".join(
        _song_card(
            entry,
            secrets,
            position=index,
            frame_label=frame_label,
        )
        for index, entry in enumerate(entries, start=1)
    ) + "</div>"


def _frame_panel(
    key: str,
    title: str,
    entries: Sequence[object],
    secrets: Sequence[str],
) -> str:
    return (
        f'<section class="frame-panel" data-frame="{key}" '
        f'data-entry-count="{len(entries)}">'
        '<header class="panel-head"><div><p class="panel-kicker">RATING FRAME</p><h2>'
        + _display_text(title, secrets)
        + '</h2></div><span class="panel-count">'
        + str(len(entries))
        + " charts</span></header>"
        + _card_grid(entries, secrets, frame_label=None)
        + "</section>"
    )


def _subframe(
    key: str,
    title: str,
    entries: Sequence[object],
    secrets: Sequence[str],
) -> str:
    return (
        f'<section class="subframe" data-frame="{key}" '
        f'data-entry-count="{len(entries)}"><header class="subframe-head"><h3>'
        + _display_text(title, secrets)
        + '</h3><span>'
        + str(len(entries))
        + " charts</span></header>"
        + _card_grid(entries, secrets, frame_label=None)
        + "</section>"
    )


def _candidate_panel(
    groups: dict[str, list[object]],
    secrets: Sequence[str],
) -> str:
    candidate_entries: list[tuple[object, str]] = []
    for key, entries in groups.items():
        if key in _MAIN_FRAME_KEYS:
            continue
        candidate_entries.extend((entry, _frame_label(key)) for entry in entries)
    if not candidate_entries:
        return ""
    cards = "".join(
        _song_card(
            entry,
            secrets,
            position=index,
            frame_label=frame_label,
        )
        for index, (entry, frame_label) in enumerate(candidate_entries, start=1)
    )
    candidate_columns = (
        8 if len(candidate_entries) <= 8 else 12 if len(candidate_entries) <= 36 else 20
    )
    return (
        f'<section class="candidate-panel" data-entry-count="{len(candidate_entries)}">'
        '<header class="panel-head"><div><p class="panel-kicker">ADDITIONAL FRAMES</p>'
        '<h2>CANDIDATES / OTHER</h2></div><span class="panel-count">'
        + str(len(candidate_entries))
        + f' charts</span></header><div class="candidate-grid candidate-grid-{candidate_columns}">'
        + cards
        + "</div></section>"
    )


_RENDER_CSS = """
* { box-sizing: border-box; }
html { background: #07101e; }
body {
  margin: 0;
  min-width: 0;
  background: #07101e;
  color: #f4f7fb;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.canvas {
  width: 100%;
  max-width: 1800px;
  min-height: 100vh;
  margin: 0 auto;
  padding: 30px;
  background:
    radial-gradient(circle at 9% 7%, rgba(54, 229, 195, .22), transparent 27%),
    radial-gradient(circle at 92% 18%, rgba(142, 102, 255, .23), transparent 30%),
    linear-gradient(135deg, #09182a 0%, #102743 48%, #171a3c 100%);
}
.summary {
  padding: 22px 24px 18px;
  border: 1px solid rgba(177, 220, 255, .25);
  border-radius: 20px;
  background: linear-gradient(135deg, rgba(19, 47, 73, .96), rgba(30, 26, 69, .94));
  box-shadow: 0 18px 45px rgba(0, 0, 0, .22);
}
.brand-line, .identity-line, .summary-meta, .panel-head, .subframe-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.brand-line { margin-bottom: 16px; }
.brand { color: #8ef4d3; font-size: 18px; font-weight: 800; letter-spacing: .12em; }
.brand span { color: #a8b8d3; font-size: 10px; font-weight: 600; letter-spacing: .16em; }
.source-name { color: #a8b8d3; font-size: 12px; }
.identity-line { align-items: flex-end; gap: 28px; }
.eyebrow, .panel-kicker, .stat-label, .chart-level-label {
  color: #8fa7c4;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: .14em;
  text-transform: uppercase;
}
.player-block { min-width: 220px; }
.player-block h1 { margin: 5px 0 0; color: #ffffff; font-size: 32px; line-height: 1.08; }
.metrics {
  display: grid;
  flex: 1;
  grid-template-columns: repeat(3, minmax(125px, 1fr));
  gap: 10px;
}
.metric {
  min-width: 0;
  padding: 12px 14px;
  border: 1px solid rgba(188, 220, 255, .14);
  border-radius: 12px;
  background: rgba(6, 19, 37, .46);
}
.metric-primary { border-color: rgba(109, 242, 204, .48); background: rgba(36, 122, 116, .24); }
.metric strong { display: block; margin-top: 4px; color: #ffffff; font-size: 24px; line-height: 1; }
.summary-meta { justify-content: flex-start; margin-top: 17px; color: #afc0d7; font-size: 11px; }
.summary-meta span + span::before { margin: 0 10px; color: #587390; content: "•"; }
.frame-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 18px;
  margin-top: 18px;
}
.frame-panel, .candidate-panel {
  min-width: 0;
  padding: 16px;
  border: 1px solid rgba(173, 219, 244, .22);
  border-radius: 16px;
  background: rgba(7, 20, 38, .66);
  box-shadow: 0 14px 30px rgba(0, 0, 0, .16);
}
.panel-head { align-items: flex-end; margin-bottom: 12px; }
.panel-kicker { margin: 0 0 4px; color: #72e7ca; font-size: 9px; }
.panel-head h2 { margin: 0; color: #f5fbff; font-size: 20px; letter-spacing: .08em; }
.panel-count, .subframe-head span { color: #9db3ca; font-size: 11px; white-space: nowrap; }
.new-recent-panel { background: rgba(13, 22, 53, .72); }
.subframe + .subframe {
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid rgba(173, 219, 244, .15);
}
.subframe-head { align-items: baseline; margin-bottom: 8px; }
.subframe-head h3 { margin: 0; color: #d5e7ff; font-size: 13px; letter-spacing: .12em; }
.card-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }
.song-card {
  position: relative;
  min-width: 0;
  height: 166px;
  overflow: hidden;
  padding: 11px 11px 9px 13px;
  border: 1px solid rgba(166, 201, 227, .22);
  border-radius: 12px;
  background: linear-gradient(145deg, rgba(31, 52, 78, .98), rgba(16, 29, 51, .98));
  box-shadow: 0 5px 12px rgba(0, 0, 0, .18);
}
.song-card::before {
  position: absolute;
  inset: 0 auto 0 0;
  width: 4px;
  background: var(--accent);
  content: "";
}
.accent-0 { --accent: #55e5be; }
.accent-1 { --accent: #6dc7ff; }
.accent-2 { --accent: #c897ff; }
.accent-3 { --accent: #ffcb70; }
.accent-4 { --accent: #ff83bc; }
.accent-5 { --accent: #8f9dff; }
.card-top { display: flex; min-width: 0; align-items: center; gap: 6px; height: 15px; }
.card-rank { color: var(--accent); font-size: 12px; font-weight: 800; }
.card-difficulty, .card-frame {
  min-width: 0;
  overflow: hidden;
  color: #c6d6e9;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: .08em;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.card-frame { margin-left: auto; color: #91a7bf; font-size: 8px; }
.song-card h3 {
  display: -webkit-box;
  height: 2.5em;
  overflow: hidden;
  margin: 6px 0 4px;
  color: #ffffff;
  font-size: 13px;
  line-height: 1.25;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
.chart-line {
  display: flex;
  align-items: baseline;
  gap: 5px;
  height: 15px;
  overflow: hidden;
  margin: 0 0 6px;
  color: #d4e4f5;
  font-size: 11px;
}
.chart-level-label { color: #829ab5; font-size: 8px; }
.card-stats { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 5px 8px; }
.stat { min-width: 0; }
.stat-label { display: block; color: #8099b4; font-size: 7px; letter-spacing: .1em; }
.stat-value {
  display: block;
  overflow: hidden;
  color: #f5fbff;
  font-size: 11px;
  line-height: 1.2;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.statuses {
  display: flex;
  height: 18px;
  min-width: 0;
  gap: 4px;
  align-items: center;
  overflow: hidden;
  margin-top: 7px;
  white-space: nowrap;
}
.status-chip {
  max-width: 100%;
  overflow: hidden;
  padding: 2px 5px;
  border: 1px solid rgba(113, 229, 199, .3);
  border-radius: 4px;
  color: #b9f6e7;
  font-size: 8px;
  text-overflow: ellipsis;
}
.status-muted { border-color: rgba(166, 190, 215, .2); color: #829ab5; }
.empty { margin: 0; padding: 24px 4px; color: #a8bad0; font-size: 12px; }
.candidate-panel { margin-top: 18px; }
.candidate-grid { display: grid; grid-template-columns: repeat(8, minmax(0, 1fr)); gap: 8px; }
.candidate-grid-12 { grid-template-columns: repeat(12, minmax(0, 1fr)); }
.candidate-grid-20 { grid-template-columns: repeat(20, minmax(0, 1fr)); }
.candidate-grid .song-card { height: 88px; padding: 5px 6px 4px 8px; border-radius: 9px; }
.candidate-grid .card-top { height: 11px; gap: 3px; }
.candidate-grid .card-rank { font-size: 9px; }
.candidate-grid .card-difficulty, .candidate-grid .card-frame { font-size: 6px; }
.candidate-grid .song-card h3 {
  height: 1.3em;
  margin: 2px 0 2px;
  font-size: 9px;
  line-height: 1.3;
  -webkit-line-clamp: 1;
}
.candidate-grid .chart-line { height: 9px; margin-bottom: 2px; font-size: 7px; }
.candidate-grid .chart-level-label { font-size: 5px; }
.candidate-grid .card-stats { gap: 1px 3px; }
.candidate-grid .stat-label { font-size: 5px; }
.candidate-grid .stat-value { font-size: 7px; line-height: 1; }
.candidate-grid .statuses { height: 10px; margin-top: 2px; }
.candidate-grid .status-chip { padding: 1px 2px; font-size: 5px; }
.footer { margin-top: 16px; color: #9bb0c8; font-size: 11px; }
@media (max-width: 1200px) {
  .canvas { padding: 20px; }
  .identity-line { align-items: stretch; flex-direction: column; gap: 14px; }
  .player-block { min-width: 0; }
  .candidate-grid { grid-template-columns: repeat(8, minmax(0, 1fr)); }
}
@media (max-width: 760px) {
  .frame-layout { grid-template-columns: minmax(0, 1fr); }
  .card-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .candidate-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
}
@media (max-width: 480px) {
  .canvas { padding: 12px; }
  .summary { padding: 18px 16px 15px; }
  .brand-line, .summary-meta { align-items: flex-start; flex-direction: column; gap: 6px; }
  .summary-meta span + span::before { display: none; }
  .metrics { grid-template-columns: 1fr; }
  .card-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .candidate-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
"""


class CHUNITHMRenderer:
    """Render normalized CHUNITHM data as safe inline HTML and PNG metadata."""

    def __init__(
        self,
        backend: PNGBackend | None = None,
        *,
        previous_snapshot: RatingSnapshot | None = None,
        secrets: Sequence[str] = (),
    ) -> None:
        self.backend = backend if backend is not None else PlaywrightPngBackend()
        self.previous_snapshot = previous_snapshot
        self.secrets = tuple(secret for secret in secrets if secret)

    def build_html(
        self,
        snapshot: RatingSnapshot,
        *,
        previous_snapshot: RatingSnapshot | None = None,
        previous_rating: object | None = None,
    ) -> str:
        """Create a bounded landscape card board with all provider text escaped."""

        current_previous = previous_snapshot or self.previous_snapshot
        if previous_rating is None and current_previous is not None:
            previous_rating = current_previous.rating
        groups = _group_entries(snapshot.entries)
        best_entries = groups["best"]
        recent_entries = groups["recent"]
        new_entries = groups["new_equivalent"]
        main_sections = (
            '<div class="frame-layout">'
            + _frame_panel("best", "BEST", best_entries, self.secrets)
            + '<section class="frame-panel new-recent-panel" data-frame="new_recent" '
            f'data-entry-count="{len(recent_entries) + len(new_entries)}">'
            '<header class="panel-head"><div><p class="panel-kicker">RATING FRAMES</p>'
            '<h2>NEW / RECENT</h2></div><span class="panel-count">'
            + str(len(recent_entries) + len(new_entries))
            + " charts</span></header>"
            + _subframe("recent", "RECENT", recent_entries, self.secrets)
            + _subframe("new_equivalent", "NEW", new_entries, self.secrets)
            + "</section></div>"
        )
        current = _measure_text(snapshot.rating)
        previous = _measure_text(previous_rating)
        delta = _delta_text(snapshot.rating, previous_rating)
        return (
            "<!doctype html>\n"
            '<html lang="ja"><head><meta charset="utf-8">'
            "<title>CHUNITHM rating</title>"
            "<style>"
            + _RENDER_CSS
            + "</style></head><body><main class=\"canvas\">"
            '<header class="summary">'
            '<div class="brand-line"><div class="brand">CHUNITHM '
            '<span>RATING BOARD</span></div><span class="source-name">Source · '
            + _display_text(snapshot.source, self.secrets)
            + "</span></div>"
            '<div class="identity-line"><div class="player-block"><p class="eyebrow">PLAYER</p><h1>'
            + _display_text(snapshot.player_name or "Player", self.secrets)
            + '</h1></div><div class="metrics">'
            '<div class="metric metric-primary"><span class="eyebrow">Current rating</span><strong>'
            f'{_display_text(current, self.secrets)}</strong></div>'
            '<div class="metric"><span class="eyebrow">Previous rating</span><strong>'
            f'{_display_text(previous, self.secrets)}</strong></div>'
            '<div class="metric"><span class="eyebrow">Delta</span><strong>'
            f'{_display_text(delta, self.secrets)}</strong></div>'
            '</div></div><div class="summary-meta"><span>Collected · '
            + _display_text(_timestamp_text(snapshot.collected_at), self.secrets)
            + '</span><span>Source updated · '
            + _display_text(_timestamp_text(snapshot.source_updated_at), self.secrets)
            + "</span></div></header>"
            + main_sections
            + _candidate_panel(groups, self.secrets)
            + '<footer class="footer">Copyright-safe data view · no jacket artwork or '
            'external images</footer>'
            "</main></body></html>"
        )

    html = build_html
    render_html = build_html

    def _failure(self, code: RenderFailureCode) -> RenderedImageResult:
        # The domain result has a provider-independent reason field.  The
        # explicit ``rendering_failure:`` prefix keeps this classification
        # distinct from a collection failure without changing the domain API.
        return RenderedImageResult(
            success=False,
            failure_reason=CollectionReason.INVALID_RESPONSE,
            message=f"rendering_failure:{code.value}",
        )

    def render(
        self,
        snapshot: RatingSnapshot,
        output_path: str | Path | None = None,
        *,
        previous_snapshot: RatingSnapshot | None = None,
        previous_rating: object | None = None,
    ) -> RenderedImageResult:
        """Render and verify a caller-selected PNG path.

        A successful result is emitted only after the backend completion event
        and a real PNG file are both observed.
        """

        if snapshot.game is not Game.CHUNITHM:
            return self._failure(RenderFailureCode.WRONG_GAME)
        if output_path is None:
            return self._failure(RenderFailureCode.OUTPUT_PATH_REQUIRED)
        try:
            target = Path(output_path)
            target.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, TypeError, ValueError):
            return self._failure(RenderFailureCode.OUTPUT_PATH_REQUIRED)
        prior_stat = None
        prior_sha256 = None
        try:
            if target.is_file():
                prior_stat = target.stat()
                prior_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError:
            return self._failure(RenderFailureCode.OUTPUT_PATH_REQUIRED)
        document = self.build_html(
            snapshot,
            previous_snapshot=previous_snapshot,
            previous_rating=previous_rating,
        )
        try:
            screenshot = getattr(self.backend, "screenshot", None)
            if not callable(screenshot):
                screenshot = getattr(self.backend, "render", None)
            if not callable(screenshot):
                return self._failure(RenderFailureCode.SCREENSHOT_NOT_COMPLETED)
            event = screenshot(document, target)
        except Exception:
            return self._failure(RenderFailureCode.SCREENSHOT_FAILED)
        if not isinstance(event, ScreenshotCompletionEvent):
            return self._failure(RenderFailureCode.SCREENSHOT_NOT_COMPLETED)
        if not event.completed:
            return self._failure(event.failure_reason or RenderFailureCode.SCREENSHOT_NOT_COMPLETED)
        if event.output_path is not None and Path(event.output_path) != target:
            return self._failure(RenderFailureCode.OUTPUT_PATH_MISMATCH)
        try:
            if not target.is_file():
                return self._failure(RenderFailureCode.OUTPUT_MISSING)
            payload = target.read_bytes()
            if prior_stat is not None and prior_sha256 is not None:
                current_stat = target.stat()
                current_sha256 = hashlib.sha256(payload).hexdigest()
                if (
                    current_stat.st_ino == prior_stat.st_ino
                    and current_stat.st_size == prior_stat.st_size
                    and current_stat.st_mtime_ns == prior_stat.st_mtime_ns
                    and current_stat.st_ctime_ns == prior_stat.st_ctime_ns
                    and current_sha256 == prior_sha256
                ):
                    # A completion event cannot promote a file that was
                    # already present and was not refreshed by this render.
                    return self._failure(RenderFailureCode.OUTPUT_NOT_REFRESHED)
        except OSError:
            return self._failure(RenderFailureCode.OUTPUT_MISSING)
        try:
            # A legacy injected fixture may provide only a PNG signature, but
            # a real backend without explicit dimensions must prove IHDR/IEND
            # structure.  If a backend supplies dimensions, retain the small
            # fixture seam while still checking a complete file whenever one
            # is available.
            width, height = png_dimensions(
                payload,
                require_complete=event.width is None or event.height is None,
            ) or (None, None)
        except RatingImageVerificationError:
            return self._failure(RenderFailureCode.OUTPUT_NOT_PNG)
        if width is None or height is None:
            if event.width is None or event.height is None:
                return self._failure(RenderFailureCode.OUTPUT_NOT_PNG)
            width, height = event.width, event.height
        elif event.width is not None and event.width != width:
            return self._failure(RenderFailureCode.OUTPUT_NOT_PNG)
        elif event.height is not None and event.height != height:
            return self._failure(RenderFailureCode.OUTPUT_NOT_PNG)
        if type(width) is not int or type(height) is not int or width < 1 or height < 1:
            return self._failure(RenderFailureCode.OUTPUT_NOT_PNG)
        metadata = RenderedImageMetadata(
            path=target,
            sha256=hashlib.sha256(payload).hexdigest(),
            mime_type="image/png",
            width=width,
            height=height,
            byte_size=len(payload),
        )
        return RenderedImageResult(success=True, metadata=metadata)


CHUNITHMHtmlRenderer = CHUNITHMRenderer
CHUNITHMImageRenderer = CHUNITHMRenderer
ChunithmRenderer = CHUNITHMRenderer


__all__ = [
    "CHUNITHMHtmlRenderer",
    "CHUNITHMImageRenderer",
    "CHUNITHMRenderer",
    "ChunithmRenderer",
    "PNGBackend",
    "PlaywrightPngBackend",
    "RenderFailureCode",
    "ScreenshotCompletionEvent",
]
