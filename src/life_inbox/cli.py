"""Local CLI entry points: database, import, backup, export, and app commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from .archive_export import ArchiveExportError, ArchiveExportService
from .archive_health import ArchiveHealthService
from .archive_ingest import ArchiveIngestError, ArchiveIngestService
from .backup import BackupError, BackupService
from .config import Settings
from .db import Database
from .export import ExportError, ExportService
from .maimai import MaimaiSnapshotImportService
from .repositories import AliasRepository


def _settings(args: argparse.Namespace) -> Settings:
    kwargs = {}
    if args.database_url:
        kwargs["database_url"] = args.database_url
    if args.data_dir:
        kwargs["data_dir"] = Path(args.data_dir)
    return Settings(_env_file=None if args.no_env_file else (".env", ".env.local"), **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="life-inbox", description="Discord生活Inbox Bot CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("init-db", "SQLiteテーブルを初期化"),
        ("check", "秘密値を表示せず設定を確認"),
        ("doctor", "DB・Web・バックアップ・設定を秘密値なしで診断"),
        ("backup", "SQLiteと添付ファイルを安全にバックアップ"),
        ("archive-ingest", "ローカルfixtureをLife Archiveへ取り込む（外部通信なし）"),
        ("archive-retry", "失敗・隔離済みのLife Archive配送をローカル再試行"),
        ("archive-health", "Life Archiveの同期ヘルスを表示"),
        ("archive-export", "Life Archiveを完全エクスポートして再検証"),
        ("export-csv", "家計データをCSV ZIPに安全にエクスポート"),
        (
            "import-maimai-snapshots",
            "サニタイズ済みmaimai NETお気に入り・MY BEST JSONを取り込む",
        ),
        (
            "import-legacy-rhythm-app",
            "旧ArcadeRhythmLog JSONを検査し、明示時だけSQLiteへ冪等移行",
        ),
        ("bot", "Discord Botを起動"),
        ("web", "ローカル生活マイページを起動"),
        (
            "provision-command-guide",
            "生活サーバーへBotコマンド一覧チャンネルを冪等に作成・更新",
        ),
        (
            "provision-channel-guides",
            "生活サーバーの既存テキストチャンネルへ利用案内を冪等に投稿・更新・固定",
        ),
        (
            "provision-schedule-timeline",
            "生活サーバーへ現在設定の定期更新タイムラインを冪等に投稿・更新・固定",
        ),
        (
            "device-sync-cloud",
            "iPhone音ゲー利用額の公開HTTPS同期Ingressを起動",
        ),
    ):
        aliases = ["import-maimai-mybest"] if name == "import-maimai-snapshots" else []
        sub = subparsers.add_parser(name, aliases=aliases, help=help_text)
        sub.add_argument("--no-env-file", action="store_true", help=".env.local/.envを読まない")
        sub.add_argument("--database-url", default=None)
        sub.add_argument("--data-dir", default=None)
        if name == "backup":
            sub.add_argument("--output-dir", default=None)
        if name == "archive-ingest":
            sub.add_argument("fixture_path", help="ローカルJSON fixtureのパス")
            sub.add_argument(
                "--continue-on-error",
                action="store_true",
                help="個別entryの検証失敗後も次のentryを処理",
            )
        if name == "archive-retry":
            sub.add_argument("identifier", help="Archive IDまたは内部entry ID")
        if name == "archive-export":
            sub.add_argument("--output-dir", default=None)
        if name == "export-csv":
            sub.add_argument("--month", default=None, help="対象月（YYYY-MM）")
            sub.add_argument("--output-dir", default=None)
        if name == "import-maimai-snapshots":
            sub.add_argument("json_path", help="サニタイズ済みスナップショットJSONのパス")
        if name == "import-legacy-rhythm-app":
            sub.add_argument("json_path", help="旧アプリのdata.jsonパス")
            sub.add_argument(
                "--apply",
                action="store_true",
                help="検査だけでなくSQLite、Discord配送キュー、Notion outboxへ反映",
            )
            sub.add_argument(
                "--no-notion",
                action="store_true",
                help="適用時もNotion outboxへ追加しない",
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "device-sync-cloud":
        # This command deliberately bypasses the normal local .env loader.
        # The Cloud Run entry point reads only explicit process environment
        # values and refuses unsafe persistence configuration.
        from .device_sync_cloud import (
            DeviceSyncConfigurationError,
            run_device_sync_cloud,
        )

        try:
            run_device_sync_cloud()
        except DeviceSyncConfigurationError as exc:
            print(f"Device sync configuration error: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 - keep startup output secret-safe
            print(
                f"Device sync startup error: {type(exc).__name__}",
                file=sys.stderr,
            )
            return 1
        return 0

    try:
        settings = _settings(args)
    except (ValidationError, ValueError) as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        return 2

    if args.command == "init-db":
        database = Database(settings.database_url)
        database.init()
        from .finance_digest import init_storage as init_finance_digest_storage

        init_finance_digest_storage(database.session_factory)
        with database.session() as session:
            AliasRepository(session).seed_defaults()
            session.commit()
        from .finance import FinanceService

        FinanceService(database.session_factory).ensure_default_accounts()
        print(f"DB initialized: {settings.safe_summary()['database_url']}")
        return 0

    if args.command == "check":
        print(json.dumps(settings.safe_summary(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "doctor":
        from .operations import run_operations_check

        report = run_operations_check(settings)
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return 0 if report.healthy else 1

    if args.command == "backup":
        try:
            result = BackupService(
                settings.database_url,
                settings.data_dir,
                output_dir=args.output_dir,
            ).create_backup()
        except BackupError as exc:
            print(f"バックアップエラー: {exc}", file=sys.stderr)
            return 1
        print(f"Backup archive: {result.archive_path}")
        archive_suffix = (
            f"; {result.archive_media_count} archive media file(s)"
            if result.archive_media_count
            else ""
        )
        print(
            f"Summary: {result.attachment_count} attachment file(s)"
            f"{archive_suffix}; database checks passed"
        )
        return 0

    if args.command == "archive-ingest":
        database = Database(settings.database_url)
        database.init()
        try:
            summary = ArchiveIngestService(
                database.session_factory,
                data_dir=settings.data_dir,
                max_media_bytes=settings.attachment_max_bytes,
            ).ingest_fixture_file(
                args.fixture_path,
                continue_on_error=args.continue_on_error,
            )
        except (ArchiveIngestError, OSError, ValueError) as exc:
            print(f"Life Archive取込エラー: {type(exc).__name__}", file=sys.stderr)
            return 1
        print(
            "Life Archive取込完了: "
            f"処理 {summary.imported_count}件 / エラー {summary.failed_count}件"
        )
        return 1 if summary.errors else 0

    if args.command == "archive-retry":
        database = Database(settings.database_url)
        database.init()
        identifier: int | str = args.identifier
        if args.identifier.isdecimal():
            identifier = int(args.identifier)
        try:
            result = ArchiveIngestService(
                database.session_factory,
                data_dir=settings.data_dir,
                max_media_bytes=settings.attachment_max_bytes,
            ).retry(identifier)
        except (ArchiveIngestError, OSError, ValueError) as exc:
            print(f"Life Archive再試行エラー: {type(exc).__name__}", file=sys.stderr)
            return 1
        print(
            "Life Archive再試行完了: "
            f"revision {result.revision} / status {result.status} / "
            f"quarantine {result.quarantined_attachment_count}件"
        )
        return 0

    if args.command == "archive-health":
        database = Database(settings.database_url)
        database.init()
        report = ArchiveHealthService(
            database.session_factory,
            data_dir=settings.data_dir,
        ).health()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.command == "archive-export":
        database = Database(settings.database_url)
        database.init()
        try:
            result = ArchiveExportService(
                database.session_factory,
                data_dir=settings.data_dir,
                output_dir=args.output_dir,
            ).create_export()
        except ArchiveExportError as exc:
            print(f"Life Archiveエクスポートエラー: {type(exc).__name__}", file=sys.stderr)
            return 1
        print(f"Life Archive export: {result.archive_path}")
        print(
            "Summary: "
            f"{result.entry_count} entry(s), {result.original_count} verified original(s); "
            "checksums and manifest counts passed"
        )
        return 0

    if args.command == "export-csv":
        try:
            result = ExportService(
                settings.database_url,
                settings.data_dir,
                timezone=settings.timezone,
                output_dir=args.output_dir,
            ).create_export(month=args.month)
        except ExportError as exc:
            print(f"エクスポートエラー: {exc}", file=sys.stderr)
            return 1
        print(f"CSV export archive: {result.archive_path}")
        print(
            "Summary: "
            f"{result.row_counts.get('expenses.csv', 0)} expense row(s); "
            f"{result.row_counts.get('account_transactions.csv', 0)} transaction row(s)"
        )
        return 0

    if args.command in {"import-maimai-snapshots", "import-maimai-mybest"}:
        database = Database(settings.database_url)
        database.init()
        try:
            summary = MaimaiSnapshotImportService(database.session_factory).import_json_file(
                args.json_path
            )
        except (OSError, ValueError):
            # Do not echo validation details or input JSON.  In particular,
            # an accidentally supplied credential-shaped field must never be
            # copied to a terminal/log by this command.
            print(
                "maimaiスナップショット取込エラー: JSON形式を確認してください。"
                "既存データは変更していません。",
                file=sys.stderr,
            )
            return 1
        if summary.no_op:
            print(
                "maimaiスナップショットは両方とも空のため、取り込みませんでした。既存データは変更していません。",
                file=sys.stderr,
            )
            return 1
        print(
            "maimaiスナップショット取込完了: "
            f"公式お気に入り {summary.favorites_count}件、"
            f"MY BEST {summary.my_best_count}件"
        )
        return 0

    if args.command == "import-legacy-rhythm-app":
        from .legacy_rhythm_import import (
            LegacyRhythmAppImporter,
            LegacyRhythmImportError,
            LegacyRhythmImportResult,
        )
        from .notion_sync import NotionSyncService

        database = None
        notion_sync = None
        if args.apply:
            database = Database(settings.database_url)
            database.init()
            if settings.notion_sync_enabled and not args.no_notion:
                notion_sync = NotionSyncService(
                    database.session_factory,
                    settings=settings,
                    data_dir=settings.data_dir,
                )
        try:
            if database is None:
                result = LegacyRhythmImportResult(
                    plan=LegacyRhythmAppImporter.plan_file(args.json_path),
                    applied=False,
                )
            else:
                result = LegacyRhythmAppImporter(database.session_factory).import_file(
                    args.json_path,
                    apply=True,
                    display_name=settings.rhythm_player_display_name,
                    discord_user_id=settings.allowed_user_id,
                    source_user_id=settings.rhythm_player_display_name,
                    time_zone=settings.rhythm_time_zone,
                    notion_sync=notion_sync,
                )
        except (LegacyRhythmImportError, OSError, ValueError):
            print(
                "旧音ゲーアプリ取込エラー: JSON形式または既存revisionを確認してください。"
                "旧ファイルは変更していません。",
                file=sys.stderr,
            )
            return 1
        totals = dict(result.plan.totals_by_game)
        mode = "適用" if result.applied else "dry-run"
        print(
            f"旧音ゲーアプリ取込 {mode}: "
            f"検出 {result.plan.scanned_credit_count}件 / "
            f"対象 {result.plan.supported_credit_count}件 / "
            f"対象外保持 {result.plan.unsupported_credit_count}件 / "
            f"合計 ¥{result.plan.total_yen:,}"
        )
        print(
            "機種別: "
            f"ウニ ¥{totals.get('chunithm', 0):,} / "
            f"マイ ¥{totals.get('maimai', 0):,} / "
            f"ゲキ ¥{totals.get('ongeki', 0):,}"
        )
        if not result.applied:
            print("dry-runのためSQLite・Discord・Notionは変更していません。")
            return 0
        print(
            "SQLite反映: "
            f"新規 {result.spending_created_count}件 / "
            f"更新 {result.spending_updated_count}件 / "
            f"変更なし {result.spending_idempotent_count}件 / "
            f"Life Archive Event新規 {result.archive_event_created_count}件"
        )
        print(
            "Notion outbox: "
            f"対象 {result.notion_queued_count}件 / "
            f"失敗 {result.notion_queue_failed_count}件"
        )
        return 1 if result.notion_queue_failed_count else 0

    if args.command == "provision-command-guide":
        from .command_guide import provision_command_guide

        try:
            result = provision_command_guide(settings)
        except (RuntimeError, ValueError) as exc:
            print(f"コマンド案内エラー: {exc}", file=sys.stderr)
            return 1
        action = "作成" if result.channel_created else "更新"
        print(
            f"コマンド案内チャンネルを{action}しました: "
            f"#{result.channel_name} ({result.channel_id}) / "
            f"新規メッセージ {result.messages_created}件 / 更新 {result.messages_updated}件"
        )
        return 0

    if args.command == "provision-channel-guides":
        from .channel_guides import provision_channel_guides

        try:
            result = provision_channel_guides(settings)
        except (RuntimeError, ValueError) as exc:
            print(f"チャンネル利用案内エラー: {exc}", file=sys.stderr)
            return 1
        print(
            "チャンネル利用案内を処理しました: "
            f"既知 {result.known_channels}件 / 発見 {result.channels_found}件 / "
            f"処理済み {result.channels_processed}件 / 未存在 {result.channels_missing}件 / "
            f"安全スキップ・失敗 {result.channels_failed}件 / "
            f"新規 {result.messages_created}件 / 更新 {result.messages_updated}件 / "
            f"変更なし {result.messages_unchanged}件 / "
            f"固定 {result.messages_pinned}件 / 固定スキップ {result.messages_pin_skipped}件"
        )
        if result.missing_channel_names:
            print(
                "未存在のため変更しなかったチャンネル: "
                + ", ".join(f"#{name}" for name in result.missing_channel_names)
            )
        if result.duplicate_channel_names:
            print(
                "同名チャンネルのため安全のため変更しなかったチャンネル: "
                + ", ".join(f"#{name}" for name in result.duplicate_channel_names),
                file=sys.stderr,
            )
        if result.failed_channels:
            print(
                "処理できなかったチャンネル: "
                + ", ".join(f"#{failure.channel_name}" for failure in result.failed_channels),
                file=sys.stderr,
            )
        return 1 if result.has_failures else 0

    if args.command == "provision-schedule-timeline":
        from .schedule_timeline import provision_schedule_timeline

        try:
            result = provision_schedule_timeline(settings)
        except (RuntimeError, ValueError) as exc:
            print(f"定期更新タイムラインエラー: {exc}", file=sys.stderr)
            return 1
        action = "新規投稿" if result.created else "更新" if result.updated else "変更なし"
        pin_state = "固定" if result.pinned else "固定済み・スキップ"
        print(
            "定期更新タイムラインを処理しました: "
            f"{action} / {pin_state} / "
            f"重複管理メッセージ {result.duplicate_managed_messages}件"
        )
        return 0

    try:
        if args.command == "web":
            from .web import run_web

            run_web(settings)
        else:
            # Keep init-db/check/web usable in a local environment that has
            # not configured Discord. Discord credentials are needed only here.
            from .discord_bot import run_bot

            run_bot(settings)
    except (ValidationError, ValueError) as exc:
        print(f"起動設定エラー: {exc}", file=sys.stderr)
        return 2
    return 0
