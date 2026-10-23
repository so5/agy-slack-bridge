# Installation and operation

## Prerequisites

- A Linux machine (Linux + systemd user services is what has been tested).
- Python 3.10+ (with `venv`) and `git`.
- `agy` (Antigravity CLI) installed and **logged in as the user that runs the bridge**.
- A Slack workspace and the two tokens of the Slack app you create ([slack-app.en.md](slack-app.en.md)).
- Optional: `playwright` and Chromium to render Mermaid diagrams ("Mermaid diagrams" in [usage.en.md](usage.en.md)).

## 1. Install

```sh
git clone https://github.com/so5/agy-slack-bridge.git
cd agy-slack-bridge
python3 tools/install.py [--prefix DIR] [--config-dir DIR] [--with-gdrive]
```

This puts the bridge, `tools/` (safety rules, hook, Drive CLI, setup script) and a venv in a
**fixed install location**. The bridge does not run out of your checkout: the parts the
permission model relies on must live where the agent cannot write (see
[tools/README.en.md](../tools/README.en.md) for the reasoning).

| Option | Meaning |
|---|---|
| `--prefix DIR` | install location (default `~/.local/share/agy-slack-bridge`) |
| `--config-dir DIR` | config directory (default `~/.config/agy-slack-bridge`) |
| `--with-gdrive` | also install the Google client libraries for the Drive CLI |
| `--no-setup` | skip the final "run the project setup now?" prompt |
| `--no-settings` | do not touch agy's `settings.json` / `hooks.json` |
| `--config-only` | leave the code alone; only set up the config dir and agy's settings |
| `--no-venv`, `--no-config` | do not create the venv / the config dir |

What it does:

- Installs the code read-only (0444) and creates `<prefix>/.venv`.
- Creates sample `config.yaml` and `env` in the config dir (0700). **Existing files are never overwritten.**
- Adds `deny` rules to agy's `~/.gemini/antigravity-cli/settings.json` (no read or write of the
  install dir and config dir for the agent) and registers the script-integrity hook in
  `~/.gemini/config/hooks.json`. It never writes an `allow`.
- Generates the systemd unit (`<prefix>/systemd/agy-slack-bridge.service`) with the absolute
  path of `agy` as `AGY_BIN` (a warning if it cannot find it).
- Finally asks whether to run the project setup (next section) right away (Y runs it; N prints
  the command to run later).

### Installing as root under `/opt` (strongest)

The agent runs as your OS user, so a user-owned install dir is protected only by the deny rules.
A root-owned one is protected more strongly.

```sh
sudo python3 tools/install.py --prefix /opt/agy-slack-bridge --with-gdrive
python3 /opt/agy-slack-bridge/tools/install.py --prefix /opt/agy-slack-bridge --config-only
```

Run the second line as your normal user (so the config goes to your home, not root's).

## 2. Set up a project

```sh
<prefix>/.venv/bin/python <prefix>/tools/setup_agy_project.py [<path-to-project>]
```

Interactive; a human runs it from a terminal (the agent must not run it). Answer Y at the end of
`install.py` to start it directly. It:

1. Picks the target (choose from the projects agy already knows, or enter a new directory path).
   A directory that is not a git repository gets `git init` (git is mandatory).
2. Registers it with agy (`agy --new-project`, one model call) if needed.
3. Sets up the safety rules (`.agents/rules/agent-safety-rules.md`), trustedWorkspaces and the hook registration.
4. For the Google Drive CLI: creates the profile and sets its policy (enabled commands, allowed
   folders, upload deny patterns).
5. Maps a Slack channel (channel ID, display name, icon, model, effort, ...; optional items can be skipped).
6. Asks for the Slack tokens if they are not set yet.

It is idempotent: the current values are the defaults (Enter keeps, `-` clears), so changing or
repairing a setup is the same command. Details in [tools/README.en.md](../tools/README.en.md).

## 3. Start

```sh
mkdir -p ~/.config/systemd/user
cp <prefix>/systemd/agy-slack-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agy-slack-bridge.service
loginctl enable-linger "$USER"   # to keep it running after logout / start at boot
```

To just try it in a terminal:

```sh
set -a; . ~/.config/agy-slack-bridge/env; set +a
AGY_BRIDGE_CONFIG=~/.config/agy-slack-bridge/config.yaml \
AGY_BRIDGE_STATE_DB=~/.config/agy-slack-bridge/state.sqlite3 \
  <prefix>/.venv/bin/python <prefix>/bridge.py
```

Under systemd:

- The `PATH` of `systemd --user` is minimal and does not include `~/.local/bin`. Pass `agy` as an
  absolute `AGY_BIN` (the generated unit does; for a hand-written unit put it in the env file). If
  `agy` is not runnable at startup the bridge exits with a clear message.
- Logs go to the journal, not a file: `journalctl --user -u agy-slack-bridge.service -f`
- The unit's `AGY_BRIDGE_CONFIG` and the hook's `--config-dir` must be the same directory
  (`install.py` sets both; do not split them by hand).
- If a credential command (`aws`, `op`, ...) lives outside `PATH`, write its absolute path or add
  `Environment=PATH=...` to the unit.

## 4. Check that it works

1. In a channel the bot was invited to, **mention** it: `@agy hello`. A new top-level message
   needs the mention.
2. A reply in a thread means success. Follow-up replies inside that thread need no mention.
3. If you see a "permission denied" notice with buttons, grant it as described under "Managing
   permissions from Slack" in [usage.en.md](usage.en.md). Expect many buttons at first.

## 5. Day-to-day

- After changing `config.yaml` or `env`, restart the bridge:
  `systemctl --user restart agy-slack-bridge.service`
- Adding a project: run `setup_agy_project.py` again and map the new channel.
- Permissions, model switching and usage checks are done from Slack ([usage.en.md](usage.en.md)).

## 6. Upgrade

```sh
cd agy-slack-bridge && git pull
python3 tools/install.py --no-setup [same options]      # the two lines above for a root install
systemctl --user restart agy-slack-bridge.service
```

Only the code is replaced; your config (`config.yaml`, `env`, state DB) is untouched.

## 7. Uninstall

1. `systemctl --user disable --now agy-slack-bridge.service` and delete the unit file.
2. Delete the install dir (`<prefix>`).
3. Delete the config dir (it holds the tokens and the state DB; keep it if you want them).
4. Remove what the bridge added to agy's settings: the install-dir / config-dir `deny` entries and
   each project's `trustedWorkspaces` in `settings.json`, and `script-integrity` in `~/.gemini/config/hooks.json`.
5. Each project's `.agents/rules/agent-safety-rules.md` link and any bridge-created grants in its `permissionGrants`.

## Troubleshooting

| Symptom | What to check |
|---|---|
| The bot does not answer | Is the bot invited to the channel? Did the new message **mention** it? Is the channel ID in `channels` of `config.yaml`? Is the service running (`systemctl --user status …`)? Errors in the journal. The tokens in `env`, and the app's event subscriptions / Socket Mode |
| Exits right after start (`agy not found`) | Set `AGY_BIN` to the absolute path of `agy`. Under systemd's minimal `PATH` a bare `agy` is not found |
| `agy invocation failed` in Slack | Look at the journal. When `agy` exits non-zero, the tail of its stdout is logged. Is `agy` logged in (`agy -p hi` as the bridge user)? |
| `RESOURCE_EXHAUSTED` | Model quota used up. Switch to another model family with `/agy-model` |
| "Permission denied" shows up | Normal. Grant it with the button or `/agy-permissions add` |
| The same instruction is rejected once (with a script diff) | Protection for a script that changed since its last run. Check the diff, then send the same instruction again |
| A config change has no effect | Restart the bridge |
| The safety rules do not seem to apply | Is the `.agents/rules/agent-safety-rules.md` link there (re-run `setup_agy_project.py`)? A rule file needs frontmatter (`trigger: always_on`); without it it is silently ignored |
| `setup_agy_project.py` says it is not installed | Run `tools/install.py` first |
| `setup_agy_project.py` says it is interactive | Run it from a terminal (TTY) |
