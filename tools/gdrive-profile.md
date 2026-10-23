# Drive CLI のプロファイル

`setup_agy_project.py` が、このファイルを対話で作成・設定します(認証の種類、資格情報を取り出す
コマンド、スコープ、有効なコマンド、許可フォルダ、アップロード拒否パターン)。手で書いても、
後から手で直してもかまいません。以下は、ファイルの書式の説明です。

`gdrive_cli.py --profile <名前> <コマンド> ...` は、認証情報も権限の絞り込みも、
プロファイル(JSON)から読みます。スクリプト自体には、どの保管先も、どのプロジェクトの
値も入っていません。

- 置き場: `<ブリッジの設定ディレクトリ>/gdrive/<名前>.json`
  (既定 `~/.config/agy-slack-bridge/gdrive/`。環境変数 `AGY_BRIDGE_CONFIG_DIR` で変更可)。
  ここは `permissions.deny` の対象(`setup_agy_project.py` が設定ディレクトリごと塞ぐ)で、
  エージェントは読み書きしません。
- 名前に使えるのは英数字と `_` `-` だけ。
- プロファイルまたはその親ディレクトリが group/other から**書き込み可能**だと、実行を拒否します
  (ポリシーの改ざん防止)。読み取り可能なだけなら警告です。`chmod 600` / `700` にしてください。
- 未知のキーはエラーです(綴り間違いで制限が黙って外れるのを防ぐため)。
- 雛形: [`gdrive-profile.example.json`](gdrive-profile.example.json)

## キー

| キー | 必須 | 内容 | 省略時 |
|---|---|---|---|
| `kind` | はい | `service_account` または `oauth` | (エラー) |
| `credentials.command` | はい | 資格情報の JSON を stdout に出す**コマンドの argv**(リスト。シェル文字列は不可) | (エラー) |
| `credentials.paths` | いいえ | 上のコマンドが読むファイル。実行時に group/other から読めれば警告 | なし |
| `scopes.read` / `scopes.write` | いいえ | 読み取り系/書き込み系で要求するスコープ | `drive.readonly` / `drive` |
| `commands` | いいえ | 有効にするサブコマンド | 全部(読み取り5 + 書き込み3) |
| `allowed_folders` | いいえ | 操作を許すフォルダ ID。その配下(祖先をたどって判定)だけ | 空 = 制限なし |
| `upload_deny.names` / `.dirs` | いいえ | アップロード元として拒否するファイル名パターン/ディレクトリ名。**指定すると既定を丸ごと置き換える** | 既定の拒否リスト |

`commands` から書き込み系を外せば、そのプロファイルでは書き込みが一切できません。
`allowed_folders` を指定すると、`search` には `--in <フォルダID>` が必須になります。

## 資格情報を取り出すコマンド

`credentials.command` は、`gdrive_cli.py` が**直接** subprocess で実行します(シェルは通さない)。
標準出力が JSON として解釈されます。標準出力・標準エラーは、エラー文にも含めません。

| `kind` | 期待する JSON |
|---|---|
| `service_account` | サービスアカウントの鍵(`type: service_account`, `private_key`, `client_email` ...) |
| `oauth` | `client_id`, `client_secret`, `refresh_token`(任意で `token_uri`) |

保管先ごとの書き方の例:

| 保管先 | `credentials.command` |
|---|---|
| AWS Secrets Manager | `["aws","secretsmanager","get-secret-value","--secret-id","<名前>","--query","SecretString","--output","text"]` |
| ローカルファイル | `["cat","/home/you/.config/agy-slack-bridge/gdrive/proj-a.sa.json"]` (+ `credentials.paths` にそのパス。**プロジェクトのディレクトリの外**に `chmod 600` で置く) |
| 1Password | `["op","read","op://Vault/proj-a-gdrive/credential"]` |
| pass | `["pass","show","proj-a/gdrive"]` |
| GCP Secret Manager | `["gcloud","secrets","versions","access","latest","--secret","proj-a-gdrive"]` |
| Vault | `["vault","kv","get","-field=key","secret/proj-a/gdrive"]` |

### 守ること

- 取得コマンドは、**`gdrive_cli.py` が自分で実行する**ものです。エージェントの許可サーフェスには
  乗りません(エージェントが見る・承認するのは、外側の `gdrive_cli.py --profile ... list` だけ)。
  「`run_command` で取得してから渡す」形に変えると、裏の取得コマンド自体が承認対象になってしまうので、
  変えないでください。
- `aws secretsmanager ...` や `op read ...` のような取得コマンドを、**許可リストに入れない**でください。
- 取得コマンドは、ブリッジ(systemd のユーザーサービスなら、最小限の `PATH`)から実行されます。
  `PATH` に無い場所にインストールしたツール(pipx、snap、`pip --user` など)は、絶対パスで書くか、
  unit に `Environment=PATH=...` を足してください。
- 許可エントリでは `--profile <名前>` を、サブコマンドより前に固定します
  (`command(<prefix>/.venv/bin/python -I <prefix>/tools/gdrive_cli.py --profile proj-a list)`)。末尾の自由引数だと、
  エージェントが別プロジェクトのプロファイルを選べてしまいます。`setup_agy_project.py` はこの形で登録します。
- 保管先を変えても、プロファイルの中身を直すだけで、承認済みのコマンド文字列は変わりません(再承認は不要)。

## OAuth の注意(運用者の責任)

- **スコープは、リフレッシュトークンを発行したときの同意内容と一致させる必要があります。**
  リフレッシュ時に要求できるのは、最初の同意で与えたスコープの範囲内だけです。
  **実機で確認済み**: `drive` で同意したトークンに、既定の `scopes.read`(`drive.readonly`)を使うと、
  より狭いスコープでも `invalid_scope` で全て失敗しました(`scopes.read` を `drive` にして解決)。
  スクリプトは、
  トークンが `drive` で同意されたのか `drive.readonly` で同意されたのかを事前には判断できないので、
  `scopes.read` / `scopes.write` を正しい値にするのは運用者の責任です。誤っていると
  `invalid_scope` などでリフレッシュに失敗します(その旨のエラーを出します)。
  - 読み取りだけのプロファイルは、`drive.readonly` で同意したトークンを使い、`commands` から書き込み系を外すのが
    最も安全です(トークン自体が書き込めない)。
  - `drive` だけで同意したトークンしか無い場合は、`scopes.read` も `drive` にします。この場合、
    読み取り専用であることは Google 側では保証されず、`commands` の設定だけが守りになります。
- **OAuth はそのユーザーの Drive 全体が見えます**(サービスアカウントは共有されたものだけ)。
  `allowed_folders` の設定を強く推奨します。
- アクセストークンは実行のたびにリフレッシュで取得し、保存しません。
- **OAuth 同意画面の公開状態が「テスト」のままだと、リフレッシュトークンは 7 日で失効します。**
  個人アカウントでは「テスト」のまま使っていることが多く、週に一度、突然動かなくなります。
  失効したら同意をやり直してトークンを更新し、保管先を更新してください。
  恒常的に使うなら、アプリを「本番」に公開する必要があります(利用者数やスコープによっては
  Google の審査が要ります)。
