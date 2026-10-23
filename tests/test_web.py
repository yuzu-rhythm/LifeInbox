from __future__ import annotations

import calendar
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from life_inbox.career import ApplicationStage, CareerKind, CareerStatus
from life_inbox.cloud_run import create_cloud_run_app, load_cloud_run_runtime
from life_inbox.config import Settings
from life_inbox.constants import (
    AccountKind,
    EventKind,
    EventStatus,
    MealType,
    OutsideHome,
    RhythmGame,
)
from life_inbox.db import Database
from life_inbox.finance import FinanceService
from life_inbox.models import (
    Attachment,
    CareerApplication,
    CareerOpportunity,
    Event,
    Expense,
    Meal,
    Memo,
    ReceiptItem,
)
from life_inbox.rhythm import RhythmService
from life_inbox.social_drafts import SocialDraftService
from life_inbox.web import create_web_app, run_web


@dataclass
class WebEnvironment:
    settings: Settings
    database: Database
    finance: FinanceService
    rhythm: RhythmService
    app: FastAPI


@pytest.fixture
def web_environment(tmp_path: Path) -> WebEnvironment:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
        timezone="Asia/Tokyo",
        web_host="127.0.0.1",
        web_port=8765,
    )
    database = Database(settings.database_url)
    database.init()
    finance = FinanceService(database.session_factory)
    finance.ensure_default_accounts()
    rhythm = RhythmService(database.session_factory)
    app = create_web_app(
        settings,
        database=database,
        finance=finance,
        rhythm=rhythm,
    )
    return WebEnvironment(settings, database, finance, rhythm, app)


@pytest.fixture
def client(web_environment: WebEnvironment):
    with TestClient(web_environment.app) as test_client:
        yield test_client


def _local_month_date(environment: WebEnvironment, month_offset: int = 0) -> date:
    timezone = environment.settings.timezone_info()
    today = datetime.now(timezone).date()
    month_index = today.year * 12 + today.month - 1 + month_offset
    year, month_index = divmod(month_index, 12)
    return date(year, month_index + 1, 15)


def _utc_datetime(
    environment: WebEnvironment,
    *,
    month_offset: int = 0,
    day: int = 15,
    hour: int = 12,
) -> datetime:
    timezone = environment.settings.timezone_info()
    month_date = _local_month_date(environment, month_offset)
    safe_day = min(day, calendar.monthrange(month_date.year, month_date.month)[1])
    local_value = datetime(
        month_date.year,
        month_date.month,
        safe_day,
        hour,
        tzinfo=timezone,
    )
    return local_value.astimezone(UTC).replace(tzinfo=None)


def _add_event(
    environment: WebEnvironment,
    message_id: str,
    occurred_at: datetime,
    *,
    status: EventStatus = EventStatus.CONFIRMED,
    raw_text: str | None = None,
    expense: dict[str, object] | None = None,
    meal: dict[str, object] | None = None,
    memo: str | None = None,
) -> int:
    kinds: list[str] = []
    if expense is not None:
        kinds.append(EventKind.EXPENSE.value)
    if meal is not None:
        kinds.append(EventKind.MEAL.value)
    if memo is not None:
        kinds.append(EventKind.MEMO.value)
    with environment.database.session() as session:
        event = Event(
            discord_message_id=message_id,
            discord_channel_id="web-test-channel",
            discord_user_id="web-test-user",
            captured_at=occurred_at,
            occurred_at=occurred_at,
            raw_text=raw_text or message_id,
            source="discord",
            status=status.value,
            event_type="+".join(kinds) if kinds else "unknown",
            event_kinds=kinds,
        )
        if expense is not None:
            event.expenses.append(Expense(**expense))
        if meal is not None:
            event.meals.append(Meal(**meal))
        if memo is not None:
            event.memo = Memo(body=memo)
        session.add(event)
        session.commit()
        return event.id


def _add_attachment(
    environment: WebEnvironment,
    event_id: int,
    *,
    filename: str,
    content_type: str,
    local_path: Path,
    data: bytes = b"image-bytes",
) -> int:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(data)
    with environment.database.session() as session:
        attachment = Attachment(
            event_id=event_id,
            discord_attachment_id=f"attachment-{event_id}-{filename}",
            original_filename=filename,
            content_type=content_type,
            local_path=str(local_path),
            sha256=f"sha-{event_id}-{filename}",
        )
        session.add(attachment)
        session.commit()
        return attachment.id


def test_factory_initializes_dependencies_without_mutating_database(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
    )
    database = Database(settings.database_url)
    database.init()

    app = create_web_app(settings, database=database)

    assert app.state.database is database
    assert isinstance(app.state.finance, FinanceService)
    assert isinstance(app.state.rhythm, RhythmService)
    with database.session() as session:
        assert session.query(Event).count() == 0
        assert session.query(Expense).count() == 0


def test_shared_footer_distinguishes_local_and_cloud_read_only_modes(
    web_environment: WebEnvironment,
    client: TestClient,
    tmp_path: Path,
):
    local = client.get("/")

    assert local.status_code == 200
    assert "この画面はこのMacの中だけで開いています。" in local.text
    assert "Cloud Runの読み取り専用スナップショットです。" not in local.text

    cloud_data_dir = tmp_path / "cloud-data"
    cloud_runtime = load_cloud_run_runtime(
        {
            "LIFE_INBOX_CLOUD_RUN": "true",
            "LIFE_INBOX_CLOUD_RUN_READ_ONLY": "true",
            "LIFE_INBOX_CLOUD_RUN_ALLOW_EPHEMERAL_SQLITE": "true",
            "PORT": "8080",
            "DATABASE_URL": f"sqlite:///{cloud_data_dir / 'cloud.db'}",
            "DATA_DIR": str(cloud_data_dir),
        }
    )
    cloud_app = create_cloud_run_app(cloud_runtime)
    with TestClient(cloud_app) as cloud_client:
        cloud = cloud_client.get("/")

    assert cloud.status_code == 200
    assert "この画面はこのMacの中だけで開いています。" not in cloud.text
    assert "Cloud Runの読み取り専用スナップショットです。" in cloud.text
    assert "Googleログイン／IAP" in cloud.text
    assert "認証・アクセス制御" in cloud.text


def test_archive_empty_state_and_form_labels_are_explicit_on_small_screens(
    client: TestClient,
):
    empty = client.get("/archive")

    assert empty.status_code == 200
    assert "ローカル記録はまだありません" in empty.text
    assert 'id="archive-filters-heading"' in empty.text
    assert 'aria-labelledby="archive-filters-heading"' in empty.text
    field_ids = (
        "archive-query",
        "archive-status",
        "archive-kind",
        "archive-media",
        "archive-limit",
    )
    for field_id in field_ids:
        assert f'id="{field_id}"' in empty.text
        assert f'for="{field_id}"' in empty.text
    assert 'role="status"' in empty.text
    assert 'aria-live="polite"' in empty.text
    assert 'href="/quick"' in empty.text

    no_match = client.get("/archive", params={"q": "この語は存在しない"})

    assert no_match.status_code == 200
    assert "条件に一致する記録はありません" in no_match.text
    assert "条件をクリア" in no_match.text
    assert "ローカル記録はまだありません" not in no_match.text


def test_run_web_initializes_temp_database_and_passes_validated_local_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'run-web.db'}",
        data_dir=tmp_path,
        web_host="127.0.0.1",
        web_port=9876,
    )
    calls: dict[str, object] = {}

    def fake_run(app: FastAPI, *, host: str, port: int) -> None:
        calls.update(app=app, host=host, port=port)

    monkeypatch.setattr("uvicorn.run", fake_run)

    run_web(settings)

    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 9876
    database = Database(settings.database_url)
    account_names = [
        account.name for account in FinanceService(database.session_factory).list_accounts()
    ]
    assert account_names == ["現金", "普通口座", "クレジットカード"]


def test_all_existing_page_routes_render_with_shared_brand_and_sections(
    web_environment: WebEnvironment,
    client: TestClient,
):
    event_id = _add_event(
        web_environment,
        "route-event",
        _utc_datetime(web_environment),
        raw_text="680 テスト店",
        expense={
            "amount": 680,
            "merchant_raw": "テスト店",
            "merchant_normalized": "テスト店",
            "category": "食費",
            "subcategory": "外食",
            "description": "昼食",
            "confidence": 0.95,
        },
    )
    routes_and_sections = {
        "/": "カテゴリ別",
        "/quick": "何を残しますか？",
        "/transactions": "支出の記録",
        "/budgets": "月次の家計管理",
        "/accounts": "お金の置き場所",
        "/memos": "忘れたくない一言",
        "/meals": "食べたものの流れ",
        "/rhythm": "音ゲーTODOとリザルト",
        "/tasks": "Google Classroom",
        "/guide": "コマンド案内",
        f"/events/{event_id}": "保存内容",
    }

    for path, section in routes_and_sections.items():
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert "生活マイページ" in response.text
        assert section in response.text
        assert response.headers["cache-control"] == "no-store"


def test_social_page_is_read_only_and_shows_local_outbox_state_and_safe_images(
    web_environment: WebEnvironment,
    client: TestClient,
):
    ready_event_id = _add_event(
        web_environment,
        "social-web-ready",
        _utc_datetime(web_environment),
        status=EventStatus.PENDING,
        raw_text="写真付き投稿 <script>hidden</script>",
    )
    image_id = _add_attachment(
        web_environment,
        ready_event_id,
        filename="photo.png",
        content_type="image/png",
        local_path=web_environment.settings.data_dir / "attachments" / "photo.png",
    )
    blocked_event_id = _add_event(
        web_environment,
        "social-web-blocked",
        _utc_datetime(web_environment, month_offset=-1),
        status=EventStatus.PENDING,
        raw_text="画像なしの投稿",
    )
    social = SocialDraftService(web_environment.database.session_factory)
    social.register_event(ready_event_id)
    social.select_target(ready_event_id, "instagram", selected_by_user_id=10)
    social.register_event(blocked_event_id)
    social.select_target(blocked_event_id, "both", selected_by_user_id=10)

    response = client.get("/social")

    assert response.status_code == 200
    assert "SNS下書き" in response.text
    assert 'href="/social"' in response.text
    assert 'aria-current="page"' in response.text
    assert "写真付き投稿 &lt;script&gt;hidden&lt;/script&gt;" in response.text
    assert "<script>hidden</script>" not in response.text
    assert "Instagram" in response.text
    assert "両方" in response.text
    assert "ローカル出力候補" in response.text
    assert "メディア不足で停止" in response.text
    assert f'href="/attachments/{image_id}"' in response.text
    assert f'src="/attachments/{image_id}"' in response.text
    assert "外部SNSには投稿していません" in response.text
    assert "動画の捕捉・保存は未対応" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"].endswith("script-src 'none'")
    assert client.post("/social").status_code == 405

    image_response = client.get(f"/attachments/{image_id}")
    assert image_response.status_code == 200
    assert image_response.content == b"image-bytes"


def test_career_page_has_nav_route_empty_states_and_is_read_only(
    web_environment: WebEnvironment,
    client: TestClient,
):
    with web_environment.database.session() as session:
        before_opportunities = session.query(CareerOpportunity).count()
        before_applications = session.query(CareerApplication).count()

    response = client.get("/career")

    assert response.status_code == 200
    assert "就活の見通し" in response.text
    assert 'href="/career"' in response.text
    assert 'aria-current="page"' in response.text
    assert "応募中の記録はありません" in response.text
    assert "保存中の企業・求人・イベントはありません" in response.text
    assert "明示的な期限や次の行動はまだありません" in response.text
    assert "#なんでも入力" in response.text
    for command in ("/就活追加", "/就活更新", "/就活一覧"):
        assert command in response.text
    assert client.post("/career").status_code == 405

    with web_environment.database.session() as session:
        assert session.query(CareerOpportunity).count() == before_opportunities
        assert session.query(CareerApplication).count() == before_applications


def test_career_page_separates_active_records_terminal_items_and_safe_fields(
    web_environment: WebEnvironment,
    client: TestClient,
):
    career = web_environment.app.state.career_service
    opportunity = career.create_opportunity(
        "Acme <script>",
        "バックエンド職",
        kind=CareerKind.JOB,
        url='https://example.com/jobs?q="safe"',
        status=CareerStatus.INTERESTED,
        deadline="2026-09-05 15:00",
        next_action="応募書類を確認",
        next_action_at="2026-09-03 10:00",
        external_id="EXTERNAL-SECRET-DO-NOT-SHOW",
        notes="PRIVATE-NOTE-DO-NOT-SHOW",
    )
    application = career.create_application(
        opportunity_id=opportunity.id,
        stage=ApplicationStage.INTERVIEW,
        deadline="2026-09-04 09:30",
        next_action="面接準備",
        next_action_at="2026-09-02 18:00",
    )
    source_event_id = _add_event(
        web_environment,
        "career-secret-message-id",
        _utc_datetime(web_environment),
        raw_text="RAW-EVENT-SECRET <script>not for web</script>",
    )
    terminal = career.create_opportunity(
        "終了した会社",
        "過去の求人",
        kind=CareerKind.COMPANY,
        status=CareerStatus.REJECTED,
        source_event_id=source_event_id,
        external_id="TERMINAL-EXTERNAL-SECRET",
        notes="TERMINAL-PRIVATE-SECRET",
    )

    response = client.get("/career")

    assert response.status_code == 200
    assert "Acme &lt;script&gt;" in response.text
    assert "<script>" not in response.text
    assert "応募中" in response.text
    assert "面接" in response.text
    assert "進行中" in response.text
    assert "保存・興味あり" in response.text
    assert "求人" in response.text
    assert "興味あり" in response.text
    assert "2026-09-02 18:00" in response.text
    assert "2026-09-04 09:30" in response.text
    assert "Asia/Tokyo" in response.text
    assert "次の行動: 面接準備" in response.text
    assert f"LifeTask #{application.linked_task_id}" in response.text
    assert "未完了" in response.text
    assert 'href="https://example.com/jobs?q=&#34;safe&#34;"' in response.text
    assert "javascript:" not in response.text
    assert "RAW-EVENT-SECRET" not in response.text
    assert "career-secret-message-id" not in response.text
    assert "EXTERNAL-SECRET" not in response.text
    assert "PRIVATE-NOTE-DO-NOT-SHOW" not in response.text
    assert "TERMINAL-PRIVATE-SECRET" not in response.text
    assert "終了した会社" in response.text
    assert "不採用" in response.text
    assert "career-record-terminal" in response.text
    assert terminal.id > 0


def test_career_page_orders_upcoming_items_and_dashboard_shows_nearest_action(
    web_environment: WebEnvironment,
    client: TestClient,
):
    career = web_environment.app.state.career_service
    career.create_opportunity(
        "先に確認する会社",
        "説明会",
        kind=CareerKind.EVENT,
        deadline="2026-09-01 11:00",
    )
    career.create_opportunity(
        "後で確認する会社",
        "求人",
        kind=CareerKind.JOB,
        next_action="求人票を読む",
        next_action_at="2026-09-03 09:00",
    )
    terminal = career.create_opportunity(
        "除外会社",
        "終了求人",
        status=CareerStatus.CLOSED,
        deadline="2026-09-01 09:00",
    )

    response = client.get("/career")
    dashboard = client.get("/")

    assert response.status_code == 200
    assert response.text.index("先に確認する会社") < response.text.index("後で確認する会社")
    assert "2026-09-01 11:00" in response.text
    assert "2026-09-03 09:00" in response.text
    assert "求人票を読む" in response.text
    assert "除外会社" in response.text
    assert terminal.id > 0

    assert dashboard.status_code == 200
    career_card = re.search(
        r'<a class="metric card career-metric" href="/career">.*?</a>',
        dashboard.text,
        flags=re.DOTALL,
    )
    assert career_card is not None
    assert "就活" in career_card.group(0)
    assert "2件" in career_card.group(0)
    assert "先に確認する会社" in career_card.group(0)
    assert "2026-09-01 11:00" in career_card.group(0)


def test_tasks_page_renders_local_tasks_grouped_with_summary_and_source_badges(
    web_environment: WebEnvironment,
    client: TestClient,
):
    task_service = web_environment.app.state.task_service
    today = datetime.now(web_environment.settings.timezone_info()).date()
    task_service.create_manual(
        "レポートを提出",
        description="添付ファイルを確認する",
        due_date=today - timedelta(days=1),
    )
    completed_task = task_service.upsert_external(
        "notion",
        "page-42",
        "Notionから来たタスク",
        source_url="https://notion.so/page-42",
    )
    task_service.complete(completed_task.id)

    response = client.get("/tasks")

    assert response.status_code == 200
    assert "<strong>1件</strong>" in response.text
    assert "レポートを提出" in response.text
    assert "添付ファイルを確認する" in response.text
    assert "期限超過" in response.text
    assert "Notionから来たタスク" in response.text
    assert "class=\"tag task-source task-source-manual\">手動" in response.text
    assert "class=\"tag task-source task-source-notion\">Notion" in response.text
    assert "https://notion.so/page-42" in response.text
    assert "Google Classroom / Notion はまだ接続していません" in response.text
    assert client.post("/tasks").status_code == 405


def test_quick_page_is_local_bounded_prg_and_idempotent(
    web_environment: WebEnvironment,
    client: TestClient,
):
    landing = client.get("/quick")

    assert landing.status_code == 200
    assert 'method="post" action="/quick"' in landing.text
    assert 'maxlength="4000"' in landing.text
    for label in ("通常", "メモ", "タスク", "食事", "支出", "音ゲー", "就活"):
        assert label in landing.text
    token_match = re.search(
        r'name="idempotency_token" value="([A-Za-z0-9_-]+)"',
        landing.text,
    )
    assert token_match is not None
    token = token_match.group(1)
    raw_text = "<script>alert('x')</script>\nあとで確認する"

    forged = client.post(
        "/quick",
        data={
            "capture_type": "memo",
            "text": "別サイトから作った送信",
            "idempotency_token": "z" * 24,
        },
        follow_redirects=False,
    )
    assert forged.status_code == 303
    assert "送信トークンが無効" in client.get(forged.headers["location"]).text
    with web_environment.database.session() as session:
        assert session.query(Event).filter_by(
            discord_message_id=f"web-quick:{'z' * 24}"
        ).count() == 0

    saved = client.post(
        "/quick",
        data={
            "capture_type": "memo",
            "text": raw_text,
            "idempotency_token": token,
        },
        follow_redirects=False,
    )

    assert saved.status_code == 303
    result = client.get(saved.headers["location"])
    assert result.status_code == 200
    assert "ローカルSQLiteへ保存しました" in result.text
    assert "AI・Discord・外部サービスは呼び出していません" in result.text
    assert "保存内容を見る" in result.text
    assert "&lt;script&gt;" not in result.text

    with web_environment.database.session() as session:
        event = session.query(Event).filter_by(
            discord_message_id=f"web-quick:{token}"
        ).one()
        event_id = event.id
        assert event.discord_channel_id == "web-quick:memo"
        assert event.raw_text == raw_text
    detail = client.get(f"/events/{event_id}")
    assert detail.status_code == 200
    assert "すぐ記録（メモ）" in detail.text
    assert "&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;" in detail.text
    assert "<script>alert('x')</script>" not in detail.text

    replay = client.post(
        "/quick",
        data={
            "capture_type": "memo",
            "text": raw_text,
            "idempotency_token": token,
        },
        follow_redirects=False,
    )
    assert replay.status_code == 303
    with web_environment.database.session() as session:
        assert session.query(Event).filter_by(
            discord_message_id=f"web-quick:{token}"
        ).count() == 1

    conflict = client.post(
        "/quick",
        data={
            "capture_type": "memo",
            "text": "同じトークンの別内容",
            "idempotency_token": token,
        },
        follow_redirects=False,
    )
    assert conflict.status_code == 303
    assert "別の内容で使用済み" in client.get(conflict.headers["location"]).text


def test_quick_page_rejects_invalid_type_and_oversized_input(
    client: TestClient,
):
    invalid_type = client.post(
        "/quick",
        data={
            "capture_type": "not-a-type",
            "text": "入力",
            "idempotency_token": "a" * 24,
        },
        follow_redirects=False,
    )
    assert invalid_type.status_code == 303
    assert "記録の種類を選び直してください" in client.get(
        invalid_type.headers["location"]
    ).text

    oversized = client.post(
        "/quick",
        data={
            "capture_type": "normal",
            "text": "あ" * 4_001,
            "idempotency_token": "b" * 24,
        },
        follow_redirects=False,
    )
    assert oversized.status_code == 303
    assert "4,000文字以内" in client.get(oversized.headers["location"]).text


def test_tasks_page_has_explicit_empty_state(client: TestClient):
    response = client.get("/tasks")

    assert response.status_code == 200
    assert "タスクはまだありません" in response.text
    assert "Discordの許可された生活チャンネル" in response.text
    assert "Google Classroom / Notion はまだ接続していません" in response.text
    assert "0件" in response.text


def test_dashboard_and_tasks_show_today_routine_completion_and_due_groups(
    web_environment: WebEnvironment,
    client: TestClient,
):
    today = datetime.now(web_environment.settings.timezone_info()).date()
    task = web_environment.app.state.task_service.create_manual(
        "今日確認すること",
        due_date=today,
    )
    routine = web_environment.app.state.routine_service.create("水分をとる", "09:00")
    web_environment.app.state.routine_service.complete(routine.id)

    tasks = client.get("/tasks")
    dashboard = client.get("/")

    assert tasks.status_code == 200
    assert "今日確認すること" in tasks.text
    assert "今日が期限" in tasks.text
    assert "水分をとる" in tasks.text
    assert "1/1" in tasks.text
    assert f"/tasks#task-{task.id}" in dashboard.text
    assert "水分をとる" in dashboard.text
    assert "1/1" in dashboard.text


def test_dashboard_and_transaction_filters_use_confirmed_current_local_month(
    web_environment: WebEnvironment,
    client: TestClient,
):
    current = _utc_datetime(web_environment)
    previous = _utc_datetime(web_environment, month_offset=-1)
    _add_event(
        web_environment,
        "dining",
        current,
        raw_text="外食ランチ",
        expense={
            "amount": 1_200,
            "merchant_raw": "定食屋",
            "merchant_normalized": "定食屋",
            "category": "食費",
            "subcategory": "外食",
            "description": "ランチ",
            "confidence": 0.9,
        },
    )
    _add_event(
        web_environment,
        "groceries",
        current,
        raw_text="自炊材料",
        expense={
            "amount": 2_500,
            "merchant_raw": "スーパー",
            "merchant_normalized": "スーパー",
            "category": "食費",
            "subcategory": "自炊材料",
            "description": "食材",
            "confidence": 0.9,
        },
    )
    _add_event(
        web_environment,
        "large-fun",
        current,
        raw_text="ゲームセンター",
        expense={
            "amount": 6_000,
            "merchant_raw": "ゲームセンター",
            "merchant_normalized": "ゲームセンター",
            "category": "娯楽",
            "subcategory": None,
            "description": "遊び",
            "confidence": 0.9,
        },
    )
    _add_event(
        web_environment,
        "previous-month",
        previous,
        raw_text="先月の支出",
        expense={
            "amount": 9_000,
            "merchant_raw": "先月店",
            "merchant_normalized": "先月店",
            "category": "日用品",
            "subcategory": None,
            "description": "先月",
            "confidence": 0.9,
        },
    )
    _add_event(
        web_environment,
        "pending-current",
        current,
        status=EventStatus.PENDING,
        raw_text="未確定",
        expense={
            "amount": 99_999,
            "merchant_raw": "未確定店",
            "merchant_normalized": "未確定店",
            "category": "娯楽",
            "subcategory": None,
            "description": "表示しない",
            "confidence": 0.5,
        },
    )
    _add_event(
        web_environment,
        "ignored-current",
        current,
        status=EventStatus.IGNORED,
        raw_text="除外",
        expense={
            "amount": 88_888,
            "merchant_raw": "除外店",
            "merchant_normalized": "除外店",
            "category": "娯楽",
            "subcategory": None,
            "description": "表示しない",
            "confidence": 0.5,
        },
    )
    _add_event(
        web_environment,
        "confirmed-meal",
        current,
        raw_text="自炊の夕食",
        meal={
            "meal_type": MealType.DINNER.value,
            "restaurant_raw": "自宅",
            "restaurant_normalized": "自宅",
            "description": "カレー",
            "outside_home": OutsideHome.HOME.value,
            "confidence": 0.9,
        },
    )

    dashboard = client.get("/")
    assert "9,700円" in dashboard.text
    assert "3件" in dashboard.text
    assert "1,200円" in dashboard.text
    assert "1回・専用表示へ" in dashboard.text
    assert "1件</strong><small>5,000円以上" in dashboard.text
    assert "1件</strong><small>写真・外食／自炊を確認" in dashboard.text
    assert "61.9%" in dashboard.text
    assert "娯楽" in dashboard.text
    assert "未確定店" not in dashboard.text

    current_month = _local_month_date(web_environment).strftime("%Y-%m")
    response = client.get("/transactions", params={"month": current_month})
    assert response.status_code == 200
    assert "9,700円" in response.text
    assert "定食屋" in response.text
    assert "自炊材料" in response.text
    assert "ゲームセンター" in response.text
    assert "先月店" not in response.text
    assert "未確定店" not in response.text

    filtered = client.get("/transactions", params={"month": current_month, "dining": "1"})
    assert "1,200円" in filtered.text
    assert "定食屋" in filtered.text
    assert "2,500円" not in filtered.text
    assert "6,000円" not in filtered.text

    filtered = client.get("/transactions", params={"month": current_month, "minimum": "5000"})
    assert "6,000円" in filtered.text
    assert "ゲームセンター" in filtered.text
    assert "1,200円" not in filtered.text

    filtered = client.get(
        "/transactions",
        params={"month": current_month, "category": "食費"},
    )
    assert "3,700円" in filtered.text
    assert "定食屋" in filtered.text
    assert "自炊材料" in filtered.text
    assert "ゲームセンター" not in filtered.text

    previous_month = _local_month_date(web_environment, month_offset=-1).strftime("%Y-%m")
    filtered = client.get("/transactions", params={"month": previous_month})
    assert "9,000円" in filtered.text
    assert "先月店" in filtered.text
    assert "9,700円" not in filtered.text


def test_event_detail_renders_receipt_fields_saved_image_and_copy_status(
    web_environment: WebEnvironment,
    client: TestClient,
):
    occurred_at = _utc_datetime(web_environment)
    event_id = _add_event(
        web_environment,
        "receipt-event",
        occurred_at,
        raw_text="レシート原文",
        expense={
            "amount": 1_280,
            "merchant_raw": "スーパー原文",
            "merchant_normalized": "スーパー",
            "category": "食費",
            "subcategory": "コンビニ",
            "description": "買い物",
            "confidence": 0.88,
        },
    )
    with web_environment.database.session() as session:
        event = session.get(Event, event_id)
        assert event is not None
        event.copied_to_planner_at = occurred_at
        event.expenses[0].receipt_items.extend(
            [
                ReceiptItem(name="おにぎり", quantity=2, unit_price=180, amount=360, order_index=1),
                ReceiptItem(name="お茶", quantity=1, unit_price=160, amount=160, order_index=0),
            ]
        )
        session.commit()
    attachment_id = _add_attachment(
        web_environment,
        event_id,
        filename="receipt.png",
        content_type="image/png",
        local_path=web_environment.settings.data_dir / "attachments" / "receipt.png",
    )

    response = client.get(f"/events/{event_id}")
    assert response.status_code == 200
    assert "レシート原文" in response.text
    assert "スーパー" in response.text
    assert "1,280円" in response.text
    assert "食費 &gt; コンビニ" in response.text
    assert "お茶" in response.text
    assert "おにぎり" in response.text
    assert "360円" in response.text
    assert "転記済み" in response.text
    assert f"/attachments/{attachment_id}" in response.text
    assert "receipt.png" in response.text


def test_accounts_use_finance_balance_semantics_and_transfers_are_not_expenses(
    web_environment: WebEnvironment,
    client: TestClient,
):
    web_environment.finance.create_account("交通系IC", AccountKind.E_MONEY, opening_balance=5_000)
    web_environment.finance.create_account("NISA", AccountKind.INVESTMENT, opening_balance=100_000)
    current = _utc_datetime(web_environment)
    cash_expense = _add_event(
        web_environment,
        "cash-expense",
        current,
        raw_text="現金支出",
        expense={
            "amount": 1_000,
            "merchant_raw": "現金店",
            "merchant_normalized": "現金店",
            "category": "日用品",
            "subcategory": None,
            "description": None,
            "confidence": 0.9,
        },
    )
    card_expense = _add_event(
        web_environment,
        "card-expense",
        current,
        raw_text="カード支出",
        expense={
            "amount": 2_000,
            "merchant_raw": "カード店",
            "merchant_normalized": "カード店",
            "category": "娯楽",
            "subcategory": None,
            "description": None,
            "confidence": 0.9,
        },
    )
    transfer_event = _add_event(
        web_environment,
        "cash-transfer",
        current,
        raw_text="口座から現金へ",
    )
    web_environment.finance.link_expense(cash_expense, "現金")
    web_environment.finance.link_expense(card_expense, "クレジットカード")
    web_environment.finance.record_transfer(
        transfer_event,
        "普通口座",
        "現金",
        3_000,
        "生活費移動",
    )

    response = client.get("/accounts")
    assert response.status_code == 200
    assert "現金" in response.text
    assert "2,000円" in response.text
    assert "クレジットカード" in response.text
    assert 'class="debt">2,000円' in response.text
    assert "交通系IC" in response.text
    assert "5,000円" in response.text
    assert "NISA" in response.text
    assert "投資・NISA" in response.text
    assert "振替" in response.text
    assert "普通口座 → 現金" in response.text
    assert "生活費移動" in response.text

    transactions = client.get("/transactions")
    assert "3,000円" in transactions.text
    assert "現金支出" in transactions.text
    assert "カード支出" in transactions.text
    assert "口座から現金へ" not in transactions.text
    assert "5,000円" not in transactions.text


def test_confirmed_memos_meals_and_rhythm_results_render_real_links(
    web_environment: WebEnvironment,
    client: TestClient,
):
    current = _utc_datetime(web_environment)
    _add_event(
        web_environment,
        "memo-event",
        current,
        raw_text="ひとこと原文",
        memo="原文のメモをそのまま表示",
    )
    meal_event_id = _add_event(
        web_environment,
        "meal-event",
        current,
        raw_text="夕食の記録",
        meal={
            "meal_type": MealType.DINNER.value,
            "restaurant_raw": "ラーメン店",
            "restaurant_normalized": "ラーメン店",
            "description": "味噌ラーメン",
            "outside_home": OutsideHome.OUTSIDE.value,
            "confidence": 0.95,
        },
    )
    meal_attachment_id = _add_attachment(
        web_environment,
        meal_event_id,
        filename="meal.jpg",
        content_type="image/jpeg",
        local_path=web_environment.settings.data_dir / "attachments" / "meal.jpg",
    )
    todo_event_id = _add_event(web_environment, "maimai-todo", current, raw_text="TODO")
    todo = web_environment.rhythm.create_todo(todo_event_id, RhythmGame.MAIMAI, "Lv12をクリア")
    open_todo_event_id = _add_event(web_environment, "ongeki-todo", current, raw_text="TODO2")
    web_environment.rhythm.create_todo(open_todo_event_id, RhythmGame.ONGEKI, "ABFB練習")
    chunithm_todo_event_id = _add_event(
        web_environment, "chunithm-todo", current, raw_text="TODO3"
    )
    web_environment.rhythm.create_todo(chunithm_todo_event_id, RhythmGame.CHUNITHM, "AJ練習")
    result_event_id = _add_event(web_environment, "maimai-result", current, raw_text="RESULT")
    web_environment.rhythm.record_result(
        result_event_id,
        note="達成スコア",
        todo_id=todo.id,
    )
    result_attachment_id = _add_attachment(
        web_environment,
        result_event_id,
        filename="result.png",
        content_type="image/png",
        local_path=web_environment.settings.data_dir / "attachments" / "result.png",
    )

    memo_response = client.get("/memos")
    assert "原文のメモをそのまま表示" in memo_response.text
    meal_response = client.get("/meals")
    assert "ラーメン店" in meal_response.text
    assert "味噌ラーメン" in meal_response.text
    assert "外食" in meal_response.text
    assert f"/attachments/{meal_attachment_id}" in meal_response.text
    rhythm_response = client.get("/rhythm")
    assert "maimai" in rhythm_response.text
    assert "オンゲキ" in rhythm_response.text
    assert "チュウニズム" in rhythm_response.text
    assert "ABFB練習" in rhythm_response.text
    assert "AJ練習" in rhythm_response.text
    assert "達成スコア" in rhythm_response.text
    assert "達成: Lv12をクリア" in rhythm_response.text
    assert f"/attachments/{result_attachment_id}" in rhythm_response.text
    assert "未完了" in rhythm_response.text


def test_rhythm_page_renders_today_progress_images_and_empty_state(
    web_environment: WebEnvironment,
    client: TestClient,
):
    timezone = web_environment.settings.timezone_info()
    local_today = datetime.now(timezone).date()
    local_noon = datetime.combine(
        local_today,
        datetime.min.time().replace(hour=12),
        tzinfo=timezone,
    )
    occurred_at = local_noon.astimezone(UTC).replace(tzinfo=None)
    todo_event_id = _add_event(
        web_environment,
        "today-todo",
        occurred_at,
        raw_text="今日のTODO",
    )
    todo = web_environment.rhythm.create_todo(
        todo_event_id,
        RhythmGame.MAIMAI,
        "今日の曲を詰める",
    )
    result_event_id = _add_event(
        web_environment,
        "today-result",
        occurred_at,
        raw_text="maimai SSS+",
    )
    web_environment.rhythm.record_result(
        result_event_id,
        todo_id=todo.id,
        note="SSS+",
    )
    image_one = _add_attachment(
        web_environment,
        result_event_id,
        filename="today-one.png",
        content_type="image/png",
        local_path=web_environment.settings.data_dir / "attachments" / "today-one.png",
    )
    image_two = _add_attachment(
        web_environment,
        result_event_id,
        filename="today-two.png",
        content_type="image/png",
        local_path=web_environment.settings.data_dir / "attachments" / "today-two.png",
    )

    response = client.get("/rhythm")

    assert response.status_code == 200
    assert "今日の進捗" in response.text
    assert "Asia/Tokyo" in response.text
    assert "maimai" in response.text
    assert "SSS+" in response.text
    assert "達成: 今日の曲を詰める" in response.text
    assert response.text.count(f"/attachments/{image_one}") >= 2
    assert response.text.count(f"/attachments/{image_two}") >= 2


def test_rhythm_status_cards_show_safe_history_and_protected_image(
    web_environment: WebEnvironment,
    client: TestClient,
):
    image_root = web_environment.settings.data_dir / "rhythm-images"
    web_environment.settings.rhythm_image_dir = image_root
    persistence = web_environment.app.state.rhythm_persistence
    user_id = persistence.ensure_user(
        display_name="Webテスト",
        time_zone=web_environment.settings.timezone,
        discord_user_id="web-test-user",
    )
    first_collected = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    _first_snapshot = persistence.save_snapshot(
        user_id=user_id,
        game="maimai",
        player_name="Webテスト",
        rating=Decimal("15420"),
        previous_rating=None,
        semantic_fingerprint="a" * 64,
        source_updated_at=first_collected,
        collected_at=first_collected,
        raw_data={"fixture": True, "provider_html": "<script>secret</script>"},
        entries=[],
    )
    latest_collected = first_collected + timedelta(minutes=1)
    latest_snapshot = persistence.save_snapshot(
        user_id=user_id,
        game="maimai",
        player_name="Webテスト",
        rating=Decimal("15432"),
        previous_rating=Decimal("15420"),
        semantic_fingerprint="b" * 64,
        source_updated_at=latest_collected,
        collected_at=latest_collected,
        raw_data={"source": "fixture"},
        entries=[],
    )
    image_path = image_root / "maimai-fixture.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nweb-fixture")
    persistence.attach_image(
        snapshot_id=latest_snapshot,
        game="maimai",
        image_path=str(image_path),
        image_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
        width=1_200,
        height=675,
        byte_size=image_path.stat().st_size,
        generated_at=latest_collected,
        renderer_version="web-test",
        image_root=str(image_root),
    )
    run = persistence.queue_collection_runs(
        user_id=user_id,
        games=["ongeki"],
        trigger="fixture",
        now=latest_collected,
    )[0]
    persistence.transition_run(
        run_id=run.id,
        to_state="failed",
        event_type="failure",
        message="failure is recorded safely",
        error_code="provider_network_outage",
        error_message="Bearer SECRET <script>must not be shown</script>",
        now=latest_collected + timedelta(minutes=1),
    )

    response = client.get("/rhythm")

    assert response.status_code == 200
    assert "fixture" in response.text
    assert "15,432" not in response.text
    assert "15432" in response.text
    assert "+12" in response.text
    assert "前回" in response.text
    assert f"/rhythm/images/{latest_snapshot}" in response.text
    assert "取得元に接続できませんでした" in response.text
    assert "Bearer SECRET" not in response.text
    assert "must not be shown" not in response.text
    assert "<script>secret</script>" not in response.text
    image = client.get(f"/rhythm/images/{latest_snapshot}")
    assert image.status_code == 200
    assert image.content == b"\x89PNG\r\n\x1a\nweb-fixture"
    assert image.headers["cache-control"] == "no-store"
    assert client.get("/rhythm/images/0").status_code == 404
    other_user_id = persistence.ensure_user(
        display_name="別ユーザー",
        time_zone=web_environment.settings.timezone,
        discord_user_id="other-web-user",
    )
    other_snapshot = persistence.save_snapshot(
        user_id=other_user_id,
        game="chunithm",
        player_name="別ユーザー",
        rating=Decimal("16.00"),
        previous_rating=None,
        semantic_fingerprint="c" * 64,
        source_updated_at=latest_collected,
        collected_at=latest_collected,
        raw_data={"fixture": True},
        entries=[],
    )
    assert client.get(f"/rhythm/images/{other_snapshot}").status_code == 404


def test_rhythm_page_today_progress_has_honest_empty_state(client: TestClient):
    response = client.get("/rhythm")

    assert response.status_code == 200
    assert "今日のリザルトはまだありません" in response.text
    assert "Discordの進捗チャンネルに画像を投稿" in response.text


def test_attachment_route_allows_only_referenced_images_under_safe_root(
    web_environment: WebEnvironment,
    client: TestClient,
):
    event_id = _add_event(
        web_environment,
        "attachment-owner",
        _utc_datetime(web_environment),
        raw_text="添付の親",
    )
    root = web_environment.settings.data_dir / "attachments"
    allowed_id = _add_attachment(
        web_environment,
        event_id,
        filename="allowed.png",
        content_type="image/png",
        local_path=root / "allowed.png",
        data=b"allowed",
    )
    outside = web_environment.settings.data_dir / "outside.png"
    outside.write_bytes(b"outside")
    outside_id = _add_attachment(
        web_environment,
        event_id,
        filename="outside.png",
        content_type="image/png",
        local_path=outside,
    )
    traversal_id = _add_attachment(
        web_environment,
        event_id,
        filename="traversal.png",
        content_type="image/png",
        local_path=root / ".." / "outside.png",
    )
    non_image_id = _add_attachment(
        web_environment,
        event_id,
        filename="note.txt",
        content_type="text/plain",
        local_path=root / "note.txt",
        data=b"not an image",
    )
    missing_path = root / "missing.png"
    missing_id = _add_attachment(
        web_environment,
        event_id,
        filename="missing.png",
        content_type="image/png",
        local_path=missing_path,
    )
    missing_path.unlink()

    allowed = client.get(f"/attachments/{allowed_id}")
    assert allowed.status_code == 200
    assert allowed.content == b"allowed"
    assert allowed.headers["content-type"].startswith("image/png")
    assert allowed.headers["cache-control"] == "no-store"
    for attachment_id in (outside_id, traversal_id, non_image_id, missing_id):
        denied = client.get(f"/attachments/{attachment_id}")
        assert denied.status_code == 404


def test_healthz_is_minimal_and_private_responses_are_not_cached(
    client: TestClient,
):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "data_dir" not in response.text
    assert "database" not in response.text


def test_budgets_page_is_read_only_and_has_useful_empty_state(
    web_environment: WebEnvironment,
    client: TestClient,
):
    response = client.get("/budgets")

    assert response.status_code == 200
    assert "予算" in response.text
    assert "予算は未設定です" in response.text
    assert "#口座" in response.text
    assert "/budget_set" in response.text
    assert 'name="month"' in response.text
    assert 'href="/budgets"' in response.text
    assert client.post("/budgets").status_code == 405

    dashboard = client.get("/")
    assert 'href="/budgets"' in dashboard.text
    assert "未設定" in dashboard.text


def test_budgets_page_renders_query_month_progress_and_confirmed_only_spending(
    web_environment: WebEnvironment,
    client: TestClient,
):
    month = _local_month_date(web_environment).strftime("%Y-%m")
    web_environment.app.state.budget_service.set_budget(10_000, month=month)
    web_environment.app.state.budget_service.set_budget(2_000, month=month, category="食費")
    current = _utc_datetime(web_environment)
    _add_event(
        web_environment,
        "budget-confirmed",
        current,
        raw_text="確定した予算支出",
        expense={
            "amount": 3_000,
            "merchant_raw": "予算店",
            "merchant_normalized": "予算店",
            "category": "食費",
            "subcategory": None,
            "description": None,
            "confidence": 0.9,
        },
    )
    _add_event(
        web_environment,
        "budget-pending",
        current,
        status=EventStatus.PENDING,
        raw_text="保留中なので除外",
        expense={
            "amount": 99_000,
            "merchant_raw": "表示しない店",
            "merchant_normalized": "表示しない店",
            "category": "食費",
            "subcategory": None,
            "description": None,
            "confidence": 0.5,
        },
    )

    response = client.get("/budgets", params={"month": month})

    assert response.status_code == 200
    assert month in response.text
    assert "使用 3,000円" in response.text
    assert "10,000円" in response.text
    assert "残り" in response.text
    assert "7,000円" in response.text
    assert "食費" in response.text
    assert "超過 1,000円" in response.text
    assert "表示しない店" not in response.text
    assert 'role="progressbar"' in response.text
    assert 'aria-valuenow="30.0"' in response.text

    previous_month = _local_month_date(web_environment, month_offset=-1).strftime("%Y-%m")
    previous = client.get("/budgets", params={"month": previous_month})
    assert previous.status_code == 200
    assert previous_month in previous.text
    assert "10,000円" not in previous.text
    assert "予算は未設定です" in previous.text
