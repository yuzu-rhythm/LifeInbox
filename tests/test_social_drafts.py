from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from life_inbox.analyzer import NullAnalyzer
from life_inbox.config import Settings
from life_inbox.discord_bot import LifeInboxBot, SocialDraftView
from life_inbox.models import Attachment, Event
from life_inbox.services import InboxService
from life_inbox.social_drafts import SocialDraftService
from life_inbox.social_models import SocialDraft


def _add_event(db, message_id: str, raw_text: str = "今日の投稿") -> int:
    timestamp = datetime(2026, 8, 29, 3, 0)
    with db.session() as session:
        event = Event(
            discord_message_id=message_id,
            discord_channel_id="70",
            discord_user_id="10",
            captured_at=timestamp,
            occurred_at=timestamp,
            raw_text=raw_text,
            source="discord",
            status="pending",
            event_type="unknown",
            event_kinds=[],
        )
        session.add(event)
        session.commit()
        return event.id


def _add_image(db, tmp_path: Path, event_id: int) -> int:
    path = tmp_path / "attachments" / f"social-{event_id}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"saved-image")
    with db.session() as session:
        attachment = Attachment(
            event_id=event_id,
            discord_attachment_id=f"image-{event_id}",
            original_filename="social.png",
            content_type="image/png",
            local_path=str(path),
            sha256="a" * 64,
        )
        session.add(attachment)
        session.commit()
        return attachment.id


def test_registration_selection_and_content_refresh_are_idempotent(db, tmp_path: Path):
    service = SocialDraftService(
        db.session_factory,
        now=lambda: datetime(2026, 8, 30, 1, 2, 3),
    )
    event_id = _add_event(db, "social-service-idempotent")

    first = service.register_event(event_id)
    repeated = service.register_event(event_id)
    assert first.created is True
    assert first.target == "pending"
    assert first.state == "pending"
    assert first.has_media is False
    assert first.revision == 1
    assert repeated.created is False
    assert repeated.changed is False
    assert repeated.revision == first.revision

    blocked = service.select_target(
        event_id,
        "instagram",
        selected_by_user_id=10,
    )
    blocked_again = service.select_target(
        event_id,
        "instagram",
        selected_by_user_id=10,
    )
    assert blocked.state == "blocked"
    assert blocked.has_media is False
    assert blocked.revision == 2
    assert blocked_again.changed is False
    assert blocked_again.revision == blocked.revision

    _add_image(db, tmp_path, event_id)
    refreshed = service.register_event(event_id)
    assert refreshed.changed is True
    assert refreshed.target == "pending"
    assert refreshed.state == "pending"
    assert refreshed.has_media is True
    assert refreshed.selected_by_user_id is None
    assert refreshed.revision == 3

    ready = service.select_target(
        event_id,
        "instagram",
        selected_by_user_id="10",
    )
    ready_again = service.select_target(
        event_id,
        "instagram",
        selected_by_user_id="10",
    )
    assert ready.state == "ready"
    assert ready.revision == 4
    assert ready_again.changed is False
    assert ready_again.revision == ready.revision


def test_action_mapping_is_idempotent_and_event_delete_cascades(db):
    service = SocialDraftService(db.session_factory)
    event_id = _add_event(db, "social-action-map")
    draft = service.register_event(event_id)

    mapped = service.record_action_message(
        event_id,
        channel_id=70,
        message_id=700,
    )
    mapped_again = service.record_action_message(
        event_id,
        channel_id="70",
        message_id="700",
    )
    assert mapped.discord_action_channel_id == "70"
    assert mapped.discord_action_message_id == "700"
    assert mapped.revision == draft.revision
    assert mapped_again.changed is False
    assert mapped_again.revision == draft.revision

    with db.session() as session:
        event = session.get(Event, event_id)
        assert event is not None
        session.delete(event)
        session.commit()
        assert session.scalar(select(SocialDraft.id).where(SocialDraft.id == draft.id)) is None


@pytest.mark.asyncio
async def test_direct_social_capture_is_raw_first_and_duplicate_safe(db, tmp_path: Path):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        social_draft_channel_id=70,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path,
        timezone="Asia/Tokyo",
    )
    service = InboxService(db.session_factory, settings, NullAnalyzer())
    bot = LifeInboxBot(settings, service)
    send = AsyncMock(return_value=SimpleNamespace(id=700))
    channel = SimpleNamespace(id=70, send=send)
    attachment = SimpleNamespace(
        id=701,
        filename="photo.png",
        content_type="image/png",
        size=11,
        read=AsyncMock(return_value=b"image-bytes"),
    )
    message = SimpleNamespace(
        id="social-direct-1",
        content="今日の投稿: ローカルで確認する",
        author=SimpleNamespace(id=10, bot=False),
        guild=SimpleNamespace(id=20),
        channel=channel,
        attachments=[attachment],
        created_at=datetime(2026, 8, 29, 3, 0),
        webhook_id=None,
    )

    try:
        await bot.on_message(message)
        await bot.on_message(message)

        assert send.await_count == 1
        response_text = send.await_args.args[0]
        assert "ローカルSQLite" in response_text
        assert "外部SNSには投稿していません" in response_text
        view = send.await_args.kwargs["view"]
        assert isinstance(view, SocialDraftView)
        assert [item.label for item in view.children] == [
            "X",
            "Instagram",
            "両方",
            "保留",
        ]
        assert all(
            item.custom_id.startswith("life-inbox:social-draft:") for item in view.children
        )

        with db.session() as session:
            event = session.scalar(
                select(Event).where(Event.discord_message_id == "social-direct-1")
            )
            assert event is not None
            draft = session.scalar(
                select(SocialDraft).where(SocialDraft.event_id == event.id)
            )
            assert draft is not None
            assert draft.has_media is True
            assert draft.discord_action_channel_id == "70"
            assert draft.discord_action_message_id == "700"
            assert (
                session.scalar(
                    select(Event.id).where(Event.discord_message_id == "social-direct-1")
                )
                == event.id
            )

        interaction_response = SimpleNamespace(
            is_done=MagicMock(return_value=False),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10, bot=False),
            guild_id=20,
            channel_id=70,
            response=interaction_response,
        )
        await view.children[1].callback(interaction)
        interaction_response.send_message.assert_awaited_once()
        assert interaction_response.send_message.await_args.kwargs["ephemeral"] is True
        assert "外部SNSには投稿していません" in interaction_response.send_message.await_args.args[0]
        selected = bot.social_drafts.get(event.id)
        assert selected is not None
        assert selected.target == "instagram"
        assert selected.state == "ready"

        forbidden_response = SimpleNamespace(
            is_done=MagicMock(return_value=False),
            send_message=AsyncMock(),
        )
        forbidden = SimpleNamespace(
            user=SimpleNamespace(id=11, bot=False),
            guild_id=20,
            channel_id=70,
            response=forbidden_response,
        )
        await view.children[0].callback(forbidden)
        assert "許可されていません" in forbidden_response.send_message.await_args.args[0]
    finally:
        await bot.close()


def test_social_view_restore_does_not_register_same_event_twice(db):
    settings = Settings(
        _env_file=None,
        discord_guild_id=20,
        allowed_user_id=10,
        inbox_channel_id=30,
        social_draft_channel_id=70,
        database_url="sqlite:///:memory:",
    )
    service = InboxService(db.session_factory, settings, NullAnalyzer())
    bot = LifeInboxBot(settings, service)
    bot.add_view = MagicMock()
    social_service = SocialDraftService(db.session_factory)
    draft = social_service.register_event(_add_event(db, "social-restore"))

    bot._restore_social_draft_view(draft)
    bot._restore_social_draft_view(draft)

    bot.add_view.assert_called_once()
    restored = bot.add_view.call_args.args[0]
    assert isinstance(restored, SocialDraftView)
    assert restored.action_channel_id is None
