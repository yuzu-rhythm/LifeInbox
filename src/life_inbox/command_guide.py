"""Japanese Discord command guide and one-shot channel provisioning."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import discord

from .config import Settings

logger = logging.getLogger(__name__)

GUIDE_VERSION = "life-inbox-command-guide:v8"
COMMAND_GUIDE_TOPIC_MARKER = "life-inbox-command-guide-managed:v1"
_GUIDE_PART_RE = re.compile(r"`life-inbox-command-guide:v\d+:(\d+)`")


def _guide_part(content: str) -> int | None:
    match = _GUIDE_PART_RE.search(content)
    return int(match.group(1)) if match is not None else None


def _managed_topic(topic: str | None) -> bool:
    return COMMAND_GUIDE_TOPIC_MARKER in (topic or "")


def _command_guide_topic(web_url: str) -> str:
    return (
        f"生活マイページ: {web_url}/（Bot稼働Mac専用） | "
        "生活Inbox Botの日本語コマンド案内（Botが管理） | "
        f"{COMMAND_GUIDE_TOPIC_MARKER}"
    )[:1024]


def _safe_provision_failure(exc: Exception) -> str:
    """Classify Discord failures without reflecting response bodies or secrets."""

    if isinstance(exc, discord.Forbidden):
        return "Botにチャンネル作成または案内投稿の権限がありません"
    if isinstance(exc, discord.NotFound):
        return "生活サーバーまたは対象チャンネルを確認できませんでした"
    known_messages = {
        "生活サーバーを取得できませんでした",
        "Botユーザーを確認できませんでした",
        "同名のBot管理コマンド案内チャンネルが複数あります",
        "同名の既存チャンネルはBot管理と確認できないため変更しません",
    }
    if isinstance(exc, RuntimeError) and str(exc) in known_messages:
        return str(exc)
    return "Discord側の処理を完了できませんでした"


def _portal_section(
    web_url: str,
    *,
    public_web_url: str | None,
    notion_hub_url: str | None,
) -> str:
    links = [
        f"[Mac内の生活マイページを開く]({web_url}/)（Botを動かしているMac専用）"
    ]
    if public_web_url is not None:
        links.append(
            f"[クラウド版の生活マイページを開く]({public_web_url}/)"
            "（Googleログイン/IAP保護）"
        )
    if notion_hub_url is not None:
        links.append(f"[Notion Life Archiveを開く]({notion_hub_url})（二次閲覧用）")
    return (
        "【生活マイページ・閲覧リンク】\n"
        + "\n".join(links)
        + "\n\n"
        "ローカルSQLiteと添付が正本です。クラウド版はIAPで保護され、"
        "Notionは明示的に📚保存した項目だけを置く二次アーカイブです。"
        "URLが分からなくなった場合は `/mypage` でもMac内URLを確認できます。"
    )


def command_guide_sections(
    web_url: str = "http://127.0.0.1:8765",
    *,
    public_web_url: str | None = None,
    notion_hub_url: str | None = None,
) -> tuple[str, ...]:
    return (
        (
            "生活Inbox Bot コマンド一覧\n\n"
            "主な入力先は #一般 です。普通の文章や画像を送ると、先に原文・添付を"
            "保存してから家計・食事・メモ・音ゲー・生活タスクへ振り分けます。"
            "各専用チャンネルは、用途を最初から固定したい場合だけ使う任意の近道です。"
            "スラッシュコマンドは `/` を入力して選択してください。"
            "本人・生活サーバー・設定済みチャンネル以外では動作しません。\n\n"
            f"生活マイページ: <{web_url}/>（Botを動かしているMac専用）"
        ),
        """【家計・食事・履歴】
`/today` 今日の確定支出・食事
`/month` 今月の支出概要
`/history [count]` 最近の確定記録
`/detail event_id` 1件の詳細・レシート品目
`/spending [minimum] [category] [dining]` 支出を絞り込み
`/dining` 外食だけを表示
`/meals` 食事記録
`/budget [month]` 予算状況
`/budget_set amount [category] [month]` 予算設定
`/accounts` 複数口座・カード・NISA
`/account_add name kind [opening_balance]` 口座追加
`/transfer source destination amount [note]` 振替
`/引落口座一覧` カードごとの引落口座を表示（未設定も表示）
`/引落口座設定 card_name bank_name` カードと銀行口座を紐づける
`/引落口座解除 card_name` カードの紐づけだけを解除
`/memos` 一言メモ
`/家計簿今日` 今日の家計サマリーをDiscord内で表示
`/家計簿配信確認` 日次投稿予定を実送信なしで確認
`/mypage` ローカル生活マイページ""",
        """【口座・クレカの最初の登録】
1. #口座 で `/account_add name:ゆうちょ kind:銀行口座 opening_balance:50000`
2. #口座 で `/account_add name:楽天カード kind:クレジットカード opening_balance:0`
3. #口座 で `/引落口座設定 card_name:楽天カード bank_name:ゆうちょ`
4. `/accounts` と `/引落口座一覧` で確認

複数ある場合は表示名を変えて繰り返します。銀行の opening_balance は登録時残高、カードは"
"利用限度額ではなく登録時点の未払い利用残高を正数で入力します。カード利用は #クレカ へ"
"`[楽天カード] 1280 Amazon`、銀行入出金は #口座 へ"
"`[ゆうちょ] 入金 250000 給料` の形式です。カード引落は二重支出にせず、"
"`[ゆうちょ] カード引落 68000 -> [楽天カード] 8月分` と記録します。"
"番号全桁・暗証番号・CVV・ログイン情報はDiscordへ投稿しません。""",
        """【Inbox・手帳・タスク】
`/inbox` 未整理Inbox
`/retry event_id` 保留記録を再解析
`/uncopied` 手帳へ未転記の記録
`/planner [date_text]` 手帳用の日次まとめ
`/copied event_id` 1件を転記済みにする
`/copied all_eligible:true` 全対象を転記済みにする
`/task_add title [due] [description]` 生活タスク追加
`/タスク追加 title [due] [description] [due_time]` 日時付きタスク追加
`/tasks` 未完了タスク
`/タスク一覧` 未完了タスク（日本語表示）
`/task_done task_id` タスク完了
`/タスク完了 task_id` タスク完了（日本語表示）

`/日課追加 title time` 毎日のルーティンを追加（time は HH:MM）
`/日課一覧` 日課と今日の完了状態
`/日課完了 routine_id` 今日の日課を完了

#一般 では `タスク: 研究計画書を提出` のような明示入力も使えます。
曖昧な日付・時刻は推測せず、期限なしで保存します。期限が時刻付きなら24時間前・1時間前・期限時刻、
日付だけなら当日09:00（設定タイムゾーン）にリマインダーを送ります。""",
        """【就活・振り返り】
`/就活追加 company title [kind] [stage] [deadline] [notes]` 企業・求人・応募を追加
`/就活一覧 [status] [kind]` 進行中の就活情報を表示
`/就活更新 opportunity_id [status] [stage] [deadline] [notes]` 状態と期限を更新
`/振り返り [period]` 今日または今週の保存済み構造化記録を要約

振り返りは #ai-振り返り または #生活-記録 で本人だけに表示します。
既定は外部AIへ送信しないローカル集計です。OpenAI利用を明示的に有効化した場合も、
原文・店名・添付・URL・資格情報は送らず、検証済みの件数・合計・期限日だけを使います。""",
        """【音ゲーTODO・リザルト】
`/rhythm` TODOとリザルト履歴
`/favorite_add game song_title [note]` お気に入り曲追加
`/maimai_favorites` 手動のお気に入り
`/maimai_net_favorites` 公式NETお気に入り取込結果
`/maimai_mybest` 公式MY BEST取込結果
`/progress_today [game]` 今日の進捗画像

投稿例: `todo maimai でらっくす譜面を詰める`
#なんでも入力 が主入口です。#音ゲー と #今日の進捗 は任意の近道で、TODOへの画像返信、
または `オンゲキ AB` と画像を送ると成長記録になります。""",
        """【レート自動取得・利用額】
`/レート game` ウニ・マイ・ゲキの最新レート
`/レート更新 [game]` 1機種または3機種を取得キューへ追加
`/get [game]` 同じ取得処理を短い名前で実行
`/取得状況` 取得状態・失敗理由・手動操作待ち
`/取得確認 run_id` ログインや更新完了を管理者が明示確認
`/今日の利用額 [date_text]` iPhone同期済み利用額
`/再投稿 date_text game` 指定日の投稿を再実行
`/認証状況` 3機種のブラウザー／API認証状態
`/コマンド一覧` この案内のチャンネル名を表示

レート取得でログイン・CAPTCHA・規約確認などが必要な場合は成功扱いにせず、`/取得状況`へ手動操作待ちとして表示します。""",
        (
            "【iPhone アクションボタン／ショートカット】\n"
            "Botサーバーの専用APIを「URLの内容を取得」から呼び出すと、利用額記録、"
            "なんでも入力、一言メモ、今日の状況確認、レート更新をアクションボタンに"
            "割り当てられます。「現在地を取得」を選んだ場合だけ、その1地点を分類先の"
            "Discord投稿とNotionバックアップへ添えます。継続追跡はしません。"
            "Discord Bot TokenではなくiPhone専用トークンを使い、再送時はrevisionまたは"
            "Idempotency-Keyで二重登録を防ぎます。詳しいJSONと設定例はリポジトリの "
            "`docs/RHYTHM_IPHONE_API.md` を参照してください。\n\n"
            "秘密情報（Bot Token、SEGA ID、パスワード、Cookie、APIトークン）は"
            "Discordへ投稿しないでください。"
        ),
        _portal_section(
            web_url,
            public_web_url=public_web_url,
            notion_hub_url=notion_hub_url,
        ),
    )


def command_guide_messages(
    web_url: str = "http://127.0.0.1:8765",
    *,
    public_web_url: str | None = None,
    notion_hub_url: str | None = None,
) -> tuple[str, ...]:
    sections = command_guide_sections(
        web_url,
        public_web_url=public_web_url,
        notion_hub_url=notion_hub_url,
    )
    total = len(sections)
    return tuple(
        f"【Botコマンド案内 {index}/{total}】\n`{GUIDE_VERSION}:{index}`\n\n{section}"
        for index, section in enumerate(sections, start=1)
    )


@dataclass(frozen=True, slots=True)
class CommandGuideProvisionResult:
    channel_id: int
    channel_name: str
    channel_created: bool
    messages_created: int
    messages_updated: int


class _CommandGuideClient(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.settings = settings
        self.result: CommandGuideProvisionResult | None = None
        self.failure: Exception | None = None

    async def on_ready(self) -> None:
        try:
            web_url = f"http://{self.settings.web_host}:{self.settings.web_port}"
            guild = self.get_guild(self.settings.discord_guild_id or 0)
            if guild is None:
                raise RuntimeError("生活サーバーを取得できませんでした")
            if self.user is None:
                raise RuntimeError("Botユーザーを確認できませんでした")

            named_channels = [
                item
                for item in guild.text_channels
                if item.name == self.settings.command_guide_channel_name
            ]
            managed_candidates: list[
                tuple[discord.TextChannel, list[discord.Message]]
            ] = []
            unmanaged_name_collision = False
            for candidate in named_channels:
                bot_messages = [
                    message
                    async for message in candidate.history(limit=None)
                    if message.author.id == self.user.id
                    and _guide_part(message.content) is not None
                ]
                if _managed_topic(candidate.topic) or bot_messages:
                    managed_candidates.append((candidate, bot_messages))
                else:
                    unmanaged_name_collision = True

            if len(managed_candidates) > 1:
                raise RuntimeError(
                    "同名のBot管理コマンド案内チャンネルが複数あります"
                )
            if not managed_candidates and unmanaged_name_collision:
                raise RuntimeError(
                    "同名の既存チャンネルはBot管理と確認できないため変更しません"
                )

            channel_created = not managed_candidates
            existing_bot_messages: list[discord.Message] = []
            if managed_candidates:
                channel, existing_bot_messages = managed_candidates[0]
                topic = _command_guide_topic(web_url)
                if channel.topic != topic:
                    try:
                        await channel.edit(
                            topic=topic,
                            reason="生活マイページURLとBotコマンド案内を更新",
                        )
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        # The guide itself remains useful when the Bot can edit
                        # its messages but lacks Manage Channels.
                        logger.warning(
                            "command guide topic update skipped (%s)",
                            type(exc).__name__,
                        )
            else:
                inbox = guild.get_channel(self.settings.primary_inbox_channel_id or 0)
                category = getattr(inbox, "category", None)
                channel = await guild.create_text_channel(
                    self.settings.command_guide_channel_name,
                    category=category,
                    topic=_command_guide_topic(web_url),
                    reason="ユーザーが許可したBotコマンド一覧チャンネル作成",
                )

            existing_by_part: dict[int, discord.Message] = {}
            for message in existing_bot_messages:
                part = _guide_part(message.content)
                if part is not None:
                    # Discord history is newest first; preserve the newest
                    # Bot-owned guide message for each part.
                    existing_by_part.setdefault(part, message)

            created = 0
            updated = 0
            for part, content in enumerate(
                command_guide_messages(
                    web_url,
                    public_web_url=self.settings.public_web_url,
                    notion_hub_url=self.settings.notion_hub_url,
                ),
                start=1,
            ):
                existing = existing_by_part.get(part)
                if existing is None:
                    await channel.send(content)
                    created += 1
                elif existing.content != content:
                    await existing.edit(content=content)
                    updated += 1
            self.result = CommandGuideProvisionResult(
                channel_id=channel.id,
                channel_name=channel.name,
                channel_created=channel_created,
                messages_created=created,
                messages_updated=updated,
            )
        except Exception as exc:
            self.failure = exc
        finally:
            await self.close()


def provision_command_guide(settings: Settings) -> CommandGuideProvisionResult:
    settings.validate_for_bot()
    if settings.discord_bot_token is None:
        raise ValueError("DISCORD_BOT_TOKEN is required")
    client = _CommandGuideClient(settings)
    client.run(settings.discord_bot_token.get_secret_value(), log_handler=None)
    if client.failure is not None:
        detail = _safe_provision_failure(client.failure)
        raise RuntimeError(
            f"コマンド案内チャンネルの作成・更新に失敗しました: {detail}"
        ) from client.failure
    if client.result is None:
        raise RuntimeError("コマンド案内チャンネルの結果を確認できませんでした")
    return client.result
