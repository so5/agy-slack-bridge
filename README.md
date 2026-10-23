# agy-slack-bridge

Slack のチャンネルと [Antigravity CLI](https://antigravity.google.com)（`agy`）のプロジェクトをつなぎ、
Slack から agy のエージェントと話せるようにするブリッジです。

[English](README.en.md)

- Slack チャンネル 1 つが agy プロジェクト 1 つに対応します。
- ボットに**メンション**したメッセージで会話が始まり、返信はスレッドに付きます。スレッドの中の続きの
  返信にメンションは要りません。
- 実行が拒否された操作には、Slack 上に**許可ボタン**が出ます。スマホからでも許可できます。
- モデルの切り替え、使用量の確認、agy のスラッシュコマンドの実行も Slack から行えます。
- agy が作った画像・PDF・CSV などはスレッドにアップロードされ、Mermaid 図は画像になります。
- Slack の **Socket Mode** で動くので、公開 URL や受信ポートは要りません。
- `agy -p` を呼び出すだけの薄いラッパーで、agy の機能を作り直してはいません。

## ⚠ セキュリティの前提

ブリッジされたチャンネルに投稿できる人は、対応する agy プロジェクトに、**このマシン上で**作業させられます
（コマンドの実行、ファイルの編集など。プロジェクトの許可の範囲で）。メンバーを信頼できる**非公開チャンネル**を
使ってください。

エージェントはブリッジと同じ OS ユーザーで動くため、権限の仕組みは完全な防壁ではありません。誤りと
安易な探りを大きな音で失敗させる仕組みです（deny ルール、安全規則、スクリプト改ざん検知など）。
詳細は [docs/usage.md](docs/usage.md) の「Keeping the agent inside its lane」と
[docs/permissions.md](docs/permissions.md) を参照してください。

## クイックスタート

必要なもの: Linux、Python 3.10 以上、`git`、**ログイン済みの `agy`**、Slack ワークスペース。
詳しい手順と、うまくいかないときの確認項目は [docs/install.md](docs/install.md) にあります。

**1. Slack アプリを作る**（約 5 分）— [docs/slack-app.md](docs/slack-app.md)

同梱の [`slack-app-manifest.yaml`](slack-app-manifest.yaml) を Slack の「From an app manifest」に貼り付け、
App-Level Token（`connections:write`）を作ってアプリをインストールします。トークン 2 つ
（`xapp-…`、`xoxb-…`）と、ボットを招待したチャンネルの ID を控えます。

**2. インストールする**

```sh
git clone https://github.com/so5/agy-slack-bridge.git
cd agy-slack-bridge
python3 tools/install.py            # Google Drive CLI も使うなら --with-gdrive
```

**3. プロジェクトを設定する**

`install.py` の最後に「続けてセットアップしますか?」と聞かれるので Y と答えます（後から
`<prefix>/.venv/bin/python <prefix>/tools/setup_agy_project.py` でも実行できます）。対話で、
agy プロジェクトの登録（または既存プロジェクトの選択）、Slack チャンネルとの対応づけ、
トークンの入力を行います。

**4. 起動する**

```sh
mkdir -p ~/.config/systemd/user
cp ~/.local/share/agy-slack-bridge/systemd/agy-slack-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agy-slack-bridge.service
```

**5. 確かめる**

チャンネルで `@agy こんにちは` とメンションして送り、スレッドに返信が付けば完了です。「権限がない」と出たら
許可ボタンを押してください（最初のうちはよく出ます）。

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
| [tools/gdrive-profile.md](tools/gdrive-profile.md) | Drive CLI のプロファイルの書式（資格情報、OAuth の注意） |

英語版: [README.en.md](README.en.md)、`docs/*.en.md`、`tools/*.en.md`。

## 開発

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## ライセンス

MIT — [LICENSE](LICENSE) を参照。
