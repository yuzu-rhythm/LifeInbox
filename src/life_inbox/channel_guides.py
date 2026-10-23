"""Japanese usage guides for the known Life Server text channels.

This module deliberately has a narrower write surface than the normal Bot:
it only searches the configured guild, only considers exact channel names from
the reviewed static mapping, and only edits messages that both belong to this
Bot and carry the channel-specific management marker.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import discord

from .config import Settings

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2_000
GUIDE_VERSION = "life-inbox-channel-guide:v5"
CHANNEL_GUIDE_VERSION = GUIDE_VERSION
CHANNEL_GUIDE_MARKER = "life-inbox-channel-guide"

KNOWN_CHANNEL_NAMES: tuple[str, ...] = (
    "一般",
    "一般アーカイブ",
    "なんでも入力",
    "一言メモ",
    "独り言",
    "外食",
    "思い出",
    "毎日の投稿",
    "食事",
    "買い物リスト",
    "神リンク",
    "日課・リマインダー",
    "就活情報",
    "クレカ",
    "口座",
    "生活-記録",
    "音ゲー",
    "今日の進捗",
    "chunithm-レート",
    "maimai-レート",
    "ongeki-レート",
    "音ゲー利用額",
    "定期更新スケジュール",
    "bot-コマンド一覧",
    "bot-エラー",
    "ai-振り返り",
)
# A descriptive alias keeps callers from having to remember whether the
# constant is phrased in terms of names or channels.
CHANNEL_GUIDE_NAMES = KNOWN_CHANNEL_NAMES


# The values intentionally state supported, unsupported, or conditional
# behavior plainly.  Manual channels are not treated as Bot destinations
# merely because they exist in the Life Server.
CHANNEL_GUIDE_BODIES: dict[str, str] = {
    "一般": (
        "目的: 毎日の文章・URL・写真を何でも投げる、生活Inboxの主入口です。\n"
        "投稿するもの: 支出・食事・メモ・独り言・思い出・タスク・買い物・就活・"
        "音ゲー進捗など、あとで整理したい原文と添付。\n"
        "投稿しないもの: パスワード、Bot/AI/プロバイダーのトークン、Cookie、"
        "カード番号・口座番号の全桁などの秘密情報。\n"
        "例: `680 マック` / `あとで研究計画を整理する` / 食事写真 + `昼ごはん`\n"
        "Bot自動処理: ローカルDBと #一般アーカイブ へ原文・添付を先に保存し、"
        "候補ボタンと絵文字リアクションを表示します。保存先へのコピー成功後だけ"
        "元投稿を削除し、失敗時は元投稿を残します。\n"
        "結果を見る: 主閲覧は保護された生活マイページです。Discordでは"
        " #一般アーカイブ と用途別チャンネル、`/mypage` の案内を確認します。"
    ),
    "一般アーカイブ": (
        "目的: #一般 に届いた原文・添付を、分類や元投稿削除より先に保管する"
        "Bot管理のバックアップです。\n"
        "投稿するもの: 原則ありません。Botが出典・Event ID付きで自動保存します。\n"
        "投稿しないもの: 手動の新規入力、分類結果の編集、パスワード、トークン、Cookie、"
        "カード番号・口座番号の全桁。新規入力は #一般 を使います。\n"
        "例: `【#一般 自動アーカイブ】 Event #16` のようなBot投稿を読みます。\n"
        "Bot自動処理: アーカイブ送信と添付保存が成功した後にだけ分類候補を出します。"
        "アーカイブ失敗時は分類・削除を止め、#一般 の元投稿を残します。\n"
        "結果を見る: 原文確認はこのチャンネル、検索・一覧・構造化表示は"
        "保護された生活マイページを使います。Notionは安全対象だけの二次バックアップです。"
    ),
    "なんでも入力": (
        "目的: 生活Inboxの主入口です。文章や画像を一度受け取り、用途ごとの保存・"
        "振り分けを試します。\n"
        "投稿するもの: 支出・食事・外食・メモ・独り言・思い出・SNS投稿下書き・"
        "生活タスク・音ゲーTODO/リザルトなどの原文と添付。\n"
        "投稿しないもの: パスワード、Bot/AI/プロバイダーのトークン、Cookie、"
        "カード番号全桁などの秘密情報。\n"
        "例: `680 マック` / `メモ: 読書を続ける` / `今日の投稿: おつかれさま`\n"
        "Bot自動処理: 原文・添付を先に保存し、判定できる用途の候補をボタンと"
        "絵文字リアクションで表示します。どちらかを選ぶと同じ振り分け処理を実行し、"
        "送信成功後だけ元投稿を削除します。曖昧・未対応・解析失敗は保留になり、"
        "自動確定を保証しません。\n"
        "結果を見る: 元投稿への返答、`/inbox`、用途別の #一言メモ・#独り言・#外食・#思い出・"
        "#毎日の投稿・#生活-記録・#音ゲー、"
        "およびローカルの `/mypage` で確認します。"
    ),
    "一言メモ": (
        "目的: 後で読み返したい短い考え・記録を保存します。\n"
        "投稿するもの: メモ本文や、本文に添える画像。パスワード、トークン、Cookie、"
        "カード番号全桁は投稿しません。\n"
        "投稿しないもの: 家計・口座操作として処理したい入力、秘密情報。\n"
        "例: `今日わかったこと: 睡眠を優先する` / `/memos`\n"
        "Bot自動処理: `MEMO_CHANNEL_ID` に設定された場合は本文を一言メモとして保存します。"
        "未設定なら専用チャンネルからの自動処理は保証しません。\n"
        "結果を見る: 保存後の返答、#一言メモ のカード、`/memos`、ローカル `/mypage` です。"
        "#一般 の `メモ:` から振り分けることもできます。"
    ),
    "独り言": (
        "目的: 気軽な考え・気分・感情を書き留めます。構造化されたメモやタスクとして"
        "扱う必要はありません。\n"
        "投稿するもの: ふと思ったこと、感じたこと、短いつぶやき。\n"
        "投稿しないもの: パスワード、トークン、Cookie、カード番号全桁などの秘密情報。\n"
        "例: `今日はちょっと疲れた` / `独り言: 明日はゆっくりしたい`\n"
        "Bot自動処理: #一般で独り言候補を選ぶと、原文と保存済み添付を"
        "このチャンネルへ出典付きでコピーします。通常投稿を構造化メモ・タスクとして"
        "自動処理はしません。\n"
        "結果を見る: このチャンネルの履歴と、#一般の振り分け結果を確認します。"
    ),
    "外食": (
        "目的: レストラン・カフェ・テイクアウトなど、外で楽しんだ食事を振り返ります。\n"
        "投稿するもの: 店名、食べたもの、金額、外食の感想。\n"
        "投稿しないもの: パスワード、トークン、Cookie、カード番号全桁などの秘密情報。\n"
        "例: `外食: サイゼリヤ 1200円` / `カフェで昼食`\n"
        "Bot自動処理: #一般で外食候補を選ぶと、原文と保存済み添付を"
        "このチャンネルへ出典付きでコピーします。日常の食事・自炊は #食事 で扱います。\n"
        "結果を見る: このチャンネルの履歴と、#一般の振り分け結果を確認します。"
    ),
    "思い出": (
        "目的: 思い出・記念として残しておきたい出来事を保管します。\n"
        "投稿するもの: 記念の日の文章、思い出の写真や動画、後で振り返りたい出来事。\n"
        "投稿しないもの: パスワード、トークン、Cookie、カード番号全桁などの秘密情報。\n"
        "例: `思い出として残す: 初めてAJを取った日` / 記念写真\n"
        "Bot自動処理: #一般で思い出候補を選ぶと、原文と保存済み添付を"
        "このチャンネルへ出典付きでコピーします。画像だけで思い出とは判定しません。\n"
        "結果を見る: このチャンネルの履歴と、#一般の振り分け結果を確認します。"
    ),
    "毎日の投稿": (
        "目的: X・Instagramなど外部SNSへの投稿を検討するための、"
        "毎日のローカル下書きをまとめます。\n"
        "投稿するもの: 外部SNSへの投稿候補としてローカルに保存する投稿文の下書きと、"
        "現在保存できる画像（JPEG・PNG・WebP・GIF）。\n"
        "投稿しないもの: パスワード、トークン、Cookie、カード番号全桁などの秘密情報。\n"
        "例: `今日の投稿: 今日も一日おつかれさま` + 画像\n"
        "Bot自動処理: 下書きを表示し、`X`・`Instagram`・`両方`・`保留`の選択肢を"
        "提示します。明示的な選択後も外部SNSには投稿せず、選択先をローカルSQLiteの"
        "outboxへ保存します。Instagram／両方には保存済み画像が必要で、動画の捕捉・保存は"
        "現在未対応です。OAuth・認証情報・外部SNS APIは使用しません。\n"
        "結果を見る: このチャンネルの下書き、ローカル選択結果、`/social`の状態を確認します。"
    ),
    "食事": (
        "目的: 食べたもの・外食・自炊の記録を残します。\n"
        "投稿するもの: 食事の本文や写真。店名・金額は分かる範囲で書きます。\n"
        "投稿しないもの: パスワード、トークン、Cookie、カード番号全桁などの秘密情報。\n"
        "例: `昼 ラーメン 900円` / 食事の写真1枚\n"
        "Bot自動処理: `MEAL_CHANNEL_ID` に設定された場合、原文を保存して解析候補を返します。"
        "内容の確定・修正は確認操作で行えます。未設定時の自動処理は保証しません。\n"
        "結果を見る: 元投稿への候補・確認応答、`/meals`、`/today`、#生活-記録、"
        "ローカル `/mypage` です。"
    ),
    "買い物リスト": (
        "目的: 買う物を手動で並べるためのリストです。\n"
        "投稿するもの: 商品名、数量、購入済みかどうかの手動メモ。\n"
        "投稿しないもの: パスワード、トークン、Cookie、カード番号全桁、個人用決済URL。\n"
        "例: `牛乳 1本` / `洗剤を買う`\n"
        "Bot自動処理: #一般で買い物リスト候補を選ぶと、原文と保存済み添付を"
        "このチャンネルへ出典付きでコピーします。候補が不明な投稿は保留されます。\n"
        "結果を見る: 手動リストとBotが振り分けた原文はこのチャンネルの履歴です。"
        "専用の商品集計や購入状態の自動更新は未提供です。"
    ),
    "神リンク": (
        "目的: 後で使うWebリンクを手動で保管します。\n"
        "投稿するもの: ページのURLと、何のリンクか分かる短い説明。\n"
        "投稿しないもの: パスワード、トークン、Cookie、認証付きURLや秘密を含むクエリ。\n"
        "例: `https://example.com — 参考資料` / `公式申込ページ`\n"
        "Bot自動処理: #一般で神リンク候補を選ぶと、URLを含む原文と保存済み添付を"
        "このチャンネルへ出典付きでコピーします。URLの自動要約・検査は行いません。\n"
        "結果を見る: 手動で残したリンクと、Botが振り分けた原文をこのチャンネルの"
        "履歴から確認します。"
        "自動保存したい文章は #一般 へ送ります。"
    ),
    "日課・リマインダー": (
        "目的: 期限付きタスクと日課の通知をまとめて確認する場所です。\n"
        "投稿するもの: 通知に関する手動メモ。通常はBot通知を読むだけにします。\n"
        "投稿しないもの: パスワード、トークン、Cookie、カード番号全桁などの秘密情報。\n"
        "例: `/日課追加 title:ストレッチ time:21:00` / `/日課完了 routine_id:1` / "
        "`/タスク追加 title:提出 due:2026-09-05 due_time:18:30`\n"
        "Bot自動処理: リマインダー設定と通知先設定が有効な場合だけBotが通知します。"
        "このチャンネルへの通常投稿を日課として自動登録はしません。\n"
        "結果を見る: 通知はこのチャンネル、一覧・完了状態は許可された場所で"
        "`/日課一覧`・`/タスク一覧`、ローカル `/mypage` から確認します。"
    ),
    "就活情報": (
        "目的: 会社・求人・説明会・応募に関する情報を手動で整理します。\n"
        "投稿するもの: 会社名、職種、期限、次の行動、公開URLなど。\n"
        "投稿しないもの: 求人サイトのパスワード、セッションCookie、非公開トークン、"
        "カード番号全桁。\n"
        "例: `/就活追加 company:株式会社A title:開発職 deadline:2026-09-15` / "
        "`/就活一覧` / `/就活更新 opportunity_id:1 stage:interview`\n"
        "Bot自動処理: #一般で就活情報候補を選ぶと、原文と保存済み添付を"
        "このチャンネルへ出典付きでコピーします。曖昧なものは保留として確認します。\n"
        "結果を見る: 手動メモとBotが振り分けた原文はこの履歴、構造化済みの就活記録は"
        "許可された場所の`/就活一覧` とローカル `/career` で確認します。"
        "外部サービス取込は未保証です。"
    ),
    "クレカ": (
        "目的: 登録済みクレジットカードで支払った支出を入力します。\n"
        "投稿するもの: 金額・店名・任意の登録済みカード表示名。\n"
        "投稿しないもの: カード番号全桁、セキュリティコード、パスワード、トークン、"
        "Cookie、明細サイトの認証情報。\n"
        "例: `680 マック` / `[楽天カード] 1280 Amazon` / レシート画像 + 金額・店名\n"
        "最初の登録: #口座 で `/account_add name:楽天カード kind:クレジットカード "
        "opening_balance:0` を実行します。opening_balance は利用限度額ではなく、"
        "登録時点の未払い利用残高を正数で入力します。次に #口座 で"
        " `/引落口座設定 card_name:楽天カード bank_name:ゆうちょ` を実行します。\n"
        "Bot自動処理: `CARD_CHANNEL_ID` に設定された場合、原文保存と支出候補の解析を行い、"
        "確認操作を返します。`[カード名]` を省略した場合は既定カードを使います。"
        "カード代金の銀行引落は新しい支出ではないため、#口座 で"
        " `[ゆうちょ] カード引落 68000 -> [楽天カード] 8月分` と記録します。"
        "未設定時は #一般 を使います。\n"
        "結果を見る: 元投稿への候補・確認応答、`/today`・`/detail`・`/spending`、"
        "#生活-記録、`/accounts`、`/引落口座一覧`、ローカル `/mypage` です。"
    ),
    "口座": (
        "目的: 口座・カード・現金などの入出金と振替を記録します。\n"
        "投稿するもの: 登録済み口座名、金額、入金・支出・振替の内容。"
        "カードごとの引落口座の設定・解除・一覧もここで行います。\n"
        "投稿しないもの: 口座番号全桁、暗証番号、パスワード、トークン、Cookie、"
        "カード番号全桁。\n"
        "登録手順: ① `/account_add name:ゆうちょ kind:銀行口座 opening_balance:50000` "
        "② `/account_add name:楽天カード kind:クレジットカード opening_balance:0` "
        "③ `/引落口座設定 card_name:楽天カード bank_name:ゆうちょ` "
        "④ `/accounts` と `/引落口座一覧` で確認します。複数の銀行・カードは"
        "表示名を変えて①〜③を繰り返します。\n"
        "例: `[ゆうちょ] 入金 250000 給料` / `[ゆうちょ] 支出 1200 電気代` / "
        "`[ゆうちょ] 振替 5000 -> [現金] ATM` / "
        "`[ゆうちょ] カード引落 68000 -> [楽天カード] 8月分` / "
        "`/引落口座設定 card_name:楽天カード bank_name:ゆうちょ` / "
        "`/引落口座解除 card_name:楽天カード` / `/引落口座一覧`\n"
        "Bot自動処理: `ACCOUNT_CHANNEL_ID` に設定された場合、対応する構文を保存します。"
        "不明な口座や形式は保留になり、未設定時の専用処理は保証しません。"
        "引落口座コマンドの応答は本人だけに表示され、口座番号などは扱いません。\n"
        "カードの開始残高は利用限度額ではなく未払い利用残高を正数で入力します。\n"
        "結果を見る: 元投稿への記録応答、`/accounts`、`/引落口座一覧`、`/detail`、#生活-記録、"
        "ローカル `/mypage` です。"
    ),
    "生活-記録": (
        "目的: 確定した家計・食事・メモ・音ゲーを閲覧し、毎日の家計サマリーを受け取ります。\n"
        "投稿するもの: 原則ありません。閲覧コマンドの結果を確認します。\n"
        "投稿しないもの: 新しい支出・口座操作・秘密情報。入力は用途別チャンネルか"
        " #一般 を使います。\n"
        "例: `/家計簿今日` / `/家計簿配信確認` / `/history count:10`\n"
        "Bot自動処理: 通常投稿を取り込みません。設定が有効なら毎日23:45（日本時間）に、"
        "当日合計・カテゴリ・外食など・月累計・予算・保留件数を投稿し、同日修正は既存投稿を更新します。\n"
        "結果を見る: 日次家計サマリーと、`/month`、`/detail`、`/accounts`、"
        "`/meals`、`/rhythm` などの"
        "本人向け応答と、ローカル `/mypage` です。"
    ),
    "音ゲー": (
        "目的: maimai・オンゲキ・チュウニズムのTODOとリザルトを記録します。\n"
        "投稿するもの: ゲーム名付きの課題・結果、またはTODOへの画像返信。\n"
        "投稿しないもの: ゲームサービスのパスワード、Cookie、APIトークン、"
        "カード番号全桁などの秘密情報。\n"
        "例: `todo maimai でらっくす譜面を詰める` / `result チュウニズム SSS+`\n"
        "Bot自動処理: `RHYTHM_CHANNEL_ID` に設定された場合、TODO/リザルトを保存します。"
        "ゲーム名が不明な入力は保留になり、未設定時の専用処理は保証しません。\n"
        "結果を見る: 元投稿への応答、`/rhythm`、`/progress_today`、ローカル `/mypage` です。"
    ),
    "今日の進捗": (
        "目的: 音ゲーの当日進捗を画像中心で残します。\n"
        "投稿するもの: ゲーム名付きのスクリーンショットを1枚以上、または既存TODOへの返信。\n"
        "投稿しないもの: パスワード、Cookie、APIトークン、カード番号全桁などの秘密情報。\n"
        "例: `maimai SSS+` + 画像 / `オンゲキ AB` + 画像\n"
        "Bot自動処理: `PROGRESS_CHANNEL_ID` に設定された場合、画像とゲーム名を保存します。"
        "画像なし・ゲーム不明の投稿は進捗として確定しません。\n"
        "結果を見る: 元投稿への応答、`/progress_today`、`/rhythm`、ローカル `/mypage` です。"
    ),
    "chunithm-レート": (
        "目的: CHUNITHM（ウニ）のレート取得結果をBotが投稿する専用表示先です。\n"
        "投稿するもの: 原則ありません。Botの結果と失敗・手動確認待ちを読みます。\n"
        "投稿しないもの: パスワード、Cookie、プロバイダー/APIトークン、カード番号全桁。\n"
        "例: `/レート game:ウニ（CHUNITHM）` / `/get game:ウニ（CHUNITHM）` / "
        "`/取得状況`\n"
        "Bot自動処理: 音ゲー設定・スケジューラー・投稿先が有効な場合だけ取得結果を投稿します。"
        "ログイン、CAPTCHA、規約確認などで手動待ちになることがあり、成功は保証しません。\n"
        "結果を見る: このチャンネルのBot投稿、`/レート`、`/取得状況`、ローカル `/mypage` です。"
    ),
    "maimai-レート": (
        "目的: maimai（マイ）のレート取得結果をBotが投稿する専用表示先です。\n"
        "投稿するもの: 原則ありません。Botの結果と失敗・手動確認待ちを読みます。\n"
        "投稿しないもの: パスワード、Cookie、SEGA ID、プロバイダー/APIトークン、"
        "カード番号全桁。\n"
        "例: `/レート game:マイ` / `/get game:マイ` / `/取得状況`\n"
        "Bot自動処理: 音ゲー設定・スケジューラー・投稿先が有効な場合だけ取得結果を投稿します。"
        "ブラウザー操作や手動確認が必要な場合は成功扱いにしません。\n"
        "結果を見る: このチャンネルのBot投稿、`/レート`、`/取得状況`、ローカル `/mypage` です。"
    ),
    "ongeki-レート": (
        "目的: オンゲキ（ゲキ）のレート取得結果をBotが投稿する専用表示先です。\n"
        "投稿するもの: 原則ありません。Botの結果と失敗・手動確認待ちを読みます。\n"
        "投稿しないもの: パスワード、Cookie、SEGA ID、プロバイダー/APIトークン、"
        "カード番号全桁。\n"
        "例: `/レート game:ゲキ` / `/get game:ゲキ` / `/取得状況`\n"
        "Bot自動処理: 音ゲー設定・スケジューラー・投稿先が有効な場合だけ取得結果を投稿します。"
        "ログインや手動確認が必要な場合は自動成功を保証しません。\n"
        "結果を見る: このチャンネルのBot投稿、`/レート`、`/取得状況`、ローカル `/mypage` です。"
    ),
    "音ゲー利用額": (
        "目的: iPhone同期された音ゲー利用額のBot投稿を確認します。\n"
        "投稿するもの: 原則ありません。同期結果・再投稿結果を読みます。\n"
        "投稿しないもの: iPhone専用トークン、パスワード、Cookie、カード番号全桁、"
        "プロバイダー認証情報。\n"
        "例: `/今日の利用額` / `/今日の利用額 date_text:2026-08-29`\n"
        "Bot自動処理: 音ゲー機能、スケジューラー、同期済みデータ、投稿先設定がそろった場合だけ"
        "投稿します。未同期・設定不足の日は投稿されません。\n"
        "結果を見る: このチャンネルの投稿、`/今日の利用額`、音ゲーの取得状況を確認します。"
    ),
    "定期更新スケジュール": (
        "目的: Botの定期取得・日次通知・即時処理が、いつ何をするかを一覧で確認します。\n"
        "投稿するもの: 原則ありません。固定された運用タイムラインと更新日時を読みます。\n"
        "投稿しないもの: 個人記録、パスワード、Cookie、Bot/AI/プロバイダーのトークン、"
        "カード番号・口座番号の全桁。\n"
        "例: `10:00 音ゲーレート画像取得` / `23:55 音ゲー利用額日次まとめ` / "
        "`/get game:マイ`\n"
        "Bot自動処理: タイムラインは現在のBot設定から生成します。無効な処理は無効と明記し、"
        "固定時刻の経過だけを外部取得成功とは扱いません。認証・手動確認待ちは別状態です。\n"
        "結果を見る: このチャンネルの固定タイムライン、各機種レートチャンネル、"
        "#音ゲー利用額、#生活-記録、#bot-エラー、`/取得状況` を確認します。"
    ),
    "bot-コマンド一覧": (
        "目的: 既存の複数パートのBotコマンド案内を見つける場所です。\n"
        "投稿するもの: 原則ありません。案内を読み、通常の入力は #一般 へ送ります。\n"
        "投稿しないもの: 個人記録、パスワード、Bot/AI/プロバイダーのトークン、"
        "Cookie、カード番号全桁。\n"
        "例: `/コマンド一覧` / `/家計簿今日` / `/振り返り` / `/mypage`\n"
        "Bot自動処理: `provision-command-guide` が管理する既存の複数メッセージは別管理です。"
        "このチャンネル案内は、そのメッセージを編集・削除・複製しません。\n"
        "結果を見る: 固定されたこの案内から下部のBot管理コマンド案内へ進みます。"
        "Mac内Web、IAP保護されたクラウドWeb、Notion Hubの設定済みリンクも"
        "Bot管理案内の最終パートで確認できます。"
    ),
    "bot-エラー": (
        "目的: 音ゲー取得などで発生した、利用者向けにサニタイズ済みのBotエラーを確認します。\n"
        "投稿するもの: 必要なら発生日時、run ID、実行したコマンドなどの非秘密情報。\n"
        "投稿しないもの: パスワード、Cookie、Bot/AI/プロバイダー資格情報、カード番号全桁。"
        "プロバイダー資格情報はDiscordへ投稿しません。\n"
        "例: `/取得状況` / `/認証状況`\n"
        "Bot自動処理: `DISCORD_ERROR_CHANNEL_ID` がこのチャンネルに設定され、"
        "対象処理が失敗したときだけ、本文をサニタイズして通知します。全エラーの掲載は保証しません。\n"
        "結果を見る: このチャンネルのサニタイズ済み通知、`/取得状況`、`/認証状況` です。"
    ),
    "ai-振り返り": (
        "目的: 今日または今週の保存済み構造化記録を、本人だけの応答で振り返ります。\n"
        "投稿するもの: 自分で確認した振り返り、次に試すこと、判定への補足。\n"
        "投稿しないもの: パスワード、トークン、Cookie、カード番号全桁、外部サービスの秘密。\n"
        "例: `/振り返り period:今日` / `/振り返り period:今週` / `次回は予定を前日に確認する`\n"
        "Bot自動処理: コマンド実行時だけ集計します。既定は外部AIへ送らないローカル処理です。"
        "外部AIを明示的に有効化しても、生本文・店名・添付・URL・資格情報は送信対象にしません。\n"
        "結果を見る: `/振り返り` の本人限定応答と、このチャンネルへ手動で残した補足です。"
    ),
}


_GUIDE_MARKER_RE = re.compile(
    rf"`({re.escape(CHANNEL_GUIDE_MARKER)}:v\d+):([^`\r\n]+)`"
)


def _guide_marker(channel_name: str) -> str:
    """Return the raw, channel-specific marker used in managed messages."""

    if channel_name not in CHANNEL_GUIDE_BODIES:
        raise ValueError("unknown Life Server channel name")
    return f"{GUIDE_VERSION}:{channel_name}"


def channel_guide_marker(channel_name: str) -> str:
    """Public alias for the deterministic channel-specific marker."""

    return _guide_marker(channel_name)


def _guide_channel(content: str | None) -> str | None:
    if not content:
        return None
    match = _GUIDE_MARKER_RE.search(content)
    return match.group(2) if match is not None else None


def _is_managed_guide(content: str | None, channel_name: str) -> bool:
    return _guide_channel(content) == channel_name


def channel_guide_message(channel_name: str) -> str:
    """Render exactly one deterministic guide for one known channel."""

    if channel_name not in CHANNEL_GUIDE_BODIES:
        raise ValueError("unknown Life Server channel name")
    content = (
        f"【#{channel_name} の使い方】\n"
        f"`{_guide_marker(channel_name)}`\n\n"
        f"{CHANNEL_GUIDE_BODIES[channel_name]}"
    )
    if len(content) > DISCORD_MESSAGE_LIMIT:
        raise ValueError("channel guide exceeds Discord message limit")
    return content


def channel_guide_messages() -> tuple[str, ...]:
    """Return all guide messages in the reviewed channel order."""

    return tuple(channel_guide_message(name) for name in KNOWN_CHANNEL_NAMES)


def channel_guide_contents() -> dict[str, str]:
    """Return a name-to-message view without exposing mutable source bodies."""

    return {
        name: channel_guide_message(name)
        for name in KNOWN_CHANNEL_NAMES
    }


@dataclass(frozen=True, slots=True)
class ChannelGuideFailure:
    """A safe per-channel failure description without Discord response bodies."""

    channel_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class ChannelGuideProvisionResult:
    """Summary of one guild-scoped guide provisioning pass."""

    known_channels: int
    channels_found: int
    channels_processed: int
    channels_missing: int
    channels_failed: int
    messages_created: int
    messages_updated: int
    messages_unchanged: int
    messages_pinned: int
    messages_pin_skipped: int
    missing_channel_names: tuple[str, ...] = ()
    duplicate_channel_names: tuple[str, ...] = ()
    failed_channels: tuple[ChannelGuideFailure, ...] = ()
    pin_skipped_channels: tuple[str, ...] = ()
    duplicate_managed_messages: int = 0

    @property
    def pinned_count(self) -> int:
        """Compatibility/readability alias for CLI and callers."""

        return self.messages_pinned

    @property
    def pin_skipped_count(self) -> int:
        """Number of guides that were not newly pinned in this pass."""

        return self.messages_pin_skipped

    @property
    def has_failures(self) -> bool:
        return self.channels_failed > 0


@dataclass(slots=True)
class _ProvisionAccumulator:
    channels_found: int = 0
    channels_processed: int = 0
    channels_missing: int = 0
    channels_failed: int = 0
    messages_created: int = 0
    messages_updated: int = 0
    messages_unchanged: int = 0
    messages_pinned: int = 0
    messages_pin_skipped: int = 0
    missing_channel_names: list[str] | None = None
    duplicate_channel_names: list[str] | None = None
    failed_channels: list[ChannelGuideFailure] | None = None
    pin_skipped_channels: list[str] | None = None
    duplicate_managed_messages: int = 0

    def __post_init__(self) -> None:
        self.missing_channel_names = []
        self.duplicate_channel_names = []
        self.failed_channels = []
        self.pin_skipped_channels = []

    def result(self) -> ChannelGuideProvisionResult:
        return ChannelGuideProvisionResult(
            known_channels=len(KNOWN_CHANNEL_NAMES),
            channels_found=self.channels_found,
            channels_processed=self.channels_processed,
            channels_missing=self.channels_missing,
            channels_failed=self.channels_failed,
            messages_created=self.messages_created,
            messages_updated=self.messages_updated,
            messages_unchanged=self.messages_unchanged,
            messages_pinned=self.messages_pinned,
            messages_pin_skipped=self.messages_pin_skipped,
            missing_channel_names=tuple(self.missing_channel_names or ()),
            duplicate_channel_names=tuple(self.duplicate_channel_names or ()),
            failed_channels=tuple(self.failed_channels or ()),
            pin_skipped_channels=tuple(self.pin_skipped_channels or ()),
            duplicate_managed_messages=self.duplicate_managed_messages,
        )


def _same_id(expected: Any, actual: Any) -> bool:
    return expected is not None and actual is not None and str(expected) == str(actual)


def _is_text_channel(channel: Any) -> bool:
    """Accept real text channels and deliberately small test fakes."""

    if channel is None or not callable(getattr(channel, "send", None)):
        return False
    if isinstance(channel, discord.TextChannel):
        return True
    channel_type = getattr(channel, "type", None)
    if channel_type is None:
        return True
    return channel_type == discord.ChannelType.text


async def _history_messages(channel: Any):
    history = channel.history(limit=None)
    if hasattr(history, "__aiter__"):
        async for message in history:
            yield message
        return
    for message in history:
        yield message


def _message_order_key(message: Any, fallback_index: int) -> tuple[int, int | float]:
    """Choose the newest message deterministically without message content."""

    message_id = getattr(message, "id", None)
    try:
        return (3, int(message_id))
    except (TypeError, ValueError, OverflowError):
        pass
    created_at = getattr(message, "created_at", None)
    if isinstance(created_at, datetime):
        try:
            return (2, created_at.timestamp())
        except (OverflowError, OSError, ValueError):
            pass
    # discord.py history is newest-first.  The negative index preserves that
    # ordering for deliberately minimal fakes that expose neither ID nor time.
    return (1, float(-fallback_index))


def _newest_message(messages: list[Any]) -> Any:
    return max(
        enumerate(messages),
        key=lambda item: _message_order_key(item[1], item[0]),
    )[1]


def _safe_provision_failure(exc: Exception) -> str:
    """Classify failures without reflecting response bodies, URLs, or secrets."""

    if isinstance(exc, discord.Forbidden):
        return "Botに対象チャンネルの閲覧・投稿・固定権限がありません"
    if isinstance(exc, discord.NotFound):
        return "生活サーバーまたは対象チャンネルを確認できませんでした"
    known_messages = {
        "生活サーバーを取得できませんでした",
        "Botユーザーを確認できませんでした",
        "設定済みGuild以外は処理しません",
    }
    if isinstance(exc, RuntimeError) and str(exc) in known_messages:
        return str(exc)
    return "Discord側の処理を完了できませんでした"


async def _pin_if_needed(
    message: Any,
    channel_name: str,
    accumulator: _ProvisionAccumulator,
) -> None:
    if bool(getattr(message, "pinned", False)):
        accumulator.messages_pin_skipped += 1
        accumulator.pin_skipped_channels.append(channel_name)
        return
    pin = getattr(message, "pin", None)
    if not callable(pin):
        accumulator.messages_pin_skipped += 1
        accumulator.pin_skipped_channels.append(channel_name)
        logger.warning("channel guide pin unavailable (%s)", channel_name)
        return
    try:
        await pin()
    except Exception as exc:
        # A pin is useful but not required for a valid guide.  Do not include
        # Discord's exception text: it can contain response bodies or URLs.
        accumulator.messages_pin_skipped += 1
        accumulator.pin_skipped_channels.append(channel_name)
        logger.warning("channel guide pin skipped (%s)", type(exc).__name__)
        return
    accumulator.messages_pinned += 1


async def _provision_guild_guides(
    guild: Any,
    *,
    bot_user_id: Any,
) -> ChannelGuideProvisionResult:
    accumulator = _ProvisionAccumulator()
    all_channels = tuple(getattr(guild, "text_channels", ()))

    for channel_name in KNOWN_CHANNEL_NAMES:
        named_channels = [
            channel
            for channel in all_channels
            if getattr(channel, "name", None) == channel_name
            and _is_text_channel(channel)
        ]
        accumulator.channels_found += len(named_channels)
        if not named_channels:
            accumulator.channels_missing += 1
            accumulator.missing_channel_names.append(channel_name)
            continue
        if len(named_channels) > 1:
            # The same name does not identify one safe destination.  Do not
            # inspect or mutate any of these channels.
            accumulator.channels_failed += 1
            accumulator.duplicate_channel_names.append(channel_name)
            accumulator.failed_channels.append(
                ChannelGuideFailure(channel_name, "同名のテキストチャンネルが複数あります")
            )
            continue

        channel = named_channels[0]
        managed_messages: list[Any] = []
        try:
            async for message in _history_messages(channel):
                author = getattr(message, "author", None)
                author_id = getattr(author, "id", getattr(message, "author_id", None))
                if _same_id(bot_user_id, author_id) and _is_managed_guide(
                    getattr(message, "content", None), channel_name
                ):
                    managed_messages.append(message)
        except Exception as exc:
            accumulator.channels_failed += 1
            accumulator.failed_channels.append(
                ChannelGuideFailure(channel_name, "チャンネル履歴を確認できませんでした")
            )
            logger.warning("channel guide history failed (%s)", type(exc).__name__)
            continue

        accumulator.duplicate_managed_messages += max(0, len(managed_messages) - 1)
        existing = _newest_message(managed_messages) if managed_messages else None
        desired_content = channel_guide_message(channel_name)
        if existing is None:
            try:
                managed_message = await channel.send(desired_content)
            except Exception as exc:
                accumulator.channels_failed += 1
                accumulator.failed_channels.append(
                    ChannelGuideFailure(channel_name, "案内を投稿できませんでした")
                )
                logger.warning("channel guide send failed (%s)", type(exc).__name__)
                continue
            accumulator.messages_created += 1
        else:
            managed_message = existing
            if getattr(existing, "content", None) != desired_content:
                try:
                    edited = await existing.edit(content=desired_content)
                    if edited is not None:
                        managed_message = edited
                except Exception as exc:
                    accumulator.channels_failed += 1
                    accumulator.failed_channels.append(
                        ChannelGuideFailure(channel_name, "案内を更新できませんでした")
                    )
                    logger.warning("channel guide edit failed (%s)", type(exc).__name__)
                    continue
                accumulator.messages_updated += 1
            else:
                accumulator.messages_unchanged += 1

        accumulator.channels_processed += 1
        await _pin_if_needed(managed_message, channel_name, accumulator)

    return accumulator.result()


class _ChannelGuideClient(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.settings = settings
        self.result: ChannelGuideProvisionResult | None = None
        self.failure: Exception | None = None

    async def on_ready(self) -> None:
        try:
            guild_id = self.settings.discord_guild_id
            if guild_id is None:
                raise RuntimeError("設定済みGuild以外は処理しません")
            guild = self.get_guild(guild_id)
            if guild is None:
                raise RuntimeError("生活サーバーを取得できませんでした")
            actual_guild_id = getattr(guild, "id", None)
            if actual_guild_id is not None and not _same_id(guild_id, actual_guild_id):
                raise RuntimeError("設定済みGuild以外は処理しません")
            if self.user is None or getattr(self.user, "id", None) is None:
                raise RuntimeError("Botユーザーを確認できませんでした")
            self.result = await _provision_guild_guides(
                guild,
                bot_user_id=self.user.id,
            )
        except Exception as exc:
            self.failure = exc
        finally:
            await self.close()


def provision_channel_guides(settings: Settings) -> ChannelGuideProvisionResult:
    """Provision all known guides once using the configured guild and Bot token."""

    settings.validate_for_bot()
    if settings.discord_bot_token is None:
        raise ValueError("DISCORD_BOT_TOKEN is required")
    client = _ChannelGuideClient(settings)
    try:
        client.run(settings.discord_bot_token.get_secret_value(), log_handler=None)
    except Exception as exc:
        detail = _safe_provision_failure(exc)
        raise RuntimeError(f"チャンネル利用案内の作成・更新に失敗しました: {detail}") from exc
    if client.failure is not None:
        detail = _safe_provision_failure(client.failure)
        raise RuntimeError(
            f"チャンネル利用案内の作成・更新に失敗しました: {detail}"
        ) from client.failure
    if client.result is None:
        raise RuntimeError("チャンネル利用案内の結果を確認できませんでした")
    return client.result


__all__ = [
    "CHANNEL_GUIDE_BODIES",
    "CHANNEL_GUIDE_MARKER",
    "CHANNEL_GUIDE_NAMES",
    "CHANNEL_GUIDE_VERSION",
    "ChannelGuideFailure",
    "ChannelGuideProvisionResult",
    "DISCORD_MESSAGE_LIMIT",
    "GUIDE_VERSION",
    "KNOWN_CHANNEL_NAMES",
    "channel_guide_contents",
    "channel_guide_marker",
    "channel_guide_message",
    "channel_guide_messages",
    "provision_channel_guides",
]
