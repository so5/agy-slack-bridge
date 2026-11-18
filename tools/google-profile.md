# Google カレンダー・メールのプロファイル

`tools/google_calendar.py` と `tools/google_mail.py` が使う、アカウントごとの設定ファイル
`<設定ディレクトリ>/google/<プロファイル名>.json` の書式です。通常は、`setup_agy_project.py`
（項目 `10`）が作ります。手で書くときは、ディレクトリを 0700、ファイルを 0600 にしてください
（自分以外が書き込めるファイルは、CLI が拒否します）。

```json
{
  "token": {
    "client_id": "…",
    "client_secret": "…",
    "refresh_token": "…",
    "scopes": ["https://www.googleapis.com/auth/calendar",
               "https://www.googleapis.com/auth/gmail.modify"]
  },
  "mail_filter": "(to:a@example.com OR from:example.com)",
  "commands": ["calendar.list", "calendar.search", "mail.search", "mail.read"],
  "write_mode": "confirm",
  "timezone": "Asia/Tokyo"
}
```

| キー | 意味 |
|---|---|
| `token` | `client_id` / `client_secret` / `refresh_token`（必須）と、`scopes`（任意）。`token_from` と、どちらか一方だけ |
| `token_from` | 別のプロファイルの名前。そのプロファイルのトークンを使う（同じアカウントで、方針や検索条件だけ変えたいとき）。連鎖はできない |
| `mail_filter` | 任意。Gmail の検索条件。`search` に AND で足され、`read` / `attachment` / `draft-reply` は、この条件に合うメールにしか使えない。**絞り込みであって、権限の境界ではない** |
| `commands` | 任意（省略 = 全部）。`calendar.list` `calendar.search` `calendar.create` `calendar.update` `calendar.delete` `mail.search` `mail.read` `mail.attachment` `mail.draft-reply` `mail.draft-new` |
| `write_mode` | 任意（既定 `confirm`）。`confirm`: 書き込み系は、毎回 Slack の許可ボタンで確認する。`allow`: 恒久許可（実行した全件を、エージェントがユーザーに報告する） |
| `timezone` | 任意（既定 `Asia/Tokyo`）。タイムゾーンの無い時刻に使う |

未知のキーはエラーです（綴り間違いで制限が黙って効かなくなるのを防ぐため）。

## トークンの取得

トークンは、OAuth で取得した、リフレッシュトークン入りの JSON（google-auth の `authorized_user` 形式の
`token.json`）です。取得は、このリポジトリのスクリプトでは行いません。Google のドキュメントを見てください:

- [OAuth クライアント ID の作成](https://developers.google.com/workspace/guides/create-credentials#oauth-client-id)
- [OAuth 2.0 の概要](https://developers.google.com/identity/protocols/oauth2)

必要なスコープ: カレンダー `https://www.googleapis.com/auth/calendar`（Meet の文字起こしには
`https://www.googleapis.com/auth/meetings.space.settings` も）、メール `https://www.googleapis.com/auth/gmail.modify`
（下書きの作成に必要。送信・削除は、この CLI にはない）。

トークンの `scopes` に書かれたもののうち、そのツールのスコープだけを要求します。読み取り専用の
スコープ（`calendar.readonly` `gmail.readonly`）だけで取得したトークンを使えば、書き込みは、Google 側でも
できなくなります（CLI の `commands` による制限に加えた、もう一段の制限）。

## 実行の記録

実行のたびに、`<設定ディレクトリ>/google/actions.log` に 1 行（JSON）追記されます（0600。エージェントは読めません）。
日時・プロファイル・コマンド・成否と、IDと件名などの要点です。メールの本文や、予定の説明は記録しません。
