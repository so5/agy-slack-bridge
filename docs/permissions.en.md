# How Execution Permissions Work

For how to operate permissions from Slack, see "Managing permissions from Slack" in [usage.en.md](usage.en.md).

![How a command gets to run](images/permissions-flow.en.svg)

## 1. Permission layers

| Layer | Location | Contents | Scope |
|---|---|---|---|
| Project permissions | `permissionGrants.permissionGrants.allow` in `~/.gemini/config/projects/<id>.json` | Entries such as `command(...)` and `read_url(...)` | That project (= that Slack channel) only |
| Temporary grants | The bridge's state DB (`temp_grants`) | Same kind of entries | Valid only for the duration of that conversation. Removed automatically once turns succeed |
| Permanent-grant ledger (optional) | `integrity/<project key>/permanent.json` in the config directory | A sha256 per script (seal) | That project only |
| agy's own allow/deny lists | `permissions.allow` / `deny` and `trustedWorkspaces` in `~/.gemini/antigravity-cli/settings.json` | Same kind of entries, or absolute paths | All of agy (the bridge does **not** write here) |

**The bridge writes only to the project's `permissionGrants` (and, for scripts, the ledger `permanent.json` in the config directory).** Both temporary and permanent grants are written only to the file of the project that the channel is linked to. You are free to edit agy's own `settings.json` yourself (or through operations of the `agy` command itself), and it is fine to leave `trustedWorkspaces` and `deny` there. What this means is that the bridge has no feature that creates a grant that applies across all projects.

### Why permissions are per project

If a permission is written to `permissions.allow` in `settings.json`, it applies to all of agy's projects (= all Slack channels). For example, if you allow `aws logs filter-log-events` in channel A, the same command can be executed without a prompt from conversations in channel B. When we actually operated it this way, commands and MCP tools meant for one project turned out to be usable from other projects as well. For that reason, all permissions are now written to the `permissionGrants` of the relevant project.

Several projects may use the same script (such as `gdrive_cli.py`), but that is a state in which **each project allows that command separately**; they do not reference a single shared list. Adding a new project does not inherit any permissions from other projects (you start granting from scratch).

## 2. How matching works

- `command(git diff)` matches if the leading tokens match. `git diff --stat` and `git diff HEAD~1` both pass. `git diffX` and `git status` do not.
- Appending `*` at the end does not reliably work. Do not write it.
- For compound commands (`&&`, `;`), each subcommand is matched individually. `echo hi && touch x` is rejected as a whole unless `touch` is allowed.
- deny takes precedence over allow.
- If a single line mixes allowed and denied parts, the whole line is rejected.

## 3. Permission granularity (guidelines)

- Subcommand level is the basic unit. A broad entry such as `command(aws logs)` would also let deletion-type commands through. Write only the read operations individually, as in `command(aws logs get-log-events)`.
- Do not allow the following.
  - Interpreters themselves (`python3`, `node`, `bash`, etc.).
  - Execution of arbitrary packages via package runners (`npx`, `uvx`, etc.).
  - Task-runner wildcards (`npm run *`, `make *`, etc.).
  - Write-type commands such as `gh api`, `docker exec`, `sudo`, `rm`, and `git push`.
- Read-only scripts with fixed arguments (e.g. `scripts/foo.py`) may be allowed by prefix. However, if the script lets you pass the output destination as an argument, fix the output destination first.
- `read_url(domain)` works per domain. Since it is a read-only tool, it is relatively safe to allow in bulk the known domains used by that project (e.g. `arxiv.org` for a research project).

## 4. Temporary grants and permanent grants

- The approval button on a denied action (「許可して再実行」) is a temporary grant for that conversation; it writes immediately to the **project's** `permissionGrants.allow`, and is also recorded in the bridge's state DB (`temp_grants`, per conversation).
  - It is not removed after a single failure. It is removed when that conversation ends without errors (a clean turn), provided no other conversation in the same project still needs it.
  - It is also removed 1 hour (`AGY_TEMP_GRANT_MAX_AGE_SEC`) after it was granted (with the same check of whether anything else in the same project is using it).
  - Temporary grants for commands that run a script (automatic or by button) are not removed at a clean turn; only by this time limit (see [Automatic check before the permission button](#automatic-check-before-the-permission-button-ai)).
  - **Grant scope (`tools/grant_scopes.json`)**: agy matches an allow entry as a prefix of the command, so granting the full command text would also pin arguments nobody cares about, such as which file to upload. For the write commands of the Drive CLI, the grant therefore ends at the target: `upload --folder <folder ID> <file>`, `mkdir --parent <folder ID> <name>` and `update --file <file ID> <file>` ask "Allow 「このフォルダへのアップロード」 (upload to this folder) / 「このフォルダへのディレクトリ作成」 (create a directory in this folder) / 「このファイルの上書き」 (overwrite this file)?", and once allowed, the same operation on the same folder (file) is allowed for the conversation (at most 1 hour) whatever the file or name. The CLI still refuses sources outside the project, files matching `upload_deny`, and targets outside the profile's `allowed_folders` (separate from the grant scope). The rule format and matching are in `tools/grant_scope.py`; the rule file lives in the install directory, which the agent cannot write. A command that matches no rule, or contains shell syntax, keeps the full text as the grant. Not used for permanent grants (`/agy-permissions add`).
- A permanent grant is made with `/agy-permissions add`, not with a button. It leaves the entry written in the same **project's** `permissionGrants.allow`. For a script, a seal is also stored in the ledger `permanent.json` in the config directory.
- The number of times each grant is used is recorded in `permission_log`. You can view it with `/agy-permissions stats` (recording is toggled by `permission_stats` under `telemetry`).
- Stall detection (silent denial): a `run_command` for which agy finishes with "DONE, no output" can be misjudged as a denial. If it matches an already-allowed entry, it is not treated as a denial.

### Deleting scripts (`scratch_clean.py`)

When a script under `scratch/` (one the agent made) is temporarily allowed (by the button, or automatically after the check), a deletion grant is issued with it, also as a temporary grant for the same conversation. `rm` is not used: because of prefix matching, `rm <allowed path> <another path>` would also pass. The installed `tools/scratch_clean.py` is used instead.

- The grant is `command(<python> -I <install dir>/tools/scratch_clean.py --root <project path>)`. The project path is part of the grant, so another directory's `scratch/` is out of reach.
- `scratch_clean.py` does the checking. It can remove only things under `scratch/` (regular files, directories, sockets, anything; directories with their contents). `scratch/` itself cannot be removed.
- Each path is resolved except for its last name, and must then be under `scratch/`. If the last name is a symlink, the link itself is removed (the target is never removed, wherever it is). Links inside a removed directory are not followed either; only the links themselves go.
- All arguments are checked first; if any one fails, nothing is removed. Arguments starting with `-` are refused.
- The lifetime is that of the script's temporary grant (the conversation, at most one hour). It is never added to permanent grants.
- The agent is told to call it in this form by safety rule 8 and by the retry prompt (which gives the exact command line).

## 5. Permanently allowed scripts (seal)

![How scripts are checked](images/script-check.en.svg)

The ledger `<config directory>/integrity/<project key>/permanent.json` has the format `{ "<absolute path of script>": "<sha256>" }`. It is a separate file for each project, and it is placed not inside the project but in the config directory, which the agent cannot read or write (the key is a hash of the real path of the project root).

- **seal**: Writes the hash of the content at the time of approval. The bridge writes it automatically on `/agy-permissions add`. Sending the same `add` again rewrites the hash with the current content (re-seal).
- **Hook decision** (agy's global `PreToolUse` hook; registered by `tools/install.py` and active only for projects set up with `setup_agy_project.py`):
  - A permanently allowed script passes only if its hash matches and it is committed in git with no diff.
  - Even if it is committed, it is rejected when the hash differs. A commit does not count as approval.
- **Temporary scripts** (not permanently allowed): the first content seen is recorded in the ledger (`hashes.json` in the same location). If the content has changed later, the script is rejected once with a diff attached, and the ledger is updated. Sending the same instruction again lets it through. git is not required.
- If you gitignore a working directory (e.g. `scratch/`), scripts there cannot be permanently allowed (they cannot satisfy the git clean requirement). If you want to allow one permanently, move it to somewhere like `scripts/`, commit it, and then seal it.

### Order of operations

1. Edit the script.
2. Commit it (the hook requires a clean state).
3. Send `/agy-permissions add` again to re-seal.

### Automatic check before the permission button (AI)

![How the automatic check works](images/script-review-flow.en.svg)

When a command that would get a permission button runs a script, the bridge first starts separate agy conversations to check the script (stages 1–2 always, stage 3 only when clicked). A script that passes stage 1 is **granted automatically for this conversation only and re-run without a button** (Slack shows "granted automatically" and `permission_log` records `granted_auto`). Everything else **gets the button** (a person decides). A command refused with the Deny button is refused automatically for the rest of that conversation, with no check and no button (`/agy-permissions add` still allows it explicitly). The grant lasts for the conversation (at most 1 hour) and, unlike an ordinary temporary grant, is not released at the first clean turn; a repeat of the same command (including a retry the agent makes without asking) is allowed; the message says so. A script whose content changed (same command, different content) is stopped once by the hook. The bridge then runs the same check as for a first run (the diff is not shown): if stage 1 is clean it is granted and re-run automatically, otherwise the button is shown. A failed check, or a script over about 60 KB where only the beginning could be checked, also gets the button.

1. **Does it do anything harmful? (always)**: agy's file rules (`read_file(...)` / `write_file(...)` in `deny` and `allow` of `settings.json`, and in the project's allow list) do not bind the file operations a script performs itself. So the script's file operations are checked against them: they must stay **inside the project directory or in a place listed in `allow`**. A place matching `deny` is not allowed even inside the project (deny wins). Any file operation (read, write, create, delete, move) outside that range, or whose target cannot be determined, counts as harm. Besides file operations, external calls other than `gdrive_cli.py`, running other programs, and `eval` or obfuscation also count. If there is none, even a script that strays from the request cannot do damage, so it ends here (granted temporarily and re-run). Automatic grants count toward the consecutive-denial limit (3 per 10 minutes); beyond it, both buttons and automatic grants stop.
2. **Does it match the request? (only if stage 1 found something)**: compares the script with the user's request and decides whether it does only what the request needs. If so, the operations found in stage 1 are listed, then the button.
3. **Show summary and content (only if stage 2 says no, and only when clicked)**: stage 3 takes about 20 s, so it is not run unasked. A script that stage 2 flags gets a "要約を見る" (show summary) button next to the permission button. Clicking it posts a summary of up to 3 lines and the script's content (first 2,500 characters) in the thread (the permission buttons stay; each button works once).

Each stage is a separate agy call, run without `--project`, in an empty temporary directory, in plan mode, so no project permissions apply. The script is passed as data inside a random delimiter, with instructions not to follow anything written in it. The per-conversation files agy leaves after each call (`brain`, `conversations`, `annotations`, `presence` under `~/.gemini/antigravity-cli/`) are deleted after every call.

**Condition for automatic grants**: if the command contains a redirection (`>`), a pipe, `$(...)`, `;` or `&&`, it is not granted automatically even when stage 1 is clean; the button is shown. agy matches allow entries as a prefix, so allowing `command(python3 a.py)` also lets `python3 a.py > /anywhere` through (this keeps what was checked equal to what is granted).

**Redirect targets (hook)**: Because of that, the hook (`script_integrity.py`) denies, whether or not the command is allowed, a command that runs a script when its output redirection (`>` `>>` `2>` `&>`) points outside the project (`/dev/null` and `2>&1` are fine). Appending `> /tmp/x` to an allowed `python3 a.py` therefore cannot write outside; this also applies to commands allowed with the button. Appended arguments are not prevented by this.

**Exempt**: the installed Drive CLI (`gdrive_cli.py`) is the bridge's own code and is not reviewed (reading the profile is its job, and folder limits are enforced inside it). The normal permission button is shown. Likewise, a script in a place this user cannot write (root-owned files under `/usr/local` or `/opt`: neither the file nor its directory is writable) is not reviewed, since the agent cannot have created or changed it.

**Limits**: it is an AI verdict. A clever script may fool it, and "not found" is not a guarantee of safety. Scripts over about 60 KB are checked only up to the first 60 KB, and the message says so. If the check fails (e.g. times out), the button is still shown with a note that it was not checked. This is not a per-run check of already-allowed scripts (that is the hook above).

### Credentials (`cred_run.py`)

Tokens and the like are kept not in the project's `.env` but in the config dir's `credentials/<name>.json` (0600; `deny` for the agent). To use them, prefix the command with `tools/cred_run.py --profile <name> -- <command>`. It runs the command with the profile's environment variables and replaces their values in stdout/stderr (as is, base64, URL-encoded, JSON-escaped) with `***`. The bridge likewise replaces every profile's values with `***` in the text it posts to Slack.

- **Permissions:** a command with the prefix is granted per command string like any other (a button, or the temporary grant after the script's automatic check). The prefix gets no special grant and grant scopes do not widen it. The hook and the automatic script check skip the prefix (`cred_run.py … --`) and a runner such as `op run … --`, and look at **the script behind it** - so a script the agent wrote under `scratch/` is checked as before.
- **Automatic check:** printing, saving or sending the whole environment, or a value that looks like a credential, counts as harmful. A script using credentials is auto-granted only when it merely passes them to the service through something like `op run`.
- **Refused commands:** `env`, `printenv`, `export`, `eval`, `python -c`, `sh -c`, `node -e` and so on (anything that can print the environment directly), and commands pointing at `/proc`, are refused by the prefix.
- **Limits:** the refusal covers the obvious forms; it is not a sandbox. A script that transforms a value before printing it (reversed, ...) is not masked - that is what the automatic check and the person's review before a grant are for. For anything you must not expose even in part, use a token scoped down at the service (read-only, ...). Values `op` resolves (what the `.env` references point to) are not the profile's values, so the prefix does not mask them (`op run` itself masks them in its output).

## 6. MCP tools

MCP tools are allowed by per-server tool name. This is finer-grained than a `run_command` prefix permission. Do not allow tools that execute arbitrary code. As with other entries, permissions are per project (for example, `mcp(playwright/...)` for browser operation should be allowed only in the projects that need it).

## 7. How to check

- `/agy-permissions list`: The `permissionGrants.allow` of **the project of the channel you are in**. Entries from other projects are neither visible nor included.
- `/agy-permissions stats [days]`: In this channel, ordered by how often permission was requested.
- `/agy-stats buttons [days]`: The same content, shown together with button outcomes (once, permanent, denied).

## 8. When adding a new project

Immediately after you add a new project (= a new Slack channel) to `config.yaml`, that project's `permissionGrants.allow` is empty. No permissions from any other existing project are inherited. It is normal for the permission buttons to appear frequently the first few times; if you make each one permanent with `/agy-permissions add`, a permission list dedicated to that project will grow.

## 9. When a safety mechanism is missing

Before every `agy` call (and at startup), the bridge checks that the following safety mechanisms are in place. Whatever is missing is **put back automatically, with the same content setup would create** ("auto-repair"). If something that cannot be put back remains, the bridge **does not run `agy`** and replies to Slack with what is missing.

| What is checked | Example of what is missing |
|---|---|
| The project has `.agents/rules/agent-safety-rules.md`, starting with `trigger: always_on` (without it agy ignores the file) | Deleted file, broken link, missing frontmatter |
| The `script-integrity` hook is registered in `~/.gemini/config/hooks.json` and its script exists in the install directory | Removed registration, damaged install directory |
| The hook's state directory (`integrity/<key>/` in the config directory) exists | Deleted config directory, project not set up |
| `permissions.deny` in `settings.json` contains `write_file(...)` for the install directory and the config directory | Removed deny rules |

Only **presence** is checked. It does not stop you from deliberately loosening the allowed folders or the contents of the safety rules. For agy's built-in project without a workspace, the project-side checks (rules, state directory) are skipped.

**What auto-repair covers**

| Missing | Auto-repair |
|---|---|
| The safety rules file / a broken link | Re-creates the link to `agent-safety-rules.md` in the install directory |
| The hook's state directory | Re-creates it |
| The hook registration | Registers it again in `hooks.json` (other hooks are kept) |
| `deny` | Adds to `permissions.deny` in `settings.json` (other entries are kept; no `allow` is ever written) |
| The safety rules file lacks `trigger: always_on` (agy ignores it otherwise) | Only the `trigger: always_on` line is fixed (added to the top if there is no frontmatter, or the `trigger` line is rewritten if it has another value; nothing else changes). A symlink pointing elsewhere is not edited: `agy` is not run and it is reported |
| `script_integrity.py`, `hooks.json` or `agent-safety-rules.md` in the install directory is gone | Restored from the original that `install.py` saved in `safety-backup/` in the config directory (the agent cannot read or write the config directory). Without a backup (an old install) it cannot be repaired: run `install.py` again |

When something is repaired, the bridge logs a warning and posts a notice to the channel: "Missing safety mechanisms were repaired automatically. Someone may have deleted them." Something disappearing is itself a sign of trouble, so it is never repaired silently.
