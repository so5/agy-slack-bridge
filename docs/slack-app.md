# Slack アプリの作成

ブリッジが使う Slack アプリを作ります。同梱のマニフェスト
[`slack-app-manifest.yaml`](../slack-app-manifest.yaml) を使えば、スコープ、イベント、
スラッシュコマンド、ショートカットを手で設定せずに済みます。

## 手順（マニフェストを使う）

1. <https://api.slack.com/apps> で **Create New App** → **From an app manifest** を選び、
   ワークスペースを選びます。
2. `slack-app-manifest.yaml` の中身（YAML）を貼り付けて、アプリを作成します。
3. **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes** で、
   スコープ `connections:write` のトークンを作ります。`xapp-…` で始まるこの値が
   `SLACK_APP_TOKEN` です（マニフェストでは作れないので、ここだけ手作業です）。
4. **Install App** → **Install to Workspace**。表示される **Bot User OAuth Token**
   （`xoxb-…`）が `SLACK_BOT_TOKEN` です。
5. ブリッジに使うチャンネルにボットを招待します（チャンネルで `/invite @agy`）。
6. チャンネルの ID を控えます（チャンネルの詳細の一番下にある `C…` / `G…`）。
   セットアップの対話で入力します。

2 つのトークンは、`tools/setup_agy_project.py` の対話で入力できます（入力は画面に出ず、
`~/.config/agy-slack-bridge/env` に 0600 で保存されます）。

> ブリッジは **Socket Mode**（マシンから Slack への外向きの接続）で動きます。公開 URL や
> 受信ポートは要りません。

## マニフェストが設定するもの

マニフェストを使わずに手で設定する場合や、後から確認する場合の一覧です。

| 設定 | 値 | 用途 |
|---|---|---|
| Socket Mode | 有効 | 外向きの接続だけで動かす |
| Bot Token Scopes | `chat:write` | 返信の投稿（必須） |
| | `channels:history` / `groups:history` | 公開 / 非公開チャンネルのメッセージを受け取る（ブリッジするチャンネルの種類に合わせて） |
| | `commands` | スラッシュコマンド |
| | `files:write` | agy が作ったファイルのアップロード（任意） |
| | `chat:write.customize` | チャンネルごとの表示名・アイコン（任意） |
| Event Subscriptions（Bot Events） | `message.channels` / `message.groups` | メッセージの受信（スコープに合わせる） |
| | `app_home_opened` | Home タブ（任意） |
| Interactivity | 有効 | 許可ボタンなどの操作（Socket Mode なので Request URL は不要） |
| Slash Commands | `/agy-permissions` `/agy-model` `/agy-usage` `/agy-stats` `/agy` `/agy-link` | すべて任意。使い方は [usage.md](usage.md) |
| Message Shortcut | callback ID `agy_open_web_ui` | スレッドの会話を Web UI で開く（任意） |
| App Home | Home タブを有効 | 使用量の表示（任意） |

任意の機能を使わないなら、対応するスコープやコマンドは省いてかまいません。

## セキュリティ上の注意

ブリッジされたチャンネルに投稿できる人は、そのチャンネルに対応する `agy` プロジェクトに、
このマシン上で作業させられます（コマンドの実行、ファイルの編集、メール送信など。プロジェクトの
許可で許された範囲で）。メンバーを信頼できる非公開チャンネルを使い、公開チャンネルは避けてください。
