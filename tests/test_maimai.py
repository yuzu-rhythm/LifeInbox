from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from life_inbox.analyzer import NullAnalyzer
from life_inbox.cli import build_parser, main
from life_inbox.config import Settings
from life_inbox.db import Database
from life_inbox.discord_bot import LifeInboxBot, LifeInboxCommands
from life_inbox.finance import FinanceService
from life_inbox.maimai import (
    MaimaiMyBestService,
    MaimaiNetFavoriteService,
    MaimaiSnapshotImportService,
)
from life_inbox.models import MaimaiMyBestEntry, MaimaiNetFavoriteEntry, RhythmFavorite
from life_inbox.rhythm import RhythmService
from life_inbox.services import InboxService
from life_inbox.web import create_web_app


def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'app.db'}")
    database.init()
    return database


def _bundle(
    *,
    fetched_at: str = "2026-08-29T12:00:00+09:00",
    favorites: list[dict[str, object]] | None = None,
    my_best: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source": "maimai_net",
        "fetched_at": fetched_at,
        "favorites": favorites
        if favorites is not None
        else [
            {
                "rank": 1,
                "category": "カテゴリA",
                "title": "公式お気に入り曲",
                "artist": "アーティストA",
            },
            {
                "rank": 2,
                "category": "カテゴリB",
                "title": "公式お気に入り曲B",
                "artist": "アーティストB",
            },
        ],
        "my_best": my_best
        if my_best is not None
        else [
            {"rank": 1, "title": "プレイ回数曲", "play_count": 22},
            {"rank": 2, "title": "プレイ回数曲B", "play_count": 8},
        ],
    }


def _favorite_titles(database: Database) -> list[str]:
    return [
        row.title
        for row in MaimaiNetFavoriteService(database.session_factory).list_current()
    ]


def _my_best_titles(database: Database) -> list[str]:
    return [row.title for row in MaimaiMyBestService(database.session_factory).list_current()]


def test_bundle_imports_separate_lists_and_is_idempotent(tmp_path: Path):
    database = _database(tmp_path)
    importer = MaimaiSnapshotImportService(database.session_factory)
    payload = _bundle()

    first = importer.import_bundle(payload)
    repeated = importer.import_bundle(payload)

    assert first.favorites_count == 2
    assert first.my_best_count == 2
    assert first.changed
    assert not repeated.changed
    assert repeated.favorites_count == 2
    assert repeated.my_best_count == 2
    assert [
        (row.rank, row.title, row.artist, row.source)
        for row in MaimaiNetFavoriteService(database.session_factory).list_current()
    ] == [
        (1, "公式お気に入り曲", "アーティストA", "maimai_net"),
        (2, "公式お気に入り曲B", "アーティストB", "maimai_net"),
    ]
    assert [
        (row.rank, row.title, row.play_count, row.source)
        for row in MaimaiMyBestService(database.session_factory).list_current()
    ] == [
        (1, "プレイ回数曲", 22, "maimai_net"),
        (2, "プレイ回数曲B", 8, "maimai_net"),
    ]
    with database.session() as session:
        assert session.scalar(select(func.count(MaimaiNetFavoriteEntry.id))) == 2
        assert session.scalar(select(func.count(MaimaiMyBestEntry.id))) == 2


def test_non_empty_updates_keep_old_rows_inactive(tmp_path: Path):
    database = _database(tmp_path)
    importer = MaimaiSnapshotImportService(database.session_factory)
    importer.import_bundle(_bundle())

    importer.import_bundle(
        _bundle(
            fetched_at="2026-08-30T12:00:00+09:00",
            favorites=[
                {
                    "rank": 1,
                    "category": "新カテゴリ",
                    "title": "新しい公式曲",
                    "artist": "新アーティスト",
                }
            ],
            my_best=[{"rank": 1, "title": "新しいMY BEST曲", "play_count": 30}],
        )
    )

    assert _favorite_titles(database) == ["新しい公式曲"]
    assert _my_best_titles(database) == ["新しいMY BEST曲"]
    with database.session() as session:
        old_favorites = session.scalars(
            select(MaimaiNetFavoriteEntry).where(MaimaiNetFavoriteEntry.title == "公式お気に入り曲")
        ).all()
        old_my_best = session.scalars(
            select(MaimaiMyBestEntry).where(MaimaiMyBestEntry.title == "プレイ回数曲")
        ).all()
        assert len(old_favorites) == 1 and not old_favorites[0].active
        assert len(old_my_best) == 1 and not old_my_best[0].active


def test_empty_one_side_is_noop_for_that_side_and_both_empty_preserves_data(tmp_path: Path):
    database = _database(tmp_path)
    importer = MaimaiSnapshotImportService(database.session_factory)
    importer.import_bundle(_bundle())

    summary = importer.import_bundle(
        _bundle(
            fetched_at="2026-08-31T12:00:00+09:00",
            favorites=[],
            my_best=[{"rank": 1, "title": "片側だけ更新", "play_count": 4}],
        )
    )
    assert summary.favorites_count == 0
    assert summary.my_best_count == 1
    assert _favorite_titles(database) == ["公式お気に入り曲", "公式お気に入り曲B"]
    assert _my_best_titles(database) == ["片側だけ更新"]

    empty = importer.import_bundle(_bundle(favorites=[], my_best=[]))
    assert empty.no_op
    assert not empty.changed
    assert _favorite_titles(database) == ["公式お気に入り曲", "公式お気に入り曲B"]
    assert _my_best_titles(database) == ["片側だけ更新"]


def test_invalid_bundle_is_rejected_before_either_section_changes(tmp_path: Path):
    database = _database(tmp_path)
    importer = MaimaiSnapshotImportService(database.session_factory)
    importer.import_bundle(_bundle())
    invalid = _bundle(
        fetched_at="2026-09-01T12:00:00+09:00",
        favorites=[
            {
                "rank": 1,
                "category": "新カテゴリ",
                "title": "不正と同時に更新されない曲",
                "artist": "新アーティスト",
            }
        ],
        my_best=[
            {"rank": 1, "title": "不正な重複", "play_count": 2},
            {"rank": 1, "title": "重複", "play_count": 1},
        ],
    )

    with pytest.raises(ValueError):
        importer.import_bundle(invalid)

    assert _favorite_titles(database) == ["公式お気に入り曲", "公式お気に入り曲B"]
    assert _my_best_titles(database) == ["プレイ回数曲", "プレイ回数曲B"]


def test_manual_favorite_does_not_collide_with_official_snapshot(tmp_path: Path):
    database = _database(tmp_path)
    rhythm = RhythmService(database.session_factory)
    rhythm.add_favorite("maimai", "手動お気に入り曲")
    MaimaiNetFavoriteService(database.session_factory).import_snapshot(
        [
            {
                "rank": 1,
                "category": "カテゴリ",
                "title": "公式お気に入り曲",
                "artist": "アーティスト",
            }
        ],
        fetched_at=datetime(2026, 8, 29, 3, 0, tzinfo=UTC),
    )

    assert [
        row.song_title for row in rhythm.list_favorites("maimai") if row.source == "manual"
    ] == ["手動お気に入り曲"]
    assert _favorite_titles(database) == ["公式お気に入り曲"]
    with database.session() as session:
        assert session.scalar(select(func.count(RhythmFavorite.id))) == 1
        assert session.scalar(select(func.count(MaimaiNetFavoriteEntry.id))) == 1


def test_cli_imports_one_bundle_and_does_not_print_song_titles(tmp_path: Path, capsys):
    snapshot_path = tmp_path / "maimai-snapshot.json"
    snapshot_path.write_text(json.dumps(_bundle(), ensure_ascii=False), encoding="utf-8")
    database_path = tmp_path / "cli.db"

    args = build_parser().parse_args(
        [
            "import-maimai-snapshots",
            str(snapshot_path),
            "--no-env-file",
            "--database-url",
            f"sqlite:///{database_path}",
        ]
    )
    assert args.command == "import-maimai-snapshots"
    assert main(
        [
            "import-maimai-snapshots",
            str(snapshot_path),
            "--no-env-file",
            "--database-url",
            f"sqlite:///{database_path}",
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "公式お気に入り 2件" in output
    assert "MY BEST 2件" in output
    assert "公式お気に入り曲" not in output


def test_cli_rejects_both_empty_without_mutating_and_has_compatibility_alias(
    tmp_path: Path, capsys
):
    snapshot_path = tmp_path / "empty.json"
    snapshot_path.write_text(
        json.dumps(_bundle(favorites=[], my_best=[]), ensure_ascii=False),
        encoding="utf-8",
    )
    database_path = tmp_path / "empty.db"

    alias_args = build_parser().parse_args(["import-maimai-mybest", str(snapshot_path)])
    assert alias_args.command == "import-maimai-mybest"
    result = main(
        [
            "import-maimai-mybest",
            str(snapshot_path),
            "--no-env-file",
            "--database-url",
            f"sqlite:///{database_path}",
        ]
    )
    captured = capsys.readouterr()
    assert result == 1
    assert "両方とも空" in captured.err
    assert "公式お気に入り曲" not in captured.out + captured.err


def _interaction(*, channel_id: int):
    response = SimpleNamespace(
        is_done=MagicMock(return_value=False),
        send_message=AsyncMock(),
    )
    return SimpleNamespace(
        user=SimpleNamespace(id=10),
        guild_id=20,
        channel_id=channel_id,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_discord_commands_separate_official_favorites_and_my_best_ephemerally(
    tmp_path: Path,
):
    database = _database(tmp_path)
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        records_channel_id=40,
        rhythm_channel_id=70,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
        timezone="Asia/Tokyo",
    )
    bot = LifeInboxBot(settings, InboxService(database.session_factory, settings, NullAnalyzer()))
    MaimaiSnapshotImportService(database.session_factory).import_bundle(_bundle())
    cog = LifeInboxCommands(bot)

    try:
        favorite_interaction = _interaction(channel_id=40)
        await LifeInboxCommands.maimai_net_favorites.callback(cog, favorite_interaction)
        favorite_text = favorite_interaction.response.send_message.await_args.args[0]
        assert "公式 maimai NET お気に入り" in favorite_text
        assert "1位" in favorite_text
        assert "公式お気に入り曲" in favorite_text
        assert "2026-08-29 12:00:00" in favorite_text
        assert favorite_interaction.response.send_message.await_args.kwargs["ephemeral"] is True

        my_best_interaction = _interaction(channel_id=40)
        await LifeInboxCommands.maimai_mybest.callback(cog, my_best_interaction)
        my_best_text = my_best_interaction.response.send_message.await_args.args[0]
        assert "maimai MY BEST" in my_best_text
        assert "プレイ回数順" in my_best_text
        assert "22回" in my_best_text
        assert "プレイ回数曲" in my_best_text
        assert "公式お気に入り曲" not in my_best_text
        assert my_best_interaction.response.send_message.await_args.kwargs["ephemeral"] is True
    finally:
        await bot.close()


def test_web_separates_manual_official_favorites_and_my_best(tmp_path: Path):
    database = _database(tmp_path)
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
        timezone="Asia/Tokyo",
    )
    finance = FinanceService(database.session_factory)
    rhythm = RhythmService(database.session_factory)
    rhythm.add_favorite("maimai", "手動お気に入り曲", "手動メモ")
    MaimaiSnapshotImportService(database.session_factory).import_bundle(_bundle())
    app = create_web_app(settings, database=database, finance=finance, rhythm=rhythm)

    with TestClient(app) as client:
        response = client.get("/rhythm")

    assert response.status_code == 200
    assert "手動お気に入り" in response.text
    assert "公式NETお気に入り" in response.text
    assert "MY BEST" in response.text
    assert "手動お気に入り曲" in response.text
    assert "手動メモ" in response.text
    assert "公式お気に入り曲" in response.text
    assert "アーティストA" in response.text
    assert "プレイ回数曲" in response.text
    assert "22回" in response.text
    assert "自動同期ではありません" in response.text
