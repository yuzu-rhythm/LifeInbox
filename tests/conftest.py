from __future__ import annotations

import pytest

from life_inbox.analyzer import NullAnalyzer
from life_inbox.config import Settings
from life_inbox.db import Database
from life_inbox.services import InboxService


@pytest.fixture
def db(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'app.db'}")
    database.init()
    return database


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
        rhythm_browser_session_dir=tmp_path / "rhythm-sessions",
        rhythm_image_dir=tmp_path / "rhythm-images",
        timezone="Asia/Tokyo",
        max_expense_yen=1_000_000,
    )


@pytest.fixture
def service(db, settings):
    return InboxService(db.session_factory, settings, NullAnalyzer())
