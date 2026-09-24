# agy-slack-bridge

Bridge one or more Slack channels to [Antigravity CLI](https://antigravity.google.com)
(`agy`) projects, so you can talk to your agy agents from Slack.

- Each configured Slack channel maps 1:1 to one `agy` project.
- A new top-level message starts a fresh `agy` conversation only if it
  **@-mentions the bot** — so other traffic in the channel (a batch job
  posting its results via an Incoming Webhook, ordinary chatter) isn't
  treated as a prompt. The mention is stripped before the rest of the text
  is sent to `agy`. The reply is posted as a thread on that message.
- Replying inside that Slack thread continues the same `agy` conversation
  (looked up by the thread's root timestamp) — **no mention needed for
  thread replies**, only for starting a new one. Replying in a thread the
  bridge has no record of just starts a new conversation and adopts that
  thread from then on.
- A new top-level message can also graft onto an *existing* conversation
  instead of starting a fresh one — handy for continuing, from Slack, a
  conversation you started in the [Antigravity web UI](https://antigravity.google.com).
  Start the message with an @-mention (required for any new top-level
  message, see above) followed by `resume <conversation-id>`:

  ```
  @your-bot resume db68d299-5a1c-475a-a1ed-09604f5537e4: 続きをお願い
  ```

  The resulting Slack thread is grafted onto that conversation ID, so every
  later reply in the thread continues it normally — no need to repeat
  `resume` after the first message.

  **Caveat, confirmed by testing**: whether this is visible from the
  [Antigravity web UI](https://antigravity.google.com) depends on where the
  conversation *started*:

  - A conversation that started via `agy -p` (including one this bridge
    created) stays fully in sync both ways. The web UI can open it directly
    via a URL of the form
    `.../r/<your-instance-id>/?p=c%2F<conversation-id>%3Fsection%3D<project-id>`
    (grab the exact `<your-instance-id>` from any conversation URL the web
    UI already gives you) — it appears in the sidebar once opened that way,
    even though it never showed up there on its own. From then on, messages
    sent from the web UI are visible to a later `agy -p --conversation`
    call (confirmed: asked it "what did I just send from the web UI?" and
    it correctly quoted the message and timestamp), and vice versa.
  - A conversation that started *interactively in the web UI* cannot be
    genuinely extended from `agy -p`. `agy` does echo back the same
    `conversation_id` and does append to the conversation's transcript log
    (`~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/`), so a
    `resume` onto one of these reads real history and gives a contextually
    correct answer — but nothing new shows up in the web UI even via the
    direct URL above. It's a read: agy can see and reason about that
    conversation's past content, but can't write to the part of it the web
    UI displays. (We don't know the exact mechanism the web UI renders
    from - it isn't simply the `.system_generated/steps/` trace, since
    plain conversational turns don't always get one of those either, in
    conversations of either origin.)

  Practically: if you want a conversation you can pick up from **either**
  Slack or the web UI, start it from Slack (or any other `agy -p` caller).
  A conversation started in the web UI can be *read* from Slack via
  `resume`, but its Slack-side continuation won't be visible back in the
  web UI.
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
