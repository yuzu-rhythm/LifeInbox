"""Runtime orchestration for durable rhythm collection runs.

Only CHUNIREC currently crosses the complete structured-data -> renderer ->
publisher path.  The maimai and ONGEKI browser collectors remain explicit
manual-action gates until their caller-owned bookmarklet/DOM configuration can
produce both a verified image event and normalized rating data.  They are never
reported as successful merely because a page was open for a fixed duration.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from .config import Settings
from .rhythm_chunirec_image_live import (
    CHUNIRECImageConfig,
    PlaywrightChunirecImageCollector,
)
from .rhythm_collectors import (
    CHUNIRECClient,
    CHUNIRECCollector,
    CHUNIRECConfig,
    CHUNIRECResponse,
    CHUNITHMRenderer,
    PNGBackend,
    sanitize_for_output,
)
from .rhythm_discord import RhythmDiscordPublisher
from .rhythm_maimai_live import (
    MaimaiLiveConfig,
    PlaywrightMaimaiLiveCollector,
)
from .rhythm_ongeki_live import (
    OngekiLiveConfig,
    OngekiLiveFailureReason,
    PlaywrightOngekiLiveCollector,
)
from .rhythm_persistence import CollectionRunRecord, RhythmPersistence, utc_naive
from .rhythm_rating import RatingEntry, RatingSnapshot, RenderedImageMetadata


class _RejectRedirects(HTTPRedirectHandler):
    """Keep the official query token on the exact configured HTTPS origin."""

    def redirect_request(self, *args: object, **kwargs: object):
        return None


class UrllibCHUNIRECTransport:
    """Bounded stdlib HTTP transport whose exceptions never expose its URL."""

    def __init__(self, *, timeout_seconds: float = 20.0, max_bytes: int = 5_000_000) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self._opener = build_opener(_RejectRedirects())

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str],
    ) -> CHUNIRECResponse:
        if method != "GET":
            raise ValueError("CHUNIREC transport supports GET only")
        query = urlencode(dict(params))
        request = Request(
            f"{url}?{query}",
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "life-inbox/0.1"},
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                payload = response.read(self.max_bytes + 1)
                if len(payload) > self.max_bytes:
                    raise ValueError("CHUNIREC response exceeded the configured size limit")
                return CHUNIRECResponse(
                    status_code=int(response.status),
                    body=payload,
                    headers=dict(response.headers.items()),
                )
        except HTTPError as exc:
            payload = exc.read(self.max_bytes + 1)
            if len(payload) > self.max_bytes:
                payload = b""
            return CHUNIRECResponse(
                status_code=int(exc.code),
                body=payload,
                headers=dict(exc.headers.items()) if exc.headers is not None else {},
            )


def _decimal(value: object, *, required: bool = False) -> Decimal | None:
    if value is None or isinstance(value, bool):
        if required:
            raise ValueError("rating is required")
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("rating measure is invalid") from exc
    if not result.is_finite():
        raise ValueError("rating measure must be finite")
    return result


def _integer(value: object) -> int | None:
    number = _decimal(value)
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def _entry_row(entry: RatingEntry) -> dict[str, Any]:
    return {
        "frame_type": entry.frame_membership or "other",
        "rank": entry.rank,
        "song_id": entry.song_identifier,
        "normalized_title": entry.normalized_title or entry.title or "untitled",
        "difficulty": entry.difficulty,
        "chart_type": entry.chart_type,
        "level": entry.level,
        "chart_constant": _decimal(entry.chart_constant),
        "score": _integer(entry.score),
        "achievement": _decimal(entry.achievement),
        "technical_score": _integer(entry.technical_score),
        "p_score": _integer(entry.p_score),
        "song_rating": _decimal(
            entry.song_rating if entry.song_rating is not None else entry.rating
        ),
        "clear_status": entry.clear_status,
        "full_combo_status": entry.full_combo_status,
        "all_justice_status": entry.all_justice_status,
        "sync_status": entry.sync_status,
        "full_bell_status": entry.full_bell_status,
        "all_break_status": entry.all_break_status,
        "genre": entry.genre,
        "source_updated_at": (
            utc_naive(entry.source_updated_at)
            if isinstance(entry.source_updated_at, datetime)
            else None
        ),
    }


class RhythmCollectionExecutor:
    """Execute one leased durable run using explicit provider evidence."""

    def __init__(
        self,
        *,
        settings: Settings,
        persistence: RhythmPersistence,
        publisher: RhythmDiscordPublisher,
        chunirec_transport: object | None = None,
        chunirec_image_collector_factory: Callable[[CHUNIRECImageConfig], object]
        | None = None,
        png_backend: PNGBackend | None = None,
        maimai_live_collector_factory: Callable[[MaimaiLiveConfig], object] | None = None,
        ongeki_live_collector_factory: Callable[[OngekiLiveConfig], object] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.persistence = persistence
        self.publisher = publisher
        self.chunirec_transport = chunirec_transport or UrllibCHUNIRECTransport()
        self.chunirec_image_collector_factory = (
            chunirec_image_collector_factory or PlaywrightChunirecImageCollector
        )
        self.png_backend = png_backend
        self.maimai_live_collector_factory = (
            maimai_live_collector_factory or PlaywrightMaimaiLiveCollector
        )
        self.ongeki_live_collector_factory = (
            ongeki_live_collector_factory or PlaywrightOngekiLiveCollector
        )
        self.now = now or (lambda: datetime.now(UTC))

    def _transition(
        self,
        run_id: str,
        state: str,
        *,
        event: str,
        message: str,
        **kwargs: Any,
    ) -> CollectionRunRecord:
        return self.persistence.transition_run(
            run_id=run_id,
            to_state=state,
            event_type=event,
            message=message,
            now=self.now(),
            **kwargs,
        )

    async def execute(self, run: CollectionRunRecord) -> None:
        manual_action_confirmed = run.state == "waitingForManualAction"
        if run.state == "queued":
            run = self._transition(
                run.id,
                "openingProvider",
                event="providerOpening",
                message=f"{run.game}の取得元を確認しています",
            )
        elif run.state == "waitingForManualAction":
            run = self._transition(
                run.id,
                "refreshingSource",
                event="manualActionConfirmed",
                message="管理者の明示確認を受け、取得元の更新確認を再開しました",
                explicit_confirmation=True,
            )
        else:
            raise ValueError("collection executor received a non-processable run")

        if run.game == "chunithm":
            await self._execute_chunithm(run)
        elif run.game == "maimai":
            await self._execute_maimai(run)
        elif run.game == "ongeki":
            await self._execute_ongeki(
                run,
                score_log_confirmed=manual_action_confirmed,
            )
        else:
            await self._fail(run, "invalid_game", "対応していない機種です")

    async def _gate_browser_provider(self, run: CollectionRunRecord) -> None:
        configured = (
            bool(
                self.settings.maimai_net_rating_url
                and self.settings.maimai_analyzer_bookmarklet_path
                and self.settings.maimai_full_data_selector
                and self.settings.maimai_standard_course_enabled
            )
            if run.game == "maimai"
            else bool(
                self.settings.ongeki_official_net_url
                and self.settings.ongeki_score_log_user_id
                and self.settings.ongeki_image_generator_url
                and self.settings.ongeki_premium_enabled
            )
        )
        if not configured:
            await self._fail(
                run,
                "provider_not_configured",
                f"{run.game}の実サイト取得設定が不足しています",
            )
            return
        if run.state == "openingProvider":
            authenticated = self._transition(
                run.id,
                "authenticationRequired",
                event="authenticationRequired",
                message=f"{run.game}の永続ブラウザーセッションを管理者が確認してください",
            )
            self._transition(
                authenticated.id,
                "waitingForManualAction",
                event="manualActionRequired",
                message=(
                    f"{run.game}のログイン・CAPTCHA・規約・対象ページを"
                    "ブラウザーで確認してください"
                ),
                manual_action=f"authenticate:{run.game}",
            )
            self.persistence.update_provider_status(
                user_id=run.user_id,
                game=run.game,
                provider="official_net_browser",
                session_status="manual_authentication_required",
                error_code="authentication_required",
                safe_error="管理者の明示的なブラウザー確認が必要です",
            )
            return

        # A manual acknowledgement is not evidence that image generation and
        # normalized extraction succeeded. Keep this path explicitly failed
        # until a runtime adapter emits those concrete events.
        collecting = self._transition(
            run.id,
            "collectingData",
            event="browserCollectionStarted",
            message=f"{run.game}の構造化データを確認しています",
        )
        await self._fail(
            collecting,
            "normalized_data_not_observed",
            "画像と正規化レートデータの完了イベントを確認できませんでした",
        )

    async def _execute_chunithm(self, run: CollectionRunRecord) -> None:
        if (
            run.state == "openingProvider"
            and self.settings.chunirec_require_manual_refresh_confirmation
        ):
            self._transition(
                run.id,
                "waitingForManualAction",
                event="chunirecRefreshConfirmationRequired",
                message=(
                    "CHUNIRECへの更新完了を確認後、API取得へ進む操作を"
                    "明示的に実行してください"
                ),
                manual_action="confirmChunirecRefresh",
            )
            self.persistence.update_provider_status(
                user_id=run.user_id,
                game=run.game,
                provider="chunirec_api_v2",
                session_status="manual_refresh_confirmation_required",
                error_code="manual_confirmation_required",
                safe_error="CHUNIREC更新完了の明示確認待ちです",
            )
            return
        if run.state == "openingProvider":
            run = self._transition(
                run.id,
                "refreshingSource",
                event="sourceRefreshNotRequired",
                message="手動更新確認を無効化した設定でAPI取得へ進みます",
            )
        collecting = self._transition(
            run.id,
            "collectingData",
            event="chunirecApiRequestStarted",
            message="CHUNIREC API v2.0からレートデータを取得しています",
        )
        token = (
            self.settings.chunirec_api_token.get_secret_value()
            if self.settings.chunirec_api_token is not None
            else None
        )
        config = CHUNIRECConfig(
            token=token,
            region=self.settings.chunirec_region,
            user_id=self.settings.chunirec_user_id,
            user_name=self.settings.chunirec_user_name,
        )
        client = CHUNIRECClient(self.chunirec_transport, config)
        collector = CHUNIRECCollector(client, require_refresh_confirmation=False)
        result = await asyncio.to_thread(collector.collect, collected_at=self.now())
        if not result.success or result.snapshot is None:
            code = result.failure_reason.value if result.failure_reason is not None else "failed"
            await self._fail(
                collecting,
                f"chunirec_{code}",
                "CHUNIREC APIから有効なレートデータを取得できませんでした",
            )
            return
        provided_image = None
        renderer_version = None
        if self.settings.chunirec_user_name:
            collected_utc = result.snapshot.collected_at.astimezone(UTC)
            generated_path = (
                self.settings.rhythm_image_dir.resolve()
                / run.game
                / collected_utc.strftime("%Y/%m")
                / f"{run.id}-chunirec.png"
            )
            image_config = CHUNIRECImageConfig(
                user_name=self.settings.chunirec_user_name,
                output_path=generated_path,
                expected_player_name=result.snapshot.player_name,
                expected_rating=str(result.snapshot.rating),
                headless=self.settings.rhythm_browser_headless,
                timeout_seconds=self.settings.rhythm_browser_timeout_seconds,
            )
            image_collector = self.chunirec_image_collector_factory(image_config)
            image_result = await asyncio.to_thread(
                image_collector.collect,
                collected_at=result.snapshot.collected_at,
            )
            if image_result.success and image_result.image is not None:
                provided_image = image_result.image
                renderer_version = "chunirec-public-generator-v1"
            else:
                previous = self.persistence.latest_successful_snapshot(
                    user_id=run.user_id,
                    game=run.game,
                )
                previous_image = (
                    previous.image_links[0].image
                    if previous is not None and previous.image_links
                    else None
                )
                if (
                    previous is not None
                    and previous.semantic_fingerprint == result.snapshot.semantic_fingerprint
                    and previous_image is not None
                    and previous_image.renderer_version
                    == "chunirec-public-generator-v1"
                ):
                    renderer_version = "chunirec-public-generator-v1"
        await self._persist_render_publish(
            collecting,
            result.snapshot,
            token=token,
            provided_image=provided_image,
            renderer_version=renderer_version,
        )

    async def _execute_maimai(self, run: CollectionRunRecord) -> None:
        if not (
            self.settings.maimai_net_rating_url
            and self.settings.maimai_analyzer_script_url
            and self.settings.maimai_standard_course_enabled
        ):
            await self._fail(
                run,
                "provider_not_configured",
                "maimaiの実サイト取得設定が不足しています",
            )
            return

        sega_id = (
            self.settings.sega_id.get_secret_value()
            if self.settings.sega_id is not None
            else None
        )
        sega_password = (
            self.settings.sega_password.get_secret_value()
            if self.settings.sega_password is not None
            else None
        )
        session_dir = self.settings.rhythm_browser_session_dir.resolve() / "maimai"
        if not sega_id and not session_dir.exists():
            authenticated = self._transition(
                run.id,
                "authenticationRequired",
                event="authenticationRequired",
                message="maimaiの専用ブラウザーセッションでログインが必要です",
            )
            self._transition(
                authenticated.id,
                "waitingForManualAction",
                event="manualActionRequired",
                message="maimai NETのログイン・CAPTCHA・追加認証を完了してください",
                manual_action="authenticate:maimai",
            )
            self.persistence.update_provider_status(
                user_id=run.user_id,
                game=run.game,
                provider="official_net_browser",
                session_status="manual_authentication_required",
                error_code="authentication_required",
                safe_error="管理者の明示的なブラウザー認証が必要です",
            )
            return

        if run.state == "openingProvider":
            run = self._transition(
                run.id,
                "refreshingSource",
                event="browserSessionOpening",
                message="maimai専用の永続ブラウザーセッションを開いています",
            )
        collecting = self._transition(
            run.id,
            "collectingData",
            event="browserCollectionStarted",
            message="maimai NETで全データ版のレート画像と正規化データを取得しています",
        )
        collected_at = self.now()
        collected_utc = collected_at.astimezone(UTC)
        output_path = (
            self.settings.rhythm_image_dir.resolve()
            / "maimai"
            / collected_utc.strftime("%Y/%m")
            / f"{run.id}.png"
        )
        config = MaimaiLiveConfig(
            session_dir=session_dir,
            output_path=output_path,
            rating_url=self.settings.maimai_net_rating_url,
            analyzer_script_url=self.settings.maimai_analyzer_script_url,
            sega_id=sega_id,
            sega_password=sega_password,
            standard_course_enabled=self.settings.maimai_standard_course_enabled,
            headless=self.settings.rhythm_browser_headless,
            timeout_seconds=self.settings.rhythm_browser_timeout_seconds,
        )
        collector = self.maimai_live_collector_factory(config)
        result = await asyncio.to_thread(collector.collect, collected_at=collected_at)
        if not result.success:
            code = (
                result.failure_reason.value
                if result.failure_reason is not None
                else "failed"
            )
            diagnostic = result.message or f"maimai:{code}:provider"
            if result.authentication_required:
                authenticated = self._transition(
                    collecting.id,
                    "authenticationRequired",
                    event="authenticationRequired",
                    message="maimai NETの認証が必要です",
                )
                self._transition(
                    authenticated.id,
                    "waitingForManualAction",
                    event="manualActionRequired",
                    message="maimai NETのログイン・CAPTCHA・追加認証を完了してください",
                    manual_action="authenticate:maimai",
                )
                self.persistence.update_provider_status(
                    user_id=run.user_id,
                    game=run.game,
                    provider="official_net_browser",
                    session_status="manual_authentication_required",
                    error_code=code,
                    safe_error="maimai NETの管理者認証が必要です",
                )
                return
            if result.manual_action_required:
                self._transition(
                    collecting.id,
                    "waitingForManualAction",
                    event="manualActionRequired",
                    message=(
                        "maimaiの取得画面で管理者の明示確認が必要です "
                        f"[{diagnostic}]"
                    ),
                    manual_action="inspect:maimai",
                )
                self.persistence.update_provider_status(
                    user_id=run.user_id,
                    game=run.game,
                    provider="official_net_browser",
                    session_status="manual_action_required",
                    error_code=code,
                    safe_error=diagnostic,
                )
                return
            await self._fail(
                collecting,
                f"maimai_{code}",
                "maimaiの全データ版画像と正規化レートデータを取得できませんでした",
            )
            return
        if (
            result.collection is None
            or result.collection.snapshot is None
            or result.image is None
        ):
            await self._fail(
                collecting,
                "maimai_incomplete_result",
                "maimaiの画像または正規化データの完了イベントが不足しています",
            )
            return
        await self._persist_render_publish(
            collecting,
            result.collection.snapshot,
            token=None,
            provided_image=result.image,
            renderer_version="maimai-analyzer-v1",
        )

    async def _execute_ongeki(
        self,
        run: CollectionRunRecord,
        *,
        score_log_confirmed: bool,
    ) -> None:
        if not (
            self.settings.ongeki_official_net_url
            and self.settings.ongeki_score_log_user_id
            and self.settings.ongeki_image_generator_url
            and self.settings.ongeki_premium_enabled
        ):
            await self._fail(
                run,
                "provider_not_configured",
                "オンゲキの実サイト取得設定が不足しています",
            )
            return

        session_dir = self.settings.rhythm_browser_session_dir.resolve() / "ongeki"
        if run.state == "openingProvider":
            run = self._transition(
                run.id,
                "refreshingSource",
                event="browserSessionOpening",
                message=(
                    "オンゲキ専用セッションで公式NETから"
                    "OngekiScoreLogへの更新を開始します"
                ),
            )
        collecting = self._transition(
            run.id,
            "collectingData",
            event="browserCollectionStarted",
            message=(
                "公式オンゲキNET→OngekiScoreLog更新→画像生成の順で"
                "明示的な完了イベントを確認しています"
            ),
        )
        collected_at = self.now()
        collected_utc = collected_at.astimezone(UTC)
        output_path = (
            self.settings.rhythm_image_dir.resolve()
            / "ongeki"
            / collected_utc.strftime("%Y/%m")
            / f"{run.id}.jpg"
        )
        config = OngekiLiveConfig(
            session_dir=session_dir,
            output_path=output_path,
            user_id=self.settings.ongeki_score_log_user_id,
            mode=self.settings.ongeki_image_mode,
            copyright_mode=self.settings.ongeki_copyright_mode,
            score_log_bookmarklet_path=self.settings.ongeki_score_log_bookmarklet_path,
            official_url=self.settings.ongeki_official_net_url,
            generator_url=self.settings.ongeki_image_generator_url,
            headless=self.settings.rhythm_browser_headless,
            timeout_seconds=self.settings.rhythm_browser_timeout_seconds,
            premium_required=self.settings.ongeki_premium_enabled,
        )
        collector = self.ongeki_live_collector_factory(config)
        result = await asyncio.to_thread(
            collector.collect,
            collected_at=collected_at,
            score_log_confirmed=score_log_confirmed,
        )
        if not result.success:
            code = (
                result.failure_reason.value
                if result.failure_reason is not None
                else "failed"
            )
            diagnostic = result.message or f"ongeki:{code}:provider"
            if result.authentication_required:
                authenticated = self._transition(
                    collecting.id,
                    "authenticationRequired",
                    event="authenticationRequired",
                    message="公式オンゲキNETの認証が必要です",
                )
                self._transition(
                    authenticated.id,
                    "waitingForManualAction",
                    event="manualActionRequired",
                    message="公式オンゲキNETのログイン・追加認証を完了してください",
                    manual_action="authenticate:ongeki",
                )
                self.persistence.update_provider_status(
                    user_id=run.user_id,
                    game=run.game,
                    provider="official_net_scorelog_generator",
                    session_status="manual_authentication_required",
                    error_code=code,
                    safe_error="公式オンゲキNETの管理者認証が必要です",
                )
                return
            if result.manual_action_required:
                confirmation_required = (
                    result.failure_reason
                    is OngekiLiveFailureReason.MANUAL_CONFIRMATION_REQUIRED
                )
                action = (
                    "confirm:ongeki_scorelog_refresh"
                    if confirmation_required
                    else "inspect:ongeki"
                )
                self._transition(
                    collecting.id,
                    "waitingForManualAction",
                    event="manualActionRequired",
                    message=(
                        "OngekiScoreLog更新後の明示確認が必要です "
                        f"[{diagnostic}]"
                        if confirmation_required
                        else "オンゲキ取得設定または画面の管理者確認が必要です "
                        f"[{diagnostic}]"
                    ),
                    manual_action=action,
                )
                self.persistence.update_provider_status(
                    user_id=run.user_id,
                    game=run.game,
                    provider="official_net_scorelog_generator",
                    session_status="manual_action_required",
                    error_code=code,
                    safe_error=diagnostic,
                )
                return
            await self._fail(
                collecting,
                f"ongeki_{code}",
                "オンゲキの正規化レートデータと生成画像を取得できませんでした",
            )
            return
        if (
            result.collection is None
            or result.collection.snapshot is None
            or result.image is None
        ):
            await self._fail(
                collecting,
                "ongeki_incomplete_result",
                "オンゲキの画像または正規化データの完了イベントが不足しています",
            )
            return
        await self._persist_render_publish(
            collecting,
            result.collection.snapshot,
            token=None,
            provided_image=result.image,
            renderer_version="ongeki-scorelog-generator-v2",
        )

    async def _persist_render_publish(
        self,
        run: CollectionRunRecord,
        snapshot: RatingSnapshot,
        *,
        token: str | None,
        provided_image: RenderedImageMetadata | None = None,
        renderer_version: str | None = None,
    ) -> None:
        # A structured observation can be durable even when its image or
        # publication fails.  Compare against the last fully rendered
        # snapshot so a failed attempt never becomes the next success
        # baseline.
        previous = self.persistence.latest_successful_snapshot(
            user_id=run.user_id,
            game=run.game,
        )
        rating = _decimal(snapshot.rating, required=True)
        previous_rating = previous.rating if previous is not None else None
        fingerprint = snapshot.semantic_fingerprint
        expected_renderer_version = renderer_version or "chunithm-html-v2"
        snapshot_id = self.persistence.save_snapshot(
            user_id=run.user_id,
            game=run.game,
            player_name=snapshot.player_name or self.settings.rhythm_player_display_name,
            rating=rating,
            previous_rating=previous_rating,
            semantic_fingerprint=fingerprint,
            source_updated_at=snapshot.source_updated_at,
            collected_at=snapshot.collected_at,
            raw_data=sanitize_for_output(snapshot.raw_data or {}, (token or "",)),
            entries=[_entry_row(entry) for entry in snapshot.entries],
        )
        semantic_unchanged = (
            previous is not None and previous.semantic_fingerprint == fingerprint
        )
        previous_image = None
        if semantic_unchanged and previous is not None and previous.image_links:
            candidate_image = previous.image_links[0].image
            if candidate_image.renderer_version == expected_renderer_version:
                previous_image = candidate_image
        rendering = self._transition(
            run.id,
            "renderingImage",
            event="normalizedSnapshotSaved",
            message=(
                f"正規化レートをSnapshot #{snapshot_id}として保存し、"
                "前回の検証済み画像を再利用できるか確認します"
                if previous_image is not None
                else f"正規化レートをSnapshot #{snapshot_id}として保存し、画像を生成します"
            ),
            snapshot_id=snapshot_id,
        )
        metadata: RenderedImageMetadata | None = None
        if previous_image is not None:
            previous_generated_at = previous_image.generated_at
            if previous_generated_at.tzinfo is None:
                previous_generated_at = previous_generated_at.replace(tzinfo=UTC)
            else:
                previous_generated_at = previous_generated_at.astimezone(UTC)
            try:
                self.persistence.attach_image(
                    snapshot_id=snapshot_id,
                    game=run.game,
                    image_path=previous_image.image_path,
                    image_sha256=previous_image.image_sha256,
                    width=previous_image.width,
                    height=previous_image.height,
                    byte_size=previous_image.byte_size,
                    generated_at=previous_generated_at,
                    renderer_version=previous_image.renderer_version,
                    image_root=str(self.settings.rhythm_image_dir),
                    require_complete_png=True,
                )
            except (OSError, ValueError):
                # A missing or corrupted prior artifact is not reusable. Render
                # a fresh image from the newly collected structured snapshot.
                pass
            else:
                metadata = RenderedImageMetadata(
                    path=previous_image.image_path,
                    sha256=previous_image.image_sha256,
                    width=previous_image.width,
                    height=previous_image.height,
                    byte_size=previous_image.byte_size,
                    rendered_at=previous_generated_at,
                )
                if provided_image is not None:
                    self._discard_generated_image(
                        provided_image,
                        keep_path=Path(previous_image.image_path),
                    )

        if metadata is None and provided_image is not None:
            if (
                provided_image.image_sha256 is None
                or provided_image.width is None
                or provided_image.height is None
                or provided_image.byte_size is None
                or provided_image.width < 1
                or provided_image.height < 1
                or provided_image.byte_size < 1
            ):
                await self._fail(
                    rendering,
                    "rating_image_metadata_invalid",
                    "外部生成PNGの完了メタデータが不正です",
                    snapshot_id=snapshot_id,
                )
                return
            try:
                self.persistence.attach_image(
                    snapshot_id=snapshot_id,
                    game=run.game,
                    image_path=provided_image.image_path,
                    image_sha256=provided_image.image_sha256,
                    width=provided_image.width,
                    height=provided_image.height,
                    byte_size=provided_image.byte_size,
                    generated_at=provided_image.rendered_at,
                    renderer_version=expected_renderer_version,
                    image_root=str(self.settings.rhythm_image_dir),
                    require_complete_png=True,
                )
            except (OSError, ValueError):
                await self._fail(
                    rendering,
                    "rating_image_persistence_failed",
                    "外部生成PNGの保存確認に失敗しました。前回成功画像は保持しています",
                    snapshot_id=snapshot_id,
                )
                return
            metadata = provided_image

        if metadata is None:
            collected_utc = snapshot.collected_at.astimezone(UTC)
            image_path = (
                self.settings.rhythm_image_dir.resolve()
                / run.game
                / collected_utc.strftime("%Y/%m")
                / f"{run.id}.png"
            )
            renderer = CHUNITHMRenderer(
                backend=self.png_backend,
                secrets=(token or "",),
            )
            rendered = await asyncio.to_thread(
                renderer.render,
                snapshot,
                image_path,
                previous_rating=previous_rating,
            )
            if not rendered.success or rendered.metadata is None:
                await self._fail(
                    rendering,
                    "rating_image_render_failed",
                    "レートデータは保存しましたがPNG画像の生成を確認できませんでした",
                    snapshot_id=snapshot_id,
                )
                return
            metadata = rendered.metadata
            if (
                metadata.image_sha256 is None
                or metadata.width is None
                or metadata.height is None
                or metadata.byte_size is None
                or metadata.width < 1
                or metadata.height < 1
                or metadata.byte_size < 1
            ):
                await self._fail(
                    rendering,
                    "rating_image_metadata_invalid",
                    "PNG生成完了イベントのメタデータが不正です",
                    snapshot_id=snapshot_id,
                )
                return
            try:
                self.persistence.attach_image(
                    snapshot_id=snapshot_id,
                    game=run.game,
                    image_path=metadata.image_path,
                    image_sha256=metadata.image_sha256,
                    width=metadata.width,
                    height=metadata.height,
                    byte_size=metadata.byte_size,
                    generated_at=metadata.rendered_at,
                    renderer_version=expected_renderer_version,
                    image_root=str(self.settings.rhythm_image_dir),
                    require_complete_png=True,
                )
            except (OSError, ValueError):
                await self._fail(
                    rendering,
                    "rating_image_persistence_failed",
                    "PNGの保存確認に失敗しました。前回成功画像は保持しています",
                    snapshot_id=snapshot_id,
                )
                return
        comparing = self._transition(
            run.id,
            "comparingSnapshot",
            event="ratingImageVerified",
            message="正規化データとPNGの更新差分を比較しています",
            snapshot_id=snapshot_id,
        )
        previous_stored_image = (
            previous.image_links[0].image
            if previous is not None and previous.image_links
            else None
        )
        presentation_changed = bool(
            semantic_unchanged
            and previous_stored_image is not None
            and previous_stored_image.renderer_version != expected_renderer_version
            and previous_stored_image.image_sha256 != metadata.image_sha256
        )
        retry_identical_delivery = False
        if semantic_unchanged and not presentation_changed:
            prior_delivery_state = self.persistence.latest_rating_delivery_state(
                user_id=run.user_id,
                game=run.game,
                semantic_fingerprint=fingerprint,
            )
            if self.publisher.dry_run or prior_delivery_state == "posted":
                self._transition(
                    comparing.id,
                    "unchanged",
                    event="semanticSnapshotUnchanged",
                    message="semantic_fingerprintは前回と同じため投稿しません",
                    snapshot_id=snapshot_id,
                )
                self._provider_success(run)
                return
            if prior_delivery_state in {"unknown", "posting"}:
                await self._fail(
                    comparing,
                    "discord_delivery_requires_reconciliation",
                    "前回Discord送信の結果が不明なため、自動再送せず管理者確認を待ちます",
                    snapshot_id=snapshot_id,
                )
                return
            # A previous explicit failure (or no delivery because an earlier
            # run stopped before the claim) is safe to attempt now. Reusing an
            # identical verified PNG is expected in this recovery path.
            retry_identical_delivery = True
        previous_sha = None
        if previous is not None and previous.image_links:
            previous_sha = previous.image_links[0].image.image_sha256
        if previous_sha == metadata.image_sha256 and not retry_identical_delivery:
            await self._fail(
                comparing,
                "stale_rating_image",
                "レートデータは変化しましたが画像が前回と同一なため投稿しません",
                snapshot_id=snapshot_id,
            )
            return
        ready = self._transition(
            comparing.id,
            "readyToPost",
            event=(
                "ratingPresentationChanged"
                if presentation_changed
                else "semanticSnapshotChanged"
            ),
            message=(
                "Renderer更新による新しいPNGを確認し、既存Discord投稿を更新します"
                if presentation_changed
                else (
                    "前回の明示的な送信失敗を確認し、同一内容を安全に再試行します"
                    if retry_identical_delivery
                    else "レート変化と新しいPNGを確認し、Discord投稿準備ができました"
                )
            ),
            snapshot_id=snapshot_id,
        )
        channel_id = {
            "chunithm": self.settings.discord_chunithm_channel_id,
            "maimai": self.settings.discord_maimai_channel_id,
            "ongeki": self.settings.discord_ongeki_channel_id,
        }.get(run.game)
        if channel_id is None:
            await self._fail(
                ready,
                "discord_channel_not_configured",
                f"{run.game}のDiscord投稿先が設定されていません",
                snapshot_id=snapshot_id,
            )
            return
        local_time = snapshot.collected_at.astimezone(ZoneInfo(self.settings.rhythm_time_zone))
        posting: CollectionRunRecord | None = None
        if not self.publisher.dry_run:
            posting = self._transition(
                ready.id,
                "posting",
                event="discordDeliveryClaimed",
                message="Discordの専用チャンネルへ投稿しています",
                snapshot_id=snapshot_id,
            )
        outcome = await self.publisher.publish_rating(
            user_id=run.user_id,
            local_date=local_time.date(),
            game=run.game,
            channel_id=channel_id,
            semantic_fingerprint=fingerprint,
            image_sha256=metadata.image_sha256,
            image_path=Path(metadata.image_path),
            rating=str(rating),
            previous_rating=str(previous_rating) if previous_rating is not None else None,
            delta=str(rating - previous_rating) if previous_rating is not None else None,
            collected_label=local_time.strftime("%Y-%m-%d %H:%M:%S %Z"),
            now=self.now(),
            allow_presentation_refresh=presentation_changed,
        )
        if outcome.dry_run:
            try:
                image_label = str(Path(metadata.image_path).relative_to(Path.cwd()))
            except ValueError:
                image_label = Path(metadata.image_path).name
            delta_label = (
                str(rating - previous_rating) if previous_rating is not None else "initial"
            )
            self._transition(
                ready.id,
                "cancelled",
                event="dryRunCompleted",
                message=(
                    "dry-run完了: Discordへは投稿しません / "
                    f"source={snapshot.source} / rating={rating} / delta={delta_label} / "
                    f"image={image_label} / channel={channel_id} / "
                    f"fingerprint={fingerprint[:12]} / content={outcome.planned_content}"
                ),
                snapshot_id=snapshot_id,
            )
            self._provider_success(run)
            return
        if posting is None:
            await self._fail(
                ready,
                "discord_publish_state_invalid",
                "Discord投稿の状態を確認できませんでした",
                snapshot_id=snapshot_id,
            )
            return
        if outcome.posted:
            self._transition(
                posting.id,
                "posted",
                event="discordMessageVerified",
                message="Discord投稿が完了し、メッセージIDを保存しました",
                snapshot_id=snapshot_id,
            )
            self._provider_success(run)
            return
        if outcome.duplicate:
            self._transition(
                posting.id,
                "cancelled",
                event="discordDuplicateObserved",
                message="同一内容のDiscord投稿済み記録を確認したため送信を省略しました",
                snapshot_id=snapshot_id,
            )
            self._provider_success(run)
            return
        await self._fail(
            posting,
            outcome.error_code or "discord_publish_failed",
            (
                "Discord送信結果が不明です。自動再送せず管理者確認を待ちます"
                if outcome.uncertain
                else "Discordへ投稿できませんでした"
            ),
            snapshot_id=snapshot_id,
        )

    def _discard_generated_image(
        self,
        metadata: RenderedImageMetadata,
        *,
        keep_path: Path,
    ) -> None:
        """Remove only this run's now-unreferenced generated artifact."""

        try:
            root = self.settings.rhythm_image_dir.resolve()
            candidate = Path(metadata.image_path).resolve()
            candidate.relative_to(root)
            if candidate != keep_path.resolve() and candidate.is_file():
                candidate.unlink()
        except (OSError, ValueError):
            # Cleanup failure must not turn a verified unchanged snapshot into
            # a collection failure.  No path or provider exception is logged.
            return

    def _provider_success(self, run: CollectionRunRecord) -> None:
        self.persistence.update_provider_status(
            user_id=run.user_id,
            game=run.game,
            provider=self._provider_name(run.game),
            session_status="success",
            authenticated_at=self.now(),
        )

    @staticmethod
    def _provider_name(game: str) -> str:
        return {
            "chunithm": "chunirec_api_v2",
            "maimai": "official_net_browser",
            "ongeki": "official_net_scorelog_generator",
        }[game]

    async def _fail(
        self,
        run: CollectionRunRecord,
        code: str,
        message: str,
        *,
        snapshot_id: int | None = None,
    ) -> None:
        failed = self._transition(
            run.id,
            "failed",
            event="collectionFailed",
            message=message,
            error_code=code,
            error_message=message,
            snapshot_id=snapshot_id,
        )
        self.persistence.update_provider_status(
            user_id=run.user_id,
            game=run.game,
            provider=self._provider_name(run.game),
            session_status="failed",
            error_code=code,
            safe_error=message,
        )
        channel_id = self.settings.discord_error_channel_id
        if channel_id is not None:
            local_time = (failed.finished_at or self.now()).replace(tzinfo=UTC).astimezone(
                ZoneInfo(self.settings.rhythm_time_zone)
            )
            await self.publisher.publish_collection_error(
                user_id=run.user_id,
                local_date=local_time.date(),
                game=run.game,
                channel_id=channel_id,
                state_label=message,
                occurred_label=local_time.strftime("%Y-%m-%d %H:%M"),
                action_label="/取得状況で確認し、設定または認証を修正して再実行してください",
                now=self.now(),
            )


__all__ = ["RhythmCollectionExecutor", "UrllibCHUNIRECTransport"]
