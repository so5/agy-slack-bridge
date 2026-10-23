# Drive CLI Profile

`setup_agy_project.py` creates and configures this file interactively (authentication kind, the
command that fetches credentials, scopes, enabled commands, allowed folders, upload deny patterns).
You may also write it by hand, or edit it by hand later. What follows describes the file format.

`gdrive_cli.py --profile <name> <command> ...` reads both its credentials and its permission
restrictions from the profile (JSON). The script itself contains no storage backend and no
project-specific values.

- Location: `<bridge config directory>/gdrive/<name>.json`
  (default `~/.config/agy-slack-bridge/gdrive/`; can be changed with the environment variable `AGY_BRIDGE_CONFIG_DIR`).
  This location is covered by `permissions.deny` (`setup_agy_project.py` blocks the whole config directory),
  so the agent neither reads nor writes it.
- A name may contain only alphanumeric characters, `_`, and `-`.
- If the profile or its parent directory is **writable** by group/other, execution is refused
  (to prevent tampering with the policy). If it is merely readable, a warning is shown. Use `chmod 600` / `700`.
- Unknown keys are an error (so that a typo cannot silently drop a restriction).
- Template: [`gdrive-profile.example.json`](gdrive-profile.example.json)

## Keys

| Key | Required | Description | Default when omitted |
|---|---|---|---|
| `kind` | Yes | `service_account` or `oauth` | (error) |
| `credentials.command` | Yes | The **argv of a command** that prints the credentials JSON to stdout (a list; shell strings are not allowed) | (error) |
| `credentials.paths` | No | Files that the command above reads. A warning is shown at run time if they are readable by group/other | None |
| `scopes.read` / `scopes.write` | No | Scopes to request for read-type / write-type operations | `drive.readonly` / `drive` |
| `commands` | No | Subcommands to enable | All (5 read + 3 write) |
| `allowed_folders` | No | Folder IDs in which operations are permitted. Only items under them (determined by walking up the ancestors) | Empty = no restriction |
| `upload_deny.names` / `.dirs` | No | File name patterns / directory names to deny as upload sources. **If specified, this replaces the defaults entirely** | Default deny list |

If you remove the write-type commands from `commands`, that profile cannot write at all.
If you specify `allowed_folders`, `search` requires `--in <folder ID>`.

## The Command That Fetches Credentials

`credentials.command` is run **directly** as a subprocess by `gdrive_cli.py` (without going through a shell).
Its standard output is interpreted as JSON. Neither standard output nor standard error is included in error messages.

| `kind` | Expected JSON |
|---|---|
| `service_account` | The service account key (`type: service_account`, `private_key`, `client_email` ...) |
| `oauth` | `client_id`, `client_secret`, `refresh_token` (optionally `token_uri`) |

Examples for each storage backend:

| Storage | `credentials.command` |
|---|---|
| AWS Secrets Manager | `["aws","secretsmanager","get-secret-value","--secret-id","<name>","--query","SecretString","--output","text"]` |
| Local file | `["cat","/home/you/.config/agy-slack-bridge/gdrive/proj-a.sa.json"]` (+ that path in `credentials.paths`. Place it **outside the project directory** with `chmod 600`) |
| 1Password | `["op","read","op://Vault/proj-a-gdrive/credential"]` |
| pass | `["pass","show","proj-a/gdrive"]` |
| GCP Secret Manager | `["gcloud","secrets","versions","access","latest","--secret","proj-a-gdrive"]` |
| Vault | `["vault","kv","get","-field=key","secret/proj-a/gdrive"]` |

### What to Observe

- The fetch command is something **`gdrive_cli.py` runs itself**. It is not part of the agent's
  permission surface (all the agent sees and approves is the outer `gdrive_cli.py --profile ... list`).
  Do not change this to a form where the credentials are fetched with `run_command` and then passed in,
  because that would make the underlying fetch command itself subject to approval.
- **Do not add** fetch commands such as `aws secretsmanager ...` or `op read ...` to the allowlist.
- The fetch command is run from the bridge (with a minimal `PATH` if it is a systemd user service).
  For tools installed in locations not on `PATH` (pipx, snap, `pip --user`, etc.), either write the absolute path
  or add `Environment=PATH=...` to the unit.
- In an allow entry, fix `--profile <name>` before the subcommand
  (`command(<prefix>/.venv/bin/python -I <prefix>/tools/gdrive_cli.py --profile proj-a list)`). With trailing free-form arguments,
  the agent could choose another project's profile. `setup_agy_project.py` registers entries in this form.
- Even if you change the storage backend, you only edit the contents of the profile; the approved command string does not change (no re-approval is needed).

## OAuth Notes (Operator's Responsibility)

- **The scopes must match what was consented to when the refresh token was issued.**
  On refresh, you can only request scopes within the range granted by the initial consent.
  **Confirmed on a real agy**: when the default `scopes.read` (`drive.readonly`) was used with a token consented to with `drive`,
  every request failed with `invalid_scope`, even for narrower scopes (resolved by setting `scopes.read` to `drive`).
  The script cannot determine in advance whether the
  token was consented to with `drive` or with `drive.readonly`, so setting `scopes.read` / `scopes.write` correctly
  is the operator's responsibility. If they are wrong, the refresh fails with `invalid_scope` or similar
  (an error saying so is shown).
  - For a read-only profile, use a token consented to with `drive.readonly` and remove the write-type commands from `commands`;
    this is the safest option (the token itself cannot write).
  - If you only have a token consented to with `drive`, set `scopes.read` to `drive` as well. In that case,
    read-only behavior is not guaranteed on Google's side, and only the `commands` setting protects you.
- **OAuth can see the user's entire Drive** (a service account sees only what has been shared with it).
  Setting `allowed_folders` is strongly recommended.
- The access token is obtained by refreshing on every run and is not stored.
- **If the publishing status of the OAuth consent screen remains "Testing", the refresh token expires after 7 days.**
  Personal accounts often stay in "Testing", and the tool suddenly stops working once a week.
  When it expires, redo the consent to renew the token and update the storage backend.
  For permanent use, you need to publish the app to "Production" (depending on the number of users and the scopes,
  Google's review may be required).
