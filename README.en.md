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
- It runs over Slack's **Socket Mode**: no public URL or inbound port.
- It is a thin wrapper around `agy -p`, not a reimplementation of what agy does.

## ⚠ Security assumptions

Anyone who can post in a bridged channel can make the corresponding agy project work **on this
machine** (run commands, edit files, ... within the project's permissions). Use **private channels**
whose members you trust.

The agent runs as the same OS user as the bridge, so the permission machinery is not a hard barrier.
It makes mistakes and casual probing fail loudly (deny rules, safety rules, script-tampering
detection, ...). See "Keeping the agent inside its lane" in [docs/usage.en.md](docs/usage.en.md) and
[docs/permissions.en.md](docs/permissions.en.md).

## Quick start

You need: Linux, Python 3.10+, `git`, a **logged-in `agy`**, and a Slack workspace. Detailed steps
and what to check when something goes wrong are in [docs/install.en.md](docs/install.en.md).

**1. Create the Slack app** (about 5 minutes) — [docs/slack-app.en.md](docs/slack-app.en.md)

Paste the bundled [`slack-app-manifest.yaml`](slack-app-manifest.yaml) into Slack's "From an app
manifest", create an App-Level Token (`connections:write`) and install the app. Note the two tokens
(`xapp-…`, `xoxb-…`) and the ID of the channel you invited the bot to.

**2. Install**

```sh
git clone https://github.com/so5/agy-slack-bridge.git
cd agy-slack-bridge
python3 tools/install.py            # add --with-gdrive if you want the Google Drive CLI
```

**3. Set up a project**

At the end of `install.py` it asks whether to continue with the project setup; answer Y (you can also
run `<prefix>/.venv/bin/python <prefix>/tools/setup_agy_project.py` later). The dialogue registers an
agy project (or picks an existing one), maps a Slack channel and takes the tokens.

**4. Start**

```sh
mkdir -p ~/.config/systemd/user
cp ~/.local/share/agy-slack-bridge/systemd/agy-slack-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agy-slack-bridge.service
```

**5. Check**

Send `@agy hello` in the channel; a reply in the thread means it works. If you see "permission denied",
press the permission button (expect many at first).

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
| [tools/gdrive-profile.en.md](tools/gdrive-profile.en.md) | the Drive CLI profile format (credentials, OAuth notes) |

Japanese editions: [README.md](README.md), `docs/*.md`, `tools/*.md`.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## License

MIT — see [LICENSE](LICENSE).
