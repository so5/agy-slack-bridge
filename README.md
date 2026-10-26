# agy-slack-bridge

Slack のチャンネルと [Antigravity CLI](https://antigravity.google.com)（`agy`）のプロジェクトをつなぎ、
Slack から agy のエージェントと話せるようにするブリッジです。

[English](README.en.md)

- Slack チャンネル 1 つが agy プロジェクト 1 つに対応します。
- ボットに**メンション**したメッセージで会話が始まり、返信はスレッドに付きます。スレッドの中の続きの
  返信にメンションは要りません。
- 実行が拒否された操作には、Slack 上に**許可ボタン**が表示されます。スマホからでも許可できます。
- モデルの切り替え、使用量の確認、agy のスラッシュコマンドの実行も Slack から行えます。
- agy が作った画像・PDF・CSV などはスレッドにアップロードされ、Mermaid 図は画像になります。

## ⚠ セキュリティの前提

ブリッジしたチャンネルに投稿できる人は、agy を実行しているユーザーの権限で、コマンドの実行やファイルの編集などを
agy にさせることができます。チャンネルは、**agy とブリッジを実行している本人とボットだけの非公開チャンネル**にしてください。
第三者を招待することは、そのユーザーアカウントを渡すのと同じ意味を持ちます。

agy の行動を制限する仕組みも用意しています。詳しくは [docs/usage.md](docs/usage.md) の「Keeping the agent inside its lane」と [docs/permissions.md](docs/permissions.md) を参照してください。

## セットアップ手順

**必要なもの**: Linux（`systemd --user` が使えるもの）、Python 3.10 以上（`venv` つき）、`git`、
**ブリッジを動かすユーザーでログイン済みの `agy`**、Slack ワークスペース（アプリを作れる権限）。

セットアップは人間が端末で実行します。質問の `[...]` は既定値で、**Enter でそのまま進み、`-` で値を消せます**。
再実行すると、現在の値が既定値として表示されます。

### 1. Slack アプリを作る

1. <https://api.slack.com/apps> → **Create New App** → **From an app manifest** → ワークスペースを選ぶ。
2. [`slack-app-manifest.yaml`](slack-app-manifest.yaml) の中身を貼り付けて作成する。
3. **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes** で、スコープ
   `connections:write` のトークンを作る。`xapp-…` で始まる値が **App トークン**（`SLACK_APP_TOKEN`）。
4. **Install App** → **Install to Workspace**。表示される `xoxb-…` が **Bot トークン**（`SLACK_BOT_TOKEN`）。
5. ブリッジに使う**非公開チャンネル**を作り、ボットを招待する（チャンネルで `/invite @agy`）。
6. チャンネルの ID（チャンネルの詳細の一番下にある `C…` / `G…`）を控える。

> ⚠ このマニフェストでアプリを新規作成する手順は、まだ実際に試せていません（スコープなどの一覧は
> 運用中のアプリと突き合わせてあります）。うまくいかなければ、[docs/slack-app.md](docs/slack-app.md) の
> 設定一覧を見ながら手で設定してください。

### 2. インストールする

```sh
git clone https://github.com/so5/agy-slack-bridge.git
cd agy-slack-bridge
python3 tools/install.py --with-gdrive      # Drive CLI を使わないなら --with-gdrive は不要
```

インストール先は `~/.local/share/agy-slack-bridge`（以下 `<prefix>`）、設定は `~/.config/agy-slack-bridge` です。
`/opt` に root で入れる方法とオプションは [docs/install.md](docs/install.md)。

| 質問 | 答え方 |
|---|---|
| `続けてセットアップを実行しますか? [Y/n]` | **Y**（次の 3 が始まります）。N なら、後で実行するコマンドが表示されます |

### 3. プロジェクトを設定する（`setup_agy_project.py`）

後から実行する（N と答えた場合、設定を変える場合）ときは、次のコマンドを実行します。

```sh
~/.local/share/agy-slack-bridge/.venv/bin/python ~/.local/share/agy-slack-bridge/tools/setup_agy_project.py
```

メニューが開きます。番号かキーを入力して選びます（`[...]` は Enter で選ばれる項目）。

1. **最初の画面**: agy に登録済みのプロジェクトの一覧が出ます。設定するプロジェクトの番号を選ぶか、
   `n`（新しいプロジェクトを作る）を選びます。`t` は Slack のトークンの設定（全体で共通）、`q` は終了です。
2. **プロジェクトのメニュー**: プロジェクトを選ぶと次の項目が出ます。**初めてのときは `1`** です。

| 項目 | 内容 |
|---|---|
| `1` 順に全部設定する | 下の 3-1 のあと、3-2、3-3 を順に行う（初めて・更新） |
| `2` ルールとフックを配置・更新する | 3-1 の配置部分だけ |
| `3` Google Drive CLI を設定する | 3-2 |
| `4` Slack チャンネルとの対応づけを設定する | 3-3 |
| `5` 現在の状態を表示する | |
| `b` プロジェクトの一覧に戻る | |
| `q` 終了 | |

別のプロジェクトを設定するときは、`b` で一覧に戻って選び直します。パスを入力する質問では、タブでパスを補完できます。

#### 3-1. 対象のプロジェクト

新規作成で指定するディレクトリが、agy のプロジェクトのディレクトリ（agy が作業するルート）になります。

| 質問 | 答え方 |
|---|---|
| `新しいプロジェクトのディレクトリのパス`（最初の画面で `n` を選んだとき） | 例: `~/WORK/my-project`。登録済みのプロジェクトを選んだときは聞かれません |
| `ディレクトリ <パス> を作成しますか? [Y/n]` | Y |

ディレクトリが git リポジトリでなければ、自動で `git init` し、その旨が表示されます（git 管理は必須。コミットはしません）。

| 質問 | 答え方 |
|---|---|
| `agy に登録しますか?（`agy --new-project`） [Y/n]` | **Y**（モデルを 1 回呼びます） |

#### 3-2. Google Drive CLI（使わないなら飛ばす）

| 質問 | 答え方 |
|---|---|
| `Drive CLI を使いますか? [y/N]` | 使わないなら **N**（次の 3-3 へ）。使うなら **y** |
| `プロファイル名` | 英数字と `_` `-` だけ。プロジェクトごとに別の名前にする。既定はプロジェクト名。プロファイルは `<設定ディレクトリ>/gdrive/名前.json` |
| `プロファイルを作成しますか? [Y/n]` | **Y** |
| `認証の種類（service_account / oauth）` | 下の表を見て選ぶ。迷ったら Enter（`service_account`） |
| `サービスアカウントの JSON 鍵のパス` | `service_account` を選んだとき。ダウンロードした鍵ファイルのパス。`<設定ディレクトリ>/gdrive/<プロファイル名>.sa.json`（0600）に保存され、共有先のメールアドレスが表示される（元の鍵ファイルは削除する）。空欄なら、次の「資格情報を取り出すコマンド」で別の保管先を使う |
| `資格情報を取り出すコマンド` | `oauth` のとき、または鍵のパスを空欄にしたとき。鍵／トークンの JSON を**標準出力に出力するコマンド**（下の例）。スコープ（`drive` など）を入れないこと |
| `` `…` というコマンドが見つかりません。このまま使いますか? [y/N]`` | コマンドが見つからないときだけ表示されます。打ち間違いなら **N** にして、やり直す |
| `詳細設定（スコープ・方針）を変更しますか? [y/N]` | スコープと方針（有効なコマンド・許可フォルダ・アップロード拒否）の設定。既定値のままなら **N**、変えるなら **y** |

**認証の種類の選び方**

| | `service_account`（推奨） | `oauth` |
|---|---|---|
| 向いている用途 | Drive のフォルダを**読む・人が作ったファイルを更新する** | agy に**新規ファイルを自分で作らせたい**、ユーザー自身の権限で動かしたい |
| 準備 | サービスアカウントを作り、フォルダをそのメールアドレスに共有するだけ | 同意画面の設定、リフレッシュトークンの取得が要る |
| 見える範囲 | 共有されたものだけ | ユーザーの Drive 全体（許可フォルダで絞る） |
| 注意 | 個人の Drive には**新規ファイルを作れません**（容量が無いため） | 同意画面が「テスト」だとトークンが 7 日で失効する。スコープは最初の同意と一致させる |

**サービスアカウントの事前準備**（セットアップの前に行う）:

1. [Google Cloud コンソール](https://console.cloud.google.com/)でプロジェクトを作る（既存でもよい）。
2. **API とサービス** → **ライブラリ**で **Google Drive API** を有効にする。
3. **IAM と管理** → **サービス アカウント** → **サービス アカウントを作成**（ロールの付与は不要）。
4. 作成したサービスアカウント → **キー** → **鍵を追加** → **新しい鍵を作成** → **JSON** で鍵ファイルをダウンロードする。
5. セットアップで、その鍵ファイルのパスを入力する。表示されたメールアドレス（`…@….iam.gserviceaccount.com`）に、
   Drive で使わせたいフォルダを共有する（読み取りだけなら「閲覧者」、書き込みも許すなら「編集者」）。

> ⚠ **鍵ファイルは、平文の JSON のままディスクに保存されます。** 同じ OS ユーザーで動くプロセスは読めます
> （agy には読ませない設定を入れますが、完全な防壁ではありません）。鍵が漏れると、共有したフォルダを誰でも操作できます。
> 共有するフォルダは必要最小限にし、書き込みが要らなければ「閲覧者」で共有してください。漏れたら、Cloud コンソールで
> その鍵を削除します。より堅く保管したいときは、鍵のパスを空欄にして、パスワードマネージャーなどから取り出すコマンドを使います。

OAuth の準備と注意、新規ファイルの扱いは [tools/gdrive-profile.md](tools/gdrive-profile.md)。

**資格情報を取り出すコマンドの例**（`oauth`、または鍵ファイル以外の保管先を使う場合。シェルを通さず直接実行されます）

```
cat /home/you/secrets/drive-key.json
op read 'op://Vault/Drive key/credential'
aws secretsmanager get-secret-value --secret-id NAME --query SecretString --output text
```

その他の保管先（pass、GCP Secret Manager、Vault）の例は [tools/gdrive-profile.md](tools/gdrive-profile.md)。

「詳細設定を変更する」で **y** にしたときの質問:

| 質問 | 答え方 |
|---|---|
| `読み取りに使うスコープ` / `書き込みに使うスコープ` | 新規作成のときだけ。既定は `drive.readonly` / `drive`。OAuth で別のスコープで同意したトークンを使うときだけ変える |
| `有効にするコマンド（カンマ区切り or all）` | 使わせるサブコマンド。既定は読み取り系 5 つ（`list,search,meta,read,download`）。書き込み系（`mkdir` / `upload` / `update`）を足しても、許可リストには入らず、**毎回承認**が要る |
| `許可フォルダ（URL か ID、カンマ区切り。`-` で制限なし）` | 操作を許すフォルダ。フォルダの URL を貼ってよい。`-` で制限なし。**指定を強く推奨**（OAuth はユーザーの Drive 全体が見える） |
| `アップロード拒否パターン（k=今のまま / a=追加 / r=置換）` | 普通は **k**。a / r を選ぶと、拒否するファイル名パターン（例 `*.sqlite`）とディレクトリ名を聞かれる（`追加する拒否ファイル名パターン` など） |
| `緩める変更を適用しますか? [y/N]` | 権限が広がる変更をしたときだけ。差分が表示されるので確認する |
| `…json に書き込みますか? [Y/n]` | **Y** |

#### 3-3. Slack チャンネルとの対応づけ

| 質問 | 答え方 |
|---|---|
| `Slack チャンネルを対応づけますか? [Y/n]` | **Y** |
| `チャンネル ID` | 手順 1 で控えた `C…` / `G…` |
| `表示名` | 空欄可。ボットの返信に付く名前（`chat:write.customize` が必要） |
| `アイコン絵文字` | 空欄可。例 `robot_face`（前後の `:` は省略可）。名前は Slack の入力欄で `:` に続けて文字を打つと候補が出ます（カスタム絵文字も同じ形）。入力した場合、画像 URL は聞かれない |
| `アイコン画像 URL` | 絵文字を空欄にしたときだけ。`https://` で始まる URL |
| `モデル` | 空欄可（agy の既定）。`agy models` の名前 |
| `effort（…）` | 空欄可。モデルの推論の深さ |
| `監視するファイル（相対パス、カンマ区切り）` | 既定は空（何も監視しない）。プロジェクトのディレクトリからの相対パス（例 `AGENTS.md`）。エージェントによる変更を止めはせず、変更後に差分つきの通知が Slack に届きます（確認間隔は既定 30 分）。通知のボタンで、変更を承認するか元に戻せます。詳細は [usage.md](docs/usage.md) の「Config drift detection」 |
| `権限ボタンの統計を取りますか? [y/N]` | 普通は **N** |

元の `config.yaml` は `config.yaml.bak` に残ります。

#### 3-4. Slack のトークン

| 質問 | 答え方 |
|---|---|
| `SLACK_BOT_TOKEN（xoxb-…）` | 手順 1 の Bot トークンを貼る（入力は画面に出ません）。空欄ならスキップ |
| `SLACK_APP_TOKEN（xapp-…）` | 手順 1 の App トークンを貼る |

トークンは `~/.config/agy-slack-bridge/env`（0600）に保存されます。設定済みのものは聞かれません。

### 4. 起動する

```sh
mkdir -p ~/.config/systemd/user
cp ~/.local/share/agy-slack-bridge/systemd/agy-slack-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agy-slack-bridge.service
loginctl enable-linger "$USER"      # ログアウト後も動かす／起動時に自動で始める場合
```

起動に失敗する場合は `journalctl --user -u agy-slack-bridge.service -f` を見ます
（`agy` が実行できないとき、ブリッジはその旨を出して終了します）。詳細は [docs/install.md](docs/install.md)。

### 5. 動作を確かめる

チャンネルで `@agy こんにちは` とメンションして送り、スレッドに返信が付けば完了です。「権限がない」と出たら
**許可ボタン**を押してください（最初のうちはよく表示されます）。

使い方（スラッシュコマンド、`resume`、統計、チャンネルごとの設定など）は [docs/usage.md](docs/usage.md)。

## 運用

- 設定を変えたら再起動: `systemctl --user restart agy-slack-bridge.service`
- ログ: `journalctl --user -u agy-slack-bridge.service -f`
- プロジェクトの追加・設定の変更: `setup_agy_project.py` をもう一度実行する（冪等）。
- アップグレード: `git pull` して `python3 tools/install.py --no-setup` を再実行し、再起動する
  （設定は上書きされません）。
- アンインストールとトラブルシュートは [docs/install.md](docs/install.md)。

## ドキュメント

| 文書 | 内容 |
|---|---|
| [docs/install.md](docs/install.md) | 前提、インストール、起動、確認、アップグレード、アンインストール、トラブルシュート |
| [docs/slack-app.md](docs/slack-app.md) | Slack アプリの作成（マニフェスト）と必要な設定の一覧 |
| [docs/usage.md](docs/usage.md) | Slack での使い方（スレッド、`resume`、許可ボタン、各スラッシュコマンド、統計、Mermaid、設定ドリフト検知）と、チャンネルごとの設定 |
| [docs/permissions.md](docs/permissions.md) | 許可の仕組みと保存場所、スクリプトの封印（seal） |
| [tools/README.md](tools/README.md) | インストーラ、セットアップスクリプト、フック、Drive CLI の詳細 |
| [tools/gdrive-profile.md](tools/gdrive-profile.md) | Drive CLI のプロファイルの書式（認証の選び方、資格情報、OAuth の注意） |

英語版: [README.en.md](README.en.md)、`docs/*.en.md`、`tools/*.en.md`。

## 開発

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## ライセンス

MIT — [LICENSE](LICENSE) を参照。
