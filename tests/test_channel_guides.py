from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from life_inbox import channel_guides
from life_inbox.channel_guides import (
    CHANNEL_GUIDE_BODIES,
    DISCORD_MESSAGE_LIMIT,
    GUIDE_VERSION,
    KNOWN_CHANNEL_NAMES,
    ChannelGuideFailure,
    ChannelGuideProvisionResult,
    _guide_channel,
    _is_managed_guide,
    _provision_guild_guides,
    _safe_provision_failure,
)
from life_inbox.cli import build_parser, main


@dataclass
class FakeAuthor:
    id: int


@dataclass
class FakeMessage:
    id: int
    content: str
    author: FakeAuthor
    pinned: bool = False
    edit_calls: list[str] = field(default_factory=list)
    pin_calls: list[dict[str, Any]] = field(default_factory=list)
    pin_error: Exception | None = None

    async def edit(self, *, content: str) -> FakeMessage:
        self.edit_calls.append(content)
        self.content = content
        return self

    async def pin(self, **kwargs: Any) -> None:
        self.pin_calls.append(kwargs)
        if self.pin_error is not None:
            raise self.pin_error
        self.pinned = True


@dataclass
class FakeChannel:
    name: str
    bot_user_id: int = 9001
    messages: list[FakeMessage] = field(default_factory=list)
    send_error: Exception | None = None
    history_error: Exception | None = None
    history_calls: int = 0
    sent: list[FakeMessage] = field(default_factory=list)

    def history(self, *, limit: int | None = None):
        async def iterator():
            self.history_calls += 1
            if self.history_error is not None:
                raise self.history_error
            for message in sorted(self.messages, key=lambda item: item.id, reverse=True):
                yield message

        return iterator()

    async def send(self, content: str) -> FakeMessage:
        if self.send_error is not None:
            raise self.send_error
        message = FakeMessage(
            id=100_000 + len(self.messages),
            content=content,
            author=FakeAuthor(self.bot_user_id),
        )
        self.messages.append(message)
        self.sent.append(message)
        return message


@dataclass
class FakeGuild:
    text_channels: list[FakeChannel]


def _guild(*channels: FakeChannel) -> FakeGuild:
    return FakeGuild(list(channels))


@pytest.mark.asyncio
async def test_static_guides_cover_every_known_channel_and_fit_discord():
    assert tuple(CHANNEL_GUIDE_BODIES) == KNOWN_CHANNEL_NAMES

    messages = channel_guides.channel_guide_messages()
    assert len(messages) == len(KNOWN_CHANNEL_NAMES)
    assert all(len(message) <= DISCORD_MESSAGE_LIMIT for message in messages)
    for channel_name, message in zip(KNOWN_CHANNEL_NAMES, messages, strict=True):
        assert f"{GUIDE_VERSION}:{channel_name}" in message
        for required_label in (
            "目的:",
            "投稿するもの:",
            "投稿しないもの:",
            "例:",
            "Bot自動処理:",
            "結果を見る:",
        ):
            assert required_label in message
    assert "/家計簿今日" in CHANNEL_GUIDE_BODIES["生活-記録"]
    assert "毎日23:45" in CHANNEL_GUIDE_BODIES["生活-記録"]
    assert "/振り返り" in CHANNEL_GUIDE_BODIES["ai-振り返り"]
    assert "外部AIへ送らない" in CHANNEL_GUIDE_BODIES["ai-振り返り"]
    assert "/就活追加" in CHANNEL_GUIDE_BODIES["就活情報"]
    assert "/引落口座設定" in CHANNEL_GUIDE_BODIES["口座"]
    assert "/引落口座解除" in CHANNEL_GUIDE_BODIES["口座"]
    assert "/引落口座一覧" in CHANNEL_GUIDE_BODIES["口座"]
    assert "口座番号などは扱いません" in CHANNEL_GUIDE_BODIES["口座"]
    assert "最初の登録" in CHANNEL_GUIDE_BODIES["クレカ"]
    assert "登録手順" in CHANNEL_GUIDE_BODIES["口座"]
    assert "利用限度額ではなく" in CHANNEL_GUIDE_BODIES["口座"]
    assert "カード引落" in CHANNEL_GUIDE_BODIES["クレカ"]
    assert "主入口" in CHANNEL_GUIDE_BODIES["一般"]
    assert "候補ボタン" in CHANNEL_GUIDE_BODIES["一般"]
    assert "絵文字リアクション" in CHANNEL_GUIDE_BODIES["一般"]
    assert "コピー成功後だけ" in CHANNEL_GUIDE_BODIES["一般"]
    assert "#一般アーカイブ" in CHANNEL_GUIDE_BODIES["一般"]
    assert "分類・削除を止め" in CHANNEL_GUIDE_BODIES["一般アーカイブ"]
    assert "生活マイページ" in CHANNEL_GUIDE_BODIES["一般アーカイブ"]
    assert "23:55" in CHANNEL_GUIDE_BODIES["定期更新スケジュール"]
    assert "固定時刻の経過だけ" in CHANNEL_GUIDE_BODIES["定期更新スケジュール"]
    assert "気軽な考え・気分・感情" in CHANNEL_GUIDE_BODIES["独り言"]
    assert "#一般" in CHANNEL_GUIDE_BODIES["独り言"]
    assert "レストラン・カフェ・テイクアウト" in CHANNEL_GUIDE_BODIES["外食"]
    assert "日常の食事・自炊" in CHANNEL_GUIDE_BODIES["外食"]
    assert "思い出として残す" in CHANNEL_GUIDE_BODIES["思い出"]
    assert "画像だけで思い出とは判定しません" in CHANNEL_GUIDE_BODIES["思い出"]
    assert "X" in CHANNEL_GUIDE_BODIES["毎日の投稿"]
    assert "Instagram" in CHANNEL_GUIDE_BODIES["毎日の投稿"]
    assert "下書き" in CHANNEL_GUIDE_BODIES["毎日の投稿"]
    assert "両方" in CHANNEL_GUIDE_BODIES["毎日の投稿"]
    assert "明示的な選択後" in CHANNEL_GUIDE_BODIES["毎日の投稿"]
    assert "ローカルSQLite" in CHANNEL_GUIDE_BODIES["毎日の投稿"]
    assert "外部SNSには投稿せず" in CHANNEL_GUIDE_BODIES["毎日の投稿"]
    assert "保存済み画像" in CHANNEL_GUIDE_BODIES["毎日の投稿"]
    assert "動画の捕捉・保存は現在未対応" in CHANNEL_GUIDE_BODIES["毎日の投稿"]


def test_channel_marker_is_version_tolerant_but_channel_specific():
    message = "`life-inbox-channel-guide:v7:一般`"

    assert _guide_channel(message) == "一般"
    assert _is_managed_guide(message, "一般") is True
    assert _is_managed_guide(message, "なんでも入力") is False
    assert _guide_channel("`life-inbox-command-guide:v5:1`") is None


@pytest.mark.asyncio
async def test_first_pass_sends_and_pins_one_guide_per_existing_channel():
    channels = [FakeChannel(name) for name in KNOWN_CHANNEL_NAMES]

    result = await _provision_guild_guides(_guild(*channels), bot_user_id=9001)

    assert result.known_channels == len(KNOWN_CHANNEL_NAMES)
    assert result.channels_found == len(KNOWN_CHANNEL_NAMES)
    assert result.channels_processed == len(KNOWN_CHANNEL_NAMES)
    assert result.channels_missing == 0
    assert result.channels_failed == 0
    assert result.messages_created == len(KNOWN_CHANNEL_NAMES)
    assert result.messages_updated == 0
    assert result.messages_unchanged == 0
    assert result.messages_pinned == len(KNOWN_CHANNEL_NAMES)
    assert result.messages_pin_skipped == 0
    assert all(len(channel.messages) == 1 for channel in channels)
    assert all(message.pinned for channel in channels for message in channel.messages)


@pytest.mark.asyncio
async def test_rerun_is_idempotent_and_does_not_repin_existing_guides():
    channels = [FakeChannel(name) for name in KNOWN_CHANNEL_NAMES]
    guild = _guild(*channels)

    first = await _provision_guild_guides(guild, bot_user_id=9001)
    pin_call_counts = [len(channel.messages[0].pin_calls) for channel in channels]
    second = await _provision_guild_guides(guild, bot_user_id=9001)

    assert first.messages_created == len(KNOWN_CHANNEL_NAMES)
    assert second.messages_created == 0
    assert second.messages_updated == 0
    assert second.messages_unchanged == len(KNOWN_CHANNEL_NAMES)
    assert second.messages_pinned == 0
    assert second.messages_pin_skipped == len(KNOWN_CHANNEL_NAMES)
    assert second.pin_skipped_count == len(KNOWN_CHANNEL_NAMES)
    assert all(len(channel.messages) == 1 for channel in channels)
    assert [len(channel.messages[0].pin_calls) for channel in channels] == pin_call_counts


@pytest.mark.asyncio
async def test_only_newest_bot_owned_marked_message_is_updated():
    channel = FakeChannel("一般")
    older = FakeMessage(
        id=10,
        content="old `life-inbox-channel-guide:v1:一般`",
        author=FakeAuthor(9001),
    )
    newer = FakeMessage(
        id=20,
        content="new `life-inbox-channel-guide:v9:一般`",
        author=FakeAuthor(9001),
    )
    user_message = FakeMessage(
        id=30,
        content="ユーザーの文章 `life-inbox-channel-guide:v1:一般`",
        author=FakeAuthor(42),
    )
    unmarked_bot_message = FakeMessage(id=40, content="Botの通常応答", author=FakeAuthor(9001))
    channel.messages.extend([older, newer, user_message, unmarked_bot_message])

    result = await _provision_guild_guides(_guild(channel), bot_user_id=9001)

    assert result.messages_created == 0
    assert result.messages_updated == 1
    assert result.messages_pinned == 1
    assert result.duplicate_managed_messages == 1
    assert older.content == "old `life-inbox-channel-guide:v1:一般`"
    assert newer.content == channel_guides.channel_guide_message("一般")
    assert newer.pinned is True
    assert user_message.content == "ユーザーの文章 `life-inbox-channel-guide:v1:一般`"
    assert unmarked_bot_message.content == "Botの通常応答"


@pytest.mark.asyncio
async def test_duplicate_channel_name_is_reported_without_history_or_mutation():
    first = FakeChannel("一般")
    second = FakeChannel("一般")

    result = await _provision_guild_guides(_guild(first, second), bot_user_id=9001)

    assert result.channels_found == 2
    assert result.channels_processed == 0
    assert result.channels_failed == 1
    assert result.duplicate_channel_names == ("一般",)
    assert result.failed_channels == (
        ChannelGuideFailure("一般", "同名のテキストチャンネルが複数あります"),
    )
    assert first.history_calls == 0
    assert second.history_calls == 0
    assert first.sent == []
    assert second.sent == []


@pytest.mark.asyncio
async def test_pin_failure_is_nonfatal_and_is_counted_as_skipped():
    channel = FakeChannel("一般")
    channel.send_error = None

    async def send_with_pin_failure(content: str) -> FakeMessage:
        message = await FakeChannel.send(channel, content)
        message.pin_error = RuntimeError("response body contains secret-token")
        return message

    channel.send = send_with_pin_failure  # type: ignore[method-assign]
    result = await _provision_guild_guides(_guild(channel), bot_user_id=9001)

    assert result.channels_failed == 0
    assert result.messages_created == 1
    assert result.messages_pinned == 0
    assert result.messages_pin_skipped == 1
    assert result.pin_skipped_channels == ("一般",)
    assert "secret-token" not in _safe_provision_failure(ValueError("secret-token"))


@pytest.mark.asyncio
async def test_history_failure_does_not_send_when_idempotency_cannot_be_verified():
    channel = FakeChannel("一般", history_error=RuntimeError("secret response"))

    result = await _provision_guild_guides(_guild(channel), bot_user_id=9001)

    assert result.channels_failed == 1
    assert result.failed_channels[0].reason == "チャンネル履歴を確認できませんでした"
    assert channel.sent == []


@pytest.mark.asyncio
async def test_command_guide_messages_are_not_treated_as_channel_guides():
    existing = FakeMessage(
        id=1,
        content="`life-inbox-command-guide:v5:1`\n既存の複数パート案内",
        author=FakeAuthor(9001),
    )
    channel = FakeChannel("bot-コマンド一覧", messages=[existing])

    result = await _provision_guild_guides(_guild(channel), bot_user_id=9001)

    assert result.messages_created == 1
    assert result.messages_updated == 0
    assert existing.content == "`life-inbox-command-guide:v5:1`\n既存の複数パート案内"
    assert len(channel.messages) == 2
    assert any(
        channel_guides._is_managed_guide(message.content, "bot-コマンド一覧")
        for message in channel.messages
    )


def test_cli_exposes_channel_guide_provisioning_and_reports_pin_counts():
    args = build_parser().parse_args(["provision-channel-guides", "--no-env-file"])

    assert args.command == "provision-channel-guides"
    assert args.no_env_file is True

    result = ChannelGuideProvisionResult(
        known_channels=20,
        channels_found=18,
        channels_processed=18,
        channels_missing=2,
        channels_failed=0,
        messages_created=18,
        messages_updated=0,
        messages_unchanged=0,
        messages_pinned=17,
        messages_pin_skipped=1,
        missing_channel_names=("買い物リスト", "神リンク"),
    )
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr("life_inbox.cli._settings", lambda _args: object())
        monkeypatch.setattr(channel_guides, "provision_channel_guides", lambda _settings: result)
        exit_code = main(["provision-channel-guides", "--no-env-file"])
    finally:
        monkeypatch.undo()

    assert exit_code == 0


def test_cli_returns_failure_for_safe_duplicate_channel_report(capsys: pytest.CaptureFixture[str]):
    result = ChannelGuideProvisionResult(
        known_channels=20,
        channels_found=20,
        channels_processed=19,
        channels_missing=0,
        channels_failed=1,
        messages_created=19,
        messages_updated=0,
        messages_unchanged=0,
        messages_pinned=19,
        messages_pin_skipped=0,
        duplicate_channel_names=("一般",),
        failed_channels=(ChannelGuideFailure("一般", "同名のテキストチャンネルが複数あります"),),
    )
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr("life_inbox.cli._settings", lambda _args: object())
        monkeypatch.setattr(channel_guides, "provision_channel_guides", lambda _settings: result)
        exit_code = main(["provision-channel-guides", "--no-env-file"])
    finally:
        monkeypatch.undo()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "一般" in captured.err
    assert "Discord" not in captured.err
