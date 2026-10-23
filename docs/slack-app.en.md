# Creating the Slack app

Create the Slack app the bridge uses. The bundled manifest
[`slack-app-manifest.yaml`](../slack-app-manifest.yaml) saves you from configuring scopes,
events, slash commands and the shortcut by hand.

## Steps (with the manifest)

1. At <https://api.slack.com/apps> choose **Create New App** → **From an app manifest**,
   and pick your workspace.
2. Paste the contents (YAML) of `slack-app-manifest.yaml` and create the app.
3. **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**: create a
   token with the scope `connections:write`. The `xapp-…` value is `SLACK_APP_TOKEN`
   (a manifest cannot create this one, so it is the only manual step).
4. **Install App** → **Install to Workspace**. The **Bot User OAuth Token** (`xoxb-…`)
   shown is `SLACK_BOT_TOKEN`.
5. Invite the bot to each channel you want to bridge (`/invite @agy` in the channel).
6. Note each channel's ID (the `C…` / `G…` value at the bottom of the channel details);
   you enter it in the setup dialogue.

You can enter the two tokens in the `tools/setup_agy_project.py` dialogue (the input is
not echoed; they are stored in `~/.config/agy-slack-bridge/env` with mode 0600).

> The bridge runs over **Socket Mode** (an outbound connection from your machine to Slack).
> No public URL or inbound port is needed.

## What the manifest configures

If you configure the app by hand instead, or want to review it later:

| Setting | Value | Purpose |
|---|---|---|
| Socket Mode | enabled | run with an outbound connection only |
| Bot Token Scopes | `chat:write` | post replies (required) |
| | `channels:history` / `groups:history` | receive messages in public / private channels (match the channels you bridge) |
| | `commands` | slash commands |
| | `files:write` | upload files agy generated (optional) |
| | `chat:write.customize` | per-channel display name and icon (optional) |
| Event Subscriptions (bot events) | `message.channels` / `message.groups` | receive messages (match the scopes) |
| | `app_home_opened` | Home tab (optional) |
| Interactivity | enabled | permission buttons etc. (no Request URL needed with Socket Mode) |
| Slash Commands | `/agy-permissions` `/agy-model` `/agy-usage` `/agy-stats` `/agy` `/agy-link` | all optional; usage in [usage.en.md](usage.en.md) |
| Message Shortcut | callback ID `agy_open_web_ui` | open a thread's conversation in the Web UI (optional) |
| App Home | Home tab enabled | usage overview (optional) |

If you do not use an optional feature you can leave out its scope or command.

## Security note

Anyone who can post in a bridged channel can direct the corresponding `agy` project to act on
this machine (run commands, edit files, send email, etc., within what that project's
permissions allow). Use private channels with a membership you trust, not public ones.
