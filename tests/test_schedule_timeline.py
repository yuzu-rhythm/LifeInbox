from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from life_inbox.cli import build_parser, main
from life_inbox.config import Settings
from life_inbox.schedule_timeline import (
    SCHEDULE_TIMELINE_CHANNEL_NAME,
    ScheduleTimelineProvisionResult,
    _provision_guild_timeline,
    schedule_timeline_message,
)


def _settings(**kwargs) -> Settings:
    defaults = {
        "discord_bot_token": "test-token",
        "discord_guild_id": 10,
        "allowed_user_id": 20,
        "general_inbox_channel_id": 30,
        "general_archive_channel_id": 31,
        "records_channel_id": 32,
        "reminder_channel_id": 33,
        "reminder_scheduler_enabled": True,
        "finance_digest_enabled": True,
        "finance_digest_dry_run": False,
        "finance_digest_channel_id": 32,
        "rhythm_enabled": True,
        "rhythm_scheduler_enabled": True,
        "rhythm_discord_dry_run": False,
        "discord_chunithm_channel_id": 41,
        "discord_maimai_channel_id": 42,
        "discord_ongeki_channel_id": 43,
        "discord_credit_channel_id": 44,
        "rhythm_spending_immediate_post_enabled": True,
        "rhythm_spending_daily_summary_enabled": True,
        "notion_sync_enabled": True,
        "notion_api_token": "notion-test",
        "notion_parent_page_id": "page-id",
    }
    defaults.update(kwargs)
    return Settings(_env_file=None, **defaults)


@dataclass
class FakeAuthor:
    id: int


@dataclass
class FakeMessage:
    id: int
    content: str
    author: FakeAuthor
    pinned: bool = False
    edits: list[str] = field(default_factory=list)
    pin_calls: int = 0

    async def edit(self, *, content: str):
        self.content = content
        self.edits.append(content)
        return self

    async def pin(self):
        self.pin_calls += 1
        self.pinned = True


@dataclass
class FakeChannel:
    name: str = SCHEDULE_TIMELINE_CHANNEL_NAME
    id: int = 50
    messages: list[FakeMessage] = field(default_factory=list)

    def history(self, *, limit=None):
        async def iterator():
            for message in sorted(self.messages, key=lambda item: item.id, reverse=True):
                yield message

        return iterator()

    async def send(self, content: str):
        message = FakeMessage(100 + len(self.messages), content, FakeAuthor(9001))
        self.messages.append(message)
        return message


@dataclass
class FakeGuild:
    text_channels: list[FakeChannel]


def test_timeline_renders_current_live_and_disabled_states_without_secrets():
    message = schedule_timeline_message(_settings())

    assert "23:45" in message
    assert message.count("10:00") == 2
    assert message.count("23:55") == 1
    assert "CHUNITHM・maimai・オンゲキ" in message
    assert "eachEvent" in message
    assert "60秒ごと" in message
    assert "有効（実処理）" in message
    assert "睡眠・ワークアウトまとめ / 無効" in message
    assert "notion-test" not in message
    assert "test-token" not in message
    assert len(message) <= 2_000


def test_timeline_marks_dry_run_and_missing_destinations():
    message = schedule_timeline_message(
        _settings(
            rhythm_discord_dry_run=True,
            discord_maimai_channel_id=None,
            finance_digest_enabled=False,
            finance_digest_channel_id=None,
        )
    )

    assert "有効（dry-run）" in message
    assert "設定不足" in message
    assert "投稿先未設定" in message


@pytest.mark.asyncio
async def test_provision_is_idempotent_and_only_edits_bot_owned_marker():
    settings = _settings()
    user_message = FakeMessage(
        1,
        "ユーザー文 `life-inbox-schedule-timeline:v1`",
        FakeAuthor(20),
    )
    channel = FakeChannel(messages=[user_message])
    guild = FakeGuild([channel])

    first = await _provision_guild_timeline(guild, bot_user_id=9001, settings=settings)
    second = await _provision_guild_timeline(guild, bot_user_id=9001, settings=settings)

    assert first.created is True
    assert first.pinned is True
    assert second.created is False
    assert second.updated is False
    assert second.pin_skipped is True
    assert len(channel.messages) == 2
    assert user_message.content.startswith("ユーザー文")


@pytest.mark.asyncio
async def test_provision_updates_when_schedule_settings_change():
    initial = _settings(finance_digest_cron="45 23 * * *")
    channel = FakeChannel()
    guild = FakeGuild([channel])
    await _provision_guild_timeline(guild, bot_user_id=9001, settings=initial)

    changed = _settings(finance_digest_cron="30 22 * * *")
    result = await _provision_guild_timeline(guild, bot_user_id=9001, settings=changed)

    assert result.updated is True
    assert "22:30" in channel.messages[0].content
    assert len(channel.messages) == 1


@pytest.mark.asyncio
async def test_provision_fails_closed_for_duplicate_channels():
    with pytest.raises(RuntimeError, match="複数"):
        await _provision_guild_timeline(
            FakeGuild([FakeChannel(id=1), FakeChannel(id=2)]),
            bot_user_id=9001,
            settings=_settings(),
        )


def test_cli_exposes_timeline_provisioner(monkeypatch, capsys):
    args = build_parser().parse_args(["provision-schedule-timeline", "--no-env-file"])
    assert args.command == "provision-schedule-timeline"

    result = ScheduleTimelineProvisionResult(
        channel_id=50,
        message_id=51,
        created=True,
        updated=False,
        pinned=True,
        pin_skipped=False,
    )
    monkeypatch.setattr("life_inbox.cli._settings", lambda _args: object())
    monkeypatch.setattr(
        "life_inbox.schedule_timeline.provision_schedule_timeline",
        lambda _settings: result,
    )

    assert main(["provision-schedule-timeline", "--no-env-file"]) == 0
    assert "新規投稿" in capsys.readouterr().out
