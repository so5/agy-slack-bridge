# tools/ — Tools You Install and Use

Safety rules, hooks, and a Drive CLI shared by agy projects run through the Slack bridge,
plus the scripts that set them up for a project. **Do not use them directly from this
repository's checkout; install them to a fixed location with `install.py` and use them from
there** (reasons below).

| File | Contents |
|---|---|
| `install.py` | Installs the bridge itself, `tools/`, and a venv into the install location. Also creates a template for the config directory |
| `setup_agy_project.py` | Configures an agy project (interactive, idempotent). Run it from the install location |
| `agy_settings.py` | Reads and writes agy's machine-wide `settings.json` (deny / trustedWorkspaces only). Shared by install and setup |
| `bridge_config.py` | Component that rewrites `config.yaml` / `env` while preserving comments |
| `agent-safety-rules.md` | Behavioral rules for agents (do not inspect the bridge internals, do not work around a denial, etc.) |
| `hooks.json`, `hooks/script_integrity.py` | PreToolUse hook for `run_command`. Checks that a permanently allowed script has not been quietly modified after approval |
| `gdrive_cli.py` | A CLI for Google Drive with a fixed, limited set of commands. `--profile` switches credentials and restrictions. [`gdrive-profile.en.md`](gdrive-profile.en.md) |

## Why install

If the components that support the permission model (the Drive CLI, hooks, policy, and the
bridge itself) are in a place the agent can write to (inside the project, or in a git
checkout), the agent can modify them. So the following approach is used.

- `install.py` makes the install location (default `~/.local/share/agy-slack-bridge`) and the
  config directory (default `~/.config/agy-slack-bridge`) targets of agy's
  `permissions.deny` (read and write).
- The Drive CLI and hook are not copied into the project; the ones in the install location are
  used directly by absolute path. Since there is no script inside the project, no per-project
  seal is needed.
- Policy (allowed folders, upload restrictions, etc.) lives in profiles in the config directory.
- The seals for permanent permissions (written by the bridge) and the hook's execution history
  are also kept not inside the project but in `integrity/<project key>/` in the config
  directory.

The agent runs as the same OS user, so a user-owned install location is protected only by
deny rules. It is confirmed on a real agy that `accept-edits` does not deny writes outside the
workspace by default, so this `deny` is effectively the only barrier. For the strongest
setup, install as root, e.g. with `--prefix /opt/agy-slack-bridge`.

## Usage

```sh
# 1. Install (from the checkout, as a normal user)
python3 tools/install.py [--prefix DIR] [--config-dir DIR] [--with-gdrive]

#    To install under /opt as root
sudo python3 tools/install.py --prefix /opt/agy-slack-bridge [--with-gdrive]
python3 /opt/agy-slack-bridge/tools/install.py --prefix /opt/agy-slack-bridge --config-only
#    (--config-only does not touch the code; as a normal user it configures only the config directory and settings.json)

# 2. Project setup (run by a human from a terminal; uses the copy in the install location)
<prefix>/.venv/bin/python <prefix>/tools/setup_agy_project.py [<project path>] \
    [--prefix <install location>] [--bridge-config-dir <config directory>]
```

At the end, `install.py` asks whether to run `setup_agy_project.py` next (Y runs it
immediately; N prints the command to run later). `--no-setup` skips the prompt.
Re-running `install.py` replaces the code (an upgrade). Files you placed in the install
location yourself are deleted. Existing files in the config directory are not overwritten.
Installed files are 0444 (this prevents accidents; it is not a defense). The checkout itself
cannot be used as `--prefix`.

`setup_agy_project.py` is an interactive menu. First it shows the project list (plus new project, Slack tokens and quit);
choosing a project shows that project's menu (set up everything, rules and hook, Drive CLI, Slack channel, state).
With `--all` it runs everything in order without the menu. Path prompts complete with Tab. "Set up everything" is safe to run any number of times, and current
values appear as defaults, so changing or repairing settings uses the same command (Enter:
keep as is, `-`: clear the value). It performs the following in order.

0. Choosing the target: if the path is omitted, either pick a number from the list of
   projects already registered with agy, or enter the path of a new directory (an existing
   project uses its directory. A project without a directory only gets the Drive permissions
   and the channel mapping).
1. Prepare the directory (create it if missing). Git management is required, so run `git init`
   if it is not a git repository.
2. Register with agy (run `agy --new-project -p hi` in that directory. The project name
   becomes the directory name. This calls the model once).
3. Add the target to `trustedWorkspaces` in `~/.gemini/antigravity-cli/settings.json`, and add
   a write `deny` for the safety rules link (`.agents/rules/agent-safety-rules.md`) (other
   rules in `.agents/rules/` and `AGENTS.md` remain editable). `allow` is not written here.
   (The machine-wide `deny` that protects the install location and config directory, and the
   hook registration in the global `hooks.json`, are done by `install.py`.)
   Also create the hook's state directory (`<config directory>/integrity/…`).
   Remove the per-project `.agents/hooks.json` hook placed by an earlier setup (it would run
   twice alongside the global hook).
4. Place a symbolic link to the safety rules in the target's `.agents/rules/` (`AGENTS.md` is
   not touched; the agent may edit it freely). Remove the links directly under `.agents/` and
   the `AGENTS.md` include lines created by earlier setups. Append `scratch/` to `.gitignore`
   (only if nothing about scratch is written there).
5. Ask whether to use the Drive CLI (only for "set up everything"); if so, use the profile name derived from the agy project ID (never colliding; if other projects have profiles, choose between a new profile and sharing that project's settings and permissions) and interactively create and configure the profile
   (`<config directory>/gdrive/NAME.json`, [`gdrive-profile.en.md`](gdrive-profile.en.md)).
   - If it does not exist, ask for the authentication type (`service_account` (recommended: just share the Drive folders with its email address) / `oauth`) and
     the command that retrieves the credentials (and the scopes, if needed) and create it.
     The credentials and scopes of an existing profile are not touched.
   - For the policy (enabled commands, allowed folders (folder URLs or IDs), upload deny
     patterns (keep as is / add to defaults / replace)), show the current value (the default
     for a new profile) and ask whether to change it. Only items that differ from the defaults
     are written to the file. Changes that widen permissions (adding a command, lifting the
     folder restriction, removing a deny pattern) are shown as a diff and confirmed
     separately (default: no).
   - Add the enabled read-type subcommands (`list`/`search`/`meta`/`read`/`download`) only to
     that project's own `permissionGrants.allow`. Previous grants for commands that were
     disabled are removed. Write-type commands are not added to the grants; they require
     approval every time.
   - The profile is written with 0600 and its directory with 0700 (a corrupted profile is not
     overwritten).
   - Choosing Drive for an already configured project opens an update menu: change the policy /
     re-enter the credentials (the policy is kept; a stored key that is no longer used is deleted) /
     switch to another profile (back to a dedicated one, or share another project's) / remove this
     project's Drive settings. A profile that no project uses after removing or switching can be deleted
     after confirmation (default: keep).
6. Slack channel mapping (`channels` in `config.yaml`; for "set up everything" it first asks whether to map): channel ID, display name, icon,
   model, effort, config files to watch, and permission button statistics. Optional items can be skipped. Only the relevant entry is rewritten; other comments and items are preserved
   (the original file is kept as `config.yaml.bak`).
7. The Slack tokens in `env`, if not yet set.

After changing settings, a bridge restart may be needed.

### Machine-specific files (optional)

If the following files exist in `<config directory>/shared/`, they are linked into the
project's `.agents/rules/`. They are specific to each machine and operator, so they are not
included in this repository.

- `local-tools.md` — a list of the tools available on this machine
- `gdrive-cli.md` — how to use the Drive CLI, written for agents

Rule files in `.agents/rules/` need frontmatter at the top (`---` / `trigger: always_on` /
`---`). Without it, the file is silently ignored with no error (confirmed on a real agy).
`setup_agy_project.py` warns if it is missing.
Rules in `.agents/rules/` (both regular files and symbolic links) and the per-file
`write_file` deny have been confirmed to work on a real agy. Deletion or moving via the shell
(`rm` / `mv`, etc.) is outside the scope of deny; it is guarded by the normal approval flow
and by safety rule 9.

## About the scope of permissions

The bridge itself does not create any permission that applies across all projects (see
`docs/permissions.en.md`). This script follows the same policy: what it writes to
`settings.json` is only `deny` and `trustedWorkspaces` (specified by absolute path, and safe
to share regardless of project). `allow` is written only to the target project's own
`permissionGrants`.

`--profile` becomes part of the permission entry
(`command(<prefix>/.venv/bin/python -I <prefix>/tools/gdrive_cli.py --profile <name> list)`).
If each project uses a different profile name, one project's Drive permission does not become
the right to use another project's credentials. What the name points to (storage location,
scopes, allowed folders, etc.) is inside the profile, so changing it later does not change
the approved command string and no re-approval is needed. For how to write a profile, see
[`gdrive-profile.en.md`](gdrive-profile.en.md).

### Prerequisites for using the Drive CLI

- Install with `--with-gdrive` to put the Google packages into the install location's venv.
- The command that retrieves credentials (`aws`, `op`, etc.) must be runnable by the user
  running the bridge.
- For a service account, share the target folders with its email address.
- There are no commands for deleting, moving, or changing sharing settings.

## About the hook

`install.py` registers a single hook in agy's **global `~/.gemini/config/hooks.json`** (its
`command` is the absolute path of the hook in the install location with `--config-dir`
appended; other hooks in the same file are preserved). The per-project `.agents/hooks.json`
is not used. `~/.gemini/config/` is a deny target, so the agent cannot modify it.

The hook protects scripts **inside the project** that were permanently allowed with
`/agy-permissions add` (it denies them if they differ from the sealed hash, and requires a git
commit). Both the seal (`permanent.json`) and the execution history of other scripts
(`hashes.json`) are kept in `integrity/<project key>/` in the config directory. The key is a
hash of the project root's real path, and `setup_agy_project.py` makes only the projects for
which it created this directory targets of the hook (in projects where it has not been
created, the hook does nothing. Because it is global, it also fires for the user's own
interactive agy use, but it just passes through without doing anything). Tools in the install
location are outside the project, so they are not subject to sealing.

If `script-integrity` is left in a per-project `.agents/hooks.json`, the hook runs twice, the
execution history is updated first, and detection stops working. `setup_agy_project.py`
removes it if it finds one placed by an earlier setup.

## When adding shared files

Everything placed in `tools/` is synced wholesale by `install.py`. For anything you want to
link into a project's `.agents/` and have `AGENTS.md` load, add it to `AGENTS_MD_INCLUDES` in
`setup_agy_project.py` and to the linking logic.
