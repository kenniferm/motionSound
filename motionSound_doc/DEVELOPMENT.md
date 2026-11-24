# GitHubで開発する

## PRのチェック

`.github/workflows/ci.yml` は、`main`・`develop` 向けのPR、`main`・`develop`・`work/**` へのpush、手動実行で動く。同じブランチに追加の変更を送ると、前のCI実行を中止する。

| チェック名 | 実行環境 | 検証内容 |
|---|---|---|
| `Python lint` | Ubuntu 24.04 | RuffのFルール。未定義名、未使用importなど |
| `Frontend build` | Ubuntu 24.04 | Node 22.22.2でTypeScript型検査、Reactのビルド、3D形状の回帰テスト |
| `Secret scan (Gitleaks)` | Ubuntu 24.04 | チェックアウトしたGit履歴の秘密情報漏洩検査 |
| `Tests (macOS)` | macOS 15 | 音声処理・保存・再開・Starlette APIのPythonテスト |
| `E2E (macOS)` | macOS 15 | Chromiumで画面 → API → 生成・保存 → 再表示を通す実ブラウザテスト |

Python lintとテストはuv 0.9.0、`.python-version`、`uv.lock`を使う。フロントエンドは`.node-version`のNode 22.22.2とnpm 12.2.2を使う。`uv sync --locked --dev`と`npm ci`がロックの不整合を検出するとCIは失敗する。GitHub ActionsはコミットSHAで固定する。

Gitleaks 8.30.1の公式バイナリはSHA-256を検証してから実行する。ダミーの秘密情報を検出できることを確認した後、全履歴を取得したチェックアウトをスキャンする。検出値はログで伏せる。GitGuardianのアカウントやAPIキーは不要。SonoraのGitGuardianアプリ連携自体は移植していない。

実ブラウザテストは、生成中の部屋と先に完成した候補、部屋をまたいだ再生、波形でのシーク、MP3・旧WAVのダウンロード、3Dビュー、レビュー、改善、やり直し、生成中の部屋の削除を検証する。専用の一時データで動き、API応答を模擬し、テストサーバーから外部への接続を禁止する。Chromiumの音声出力はミュートする。`E2E (macOS)`はPythonテストと別のジョブとして動く。JUnit形式の結果と画面は、成功・失敗を問わず7日間のGitHub Actions artifact `e2e-results`に保存する。

テストジョブはFFmpegを用意し、公式FABIAN SOFAファイルを取得して既存のインポーターでSHA-256を検証する。取得・検証・インポートに失敗するとテストへ進まない。データ未導入によって主要テストがスキップされる状態を避けるための準備であり、データそのものはGitに含めない。データの帰属とライセンスは[FABIANの導入](FABIAN.md)を参照する。

CIにOpenAI・ClaudeのAPIキーは設定しない。実APIの応答品質と作品の聴感はこのCIの対象外。

## ローカルで確認する

FFmpeg、Node.js 22.22.2、npm 12.2.2を用意し、依存を同期する。

```sh
uv sync --locked --dev
.venv/bin/python -m ruff check .
npm ci --prefix frontend --no-audit --no-fund
npm run --prefix frontend build
node --test tests/test_spatial_field.js
```

初回は[FABIANの導入](FABIAN.md)を実行してからテストする。データがないローカル環境では、一部のテストがスキップされる。

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest browser_tests -q
```

実ブラウザテストはFABIANがない場合に失敗する。Git初期化後の秘密情報検査は次で実行する。Gitleaksの初回取得にはネットワーク接続が必要。

```sh
.venv/bin/python scripts/check_secrets.py
```

## ブランチとマージの運用

Sonoraのローカル設定を参考に、`work/<作業名>` → `develop` → `main` を暫定の運用とする。PRテンプレートに変更内容、検証、未検証の点を記載する。

ワークフローだけでは、チェック失敗時のマージを禁止できない。GitHubへの初回反映後、対象リポジトリの Settings → Rules → Rulesets で `main`・`develop` を対象に、次のルールを設定する。

- マージ前にPRを必須にする。
- 必須チェックを `Python lint`、`Frontend build`、`Secret scan (Gitleaks)`、`Tests (macOS)`、`E2E (macOS)` にする。既存のGitGuardian連携を使う場合は`GitGuardian Security Checks`も追加する。初回CIの実行後に名前を選択する。
- マージ前に会話の解決を必須にする。
- force pushとブランチ削除を禁止する。

Rulesetsの利用可否はGitHubのプランと公開範囲に依存する。これらのGitHub側のルール設定とブランチ作成は、このファイルの追加では適用されない。承認レビューの必須人数は運用人数に合わせて決める。

## 初回反映前に確認する

ファイルを含まない初期コミット`e944fc7`を起点に、ローカルとGitHubへ`main`・`develop`を作成した。GitHubの既定ブランチは`develop`で、新しい作業ブランチのPRも既定で`develop`へ向く。アプリとCIの初回反映は`work/initial-studio-and-ci`から`develop`向けのPRで行う。`.env.local`などの秘密情報、`data/`の生成音源・履歴・FABIAN、`.venv/`、各種キャッシュ、ローカル専用の`.agents/`・`.claude/`は`.gitignore`で除外している。ステージしたファイルの一覧を確認してからcommitする。

初回はローカルのSonoraの設定を参考にした。その後、認証済みGitHub CLIで[Sonora PR #1](https://github.com/kenniferm/Sonora/pull/1)と`develop`のCIを確認した。PRのチェックは`Lint`、`Knip`、`Build`、`Python lint`、`Tests (macOS)`、`GitGuardian Security Checks`の6種類。`Tests (macOS)`にはNode統合テスト、Pythonテスト、5つの実ブラウザ回帰スクリプトが含まれる。

TypeScriptの型検査とビルド、PythonのRuff、ブラウザ回帰、Gitleaksを使って検証する。
