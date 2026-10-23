from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from life_inbox.rhythm_models import DailyGameSpendingRow, RhythmUserRow


def test_additive_rhythm_schema_is_registered_with_existing_database(db):
    table_names = set(inspect(db.engine).get_table_names())

    assert {
        "rhythm_users",
        "rhythm_provider_accounts",
        "rating_snapshots",
        "rating_entries",
        "rating_images",
        "rating_snapshot_images",
        "discord_deliveries",
        "daily_game_spending",
        "collection_runs",
        "collection_run_events",
        "collection_leases",
    }.issubset(table_names)


def test_spending_schema_accepts_yen_and_rejects_mismatched_total(db):
    with db.session() as session:
        user = RhythmUserRow(
            display_name="テスト",
            time_zone="Asia/Tokyo",
            discord_user_id="rhythm-schema-user",
        )
        session.add(user)
        session.flush()
        session.add(
            DailyGameSpendingRow(
                user_id=user.id,
                local_date=date(2026, 8, 29),
                time_zone="Asia/Tokyo",
                chunithm_amount_yen=500,
                maimai_amount_yen=300,
                ongeki_amount_yen=400,
                total_amount_yen=1_200,
                revision=1,
                synced_at=datetime(2026, 8, 29, 12, 0),
            )
        )
        session.commit()

    with pytest.raises(IntegrityError):
        with db.session() as session:
            user = session.query(RhythmUserRow).one()
            session.add(
                DailyGameSpendingRow(
                    user_id=user.id,
                    local_date=date(2026, 8, 30),
                    time_zone="Asia/Tokyo",
                    chunithm_amount_yen=100,
                    maimai_amount_yen=0,
                    ongeki_amount_yen=0,
                    total_amount_yen=1,
                    revision=1,
                    synced_at=datetime(2026, 8, 30, 12, 0),
                )
            )
            session.commit()


def test_provider_schema_has_only_a_secret_reference_not_secret_value(db):
    columns = {column["name"] for column in inspect(db.engine).get_columns(
        "rhythm_provider_accounts"
    )}

    assert "encrypted_credentials_reference" in columns
    assert "password" not in columns
    assert "cookie" not in columns
    assert "access_token" not in columns
