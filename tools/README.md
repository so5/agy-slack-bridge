# tools/ — agy プロジェクトのセットアップ用ツール

Slack ブリッジ経由で動かす agy プロジェクトに共通で入れる、安全規則・フック・
Drive CLI と、それを配るブートストラップスクリプトです。ブリッジ本体(`bridge.py`)
とは独立していて、ブリッジの動作には影響しません。

| ファイル | 内容 |
|---|---|
| `setup_agy_project.py` | 新しい(または壊れた)agy プロジェクトに設定一式を入れる、冪等なスクリプト |
| `agent-safety-rules.md` | エージェント向けの行動規則(ブリッジ内部を調べない、拒否を迂回しない、など) |
| `hooks.json`, `hooks/script_integrity.py` | `run_command` の PreToolUse フック。恒久許可したスクリプトが、承認後にこっそり書き換えられていないかを検査する |
| `gdrive_cli.py` | Google Drive 用の、固定・限定コマンドの CLI(サービスアカウント + AWS Secrets Manager 前提)。`--with-gdrive` で配る |

## このディレクトリの置き方は運用者が決める

`tools/` の内容を「正本」としてどこに置くかは、このリポジトリでは決めません。
`setup_agy_project.py` は **共有ファイルの置き場(`--source-dir`、既定はスクリプト自身のある
ディレクトリ)** から、対象プロジェクトへシンボリックリンクを張るだけです。次のどちらでも動きます。

- **コピーして使う**: `tools/` を好きな場所(例: `~/agy-shared/`)にコピーし、そこから
  `setup_agy_project.py` を実行する。ブリッジを更新しても共有ファイルは勝手に変わらず、
  変えたいときに自分でコピーし直す。運用者側で編集もできる。
  コピー先はブリッジのチェックアウトの外になるので、`--bridge-dir` が必要。
- **symlink を貼る**: ブリッジのチェックアウトの `tools/` を、そのまま(または
  `ln -s <チェックアウト>/tools ~/agy-shared` のようにして)使う。ブリッジを
  `git pull` すると、全プロジェクトの共有ファイルも同時に更新される(便利だが、
  レビューせずに全プロジェクトへ反映される点に注意)。

どちらの場合も、各プロジェクトの `.agents/` 配下にできるのは、選んだ置き場への
シンボリックリンクです。置き場を後から変える場合は、プロジェクト側のリンクを
削除してから `setup_agy_project.py` を再実行してください(別のリンク先を指す
リンクは、安全のため上書きせずエラーにします)。

### マシン固有のファイル(任意)

置き場に次のファイルがあれば、リンクして `AGENTS.md` から読み込ませます。無ければスキップします。
これらはマシンごと・運用者ごとの内容なので、このリポジトリには含みません。

- `local-tools.md` — このマシンで使えるツールの一覧
- `gdrive-cli.md` — `--with-gdrive` のとき、エージェント向けの Drive CLI の使い方

`hooks.json` の `command`（`python3 hooks/script_integrity.py`）の相対パスは、
`hooks.json` 自身のあるディレクトリが基準です（運用で確認済み）。`.agents/hooks.json` は
置き場の `hooks.json` へのシンボリックリンクなので、同じ置き場の `hooks/` が使われます。

## 使い方

```sh
python3 tools/setup_agy_project.py <対象プロジェクトのパス> \
    [--source-dir <共有ファイルの置き場>] \
    [--bridge-dir <ブリッジのチェックアウト>] \
    [--bridge-config-dir <ブリッジの設定ディレクトリ>] \
    [--with-gdrive --gdrive-profile <プロファイル名> [--gdrive-script scripts/gdrive_cli.py]]
```

- 対象は git リポジトリであること(恒久許可コマンドの commit-before-run 判定の前提)。
- 何度実行しても安全です(揃っているものはスキップします)。
- `--bridge-dir` は、`tools/` がブリッジのチェックアウト直下にあれば自動で決まります。
  `tools/` を別の場所にコピーして使う場合は必須です。
- 実行すると次を行います(詳細は `setup_agy_project.py` の docstring)。
  1. 対象の `.agents/` に、置き場の共有ファイルへのシンボリックリンクを張り、
     `AGENTS.md` に読み込み行を追記する。
  2. `.gitignore` に `scratch/` が一切出てこなければ、除外を追記する。
  3. `~/.gemini/antigravity-cli/settings.json` に、ブリッジ本体・設定ディレクトリ・
     agy 自身の設定を読ませない `permissions.deny` と、対象の `trustedWorkspaces` を追加する。
  4. `--with-gdrive` のとき、Drive CLI のリンクと seal、および、対象プロジェクト自身の
     `~/.gemini/config/projects/<id>.json` の `permissionGrants` への読み取り系サブコマンドの恒久許可。
- agy へのプロジェクト登録(`agy --project <名前> -p "hi"` 等)と、ブリッジの
  `config.yaml` へのチャンネル追加は対象外です。未登録のプロジェクトでは許可の登録だけが
  保留されるので、登録後に同じコマンドをもう一度実行してください。

### 許可の範囲について

ブリッジ自身は、どのプロジェクトにも共通で効く許可を作りません
(`docs/permissions.md` 参照)。このスクリプトも同じ方針で、
`~/.gemini/antigravity-cli/settings.json` に書くのは `deny` と `trustedWorkspaces`
(どちらも絶対パス指定で、プロジェクトを問わず共有して安全なもの)だけです。
`allow` は、対象プロジェクト自身の `permissionGrants` にしか書きません。

`--gdrive-profile` は許可エントリの一部になります
(`command(.venv/bin/python -I scripts/gdrive_cli.py --profile <名前> list)`)。
プロジェクトごとに別のプロファイル名を使えば、あるプロジェクトの Drive 許可が、
別プロジェクトの認証情報を使う権限にはなりません。名前の先(シークレットの保管先、
スコープ、許可フォルダなど)はプロファイルの中にあるので、保管先を後から変えても、
承認済みのコマンド文字列は変わらず、再承認は要りません。

プロファイルの書き方(サービスアカウントと OAuth の両対応、資格情報を取り出す
コマンドの例、スコープの注意)は [`gdrive-profile.md`](gdrive-profile.md) を参照してください。

### Drive CLI を使うプロジェクトの前提

対象プロジェクトの `.venv` に `google-api-python-client` と `google-auth`
(OAuth のリフレッシュには `requests` 系の依存も)が必要です。
資格情報を取り出すコマンド(`aws`、`op` など)が、ブリッジを動かすユーザーで実行できること。
Drive 側では、サービスアカウントの場合はそのメールアドレスに対象フォルダを共有しておきます。
読み取り系(`list`/`search`/`meta`/`read`/`download`)だけが恒久許可され、
書き込み系(`mkdir`/`upload`/`update`)は毎回承認が要ります。
削除・移動・共有設定の変更はコマンド自体がありません。

## 共有ファイルを足したとき

`SHARED_FILES`(必須かどうかの指定付き。`AGENTS.md` に読ませたいものは
`AGENTS_MD_INCLUDES` にも)に追記してください。
