# agy-slack-bridge

Bridge Slack channels to [Antigravity CLI](https://antigravity.google.com) (`agy`) projects, so you
can talk to your agy agents from Slack.

[日本語](README.md)

- Each Slack channel maps 1:1 to one agy project.
- A message that **@-mentions the bot** starts a conversation; the reply is posted in a thread.
  Follow-up replies inside the thread need no mention.
- When an action is denied, Slack shows **permission buttons**, so you can approve even from a phone.
- Switching models, checking quota and running agy's slash commands also work from Slack.
- Images, PDFs, CSVs etc. that agy generates are uploaded to the thread; Mermaid diagrams become images.

## ⚠ Security assumptions

Anyone who can post in a bridged channel can have agy run commands, edit files, and more, with the permissions
of the user running agy. Make the channel **private, with only the person who runs agy and the bridge, plus the
bot**. Inviting anyone else amounts to handing them that user account.

Mechanisms that restrict what agy can do are also provided. See "Keeping the agent inside its lane" in [docs/usage.en.md](docs/usage.en.md) and [docs/permissions.en.md](docs/permissions.en.md). When safety mechanisms are missing, the bridge repairs them automatically (and tells you), and does not run `agy` if something cannot be repaired ([details](docs/permissions.en.md#9-when-a-safety-mechanism-is-missing)).

## Setup walkthrough

**You need**: Linux with `systemd --user`, Python 3.10+ (with `venv`), `git`, **`agy` logged in as the user who
will run the bridge**, and a Slack workspace where you can create apps.

A human runs the setup from a terminal. The setup screen is in Japanese; each prompt below is quoted as
shown, with its meaning. `[...]` in a prompt is the default: **Enter keeps it, `-` clears the value**.
When you re-run it, the current values appear as defaults.

### 1. Create the Slack app

1. <https://api.slack.com/apps> → **Create New App** → **From an app manifest** → choose the workspace.
2. Paste the contents of [`slack-app-manifest.yaml`](slack-app-manifest.yaml) and create the app.
3. **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**: create a token with the
   scope `connections:write`. The value starting with `xapp-…` is the **App token** (`SLACK_APP_TOKEN`).
4. **Install App** → **Install to Workspace**. The `xoxb-…` shown is the **Bot token** (`SLACK_BOT_TOKEN`).
5. Create a **private channel** for the bridge and invite the bot (`/invite @agy` in the channel).
6. Note the channel ID (`C…` / `G…`, at the bottom of the channel details).

> ⚠ Creating a new app from this manifest has not actually been tried yet (the scope list was checked
> against the running app). If it fails, configure the app by hand using the list in
> [docs/slack-app.en.md](docs/slack-app.en.md).

### 2. Install

```sh
git clone https://github.com/so5/agy-slack-bridge.git
cd agy-slack-bridge
python3 tools/install.py --with-gdrive      # leave out --with-gdrive if you will not use the Drive CLI
```

The install location is `~/.local/share/agy-slack-bridge` (`<prefix>` below); the config is
`~/.config/agy-slack-bridge`. For installing as root under `/opt` and the options, see
[docs/install.en.md](docs/install.en.md).

| Prompt | Answer |
|---|---|
| `続けてセットアップを実行しますか? [Y/n]` (Run the setup now?) | **Y** (step 3 starts). On N it prints the command to run later |

### 3. Set up the project (`setup_agy_project.py`)

To run it later (you answered N, or want to change settings):

```sh
~/.local/share/agy-slack-bridge/.venv/bin/python ~/.local/share/agy-slack-bridge/tools/setup_agy_project.py
```

A menu opens. Choose by typing a number or key (the `[...]` item is chosen with Enter).

1. **First screen**: the projects registered in agy are listed with their setup status. Choose the number of the project to set up, or `n`
   (create a new project). `t` is "Slack app settings" (the tokens; shared by everything; 3-3) and `q` quits.
2. **Project menu**: after choosing a project you get the items below. **The first time, choose `1`.**

| Item | What it does |
|---|---|
| `1` Set up everything | 3-1 → 3-2 → 3-4 in order (first time, or updating). Required items come first; the optional Drive CLI (3-4) comes last |
| `2` Place / update the rules and hook | Only the placement part of 3-1 |
| `3` Set up the Slack channel mapping | 3-2 |
| `4` Set up the Google Drive CLI | 3-4 |
| `5` Show the current state | |
| `b` Back to the project list | |
| `q` Quit | |

Items `2`–`4` show their current state on the right (`[設定済み]` = set / `[未設定]` = not set).

**Statuses in the list**

| Shown | Meaning |
|---|---|
| `[利用可能]` (available) | The required settings (safety mechanisms: rules, hook, `deny`; and a Slack channel) are in place, so it can be used from Slack. "Available" does **not** mean setup is finished: every unset optional item is listed after `未設定（任意）:` (Drive CLI, display name, icon, model, effort, watched files, permission-button stats). Leave alone what you do not use |
| `[利用不可]` (unavailable) | Some required settings are missing, so it cannot be used from Slack; they are listed after `未設定（必須）:` |
| `[未設定]` (not set up) | None of the required settings exist (exactly one of available / unavailable / not set up is shown) |
| `⚠ …` | An inconsistency (a Drive permission without its profile, etc.) |

The `t` line of the first screen (`Slack app の設定`) also shows the Slack token status (`設定済み` / `未設定（必須）: …`).

To set up another project, go back with `b` and choose again. Prompts that ask for a path complete it with Tab.

#### 3-2. Mapping a Slack channel

| Prompt | Answer |
|---|---|
| `Slack チャンネルを対応づけますか? [Y/n]` (map a Slack channel?) | Asked only for item `1` (set up everything). **N** to skip (go on); **Y** to map |
| `チャンネル ID` (channel ID) | **The only required item.** The `C…` / `G…` from step 1 |
| `任意の項目（表示名・アイコン・モデル・effort・監視ファイル・統計）も設定しますか? [y/N]` (also set the optional items?) | **N** (default): stop here. No display name etc. is set and the defaults apply (values already set are kept). **y**: go on to the questions below |

Questions when you answer **y** to the optional items:

| Prompt | Answer |
|---|---|
| `表示名` (display name) | Optional. The name on the bot's replies (needs `chat:write.customize`) |
| `アイコン絵文字` (icon emoji) | Optional, e.g. `robot_face` (the surrounding `:` are optional). Type `:` and a few letters in a Slack message box to see candidate names (custom emoji work the same way). If given, the image URL is not asked |
| `アイコン画像 URL` (icon image URL) | Only when the emoji is empty. A URL starting with `https://` |
| `モデル` (model) | Empty = agy's default model. Otherwise a name from `agy models`. A name with `-high` / `-low` already includes the reasoning depth, so leave `effort` empty |
| `effort（low / medium / high / xhigh / max）` | Empty = agy's default. How deeply the model reasons. Leave it empty if the model name above ends in `-high` etc. (they conflict) |
| `監視するファイル（相対パス、カンマ区切り）` (files to watch) | Default: empty (nothing is watched). Paths relative to the project directory (e.g. `AGENTS.md`). Changes are not blocked: after a change, Slack gets a notice with a diff (checked every 30 minutes by default), and its buttons let you accept the change or restore the file. See "Config drift detection" in [usage.en.md](docs/usage.en.md) |
| `権限ボタンの統計を取りますか? [y/N]` (collect permission-button stats?) | **y**: record permission-button requests (one-time / permanent) and their results in this machine's state DB (nothing is sent elsewhere). **N**: do not record (default). Can also be switched later with `/agy-stats on\|off` in Slack. See "Usage statistics" in [usage.en.md](docs/usage.en.md) |

The original `config.yaml` is kept as `config.yaml.bak`.

#### 3-3. Slack tokens

| Prompt | Answer |
|---|---|
| `SLACK_BOT_TOKEN（xoxb-…）` | Paste the Bot token from step 1 (input is not echoed). Empty skips |
| `SLACK_APP_TOKEN（xapp-…）` | Paste the App token from step 1 |

The tokens are saved to `~/.config/agy-slack-bridge/env` (0600). Ones already set are not asked again.

#### 3-4. Google Drive CLI (optional; skip if unused)

Credentials and the Drive policy (enabled commands, allowed folders, upload deny list) are kept in a profile, `<config dir>/gdrive/<profile>.json`. The profile name is set automatically to the agy project ID (unique per project), so it never collides with another project.

If other projects have profiles, you first get this choice:

| Prompt | Answer |
|---|---|
| `Google Drive CLI のプロファイル` (`選択`) (Drive CLI profile) | `n` (default): create a new profile for this project. A number: use the same settings and permissions as project "XXXX". Credentials and policy are shared, and changing the policy affects XXXX too |

| Prompt | Answer |
|---|---|
| `Drive CLI を使いますか? [y/N]` (use the Drive CLI?) | Asked only for item `1` (set up everything). **N** to skip (setup is complete); **y** to use it |
| `認証の種類（service_account / oauth）` (authentication type) | See the table below. If unsure, press Enter (`service_account`) |
| `サービスアカウントの JSON 鍵のパス` (path to the service account JSON key) | When you chose `service_account`. The path of the downloaded key file. It is saved as `<config dir>/gdrive/<profile>.sa.json` (0600) and the email address to share folders with is shown (delete the original key file). Empty = use another key store via the next prompt |
| `資格情報を取り出すコマンド` (command that prints the credentials) | For `oauth`, or when the key path was left empty. A **command that prints the key / token JSON to stdout** (examples below). Do not type a scope (`drive` etc.) here |
| `` `…` というコマンドが見つかりません。このまま使いますか? [y/N]`` (command not found; use anyway?) | Only shown when the command is not found. If it is a typo, answer **N** and start again |
| `詳細設定（スコープ・方針）を変更しますか? [y/N]` (change the advanced settings?) | **N**: keep the defaults below. **y**: answer the advanced questions below. Defaults: scopes `drive.readonly` (read) / `drive` (write); all commands enabled (`list,search,meta,read,download,mkdir,upload,update`); no folder limit; upload deny list of secret-like files (`.env*`, `*.pem`, `*.key`, …) and directories (`.git`, `.ssh`, …). With no folder limit, everything the credentials can see is in scope, so setting folders is recommended |

**Which authentication**

| | `service_account` (recommended) | `oauth` |
|---|---|---|
| Good for | **Reading** Drive folders and **updating files a person created** | Letting agy **create new files itself**, or acting with the user's own permissions |
| Preparation | Create a service account and share the folders with its email address | Consent screen and a refresh token are needed |
| What it can see | Only what is shared with it | The user's whole Drive (narrow it with allowed folders) |
| Caveats | **Cannot create new files** in a personal Drive (no storage quota) | If the consent screen is in "Testing", the token expires in 7 days; scopes must match the original consent |

**Service account preparation** (do this before the setup):

1. Create a project in the [Google Cloud console](https://console.cloud.google.com/) (an existing one is fine).
2. **APIs & Services** → **Library**: enable the **Google Drive API**.
3. **IAM & Admin** → **Service Accounts** → **Create service account** (no role needed).
4. Open the new service account → **Keys** → **Add key** → **Create new key** → **JSON**, and download the key file.
5. In the setup, enter the path of that key file. Share the Drive folders you want to allow with the email address
   shown (`…@….iam.gserviceaccount.com`): "Viewer" for read-only, "Editor" to allow writes.

> ⚠ **The key file is stored on disk as plaintext JSON.** Any process running as the same OS user can read it
> (agy is set up not to, but that is not a hard barrier). Anyone holding the key can operate on the folders you
> shared. Share only the folders you need, and as "Viewer" if no writing is needed. If it leaks, delete the key in
> the Cloud console. For stronger storage, leave the key path empty and use a command that fetches it from a
> password manager or similar.

For OAuth preparation and caveats and for how new files are handled, see
[tools/gdrive-profile.en.md](tools/gdrive-profile.en.md).

**Examples of the credential command** (for `oauth`, or a key store other than a key file; run directly, not through a shell)

```
cat /home/you/secrets/drive-key.json
op read 'op://Vault/Drive key/credential'
aws secretsmanager get-secret-value --secret-id NAME --query SecretString --output text
```

More stores (pass, GCP Secret Manager, Vault) are in [tools/gdrive-profile.en.md](tools/gdrive-profile.en.md).

Prompts when you answered **y** to "change the advanced settings":

| Prompt | Answer |
|---|---|
| `読み取りに使うスコープ` / `書き込みに使うスコープ` (read / write scope) | Only when creating the profile. Defaults: `drive.readonly` / `drive`. Change only for an OAuth token consented with other scopes |
| `有効にするコマンド（カンマ区切り or all）` (enabled commands) | Subcommands to allow. Default: all. The read ones (`list,search,meta,read,download`) are registered as permanent permissions. The write ones (`mkdir` / `upload` / `update`) never enter the allow list even when enabled; Slack asks for approval each time. To forbid writing entirely, enter only the read ones |
| `許可フォルダ（URL か ID、カンマ区切り。`-` で制限なし）` (allowed folders) | Folders that may be touched; a folder URL can be pasted. `-` = no limit. **Strongly recommended** (OAuth sees the user's whole Drive) |
| `アップロード拒否パターン（k=今のまま / a=追加 / r=置換）` (upload deny patterns) | A deny list that stops secrets from being uploaded by mistake. **k**: keep the current list. **a**: add file-name patterns (e.g. `*.sqlite`) and directory names to the default list. **r**: type the whole list again (the default entries are dropped, so be careful) |
| `緩める変更を適用しますか? [y/N]` (apply the loosening change?) | Only when you widened permissions (added a command, added a folder or removed the folder limit, removed a deny pattern). Read the diff shown just before: **y** if it is what you meant, **N** otherwise (only that change is cancelled) |

**Updating an already configured Drive CLI**

Choosing item `4` in the project menu for a project whose Drive CLI is already configured opens an update menu.

| Item | What it does |
|---|---|
| `1` Change the policy | Change enabled commands, allowed folders, and upload denials (the "advanced settings" questions above) |
| `2` Re-enter the credentials | For an expired or leaked key, or a change of type (`service_account` ⇔ `oauth`). Asks again from `認証の種類`; the policy is kept. `スコープを変更しますか?` also lets you change the scopes. A stored service account key that is no longer used is deleted. For a shared profile, a confirmation is shown because other projects are affected. **After a leak or a replacement, also disable the old key on Google's side** (the script cannot do this): for a service account, delete the old key in the Cloud console under **IAM & Admin** → **Service Accounts** → the account → **Keys**. For OAuth, revoke the app's access in [your Google account's third-party access page](https://myaccount.google.com/permissions) |
| `3` Switch to another profile | `n`: switch to this project's own profile (named after the project ID; created if missing). A number: switch to the same settings and permissions as project "XXXX". The old profile's permissions are removed from this project. If no project uses the old profile any more, you are asked whether to delete it (default N) |
| `4` Remove this project's Drive settings | Removes this project's Drive CLI permissions. If no project uses the profile any more, you are asked whether to delete it with its credentials (default N) |
| `b` Back | |

### 4. Start

```sh
mkdir -p ~/.config/systemd/user
cp ~/.local/share/agy-slack-bridge/systemd/agy-slack-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agy-slack-bridge.service
loginctl enable-linger "$USER"      # keep it running after logout / start at boot
```

If it fails to start, look at `journalctl --user -u agy-slack-bridge.service -f` (if `agy` cannot be run, the
bridge says so and exits). Details: [docs/install.en.md](docs/install.en.md).

### 5. Check that it works

In the channel, send `@agy hello` as a mention. If a reply appears in the thread, you are done. If it says
permission is missing, press the **permission button** (common at first).

For usage (slash commands, `resume`, stats, per-channel settings) see [docs/usage.en.md](docs/usage.en.md).

## Operation

- After changing the config, restart: `systemctl --user restart agy-slack-bridge.service`
- Logs: `journalctl --user -u agy-slack-bridge.service -f`
- Adding a project / changing settings: run `setup_agy_project.py` again (idempotent).
- Upgrade: `git pull`, re-run `python3 tools/install.py --no-setup`, restart (your config is not overwritten).
- Uninstall and troubleshooting: [docs/install.en.md](docs/install.en.md).

## Documentation

| Document | Contents |
|---|---|
| [docs/install.en.md](docs/install.en.md) | prerequisites, install, start, check, upgrade, uninstall, troubleshooting |
| [docs/slack-app.en.md](docs/slack-app.en.md) | creating the Slack app (manifest) and the full list of required settings |
| [docs/usage.en.md](docs/usage.en.md) | using it from Slack (threads, `resume`, permission buttons, slash commands, stats, Mermaid, config drift detection) and per-channel settings |
| [docs/permissions.en.md](docs/permissions.en.md) | how permissions work and where they are stored; script seals |
| [tools/README.en.md](tools/README.en.md) | the installer, setup script, hook and Drive CLI in detail |
| [tools/gdrive-profile.en.md](tools/gdrive-profile.en.md) | the Drive CLI profile format (which authentication to choose, credentials, OAuth notes) |

Japanese editions: [README.md](README.md), `docs/*.md`, `tools/*.md`.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## License

MIT — see [LICENSE](LICENSE).
