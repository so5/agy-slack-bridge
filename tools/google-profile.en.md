# Google Calendar / Mail profile

The per-account settings file `<config dir>/google/<profile name>.json` used by `tools/google_calendar.py` and
`tools/google_mail.py`. `setup_agy_project.py` (item `10`) normally creates it. If you write one by hand, make the
directory 0700 and the file 0600 (the CLIs refuse a file others can write).

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

| Key | Meaning |
|---|---|
| `token` | `client_id` / `client_secret` / `refresh_token` (required) and `scopes` (optional). Exactly one of this and `token_from` |
| `token_from` | Another profile's name: use that profile's token (same account, different policy or search condition). No chains |
| `mail_filter` | Optional Gmail search condition. ANDed into `search`; `read` / `attachment` / `draft-reply` only work on mail matching it. **It narrows; it is not a permission boundary** |
| `commands` | Optional (default: all). `calendar.list` `calendar.search` `calendar.create` `calendar.update` `calendar.delete` `mail.search` `mail.read` `mail.attachment` `mail.draft-reply` `mail.draft-new` |
| `write_mode` | Optional (default `confirm`). `confirm`: each write command needs a Slack grant button press. `allow`: permanently allowed (the agent must report everything it ran) |
| `timezone` | Optional (default `Asia/Tokyo`). Used for times without a zone |

Unknown keys are an error (so a misspelling cannot silently stop a restriction from applying).

## Getting the token

The token is the JSON from an OAuth flow, with a refresh token (the `authorized_user` format of google-auth,
a `token.json`). Nothing in this repository obtains it. See Google's documentation:

- [Create an OAuth client ID](https://developers.google.com/workspace/guides/create-credentials#oauth-client-id)
- [OAuth 2.0 overview](https://developers.google.com/identity/protocols/oauth2)

Scopes: Calendar `https://www.googleapis.com/auth/calendar` (plus `https://www.googleapis.com/auth/meetings.space.settings`
for Meet transcription), Mail `https://www.googleapis.com/auth/gmail.modify` (needed to create drafts; the CLI has no
send or delete).

Only the tool's own scopes from the token's `scopes` are requested. A token obtained with read-only scopes only
(`calendar.readonly`, `gmail.readonly`) cannot write on Google's side either (a second limit beside the CLI's `commands`).

## The action log

Every run appends one JSON line to `<config dir>/google/actions.log` (0600; the agent cannot read it): time, profile,
command, success, and key facts such as IDs and subjects. Mail bodies and event descriptions are not recorded.
