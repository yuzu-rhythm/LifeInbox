# 公開用コピーの検証記録

実施日: 2026-10-23。元プロジェクトの過去の結果を転記したものではなく、新しい仮想環境と公開用コピーで実施した記録です。

## 環境

macOS / arm64、Python 3.14.6。主な依存は `requirements-verified.txt` と `THIRD_PARTY_NOTICES.md` を参照してください。バージョン一覧は今回の環境のスナップショットであり、Python 3.12やWindows/Linuxでの動作確認・完全なロックファイルではありません。

確認環境の再現手順（新しい仮想環境で実行）:

```bash
python -m pip install -c requirements-verified.txt -e '.[dev,rhythm]' setuptools wheel
python -m playwright install chromium
python -m pytest -q -p no:cacheprovider
python -m ruff check src tests examples
python -m compileall -q src tests examples
python -m pip check
```

ブラウザ利用テストのためPlaywright / Chromiumを追加しています。Bot・OpenAI・Notion等の資格情報を渡さない環境で実行し、一時SQLite・fixture・Mockを使用しました。macOS常駐テストは隔離された一時ディレクトリを利用し、利用者の常駐設定は変更していません。

## 最終結果

| 検査 | 結果 |
| --- | --- |
| pytest | **793 passed / 0 failed / 0 skipped**（47.99秒） |
| Ruff | All checks passed |
| compileall | 成功 |
| pip check | No broken requirements found |
| Web TestClient | 4ルートすべてHTTP 200、架空メモ本文一致 |
| 画面例 | Chromeで架空メモ3件を照合し撮影・目視確認 |

pytestには1,892件のDeprecationWarningがありました。主にPython 3.14上のpytest-asyncio / discord.pyによるasyncio廃止予定APIの警告です。テスト失敗ではありませんが、将来のPythonへの対応は改善項目です。

## Webデモ

`examples/demo.py` の `build_demo()` による実際のFastAPIアプリで、`/`、`/memos`、`/transactions`、`/archive` のHTTP 200と、架空メモの本文をTestClientで確認しました。ローカルブラウザで架空メモ3件の存在を照合して画面を撮影し、目視で確認しました。デモは通常のWebと衝突しにくいポート8876を使用します。

## 途中で検出して修正した事項

- コピー初回はmacOSのスクリプト・plist・資料が不足していたため、関連14テストが失敗しました。汎用テンプレートを追加し、個人環境の記述を除いた資料を用意しました。
- Playwrightは導入済みでも対応ブラウザが未導入だったため2テストが失敗しました。Chromiumを追加しました。
- テストのハンドル置換では全角表記の対応が残り、正規化テストが失敗しました。架空名の全角表記へ置き換えて検証対象の意味を維持しました。
- 個人環境を記した資料の冒頭を除いた際に、安全な既定動作の説明も外れていたため、公開用の説明を追加しました。
- wheelテストはsetuptools未導入時にskipされました。確認環境へビルド用ツールを追加しています。
- 検証起動スクリプトの初回は仮想環境Pythonへのsymlinkを実体へ解決し、システムPythonを起動してしまいました。仮想環境のパスを維持して再実行しました。

## 今回未検証

Discord実投稿、OpenAI実API、Notion実同期、外部サイトの認証操作、iPhone/Watch/Health実機、Cloud Runのビルド・配備、長期間の連続運用、実データのバックアップ復元は実施していません。
