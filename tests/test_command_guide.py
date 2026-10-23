from life_inbox.cli import build_parser
from life_inbox.command_guide import (
    COMMAND_GUIDE_TOPIC_MARKER,
    GUIDE_VERSION,
    _command_guide_topic,
    _guide_part,
    _managed_topic,
    _safe_provision_failure,
    command_guide_messages,
)


def test_command_guide_is_chunked_for_discord_and_lists_new_japanese_commands():
    messages = command_guide_messages()
    combined = "\n".join(messages)

    assert len(messages) >= 2
    assert all(len(message) <= 2_000 for message in messages)
    assert all(f"{GUIDE_VERSION}:{index}" in message for index, message in enumerate(messages, 1))
    for command in (
        "/レート",
        "/レート更新",
        "/取得状況",
        "/今日の利用額",
        "/再投稿",
        "/認証状況",
        "/コマンド一覧",
        "/家計簿今日",
        "/家計簿配信確認",
        "/引落口座設定",
        "/引落口座解除",
        "/引落口座一覧",
        "/タスク追加",
        "/タスク完了",
        "/就活追加",
        "/就活一覧",
        "/就活更新",
        "/振り返り",
    ):
        assert command in combined
    assert "#一般" in combined
    assert "【口座・クレカの最初の登録】" in combined
    assert "利用限度額ではなく" in combined
    assert "カード引落" in combined
    assert "http://127.0.0.1:8765/" in combined
    assert "Botを動かしているMac専用" in combined
    assert "生活タスク" in combined
    assert "任意の近道" in combined
    assert "SEGA ID、パスワード" in combined
    assert "外部AIへ送信しない" in combined
    assert "DISCORD_BOT_TOKEN=" not in combined


def test_command_guide_lists_configured_cloud_and_notion_navigation_links():
    messages = command_guide_messages(
        public_web_url="https://archive.example.test",
        notion_hub_url="https://app.notion.com/p/example",
    )
    combined = "\n".join(messages)

    assert "https://archive.example.test/" in combined
    assert "Googleログイン/IAP保護" in combined
    assert "https://app.notion.com/p/example" in combined
    assert "二次閲覧用" in combined
    assert "ローカルSQLiteと添付が正本" in combined


def test_cli_exposes_explicit_command_guide_provisioning_action():
    args = build_parser().parse_args(["provision-command-guide", "--no-env-file"])

    assert args.command == "provision-command-guide"
    assert args.no_env_file is True


def test_command_guide_management_markers_are_explicit_and_version_tolerant():
    assert _managed_topic(f"日本語案内 | {COMMAND_GUIDE_TOPIC_MARKER}") is True
    assert _managed_topic("ユーザーが作った同名チャンネル") is False
    assert _managed_topic(None) is False
    assert _guide_part("`life-inbox-command-guide:v1:7`") == 7
    assert _guide_part("`life-inbox-command-guide:v2:3`") == 3
    assert _guide_part("ユーザーの通常メッセージ") is None
    assert _safe_provision_failure(ValueError("credential-marker")) == (
        "Discord側の処理を完了できませんでした"
    )
    assert "credential-marker" not in _safe_provision_failure(
        ValueError("credential-marker")
    )


def test_command_guide_topic_keeps_local_mypage_visible_and_managed():
    topic = _command_guide_topic("http://127.0.0.1:8765")

    assert topic.startswith("生活マイページ: http://127.0.0.1:8765/")
    assert "Bot稼働Mac専用" in topic
    assert _managed_topic(topic) is True
    assert len(topic) <= 1024
