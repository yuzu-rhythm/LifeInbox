"""SQLite/SQLAlchemy setup."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from . import archive_models as _archive_models  # noqa: F401  # register additive tables
from . import health_models as _health_models  # noqa: F401  # register additive tables
from . import kiseki_models as _kiseki_models  # noqa: F401  # register additive tables
from . import notion_models as _notion_models  # noqa: F401  # register additive tables
from . import rhythm_models as _rhythm_models  # noqa: F401  # register additive tables
from . import rhythm_spending_models as _rhythm_spending_models  # noqa: F401
from . import social_models as _social_models  # noqa: F401  # register additive tables
from .models import Base


def _sqlite_file_from_url(database_url: str) -> Path | None:
    if not database_url.startswith("sqlite"):
        return None
    if database_url.endswith(":memory:"):
        return None
    parsed = urlparse(database_url)
    # sqlite:///relative.db -> parsed.path == /relative.db under urlparse;
    # SQLAlchemy interprets the three-slash form as a relative path.
    if database_url.startswith("sqlite:////"):
        return Path(parsed.path)
    if database_url.startswith("sqlite:///"):
        return Path(database_url.removeprefix("sqlite:///"))
    return None


def make_engine(database_url: str, *, echo: bool = False) -> Engine:
    path = _sqlite_file_from_url(database_url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, echo=echo, future=True)
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


class Database:
    """Small unit-of-work wrapper used by the services and bot."""

    def __init__(self, database_url: str = "sqlite:///data/app.db") -> None:
        self.engine = make_engine(database_url)
        self.session_factory = sessionmaker(
            bind=self.engine, class_=Session, expire_on_commit=False, autoflush=True
        )

    def init(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def init_database(database_url: str) -> None:
    """Create all tables without requiring Discord or OpenAI credentials."""

    Database(database_url).init()
