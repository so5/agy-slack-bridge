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
- While `agy` is working, the thread shows Slack's native "is thinking..."
  status indicator (`assistant.threads.setStatus`) instead of going quiet —
  useful since a real task can run for many minutes. No extra Slack scope
  needed beyond `chat:write`. The status is refreshed periodically (Slack
  clears it after ~2 minutes of silence on its own) and is cleared
  automatically once the reply is posted.
- Runs entirely over Slack's **Socket Mode** (an outbound websocket from your
  machine to Slack) — no inbound port, reverse proxy, or public URL needed.
- When agy's response mentions a `file://` link to something it generated
  (a chart, a report, ...), that link is dead in Slack - nothing can open a
  local filesystem path. So the bridge also uploads the file itself
  (`files_upload_v2`, needs the `files:write` scope) as a reply in the same
  thread, whenever it's a recognized type (image/PDF/CSV/etc., see
  `UPLOADABLE_EXTS`) and lives under either that agy project's own
  workspace directory or that conversation's own
  `~/.gemini/antigravity-cli/brain/<id>/` artifact directory - deliberately
  not "any absolute path agy happens to mention," to avoid ever turning a
  response into a way to exfiltrate an arbitrary file elsewhere on the box.
  Capped at 20MB and 5 files per reply.
- When a reply comes back with a denied `run_command` call (see "Tool
  permissions" below), the message gets two buttons instead of just a text
  warning: **今回だけ許可して再実行** (grant that exact command, retry the same
  prompt, then revoke it again once the retry finishes) and **恒久的に許可して
  再実行** (grant it permanently, with a native Slack confirm prompt first).
  This makes a permission denial actionable straight from a phone, without
  SSHing in to edit `settings.json` by hand. There's also a `/agy-permissions`
  slash command (`list`, `add <command>`, `remove <command>`) for managing
  the allow-list ahead of time or cleaning up stray entries — see "Managing
  permissions from Slack" below.

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
   - `chat:write.customize` — optional, only needed if you set
     `display_name`/`icon_emoji` per channel in `config.yaml` (see below) to
     make the bot post under a different name/avatar per project instead of
     one fixed bot identity everywhere.
4. **Event Subscriptions**: enable it, and subscribe to bot events:
   - `message.channels` (public channels) and/or `message.groups` (private
     channels), matching the scopes above.
5. **Interactivity & Shortcuts**: enable it. With Socket Mode already on,
   button clicks are delivered over the same websocket — no Request URL
   needed. This is required for the permission-grant/revoke buttons.
6. **Slash Commands** (optional, only for `/agy-permissions` — see "Managing
   permissions from Slack" below): create a command named `/agy-permissions`
   (any description/usage hint you like). Same as above, no Request URL
   needed with Socket Mode on. Add the `commands` Bot Token Scope.
   - `files:write` — optional, only needed for the auto-upload-generated-
     files feature below.
7. Install the app to your workspace. Save the Bot User OAuth Token as
   `SLACK_BOT_TOKEN` (`xoxb-...`).
8. Invite the bot to each channel you want to bridge (`/invite @your-bot`).
9. Get each channel's ID from Slack ("View channel details" → bottom of the
   panel).

**Security note**: anyone who can post in a bridged channel can direct the
corresponding `agy` project to act on this machine (run commands, edit files,
send email, etc. — whatever that project's own permissions allow). Use
private channels with a membership you trust, not open/public ones.

**Tool permissions**: there's no human present in a headless `agy -p` call to
click "approve" on a tool-permission prompt, so this bridge runs with
`--mode accept-edits` rather than `--dangerously-skip-permissions`. In
testing: `accept-edits` auto-approves file edits and safe read-only shell
commands (e.g. `ls`), but a command agy considers destructive (e.g. `rm`) is
still denied unless it matches an entry under `permissions.allow` in
`~/.gemini/antigravity-cli/settings.json` (a **global** file, shared across
every `agy` project on the machine).

The match is **not** literal full-string equality, and it's **not** a bare
binary-name allow either — confirmed by testing, it's a *token-complete
prefix* match: `command(git diff)` matches `git diff --stat`, `git diff
HEAD~1`, any continuation, because each already-typed token (`git`, `diff`)
is complete and followed by a token boundary. It does **not** match
`git diffX` (not a token boundary) or `git status` (different second
token). A trailing `*` in an entry does **not** reliably help - tested and
found inconsistent, so don't use it. Grow the allow-list narrowly - a whole
subcommand prefix like `command(aws lambda)` or `command(git diff)` is fine
and typically the useful grain, but never a bare `command(rm)` or
`command(git)`, which would defeat the point. When agy silently skips an
action this way, the run still comes back `status: SUCCESS` with an
empty-looking response and a `denied_actions` field; this bridge detects that
and appends a `:warning:` note naming the denied action to the Slack reply,
instead of leaving you looking at a blank-seeming answer.

**Compound commands (`&&`, etc.)**: confirmed by testing, agy splits a
compound command line on shell operators like `&&` and checks **each
sub-command independently** against `permissions.allow` - it's not a literal
whole-string match, and it's not a blanket "any `&&` is denied" rule either.
`sleep 10 && aws logs ...` is denied only because `sleep 10 ...` on its own
isn't in the allow-list (even though `command(aws logs)` matches the second
half) - allow-listing `command(sleep)` too would make the whole line pass.
Conversely, chaining a disallowed action onto an allowed one doesn't sneak it
through: with only `command(echo hi)` allowed, `echo hi && touch
/tmp/whatever` is still denied outright (and nothing runs) because `touch
...` doesn't match anything on its own. So there's no `&&`-based bypass in
either direction - each piece of a compound command needs its own matching
allow entry, same as if it were run alone.

**Note on `grep`/`sleep`/`ls` seeming inconsistent**: there's no built-in
"safe commands are free" allowance for arbitrary shell invocations. A small
set of dedicated tools (directory listing, file viewing/writing, etc.) skip
the permission gate entirely because they aren't `run_command` calls at all -
that's why simple file/directory operations tend to just work. But the
moment agy chooses to run something as an actual shell command via
`run_command`, it needs a matching `permissions.allow` entry regardless of
how harmless that command is (confirmed: even a bare `grep` or `ls` invoked
this way is denied with zero entries configured, identical to `rm`). This
isn't something narrowing the allow-list can make worse, and it isn't new
behavior triggered by adding entries - headless `--mode accept-edits` has
always required an explicit match for every `run_command` call, full stop.

## Managing permissions from Slack

Two ways to act on `permissions.allow` without leaving Slack:

- **Buttons on a denial reply**: click 今回だけ許可して再実行 for a one-off, or
  恒久的に許可して再実行 (confirm prompt first) to keep it. Either way the bridge
  re-runs the *exact same prompt* against the *exact same conversation* right
  after granting, so you see the real result instead of just "permission
  added, try again yourself." Not just `run_command` - any denial whose own
  message spells out the exact grantable entry gets a button too (confirmed
  for `read_url_content`, e.g. `read_url(support.yayoi-kk.co.jp)` - note
  that one's granted per-domain, not per-URL, since that's the granularity
  agy itself uses).
  - If a single turn hits **two or more distinct denials at once**, each
    gets its own button pair *and* one more pair appears offering to grant
    all of them together in one click - clicking the individual ones one at
    a time would only grant one and immediately retry, likely re-hitting the
    others.
  - A one-time grant is **not** torn down the instant its own retry
    finishes - see "Temporary grant lifetime" below.
- **`/agy-permissions` slash command** (only in a bridged channel):
  - `/agy-permissions list` — every current entry as plain numbered text
    (no per-entry buttons - simpler, and doesn't depend on Slack's
    per-block length/count limits the way an earlier version did).
  - `/agy-permissions add <command>` — add it permanently. A bare command
    (no `kind(...)` wrapper typed) is wrapped as `command(<that>)`, same as
    before; typing a full entry like `read_url(example.com)` is used as-is.
  - `/agy-permissions remove <command>` — same rules, removes it. Also
    accepts one or more of `list`'s numbers instead (space/comma-
    separated, e.g. `/agy-permissions remove 1 3 5`), to delete several
    at once without retyping long command strings.

Both paths write straight to `~/.gemini/antigravity-cli/settings.json` (or
wherever `AGY_SETTINGS_PATH` points, see below), preserving everything else
already in the file. Every add/remove is logged.

**Temporary grant lifetime**: a one-time grant used to be removed the
instant its own retry finished, which turned out to be actively wrong for a
task needing *several* different grants in sequence - by the time the
second one got granted, the first was already gone, so a later retry that
needed both would just deny the first one again. Instead, a `grant_once`
entry is now tied to the conversation: it's released (and only *actually*
removed from `permissions.allow` if no other conversation on the machine is
also relying on the exact same entry, since that file is shared) the next
time that conversation has a turn with **no** fresh denial at all - the
natural "this task is done for now" signal. As a backstop for a conversation
that's abandoned mid-task and never comes back clean, a background sweep
force-releases anything older than `AGY_TEMP_GRANT_MAX_AGE_SEC` (default
3600s), checked every `AGY_TEMP_GRANT_SWEEP_INTERVAL_SEC` (default 300s). If
the sweep fires before you're actually done, you'll just see a fresh denial
again - click the button again, no harm done.

**Caveat, confirmed by testing**: `denied_actions`/`duration_seconds`/`usage`
in agy's result are cumulative for the whole conversation, not scoped to one
turn - so a plain-text "一部のツール実行が権限不足で拒否されました" warning with
no buttons can mean the conversation has an *old*, unresolved denial in its
history rather than something that just happened (agy can silently choose
not to retry a denied command at all, rather than trying it again and
failing). Buttons only ever appear when agy's stream shows a *fresh*
denial this turn - only that case has an exact command line to grant. Also,
after clicking a button, the retry prompt sent back to agy explicitly names
the just-granted command and tells it to run it now, rather than resending
your original message verbatim - agy doesn't reliably retry on its own even
once the permission is in place.

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
`agy` isn't on `PATH`, `AGY_TIMEOUT_SEC` if your conversations legitimately
run longer than 20 minutes, or `AGY_SETTINGS_PATH` if agy's settings file
isn't at the default `~/.gemini/antigravity-cli/settings.json`).

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
