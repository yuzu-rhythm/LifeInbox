from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from life_inbox.legacy_rhythm_import import (
    LegacyRhythmAppImporter,
    LegacyRhythmImportError,
)
from life_inbox.models import Event
from life_inbox.notion_models import NotionSyncRecord
from life_inbox.notion_sync import NotionSyncService
from life_inbox.rhythm_spending_models import (
    RhythmSpendingEventLink,
    RhythmSpendingEventRow,
)


def _write_legacy_file(path: Path) -> None:
    games = {
        "chunithm": "10000000-0000-0000-0000-000000000001",
        "maimai": "10000000-0000-0000-0000-000000000002",
        "ongeki": "10000000-0000-0000-0000-000000000003",
        "sdvx": "10000000-0000-0000-0000-000000000004",
    }
    payload = {
        "games": [
            {"id": games["chunithm"], "name": "CHUNITHM", "shortName": "ウニ"},
            {"id": games["maimai"], "name": "maimai", "shortName": "マイ"},
            {"id": games["ongeki"], "name": "オンゲキ", "shortName": "ゲキ"},
            {"id": games["sdvx"], "name": "SOUND VOLTEX", "shortName": "SDVX"},
        ],
        "stores": [],
        "cabinets": [],
        "charts": [],
        "sessions": [
            {
                "id": "20000000-0000-0000-0000-000000000001",
                "date": "2026-08-30T09:00:00+09:00",
                "memo": "この私的メモは移行先へコピーしない",
                "credits": [
                    {
                        "id": "30000000-0000-0000-0000-000000000001",
                        "amountJPY": 100,
                        "createdAt": "2026-08-30T09:01:00+09:00",
                        "gameID": games["chunithm"],
                        "memo": "秘密のクレジットメモ",
                        "songPlays": [],
                    },
                    {
                        "id": "30000000-0000-0000-0000-000000000002",
                        "amountJPY": 600,
                        "createdAt": "2026-08-30T10:02:00+09:00",
                        "gameID": games["maimai"],
                        "memo": "コピーしない",
                        "songPlays": [],
                    },
                    {
                        "id": "30000000-0000-0000-0000-000000000003",
                        "amountJPY": 100,
                        "createdAt": "2026-08-30T11:03:00+09:00",
                        "gameID": games["sdvx"],
                        "memo": "対象外も元ファイルには残す",
                        "songPlays": [],
                    },
                ],
            }
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_legacy_rhythm_plan_is_private_and_non_destructive(db, tmp_path: Path):
    source = tmp_path / "data.json"
    _write_legacy_file(source)
    before = _sha256(source)

    plan = LegacyRhythmAppImporter(db.session_factory).plan_file(source)

    assert _sha256(source) == before
    assert plan.scanned_credit_count == 3
    assert plan.supported_credit_count == 2
    assert plan.unsupported_credit_count == 1
    assert plan.total_yen == 700
    assert dict(plan.totals_by_game) == {
        "chunithm": 100,
        "maimai": 600,
        "ongeki": 0,
    }
    assert [credit.game for credit in plan.credits] == ["chunithm", "maimai"]
    assert "秘密" not in repr(plan)
    with db.session() as session:
        assert session.scalar(select(func.count(RhythmSpendingEventRow.id))) == 0


def test_legacy_rhythm_apply_links_spending_archive_and_notion_idempotently(
    db,
    tmp_path: Path,
):
    source = tmp_path / "data.json"
    _write_legacy_file(source)
    before = _sha256(source)
    importer = LegacyRhythmAppImporter(db.session_factory)
    notion = NotionSyncService(db.session_factory, data_dir=tmp_path)

    first = importer.import_file(
        source,
        apply=True,
        display_name="TEST",
        discord_user_id="42",
        source_user_id="tester",
        notion_sync=notion,
    )
    second = importer.import_file(
        source,
        apply=True,
        display_name="TEST",
        discord_user_id="42",
        source_user_id="tester",
        notion_sync=notion,
    )

    assert _sha256(source) == before
    assert first.spending_created_count == 2
    assert first.archive_event_created_count == 2
    assert first.notion_queued_count == 2
    assert first.notion_queue_failed_count == 0
    assert second.spending_created_count == 0
    assert second.spending_idempotent_count == 2
    assert second.archive_event_created_count == 0
    with db.session() as session:
        spending = session.scalars(
            select(RhythmSpendingEventRow).order_by(RhythmSpendingEventRow.occurred_at)
        ).all()
        events = session.scalars(
            select(Event).where(Event.source == "legacy_rhythm_app")
        ).all()
        links = session.scalars(select(RhythmSpendingEventLink)).all()
        notion_rows = session.scalars(select(NotionSyncRecord)).all()

        assert [(row.game, row.amount_yen, row.revision) for row in spending] == [
            ("chunithm", 100, 1),
            ("maimai", 600, 1),
        ]
        assert len(events) == len(links) == len(notion_rows) == 2
        assert {event.event_type for event in events} == {"rhythm_spending"}
        assert {event.raw_text for event in events} == {"ウニ ¥100", "マイ ¥600"}
        assert all("秘密" not in event.raw_text for event in events)
        for event in events:
            plan = notion.plan_event(event)
            assert plan.eligible is True
            assert plan.draft is not None
            assert '"amount_yen"' in plan.draft.body_markdown


def test_legacy_rhythm_utc_timestamp_is_converted_to_jst_for_local_date(db, tmp_path: Path):
    source = tmp_path / "data.json"
    _write_legacy_file(source)
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["sessions"][0]["credits"][0]["createdAt"] = "2026-08-30T15:30:00Z"
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = LegacyRhythmAppImporter(db.session_factory).import_file(
        source,
        apply=True,
        display_name="TEST",
        discord_user_id="42",
        source_user_id="tester",
        time_zone="Asia/Tokyo",
    )

    assert result.spending_created_count == 2
    with db.session() as session:
        row = session.scalar(
            select(RhythmSpendingEventRow).where(
                RhythmSpendingEventRow.event_id
                == "30000000-0000-0000-0000-000000000001"
            )
        )
        assert row is not None
        assert row.occurred_at.isoformat() == "2026-08-30T15:30:00"
        assert row.local_date.isoformat() == "2026-08-31"


def test_legacy_rhythm_invalid_source_fails_before_destination_write(db, tmp_path: Path):
    source = tmp_path / "invalid.json"
    source.write_text('{"games": [], "sessions": [{"credits": "not-an-array"}]}')
    importer = LegacyRhythmAppImporter(db.session_factory)

    with pytest.raises(LegacyRhythmImportError):
        importer.import_file(
            source,
            apply=True,
            display_name="TEST",
            discord_user_id="42",
            source_user_id="tester",
        )

    with db.session() as session:
        assert session.scalar(select(func.count(RhythmSpendingEventRow.id))) == 0
        assert session.scalar(select(func.count(Event.id))) == 0
