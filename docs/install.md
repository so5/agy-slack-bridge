# インストールと運用

## 前提

- Linux マシン（動作確認しているのは Linux + systemd のユーザーサービスです）。
- Python 3.10 以上（`venv` つき）、`git`。
- `agy`（Antigravity CLI）がインストール済みで、**ブリッジを動かすユーザーでログイン済み**であること。
- Slack ワークスペースと、作成した Slack アプリのトークン 2 つ（[slack-app.md](slack-app.md)）。
- 任意: Mermaid 図の画像化に `playwright` と Chromium（[usage.md](usage.md) の「Mermaid diagrams」）。

## 1. インストール

```sh
git clone https://github.com/so5/agy-slack-bridge.git
cd agy-slack-bridge
python3 tools/install.py [--prefix DIR] [--config-dir DIR] [--with-gdrive]
```

ブリッジ本体、`tools/`（安全規則、フック、Drive CLI、セットアップスクリプト）、venv を
**固定のインストール先**に入れます。チェックアウトから直接は動かしません。権限モデルが頼る部品を、
エージェントが書き換えられない場所に置くためです（理由の詳細は [tools/README.md](../tools/README.md)）。

| オプション | 意味 |
|---|---|
| `--prefix DIR` | インストール先（既定 `~/.local/share/agy-slack-bridge`） |
| `--config-dir DIR` | 設定ディレクトリ（既定 `~/.config/agy-slack-bridge`） |
| `--with-gdrive` | Google Drive CLI 用の Google 系パッケージも入れる |
| `--no-setup` | 最後の「続けてセットアップするか」の確認を出さない |
| `--no-settings` | agy の `settings.json` と `hooks.json` を書き換えない |
| `--config-only` | コードには触れず、設定ディレクトリと agy の設定だけ行う |
| `--no-venv`, `--no-config` | venv / 設定ディレクトリを作らない |

行うこと:

- インストール先にコードを 0444（読み取り専用）で置き、`<prefix>/.venv` を作る。
- 設定ディレクトリ（0700）に `config.yaml` と `env` のひな形を作る。**既存のファイルは上書きしない。**
- agy の `~/.gemini/antigravity-cli/settings.json` に、インストール先と設定ディレクトリを
  エージェントに読み書きさせない `deny` を足し、`~/.gemini/config/hooks.json` に
  script-integrity フックを登録する。`allow` は書かない。
- 起動用の systemd ユニット（`<prefix>/systemd/agy-slack-bridge.service`）を生成する。
  `agy` の絶対パスを `AGY_BIN` として書く（見つからなければ警告）。
- 最後に、続けてセットアップ（次の節）を実行するか確認する（Y でそのまま実行、N なら後で実行する
  コマンドを表示）。

### root で `/opt` などに入れる場合（最も堅い）

エージェントはあなたと同じ OS ユーザーで動くので、ユーザー所有のインストール先は deny ルールで
守っているだけです。root 所有にすると、より強く守れます。

```sh
sudo python3 tools/install.py --prefix /opt/agy-slack-bridge --with-gdrive
python3 /opt/agy-slack-bridge/tools/install.py --prefix /opt/agy-slack-bridge --config-only
```

2 行目は通常ユーザーで実行します（root のホームではなく、あなたのホームの設定を作るため）。

## 2. プロジェクトのセットアップ

```sh
<prefix>/.venv/bin/python <prefix>/tools/setup_agy_project.py [<プロジェクトのパス>]
```

対話式で、人間が端末から実行します（エージェントに実行させるものではありません）。
プロジェクトの一覧のメニューが開くので、設定するプロジェクトを選ぶか、新規作成します。プロジェクトのメニューで
「順に全部設定する」を選ぶと、以下の 1〜5 を順に聞かれます（Slack のトークンは一覧のメニューの `t`）。
`install.py` の最後の確認で Y と答えれば、そのまま始まります。主な内容:

1. 対象の指定（agy に登録済みのプロジェクトから選ぶか、新しいディレクトリのパスを入力）。
   ディレクトリが git リポジトリでなければ `git init` します（git 管理は必須です）。
2. 未登録なら `agy --new-project` で登録（モデルを 1 回呼びます）。
3. 安全規則（`.agents/rules/agent-safety-rules.md`）、trustedWorkspaces、フックの登録。
4. Slack チャンネルとの対応づけ（必須はチャンネル ID だけ。表示名・アイコン・モデルなどの任意項目は、続けるかを聞かれます）。
5. Slack のトークン（未設定のとき）。
6. （任意）Google Drive CLI を使うなら、プロファイルの作成と方針（有効なコマンド、許可フォルダ、
   アップロード拒否パターン）の設定。必須の設定が終わってから聞かれます。

何度実行しても安全で、現在の値が既定値として表示されます（Enter で維持、`-` で削除）。設定の変更や修復も
同じコマンドです（`--all` を付けると、メニューを出さず全部を順に実行して終わります）。詳細は [tools/README.md](../tools/README.md)。

## 3. 起動

```sh
mkdir -p ~/.config/systemd/user
cp <prefix>/systemd/agy-slack-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agy-slack-bridge.service
loginctl enable-linger "$USER"   # ログアウト後も動かす / 起動時に自動で始める場合
```

端末で試すだけなら:

```sh
set -a; . ~/.config/agy-slack-bridge/env; set +a
AGY_BRIDGE_CONFIG=~/.config/agy-slack-bridge/config.yaml \
AGY_BRIDGE_STATE_DB=~/.config/agy-slack-bridge/state.sqlite3 \
  <prefix>/.venv/bin/python <prefix>/bridge.py
```

systemd で動かすときの注意:

- `systemd --user` の `PATH` は最小限で、`~/.local/bin` を含みません。`agy` は絶対パスの
  `AGY_BIN` で渡します（生成ユニットには入っています。自作ユニットなら env ファイルに書く）。
  起動時に `agy` が実行できないと、ブリッジは明確なメッセージを出して終了します。
- ログはファイルではなくジャーナルに出ます: `journalctl --user -u agy-slack-bridge.service -f`
- ユニットの `AGY_BRIDGE_CONFIG` と、フックの `--config-dir` は同じディレクトリでなければなりません
  （`install.py` が両方を設定します。手で分けないでください）。
- 資格情報コマンド（`aws`、`op` など）が `PATH` の外にあるなら、絶対パスで書くか、ユニットに
  `Environment=PATH=...` を足します。

## 4. 動作確認

1. ボットを招待したチャンネルで、`@agy こんにちは` のように**メンションして**メッセージを送る。
   新しいメッセージにはメンションが必要です。
2. スレッドに返信が付けば成功です。スレッド内の続きの返信にメンションは要りません。
3. 「権限がない」という表示とボタンが出たら、[usage.md](usage.md) の「Managing permissions from Slack」の
   とおりに許可します。最初のうちは許可ボタンがよく表示されます。

## 5. 日々の運用

- 設定（`config.yaml`、`env`）を変えたら、ブリッジを再起動します:
  `systemctl --user restart agy-slack-bridge.service`
- プロジェクトを増やす: `setup_agy_project.py` をもう一度実行し、新しいチャンネルを対応づける。
- 許可の管理、モデルの切り替え、使用量の確認は Slack から行えます（[usage.md](usage.md)）。

## 6. アップグレード

```sh
cd agy-slack-bridge && git pull
python3 tools/install.py --no-setup [同じオプション]      # root 版は上の 2 行
systemctl --user restart agy-slack-bridge.service
```

コードだけが入れ替わり、設定（`config.yaml`、`env`、状態 DB）はそのままです。

## 7. アンインストール

1. `systemctl --user disable --now agy-slack-bridge.service` と、ユニットファイルの削除。
2. インストール先（`<prefix>`）の削除。
3. 設定ディレクトリの削除（トークンと状態 DB が含まれます。残したいなら残す）。
4. agy の設定から、ブリッジが足したものを削除: `settings.json` の `deny`（インストール先・設定
   ディレクトリに関するもの）と各プロジェクトの `trustedWorkspaces`、`~/.gemini/config/hooks.json` の
   `script-integrity`。
5. 各プロジェクトの `.agents/rules/agent-safety-rules.md`（リンク）と、そのプロジェクトの
   `permissionGrants` にあるブリッジ由来の許可。

## トラブルシュート

| 症状 | 確認すること |
|---|---|
| ボットが返事をしない | ボットがチャンネルに招待されているか。新しいメッセージに**メンション**を付けたか。チャンネル ID が `config.yaml` の `channels` にあるか。サービスが動いているか（`systemctl --user status …`）。ジャーナルのエラー。トークン（`env`）と、アプリのイベント購読・Socket Mode |
| 起動直後に終了する（`agy not found`） | `AGY_BIN` に `agy` の絶対パスを設定する。`PATH` が最小限の systemd では、素の `agy` は見つからない |
| Slack に `agy invocation failed` | ジャーナルを見る。`agy` が非ゼロで終了したときは、標準出力の末尾がログに残る。`agy` にログインできているか（ブリッジを動かすユーザーで `agy -p hi`） |
| `RESOURCE_EXHAUSTED` | モデルの利用枠切れ。`/agy-model` で別系統のモデルに切り替える |
| 「権限がない」と表示される | 正常な動作。ボタン、または `/agy-permissions add` で許可する |
| 同じ指示が一度だけ拒否される（スクリプトの差分つき） | スクリプトが前回から変わったときの保護。内容を確認して、同じ指示をもう一度送る |
| 設定を変えたのに反映されない | ブリッジを再起動する |
| 安全規則が効いていない | `.agents/rules/agent-safety-rules.md` のリンクがあるか（`setup_agy_project.py` を再実行）。ルールファイルには frontmatter（`trigger: always_on`）が必要で、無いとエラーなしで無視される |
| `setup_agy_project.py` が「インストールされていません」 | 先に `tools/install.py` を実行する |
| `setup_agy_project.py` が「対話形式のコマンドです」 | 端末（TTY）から実行する |
