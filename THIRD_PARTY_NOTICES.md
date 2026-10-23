# 第三者ライブラリ・素材の点検

確認日: 2026-10-23。公開用の新規仮想環境で取得した配布物メタデータを確認しました。

## 同梱物と依存物

このリポジトリはアプリのソース・テスト・テンプレート・自作の画面例を配布します。仮想環境、依存パッケージ本体、ブラウザ本体、外部サイトのジャケット画像、フォント、個人の添付は同梱しません。CSSはシステムフォントを参照し、Webフォントを配布していません。

テスト用Base64画像は1×1 PNG、他の画像テストはインラインの最小画像データです。画面例は架空のメモを表示した本アプリの画面です。音ゲーfixtureには最小限の例示データがありますが、外部サイトから実行時に取得する画像の権利やサイトの利用条件を許諾したものではありません。

## 確認したライセンス

以下は今回の環境にインストールされた直接・推移依存（開発用を含む）の一覧です。Python、pipの配布条件は各配布元にも従ってください。ライブラリは各自のライセンスを維持します。

| パッケージ | 確認バージョン | 配布物のライセンス表示 |
| --- | --- | --- |
| [aiohappyeyeballs](https://pypi.org/project/aiohappyeyeballs/) | 2.7.1 | PSF-2.0 |
| [aiohttp](https://pypi.org/project/aiohttp/) | 3.14.5 | Apache-2.0 AND MIT |
| [aiosignal](https://pypi.org/project/aiosignal/) | 1.4.0 | Apache 2.0 |
| [annotated-doc](https://pypi.org/project/annotated-doc/) | 0.0.5 | MIT |
| [annotated-types](https://pypi.org/project/annotated-types/) | 0.8.0 | MIT |
| [anyio](https://pypi.org/project/anyio/) | 4.15.1 | MIT |
| [attrs](https://pypi.org/project/attrs/) | 26.1.0 | MIT |
| [audioop-lts](https://pypi.org/project/audioop-lts/) | 0.2.2 | PSF-2.0 |
| [certifi](https://pypi.org/project/certifi/) | 2026.10.19 | MPL-2.0 |
| [click](https://pypi.org/project/click/) | 8.5.0 | BSD-3-Clause |
| [discord.py](https://pypi.org/project/discord.py/) | 2.7.1 | MIT |
| [fastapi](https://pypi.org/project/fastapi/) | 0.161.1 | MIT |
| [frozenlist](https://pypi.org/project/frozenlist/) | 1.8.0 | Apache-2.0 |
| [greenlet](https://pypi.org/project/greenlet/) | 3.5.6 | MIT AND PSF-2.0 |
| [h11](https://pypi.org/project/h11/) | 0.16.0 | MIT |
| [httpcore](https://pypi.org/project/httpcore/) | 1.0.9 | BSD-3-Clause |
| [httpcore2](https://pypi.org/project/httpcore2/) | 2.16.0 | BSD-3-Clause |
| [httpx](https://pypi.org/project/httpx/) | 0.28.1 | BSD-3-Clause |
| [httpx2](https://pypi.org/project/httpx2/) | 2.16.0 | BSD-3-Clause |
| [idna](https://pypi.org/project/idna/) | 3.20 | BSD-3-Clause |
| [iniconfig](https://pypi.org/project/iniconfig/) | 2.3.1 | MIT |
| [Jinja2](https://pypi.org/project/Jinja2/) | 3.1.6 | BSD-3-Clause |
| [jiter](https://pypi.org/project/jiter/) | 0.17.0 | MIT |
| [MarkupSafe](https://pypi.org/project/MarkupSafe/) | 3.0.4 | BSD-3-Clause |
| [multidict](https://pypi.org/project/multidict/) | 7.1.0 | Apache-2.0 |
| [openai](https://pypi.org/project/openai/) | 3.37.0 | Apache-2.0 |
| [opentelemetry-api](https://pypi.org/project/opentelemetry-api/) | 1.45.1 | Apache-2.0 |
| [packaging](https://pypi.org/project/packaging/) | 26.3 | Apache-2.0 OR BSD-2-Clause |
| [pip](https://pypi.org/project/pip/) | 26.1.2 | MIT |
| [playwright](https://pypi.org/project/playwright/) | 1.64.1 | Apache-2.0 |
| [pluggy](https://pypi.org/project/pluggy/) | 1.7.0 | MIT |
| [propcache](https://pypi.org/project/propcache/) | 0.5.4 | Apache-2.0 |
| [psycopg](https://pypi.org/project/psycopg/) | 3.3.6 | LGPL-3.0-only |
| [psycopg-binary](https://pypi.org/project/psycopg-binary/) | 3.3.6 | LGPL-3.0-only |
| [pydantic](https://pypi.org/project/pydantic/) | 2.14.1 | MIT |
| [pydantic-settings](https://pypi.org/project/pydantic-settings/) | 2.16.1 | MIT |
| [pydantic_core](https://pypi.org/project/pydantic_core/) | 2.50.1 | MIT |
| [pyee](https://pypi.org/project/pyee/) | 14.0.0 | MIT |
| [Pygments](https://pypi.org/project/Pygments/) | 2.21.0 | BSD-2-Clause |
| [pytest](https://pypi.org/project/pytest/) | 8.4.2 | MIT |
| [pytest-asyncio](https://pypi.org/project/pytest-asyncio/) | 0.26.0 | Apache-2.0 |
| [python-dotenv](https://pypi.org/project/python-dotenv/) | 1.2.4 | BSD-3-Clause |
| [ruff](https://pypi.org/project/ruff/) | 0.17.2 | MIT |
| [sniffio](https://pypi.org/project/sniffio/) | 1.3.1 | MIT OR Apache-2.0 |
| [SQLAlchemy](https://pypi.org/project/SQLAlchemy/) | 2.1.6 | MIT |
| [starlette](https://pypi.org/project/starlette/) | 1.9.0 | BSD-3-Clause |
| [truststore](https://pypi.org/project/truststore/) | 0.10.5 | MIT |
| [typing-inspection](https://pypi.org/project/typing-inspection/) | 0.4.4 | MIT |
| [typing_extensions](https://pypi.org/project/typing_extensions/) | 4.16.0 | PSF-2.0 |
| [uvicorn](https://pypi.org/project/uvicorn/) | 0.54.0 | BSD-3-Clause |
| [yarl](https://pypi.org/project/yarl/) | 1.25.1 | Apache-2.0 |
| [setuptools](https://pypi.org/project/setuptools/) | 84.2.1 | MIT |
| [wheel](https://pypi.org/project/wheel/) | 0.48.0 | MIT |

Psycopg / psycopg-binaryにはLGPL-3.0-only、certifiにはMPL-2.0の表示があります。ライブラリを同梱する実行形式・コンテナを配布する際は、ライセンス文・著作権表示・ソース提供等の条件を別途確認してください。今回、コンテナイメージや依存物の改変版は配布しません。

一次資料: [Psycopg LICENSE](https://github.com/psycopg/psycopg/blob/master/LICENSE.txt)、[Mozilla MPL FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/)。

オリジナルのプロジェクトにLICENSEは見つかりませんでした。本整備で作者の許可範囲を勝手に拡張するライセンスは追加していません。公開・閲覧と、OSSとしての再利用許諾は別途選択する必要があります。生成AIによる支援を受けたコードの全行の由来を本点検で保証するものではありません。
