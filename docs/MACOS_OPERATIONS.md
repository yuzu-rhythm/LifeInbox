# macOS 常駐運用（任意）

公開用テンプレートの説明です。今回の確認はテスト・dry runのみで、常駐登録は実施していません。

テンプレートを用意しただけではインストール、ロード、外部サービスへの接続は行いません。

## 追加された資産

```text
scripts/macos/
├── com.life-inbox.bot.plist.template
├── com.life-inbox.web.plist.template
├── com.life-inbox.backup.plist.template
├── manage_launch_agents.sh
└── status_launch_agents.sh
```

テンプレートの `__ABS_PROJECT_DIR__`、`__ABS_PYTHON_BIN__`、`__ABS_LOG_DIR__` は、未解決のまま使うための環境変数ではありません。helper が検証済みの絶対パスへ置換します。raw template を直接 `launchctl` に渡さないでください。

Bot と Web は次のポリシーです。

- `WorkingDirectory` はプロジェクトの絶対パスです。
- `ProgramArguments` はプロジェクト内 `.venv/bin/python -m life_inbox bot|web` です。シェルの PATH や `python` の解決には依存しません。
- `RunAtLoad` は有効です。
- `KeepAlive.SuccessfulExit=false` と `ThrottleInterval=30` により、異常終了は再起動しますが、意図した正常終了後に再起動ループを作りません。
- stdout/stderr はプロジェクトの `logs/` に分離して書きます。既存の `.gitignore` で `logs/` は除外されています。

Backup は `StartCalendarInterval` の毎日 03:15（Mac のローカル時刻）に一度だけ起動します。引数は既存の `python -m life_inbox backup` そのもので、`--output-dir` を追加していません。そのため、通常は `WorkingDirectory` 配下の `data/backups`、`DATA_DIR` を明示設定している場合は既存アプリの `DATA_DIR/backups` へ保存されます。Backup 自体は既存実装の SQLite `integrity_check`、`foreign_key_check`、参照添付ファイルのパス・内容検証を通過してから archive を公開します。

## 前提と初回セットアップ

プロジェクトとユーザー LaunchAgents のパスは、必ず実際の絶対パスへ置き換えてください。`~`、相対パス、`HOME` の未展開文字列は指定しません。

```bash
PROJECT_DIR="/absolute/path/to/簡単メモbot"
LAUNCH_AGENTS_DIR="/absolute/example-user/Library/LaunchAgents"

test -x "$PROJECT_DIR/.venv/bin/python"
mkdir -p "$PROJECT_DIR/logs" "$PROJECT_DIR/data/backups"
```

`.env` / `.env.local` はプロジェクトの既存設定として、プロジェクトディレクトリに利用者が安全に配置します。LaunchAgent の plist に Bot Token、API key、Cookie、パスワード、環境ファイルの内容を埋め込まないでください。アプリは `WorkingDirectory` が設定された状態で起動するため、通常の CLI と同じ相対位置から設定ファイルを読みます。launchd はシェルの profile を読むとは限らないため、依存する PATH 設定を profile にだけ置かないでください。

必要なら、常駐化の前に通常のローカル初期化と設定確認を行います。

```bash
"$PROJECT_DIR/.venv/bin/python" -m life_inbox init-db
"$PROJECT_DIR/.venv/bin/python" -m life_inbox check
```

`check` はアプリ側の secret-safe summary です。設定ファイルを表示する `cat`、`printenv`、plist への secret のコピーは行わないでください。

## Dry run / preview（既定）

引数なしの実行モードは DRY RUN です。次のコマンドは一時ディレクトリ内へ 3 つの plist をレンダーし、各ファイルを `plutil -lint` で検証した後、一時ファイルを削除します。ユーザーの `Library/LaunchAgents`、プロジェクトの plist、launchd は変更しません。

```bash
/bin/bash "$PROJECT_DIR/scripts/macos/manage_launch_agents.sh" \
  --project-dir "$PROJECT_DIR" \
  --launch-agents-dir "$LAUNCH_AGENTS_DIR"
```

出力に `DRY RUN`、`Would install`、`launchctl was not called` が含まれることを確認します。helper は、Python パスがプロジェクトの `.venv` 配下であること、ログパスがプロジェクトの `logs/` 配下であること、インストール先が指定された単一の `.../Library/LaunchAgents` 配下であることも検証します。既存の異なる plist は `--replace` を明示しない限り上書きしません。

`--dry-run` と `--preview` は既定動作を明示する別名です。

## インストールとロード

まず、DRY RUN の出力とテンプレートの内容をレビューします。レビュー後も、ロードせず plist だけを置く場合は `--install` だけを指定します。

```bash
/bin/bash "$PROJECT_DIR/scripts/macos/manage_launch_agents.sh" \
  --project-dir "$PROJECT_DIR" \
  --launch-agents-dir "$LAUNCH_AGENTS_DIR" \
  --install
```

異なる内容の同名 plist が既にある場合は、安全のため処理を停止します。既存ファイルを置き換える判断をした場合だけ、対象を確認した上で `--replace` を追加します。

```bash
/bin/bash "$PROJECT_DIR/scripts/macos/manage_launch_agents.sh" \
  --project-dir "$PROJECT_DIR" \
  --launch-agents-dir "$LAUNCH_AGENTS_DIR" \
  --install --replace
```

ロードまで行う `--load` は独立した明示フラグです。`--load` 単独では実行できず、`--install --load` の組み合わせが必要です。既に同じ label がロード済みの場合は、helper は自動 bootout せずに停止します。先に「Unload」を実行してから、再度 `--install --load` を実行してください。

```bash
/bin/bash "$PROJECT_DIR/scripts/macos/manage_launch_agents.sh" \
  --project-dir "$PROJECT_DIR" \
  --launch-agents-dir "$LAUNCH_AGENTS_DIR" \
  --install --load
```

`--load` を指定しない `--install` は、plist を置くだけで `launchctl` を呼びません。各 plist は 0600 で、known label ごとに原子的に配置されます。

## Unload

停止・更新・ロールバック前には、3 つの known label をユーザー GUI domain から明示的に unload できます。plist ファイル自体は削除しません。

```bash
/bin/bash "$PROJECT_DIR/scripts/macos/manage_launch_agents.sh" \
  --project-dir "$PROJECT_DIR" \
  --launch-agents-dir "$LAUNCH_AGENTS_DIR" \
  --unload
```

この操作は launchd の状態を変更するため、DRY RUN ではありません。`--unload` は `--install`、`--load`、`--replace` と併用できません。

## Status / diagnostics

status helper は既知の 3 label と 3 plist だけを対象にします。plist の存在、`plutil -lint`、Python、ログディレクトリ、既定バックアップディレクトリを確認し、launchd の loaded/not loaded は終了コードだけを捨てた読み取り結果として表示します。launchctl の詳細出力をそのまま表示しないため、プロセス環境を診断出力へ流しません。

```bash
/bin/bash "$PROJECT_DIR/scripts/macos/status_launch_agents.sh" \
  --project-dir "$PROJECT_DIR" \
  --launch-agents-dir "$LAUNCH_AGENTS_DIR"
```

アプリ側の secret-safe summary も必要なときだけ、`--app-check` を追加します。

```bash
/bin/bash "$PROJECT_DIR/scripts/macos/status_launch_agents.sh" \
  --project-dir "$PROJECT_DIR" \
  --launch-agents-dir "$LAUNCH_AGENTS_DIR" \
  --app-check
```

status は未インストールの plist があると終了コード 1 になります。これはインストール済みと誤認しないための状態です。status helper は環境ファイルの内容を source、cat、表示しません。

## Logs

ログは次のプロジェクトローカルファイルです。

```text
logs/bot.stdout.log
logs/bot.stderr.log
logs/web.stdout.log
logs/web.stderr.log
logs/backup.stdout.log
logs/backup.stderr.log
```

確認には既知のファイルだけを指定します。

```bash
/usr/bin/tail -n 100 "$PROJECT_DIR/logs/bot.stderr.log"
/usr/bin/tail -n 100 "$PROJECT_DIR/logs/web.stderr.log"
/usr/bin/tail -n 100 "$PROJECT_DIR/logs/backup.stderr.log"
```

ログは個人データを含み得るため共有しません。`.env`、`.env.local`、Token、API key、Cookie の内容をログへコピーしないでください。Bot/Web の例外は既存コード側でも秘密値や生の個人本文を避ける方針ですが、常駐ログは private data として扱います。

## Backup verification

手動のバックアップ作成は、常駐 agent と同じ既存コマンドです。

```bash
"$PROJECT_DIR/.venv/bin/python" -m life_inbox backup
```

成功 archive は通常 `$PROJECT_DIR/data/backups/backup-YYYYMMDDTHHMMSSZ.zip` です。明示した `DATA_DIR` がある場合は、その `DATA_DIR/backups` を確認します。Backup 実装は `.env` / `.env.local`、credentials、過去 archive、未参照ファイルを archive に含めません。既存 archive を自動削除・上書きもしません。

archive を live data へ展開せず、まず zip 構造と manifest を読み取り専用で確認します。

```bash
ARCHIVE="/absolute/path/to/data/backups/backup-YYYYMMDDTHHMMSSZ.zip"
/usr/bin/unzip -t "$ARCHIVE"
/usr/bin/unzip -l "$ARCHIVE"
/usr/bin/unzip -p "$ARCHIVE" manifest.json | \
  "$PROJECT_DIR/.venv/bin/python" -c \
  'import json, sys; m=json.load(sys.stdin); print({"schema_version": m.get("schema_version"), "database": m.get("database"), "counts": m.get("counts"), "warnings": m.get("warnings")})'
```

`database.integrity_check` と `database.foreign_key_check` が `ok` であること、`counts` と `warnings` が想定どおりであることを確認します。`unzip -l` の一覧に環境ファイル、セッション、credentials がないことも確認します。復元する場合は別の検証用ディレクトリへ展開し、Bot/Web を unload してから DB と添付ファイルの対応を確認します。稼働中の `data/app.db` へ直接展開しません。

スケジュールは Mac のローカル時計の 03:15 です。Mac がスリープ中なら launchd のカレンダー実行が起床後へずれることがあります。日次 backup の実行成功は、archive が出力され、上記の既存検証を通過した場合だけと扱います。固定秒数の待機を成功判定には使いません。

## Rollback

更新や別構成への切り替えは、次の順序で行います。

1. `status_launch_agents.sh` で現在の plist と loaded 状態を記録する。
2. 上記の `--unload` で 3 label を unload する。
3. 対象 plist だけを、利用者が決めた別の絶対パスへ `mv` して保管する。削除や再帰的な cleanup は行わない。
4. 必要なら以前の plist を同じ known filename へ戻し、`plutil -lint` と status を確認する。
5. 再開するときだけ、レビュー済みの `--install` または `--install --load` を明示的に実行する。

helper は、異なる既存 plist を暗黙に置き換えないため、誤った再インストールから既存設定を守ります。`--replace` を使う前には、既存 plist を個別の rollback 用絶対パスへコピーまたは移動して戻せる状態にしてください。plist を移動しても、プロジェクトの DB、添付、`.env.local` は変更されません。

## Limitations and safety boundaries

- LaunchAgent はユーザーのログイン GUI domain 用です。ログイン前のシステム daemon や複数ユーザー共有サービスではありません。
- `KeepAlive.SuccessfulExit=false` は異常終了時の自動再起動です。正常終了したプロセスを再起動しないため、意図した停止後は再度明示的に load します。
- 30 秒の throttle はクラッシュループを抑えますが、Discord、ネットワーク、SQLite、外部 provider の稼働を保証しません。
- Web アプリの bind 制約は既存実装のままで、ローカルホスト以外へ公開する資産ではありません。
- launchd は shell profile を読みません。`.venv/bin/python`、`WorkingDirectory`、設定ファイルの配置を変更した場合は、preview と status をやり直します。
- Backup は保存先の自動世代削除・クラウド同期・オフサイト冗長化を行いません。ディスク残量と archive の復元手順は利用者が管理します。
- 03:15 はローカル時計基準で、スリープ、ログアウト、権限、壊れた venv、設定エラー、ディスク不足による失敗を自動で解決しません。
- この文書と追加資産は、LaunchAgent が実際にインストール済み・ロード済みであることの証明ではありません。実行した preview、status、ログ、archive の内容を別々に確認してください。
