# 実行許可の仕組み

agy が `run_command`（シェルコマンド）や MCP ツールを実行するとき、どう許可されるかをまとめる。
Slack からの操作方法は README の「Managing permissions from Slack」を参照。

## 1. 許可の層

| 層 | 場所 | 中身 | 範囲 |
|---|---|---|---|
| プロジェクトの許可 | `~/.gemini/config/projects/<id>.json` の `permissionGrants.permissionGrants.allow` | `command(...)` や `read_url(...)` などのエントリ | そのプロジェクト（＝Slackチャンネル）だけ |
| 一時許可 | bridge の state DB（`temp_grants`） | 同上のエントリ | その会話の間だけ有効。成功が続いたら自動で外れる |
| 恒久許可の台帳（任意） | プロジェクトの `.agents/.permanent-commands.json` | スクリプトごとの sha256（seal） | そのプロジェクトだけ |
| agy 自身の許可/拒否リスト | `~/.gemini/antigravity-cli/settings.json` の `permissions.allow` / `deny`、`trustedWorkspaces` | 同上のエントリ、または絶対パス | agy 全体（bridge はここを**書かない**） |

**bridge が書き込むのは、プロジェクトの `permissionGrants`（と、スクリプトなら `.permanent-commands.json`）だけ。** 一時許可も恒久許可も、どちらもそのチャンネルが紐づくプロジェクトのファイルにしか書かない。agy 自身の `settings.json` は、あなたが手で（あるいは `agy` コマンド自体の操作で）編集する分には触れるし、`trustedWorkspaces` や `deny` はそこに置いたままでよい ― bridge の機能としては、どのプロジェクトにも共通で効くような許可を作る経路が無い、という意味。

### なぜプロジェクト単位にしたか（2026-10-12 変更）

以前は恒久許可・一時許可ともに `settings.json` の `permissions.allow` に書いていた。これは agy の全プロジェクト（＝全Slackチャンネル）に共通で効くリストなので、例えば経理チャンネルで `aws logs filter-log-events` を許可すると、秘書チャンネルの会話からも同じコマンドが素通りで実行できてしまっていた。実際に `aws lambda list-functions` 等の経理専用コマンドや Yayoi 操作用の `mcp(playwright/...)` 系が秘書側からも使える状態になっていたのを確認し、全エントリを該当プロジェクトの `permissionGrants` に振り分け直した。

運用上、経理と秘書が同じスクリプト（`gdrive_cli.py` など）を使うことはあるが、それは**各プロジェクトが別々にそのコマンドを許可している**状態であって、1つの共有リストを両方が参照しているわけではない。新しいプロジェクトを増やしても、他プロジェクトの許可を一切引き継がない（ゼロから許可し直しになる）。

## 2. マッチの仕方

- `command(git diff)` は、先頭のトークンが一致すれば通る。`git diff --stat` も `git diff HEAD~1` も通る。`git diffX` や `git status` は通らない。
- 末尾に `*` を付けても、確実には効かない。書かないこと。
- 複合コマンド（`&&`、`;`）は、各サブコマンドを個別に照合する。`echo hi && touch x` は、`touch` が許可されていなければ全体が拒否される。
- deny は allow より優先される。
- 1 行の中で許可されたものと拒否されたものが混ざると、全体が拒否される。

## 3. 許可の粒度（指針）

- サブコマンド単位が基本。`command(aws logs)` のように広く取ると、削除系も通ってしまう。`command(aws logs get-log-events)` のように、読み取りだけを個別に書く。
- 次は許可しない。
  - インタープリタ（`python3`、`node`、`bash` など）そのもの。
  - パッケージランナー（`npx`、`uvx` など）の、任意パッケージの実行。
  - タスクランナーのワイルドカード（`npm run *`、`make *` など）。
  - `gh api`、`docker exec`、`sudo`、`rm`、`git push` などの書き込み系。
- 決まった引数の読み取り専用スクリプト（例: `scripts/foo.py`）は、プレフィックスで許可してよい。ただし、そのスクリプトに出力先を引数で渡せるなら、先に出力先を固定する。
- `read_url(ドメイン)` はドメイン単位。読み取り専用ツールなので、そのプロジェクトで使う既知のドメイン（例: 研究用プロジェクトなら `arxiv.org` 等）をまとめて許可しておくのは比較的安全。

## 4. 一時許可と恒久許可

- Slack の承認ボタン「今回だけ許可」は、その**プロジェクトの** `permissionGrants.allow` に即座に書かれ、同時に bridge の state DB（`temp_grants`、会話単位）にも記録される。
  - 1 回失敗しても外さない。その会話が「エラーなし」で終わった（clean turn）時点で、同じプロジェクトの他の会話がまだ必要としていなければ外す。
  - 会話が 1 時間（`AGY_TEMP_GRANT_MAX_AGE_SEC`）以上動いていない場合も、外す（同じ「同一プロジェクト内で他に使ってないか」の確認付き）。
- 「恒久的に許可」も、同じ**プロジェクトの** `permissionGrants.allow` に書かれたままになる。スクリプトなら、そのプロジェクトの `.permanent-commands.json` に seal も入る。
- 使われた回数は、`permission_log` に記録される。`/agy-permissions stats` で見られる（記録は `telemetry` の `permission_stats` で切り替え）。
- 停滞の検知（silent denial）: agy が「DONE・出力なし」で終わった `run_command` は、拒否と誤判定されうる。すでに許可済みのエントリと一致する場合は、拒否扱いしない。

## 5. 恒久許可のスクリプト（seal）

`.agents/.permanent-commands.json` は、`{ "<スクリプトの絶対パス>": "<sha256>" }` の形式。プロジェクトのリポジトリ内に置かれるので、これは元から1プロジェクト専用。

- **seal**: 承認した時点の内容のハッシュを書く。bridge は `/agy-permissions add` の時に自動で書く。同じ `add` をもう一度送ると、ハッシュは今の内容で書き直される（再 seal）。
- **フックの判定**（プロジェクト側の `PreToolUse` フック）:
  - 恒久許可のスクリプトは、ハッシュが一致し、かつ git でコミット済み・差分なしの時だけ通す。
  - コミットされていても、ハッシュが違えば拒否する。コミットは承認にならない。
- **一時的なスクリプト**（恒久許可ではない）: 初回の内容を台帳（`.command-hashes.json`）に記録する。後で内容が変わっていたら、差分を付けて一度だけ拒否し、台帳を更新する。もう一度同じ指示を送れば通る。git は要らない。
- 作業用ディレクトリ（例: `scratch/`）を gitignore しているなら、そこのスクリプトは恒久許可にできない（git の clean を満たせない）。恒久許可にしたいなら、`scripts/` などに移してコミットしてから seal する。

### 運用の順番

1. スクリプトを編集する。
2. コミットする（フックは clean を要求する）。
3. `/agy-permissions add` をもう一度送って再 seal する。

## 6. MCP ツール

MCP ツールは、サーバーごとのツール名で許可する。`run_command` の prefix 許可より粒度が細かい。任意コードを実行するツールは、許可しない。プロジェクト単位である点は他のエントリと同じ（例: Yayoi 操作用の `mcp(playwright/...)` は経理プロジェクトにのみ許可されている）。

## 7. 確認方法

- `/agy-permissions list`: **今いるチャンネルのプロジェクト**の `permissionGrants.allow`。他プロジェクトのものは見えない・含まれない。
- `/agy-permissions stats [日数]`: このチャンネルで、許可を求められた回数の多い順。
- `/agy-stats buttons [日数]`: 同じ内容を、ボタンの結果（今回・恒久・拒否）つきで表示。

## 8. 新しいプロジェクトを増やすとき

新しいプロジェクト（＝新しいSlackチャンネル）を `config.yaml` に追加した直後は、そのプロジェクトの `permissionGrants.allow` は空。既存プロジェクト（経理・秘書など）の許可は一切引き継がれない。最初の数回は許可ボタンが頻発するのが正常な動作で、その都度「恒久的に許可」していけば、そのプロジェクト専用の許可リストが育っていく。
