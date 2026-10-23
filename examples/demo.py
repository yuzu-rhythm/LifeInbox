"""Run the real Web UI with synthetic records; no external credentials needed."""

from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from sqlalchemy import select

from life_inbox.config import Settings
from life_inbox.db import Database
from life_inbox.models import Event, Memo
from life_inbox.web import create_web_app


def build_demo():
    root = Path(__file__).resolve().parents[1] / "data" / "portfolio-demo"
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{root / 'demo.db'}",
        data_dir=root,
        openai_inbox_classifier_enabled=False,
        notion_sync_enabled=False,
        rhythm_enabled=False,
    )
    database = Database(settings.database_url)
    database.init()
    now = datetime.now(UTC).replace(tzinfo=None)
    texts = (
        "【架空データ】Pythonの学習メモを整理する",
        "【架空データ】バックアップの動作を検証する",
        "【架空データ】週末の読書リストを作る",
    )
    with database.session() as session:
        for index, body in enumerate(texts, 1):
            identifier = f"portfolio-demo-{index}"
            if session.scalar(select(Event).where(Event.discord_message_id == identifier)):
                continue
            event = Event(
                discord_message_id=identifier,
                discord_channel_id="demo-channel",
                discord_user_id="demo-user",
                captured_at=now,
                occurred_at=now,
                raw_text=body,
                source="portfolio-demo",
                status="confirmed",
                event_type="memo",
                event_kinds=["memo"],
            )
            event.memo = Memo(body=body)
            session.add(event)
        session.commit()
    return create_web_app(settings, database=database)


if __name__ == "__main__":
    uvicorn.run(build_demo(), host="127.0.0.1", port=8876)
