# Drive CLI Profile

`setup_agy_project.py` creates and configures this file interactively (authentication kind, the
command that fetches credentials, scopes, enabled commands, allowed folders, upload deny patterns).
You may also write it by hand, or edit it by hand later. What follows describes the file format.

`gdrive_cli.py --profile <name> <command> ...` reads both its credentials and its permission
restrictions from the profile (JSON). The script itself contains no storage backend and no
project-specific values.

**Scope of the tool**: operations on Drive **files** only (list, search, metadata, read, download, create folders,
upload, update existing files). For native Google Docs / Sheets / Slides it goes as far as **reading the content**
through Drive export (`read` gives text or CSV, `download` gives PDF / xlsx); editing inside a document, formatting,
comments, cell ranges and creating native documents are not included (the Docs / Sheets / Slides APIs are not used).
`upload` / `update` are meant for ordinary files; their behavior on native formats has not been checked.

- Location: `<bridge config directory>/gdrive/<name>.json`
  (default `~/.config/agy-slack-bridge/gdrive/`; can be changed with the environment variable `AGY_BRIDGE_CONFIG_DIR`).
  This location is covered by `permissions.deny` (`setup_agy_project.py` blocks the whole config directory),
  so the agent neither reads nor writes it.
- A name may contain only alphanumeric characters, `_`, and `-`.
- If the profile or its parent directory is **writable** by group/other, execution is refused
  (to prevent tampering with the policy). If it is merely readable, a warning is shown. Use `chmod 600` / `700`.
- Unknown keys are an error (so that a typo cannot silently drop a restriction).
- Template: [`gdrive-profile.example.json`](gdrive-profile.example.json)

## Which authentication to use

**We recommend a service account (`service_account`).** Choose OAuth only when a service account is not enough.

A service account works by **sharing the target folder with the service account's own email address**
(ordinary Drive sharing, the same as sharing with another person). It is the same for a personal Google account's
Drive and for Workspace, and needs neither domain-wide delegation nor OAuth.

- Simpler setup: no OAuth consent screen, no Google verification, no 7-day expiry in testing mode.
- Safer by default: a service account **sees only what has been shared with it**, so `allowed_folders` adds a second
  layer to an already narrow starting point. OAuth is the reverse: it gets rights to the user's whole Drive and is then
  narrowed by `allowed_folders`.
- No scope mismatch: the OAuth `invalid_scope` problem (below) cannot happen, because there is no original consent.

> ⚠ The key file the setup saves is **plaintext JSON**. Any process running as the same OS user can read it (agy's deny rules are not a hard barrier). Anyone holding it can operate on the shared folders. Share only what you need, and delete the key in the Cloud console if it leaks. For stronger storage, leave the key path empty and use a `credentials.command` that fetches it from a password manager or similar.

Setup steps (service account):

1. Create a Google Cloud project and enable the **Google Drive API**.
2. Create a service account and create a **JSON key** for it.
3. Enter the key file path in `setup_agy_project.py`: it saves the key as `<config dir>/gdrive/<profile>.sa.json` (0600) and sets `credentials.command` (`cat` of that file) and `credentials.paths`. To use another key store, leave the path empty and keep the key somewhere `credentials.command` can fetch it from (see "The Command That Fetches Credentials" below).
4. In Drive, share each folder you want to allow with the `client_email` address from the key ("Viewer" for
   read-only, "Editor" if writes are allowed).
5. Put the same folder IDs in the profile's `allowed_folders`.

**Set up OAuth (`oauth`)** when you want agy to create new files by itself (details in "Letting agy create new files on its own" below), or when you want it to act with the user's own rights. In exchange you take on the
consent-screen setup, the 7-day expiry in testing mode, verification when published (`drive` is a restricted scope),
scope matching, and access to the whole Drive (see "Notes for OAuth" below).

> **Confirmed**: a service account has no storage quota of its own, so in a personal Google account's My Drive it
> **cannot create a new file of any format or size** (`upload` fails with `storageQuotaExceeded`; creating an empty
> native Google Doc directly through the Drive API fails the same way). `mkdir` (folders) and **`update` of a file a human created and shared** work fine (the file's
> owner and quota stay with the human).

<a id="create-new-files"></a>

## Letting agy create new files on its own

What a service account can do is read and write **files a human created beforehand**. If agy needs to create
**new files** on its own initiative, use one of these:

1. **A human creates an empty file and shares it (still a service account).** Create the file in Google Drive and
   share it with the service account's email address as **editor**. agy can then `update` it. If the file's
   location and name are fixed, this is enough.
2. **Use an OAuth profile.** With OAuth the file is created with the user's own rights and quota, so `upload` can
   create new files. In this tool that means:
   - `kind` is `oauth`, and `credentials.command` prints a JSON with `client_id`, `client_secret` and `refresh_token`.
   - Obtain the refresh token by consenting to the write scope (default `drive`) (how to get it is outside this tool:
     create an OAuth client, consent, get the token).
   - `scopes` must **match** the original consent (see "Notes for OAuth" below).
   - Restrict where it can write with `allowed_folders` (OAuth can see the user's whole Drive).

OAuth is not a "substitute when the service account fails"; it is **an additional capability you set up when you want
agy to create new files by itself**. If you do not need that, a service account alone is enough.

## Keys

| Key | Required | Description | Default when omitted |
|---|---|---|---|
| `kind` | Yes | `service_account` (recommended) or `oauth` | (error) |
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

## Notes for OAuth (Operator's Responsibility)

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
