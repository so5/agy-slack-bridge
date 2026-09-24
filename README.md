# agy-slack-bridge

Bridge one or more Slack channels to [Antigravity CLI](https://antigravity.google.com)
(`agy`) projects, so you can talk to your agy agents from Slack.

- Each configured Slack channel maps 1:1 to one `agy` project.
- A new top-level message in the channel starts a fresh `agy` conversation;
  the reply is posted as a thread on that message.
- Replying inside that Slack thread continues the same `agy` conversation
  (looked up by the thread's root timestamp). Replying in a thread the bridge
  has no record of just starts a new conversation and adopts that thread from
  then on.
- A new top-level message can also graft onto an *existing* conversation
  instead of starting a fresh one — handy for continuing, from Slack, a
  conversation you started in the [Antigravity web UI](https://antigravity.google.com).
  Start the message with `resume <conversation-id>`:

  ```
  resume db68d299-5a1c-475a-a1ed-09604f5537e4: 続きをお願い
  ```

  The resulting Slack thread is grafted onto that conversation ID, so every
  later reply in the thread continues it normally — no need to repeat
  `resume` after the first message.

  **Caveat**: this reliably continues a conversation that was itself created
  via `agy -p` (print mode) — e.g. one another `resume`'d Slack thread
  started. Pointing it at a conversation created interactively in the
  Antigravity web UI has, in testing, *not* actually reattached to that
  session: `agy` returns a brand-new `conversation_id`, and the original
  conversation's on-disk state
  (`~/.gemini/antigravity-cli/brain/<id>/`) is left untouched — it reads that
  conversation's history as context rather than resuming it. Print mode
  and the web UI's interactive sessions appear to be different session
  types under the hood; there's no known CLI flag that bridges the two, and
  the interactive CLI (`agy -i`) needs a real TTY, so this bridge can't
  probe it further via a scripted process. If you need to hand off a
  conversation from Slack back to the web UI (or vice versa) with true
  continuity, that isn't currently possible through this bridge, or as far
  as we could confirm, through `agy` itself.
- Markdown in `agy`'s response (bold, links, tables, headers, ...) is
  converted to Slack's own `mrkdwn` dialect before posting, since Slack
  doesn't render standard Markdown as-is (e.g. it has no table syntax, and
  uses `*bold*`/`_italic_` instead of `**bold**`/`*italic*`).
- Runs entirely over Slack's **Socket Mode** (an outbound websocket from your
  machine to Slack) — no inbound port, reverse proxy, or public URL needed.

This is intentionally a thin process wrapper around `agy -p ... --output-format
json`, not a reimplementation of anything agy does. It just routes Slack
messages in and `agy`'s JSON responses back out, and keeps a small local
mapping from Slack threads to `agy` conversation IDs.

## Requirements

- `agy` (Antigravity CLI) installed and authenticated on this machine, with
  the project(s) you want to bridge already created (`--project <id>`).
- Python 3.10+ with `venv`.
- A Slack workspace where you can create and install a custom app.

## Slack app setup

1. Create a new Slack app ([api.slack.com/apps](https://api.slack.com/apps)),
   "From scratch".
2. **Socket Mode**: enable it. This generates an app-level token — give it
   the `connections:write` scope. Save it as `SLACK_APP_TOKEN` (`xapp-...`).
3. **OAuth & Permissions**: add Bot Token Scopes:
   - `chat:write`
   - `channels:history` (for public channels) and/or `groups:history` (for
     private channels), depending on which kind of channel you're bridging.
4. **Event Subscriptions**: enable it, and subscribe to bot events:
   - `message.channels` (public channels) and/or `message.groups` (private
     channels), matching the scopes above.
5. Install the app to your workspace. Save the Bot User OAuth Token as
   `SLACK_BOT_TOKEN` (`xoxb-...`).
6. Invite the bot to each channel you want to bridge (`/invite @your-bot`).
7. Get each channel's ID from Slack ("View channel details" → bottom of the
   panel).

**Security note**: anyone who can post in a bridged channel can direct the
corresponding `agy` project to act on this machine (run commands, edit files,
send email, etc. — whatever that project's own permissions allow). Use
private channels with a membership you trust, not open/public ones.

## Configuration

Copy the two example files somewhere **outside** this repo checkout (they
hold secrets / your own IDs, and are gitignored on purpose so you don't
accidentally commit them from inside the repo either):

```sh
mkdir -p ~/.config/agy-slack-bridge
cp env.example ~/.config/agy-slack-bridge/env
cp config.example.yaml ~/.config/agy-slack-bridge/config.yaml
chmod 600 ~/.config/agy-slack-bridge/env
$EDITOR ~/.config/agy-slack-bridge/env           # fill in the two tokens
$EDITOR ~/.config/agy-slack-bridge/config.yaml   # fill in channel -> project mapping
```

See `config.example.yaml` for the mapping format and `env.example` for the
environment variables (tokens, plus optional overrides like `AGY_BIN` if
`agy` isn't on `PATH`, or `AGY_TIMEOUT_SEC` if your conversations legitimately
run longer than 20 minutes).

## Running

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
AGY_BRIDGE_CONFIG=~/.config/agy-slack-bridge/config.yaml \
AGY_BRIDGE_STATE_DB=~/.config/agy-slack-bridge/state.sqlite3 \
  env $(cat ~/.config/agy-slack-bridge/env | xargs) .venv/bin/python bridge.py
```

Or install it as a long-running `systemd --user` service — see
`systemd/agy-slack-bridge.service.example` for a ready-to-copy unit and the
setup steps in its header comment.

## How a message becomes an `agy` call

Roughly:

```sh
agy -p "<the Slack message text>" \
    --project <project-id-for-this-channel> \
    --output-format json \
    --print-timeout 0 \
    [--conversation <id-looked-up-from-the-Slack-thread>]
```

The bridge parses the JSON `{"conversation_id": ..., "response": ...}`,
remembers `conversation_id` against the Slack thread in a local sqlite file,
and posts `response` back as a threaded reply.

## License

MIT — see `LICENSE`.
