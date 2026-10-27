#!/usr/bin/env python3
"""
Google Drive CLI（エージェント用の固定コマンド。複数プロジェクトで共有する前提）
================================================================================
Slack ブリッジ経由のエージェントが Drive を読み書きするための、承認済みコマンド。
毎回違うワンライナーを書かせると、その都度承認が要り、危険でもあるため、これに集約している。

認証は service_account（推奨。フォルダをそのメールアドレスに共有するだけ）と oauth の 2 種類。
認証情報も権限の絞り込みも、このスクリプトには一切書かれていない。すべて
「プロファイル」(JSON、ブリッジの設定ディレクトリ配下 = エージェントに読ませない場所)に
あり、`--profile <名前>` で選ぶ。書き方は tools/gdrive-profile.md を参照。

実行方法（<prefix> = インストール先。絶対パスで書くので、cd は不要。プロジェクトのディレクトリで実行する。
許可リストのエントリと一致させること。-I は必須。--profile は必ず先頭）:
  読み取り系（恒久許可・承認不要）:
    <prefix>/.venv/bin/python -I <prefix>/tools/gdrive_cli.py --profile <name> list <folder_id>
    <prefix>/.venv/bin/python -I <prefix>/tools/gdrive_cli.py --profile <name> search <name_substring> [--in <folder_id>]
    <prefix>/.venv/bin/python -I <prefix>/tools/gdrive_cli.py --profile <name> meta <file_id>
    <prefix>/.venv/bin/python -I <prefix>/tools/gdrive_cli.py --profile <name> read <file_id>       # テキスト化して標準出力
    <prefix>/.venv/bin/python -I <prefix>/tools/gdrive_cli.py --profile <name> download <file_id>   # <プロジェクト>/scratch/gdrive/ に保存し、絶対パスを出力する
  書き込み系（毎回ユーザーの承認が要る。許可リストには入れない）:
    <prefix>/.venv/bin/python -I <prefix>/tools/gdrive_cli.py --profile <name> mkdir <parent_id> <name>
    <prefix>/.venv/bin/python -I <prefix>/tools/gdrive_cli.py --profile <name> upload <local_file> <folder_id> [--name <name>]
    <prefix>/.venv/bin/python -I <prefix>/tools/gdrive_cli.py --profile <name> update <file_id> <local_file>

  `--profile <名前>` は固定の許可リストエントリの一部になる。名前の先は設定ファイルの中
  なので、裏の保管先を変えても、承認済みのコマンド文字列は変わらない。
  `--profile` は、許可エントリの中で、サブコマンドより前に固定されていること
  （末尾の自由引数にすると、エージェントが別プロジェクトのプロファイルを選べてしまう）。

設計上の制約（変更しないこと。エージェントからは書き込み不可にしてある）:
  - 資格情報を取り出すコマンドは、このスクリプトが subprocess で直接実行する。エージェントの
    許可サーフェスには乗らない（エージェントが見る・承認するのは、外側のこのコマンドだけ）。
    これを「run_command で取得してから渡す」形に変えてはいけない（取得コマンド自体が
    承認対象になってしまう）。
  - 取得コマンドの stdout/stderr は、どこにも出さない（エラー文にも）。
  - 読み取り系は読み取り専用スコープのみで動く。書き込み系だけが書き込みスコープを使う。
  - 削除・移動・共有設定の変更は提供しない。
  - 他のスクリプトを import しない（エージェントが書き換え可能なコードを実行しないため）。
  - ローカルの読み書きは、カレント（プロジェクト）配下に限る。
      * 書き出し: scratch/gdrive/ のみ。保存先は指定できない。
      * アップロード元: プロジェクト配下の通常ファイルのみ。拒否リスト（プロファイルで調整可）
        に当たるものは不可。
  - -I（isolated）で起動し、カレント/スクリプトのディレクトリを import パスに含めない。
"""

import fnmatch
import io
import json
import mimetypes
import os
import re
import stat
import subprocess
import sys

SCOPE_READ_DEFAULT = "https://www.googleapis.com/auth/drive.readonly"
SCOPE_WRITE_DEFAULT = "https://www.googleapis.com/auth/drive"
OAUTH_TOKEN_URI_DEFAULT = "https://oauth2.googleapis.com/token"
DOWNLOAD_DIR = os.path.join("scratch", "gdrive")
MAX_READ_CHARS = 200_000
FOLDER_MIME = "application/vnd.google-apps.folder"
CREDENTIAL_COMMAND_TIMEOUT_SEC = 30
MAX_PARENT_WALK = 30

ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

READ_COMMAND_NAMES = ("list", "search", "meta", "read", "download")
WRITE_COMMAND_NAMES = ("mkdir", "upload", "update")

# Google 形式 -> テキストとして export する mimeType
EXPORT_TYPES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

# アップロード元として拒否するファイル名/パス（機密になり得るもの）。プロファイルの
# upload_deny.names / upload_deny.dirs で丸ごと置き換えられる(足すのではなく置き換え)。
DENY_UPLOAD_NAMES_DEFAULT = [".env*", "*.pem", "*.key", "*.p12", "id_rsa*", "id_ed25519*",
                             "token*.json", "*credentials*.json", "*secret*", "*.kdbx"]
DENY_UPLOAD_DIRS_DEFAULT = [".git", ".venv", "node_modules", ".ssh", ".aws", ".gnupg"]

SERVICE_ACCOUNT = {"email": "?"}


class ProfileError(Exception):
    """プロファイル/資格情報まわりの失敗。メッセージに秘密を含めないこと。"""


# --- プロファイル -------------------------------------------------------------

def profile_dir():
    base = os.environ.get("AGY_BRIDGE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".config", "agy-slack-bridge")
    return os.path.join(base, "gdrive")


def _check_profile_file_mode(path):
    """ポリシーの改ざん防止: 自分以外が書けるプロファイルは拒否。読めるだけなら警告。"""
    for p in (path, os.path.dirname(path)):
        mode = os.stat(p).st_mode
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ProfileError(f"{p} が group/other から書き込み可能です。chmod go-w してください")
    if os.stat(path).st_mode & (stat.S_IRGRP | stat.S_IROTH):
        print(f"warning: {path} が group/other から読めます（chmod 600 を推奨）", file=sys.stderr)


def _str_list(value, what):
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ProfileError(f"プロファイルの {what} は文字列のリストにしてください")
    return value


def validate_policy(commands, folders, deny_names, deny_dirs):
    """方針(どのコマンドを許すか、どのフォルダ配下に限るか、アップロードの拒否パターン)の
    検証。parse_profile と、setup_agy_project.py の対話設定が共有する。"""
    commands = _str_list(commands, "commands")
    bad = set(commands) - set(READ_COMMAND_NAMES + WRITE_COMMAND_NAMES)
    if bad:
        raise ProfileError(f"プロファイルの commands に未知のコマンドがあります: {sorted(bad)}")
    folders = _str_list(folders, "allowed_folders")
    for f in folders:
        if not ID_RE.match(f):
            raise ProfileError("プロファイルの allowed_folders に Drive ID として不正な値があります")
    return (commands, folders, _str_list(deny_names, "upload_deny.names"),
            _str_list(deny_dirs, "upload_deny.dirs"))


_DRIVE_URL_ID_RES = (re.compile(r"/folders/([A-Za-z0-9_-]+)"), re.compile(r"[?&]id=([A-Za-z0-9_-]+)"),
                     re.compile(r"/d/([A-Za-z0-9_-]+)"))


def extract_drive_id(text):
    """Drive の URL(…/folders/<id>、open?id=<id>、…/d/<id>)または ID そのものから ID を取り出す。
    取り出せなければ None。"""
    text = text.strip()
    if ID_RE.match(text):
        return text
    if text.startswith("https://"):
        for rx in _DRIVE_URL_ID_RES:
            m = rx.search(text)
            if m and ID_RE.match(m.group(1)):
                return m.group(1)
    return None


def parse_profile(data):
    """プロファイル(dict)を検証し、既定値を補って返す。未知のキーは(綴り間違いで
    制限が黙って効かなくなるのを防ぐため)エラー。"""
    if not isinstance(data, dict):
        raise ProfileError("プロファイルは JSON のオブジェクトにしてください")
    known = {"kind", "credentials", "scopes", "commands", "allowed_folders", "upload_deny"}
    unknown = set(data) - known
    if unknown:
        raise ProfileError(f"プロファイルに未知のキーがあります: {sorted(unknown)}")

    kind = data.get("kind")
    if kind not in ("service_account", "oauth"):
        raise ProfileError("プロファイルの kind は service_account か oauth のどちらかを必ず指定してください")

    cred = data.get("credentials")
    if not isinstance(cred, dict) or set(cred) - {"command", "paths"}:
        raise ProfileError("プロファイルの credentials は {command, paths(任意)} にしてください")
    command = _str_list(cred.get("command"), "credentials.command")
    if not command:
        raise ProfileError("プロファイルの credentials.command が空です")
    paths = _str_list(cred.get("paths", []), "credentials.paths")

    scopes = data.get("scopes", {})
    if not isinstance(scopes, dict) or set(scopes) - {"read", "write"}:
        raise ProfileError("プロファイルの scopes は {read, write} にしてください")
    scope_read = scopes.get("read", SCOPE_READ_DEFAULT)
    scope_write = scopes.get("write", SCOPE_WRITE_DEFAULT)
    if not (isinstance(scope_read, str) and isinstance(scope_write, str)):
        raise ProfileError("プロファイルの scopes.read/write は文字列にしてください")

    deny = data.get("upload_deny", {})
    if not isinstance(deny, dict) or set(deny) - {"names", "dirs"}:
        raise ProfileError("プロファイルの upload_deny は {names, dirs} にしてください")
    commands, folders, deny_names, deny_dirs = validate_policy(
        data.get("commands", list(READ_COMMAND_NAMES + WRITE_COMMAND_NAMES)),
        data.get("allowed_folders", []),
        deny.get("names", DENY_UPLOAD_NAMES_DEFAULT),
        deny.get("dirs", DENY_UPLOAD_DIRS_DEFAULT))
    return {
        "kind": kind,
        "command": command,
        "paths": paths,
        "scope_read": scope_read,
        "scope_write": scope_write,
        "commands": set(commands),
        "allowed_folders": set(folders),
        "deny_names": deny_names,
        "deny_dirs": set(deny_dirs),
    }


def load_profile(name):
    if not PROFILE_NAME_RE.match(name):
        raise ProfileError("プロファイル名に使えるのは英数字と _ - だけです")
    path = os.path.join(profile_dir(), f"{name}.json")
    try:
        _check_profile_file_mode(path)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ProfileError(f"プロファイルがありません: {path}") from None
    except (OSError, ValueError):
        raise ProfileError(f"プロファイルを読めません（JSON として不正？）: {path}") from None
    return parse_profile(data)


# --- 資格情報 -----------------------------------------------------------------

def _warn_loose_paths(paths):
    for p in paths:
        try:
            if os.stat(os.path.expanduser(p)).st_mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
                print(f"warning: 資格情報ファイル {p} が group/other からアクセス可能です（chmod 600 を推奨）",
                      file=sys.stderr)
        except OSError:
            pass


def fetch_credential_info(profile):
    """プロファイルの credentials.command を直接実行し、stdout を JSON として返す。
    例外オブジェクト(.stdout/.stderr を持つ)を通さず、argv と returncode だけで
    自前のメッセージにする。"""
    argv = profile["command"]
    _warn_loose_paths(profile["paths"])
    try:
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=CREDENTIAL_COMMAND_TIMEOUT_SEC, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise ProfileError(f"資格情報の取得コマンドが見つかりません: {argv[0]}") from None
    except subprocess.TimeoutExpired:
        raise ProfileError(f"資格情報の取得コマンドが {CREDENTIAL_COMMAND_TIMEOUT_SEC} 秒で終わりません: "
                           f"{argv[0]}") from None
    except OSError:
        raise ProfileError(f"資格情報の取得コマンドを実行できません: {argv[0]}") from None
    if res.returncode != 0:
        raise ProfileError(f"資格情報の取得コマンドが失敗しました（終了コード {res.returncode}）: {argv[0]}")
    try:
        info = json.loads(res.stdout)
    except ValueError:
        raise ProfileError("資格情報の取得コマンドの出力が JSON ではありません") from None
    if not isinstance(info, dict):
        raise ProfileError("資格情報の取得コマンドの出力が JSON オブジェクトではありません")
    return info


def validate_credential_shape(kind, info):
    """kind と鍵の形が合うか。足りないキーの名前だけを言い、値は出さない。"""
    if kind == "service_account":
        missing = [k for k in ("private_key", "client_email") if not info.get(k)]
        if info.get("type") != "service_account":
            missing.insert(0, "type=service_account")
    else:
        missing = [k for k in ("client_id", "client_secret", "refresh_token") if not info.get(k)]
    if missing:
        raise ProfileError(f"資格情報が kind={kind} の形ではありません（足りない/不正: {', '.join(missing)}）")


def build_credentials(profile, info, write):
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials as OAuthCredentials

    scope = profile["scope_write"] if write else profile["scope_read"]
    if profile["kind"] == "service_account":
        SERVICE_ACCOUNT["email"] = info.get("client_email", "?")
        return service_account.Credentials.from_service_account_info(info, scopes=[scope])
    # OAuth: アクセストークンは実行ごとにリフレッシュで取り、保存しない。
    # リフレッシュで要求できるスコープは、最初の同意で与えたものの範囲内に限られる
    # （プロファイルの scopes.read/write は、運用者が同意時の値に合わせて指定する）。
    return OAuthCredentials(
        None, refresh_token=info["refresh_token"], client_id=info["client_id"],
        client_secret=info["client_secret"], token_uri=info.get("token_uri", OAUTH_TOKEN_URI_DEFAULT),
        scopes=[scope])


def get_service(profile, write=False):
    from googleapiclient.discovery import build

    info = fetch_credential_info(profile)
    validate_credential_shape(profile["kind"], info)
    creds = build_credentials(profile, info, write)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# --- 共通 ---------------------------------------------------------------------

def check_id(value):
    if not ID_RE.match(value):
        sys.exit(f"error: not a valid Drive ID: {value!r}")
    return value


def fmt(f):
    size = f.get("size")
    return "\t".join([
        f["id"], f.get("mimeType", ""), f.get("modifiedTime", ""),
        size if size is not None else "-", f.get("name", ""),
    ])


FIELDS = "id, name, mimeType, modifiedTime, size, parents"


def q_escape(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


def list_files(service, query, limit=200):
    out, token = [], None
    while len(out) < limit:
        res = service.files().list(
            q=query, spaces="drive", pageSize=100, pageToken=token,
            fields=f"nextPageToken, files({FIELDS})",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        out += res.get("files", [])
        token = res.get("nextPageToken")
        if not token:
            break
    return out[:limit]


def ensure_in_allowed(service, profile, file_id):
    """プロファイルに allowed_folders があるとき、file_id がその配下(または自身)か確認する。
    親をたどって調べる。空(未設定)なら制限なし。"""
    allowed = profile["allowed_folders"]
    if not allowed:
        return
    seen, frontier = set(), [file_id]
    for _ in range(MAX_PARENT_WALK):
        nxt = []
        for fid in frontier:
            if fid in allowed:
                return
            if fid in seen:
                continue
            seen.add(fid)
            meta = service.files().get(fileId=fid, fields="parents", supportsAllDrives=True).execute()
            nxt += meta.get("parents") or []
        if not nxt:
            break
        frontier = nxt
    sys.exit("error: この ID は、プロファイルで許可されたフォルダの外です")


# --- 読み取り系 ---------------------------------------------------------------

def cmd_list(service, profile, args):
    folder = check_id(args[0])
    ensure_in_allowed(service, profile, folder)
    for f in list_files(service, f"'{folder}' in parents and trashed = false"):
        print(fmt(f))


def cmd_search(service, profile, args):
    if not args:
        sys.exit("usage: search <name_substring> [--in <folder_id>]")
    query = f"name contains '{q_escape(args[0])}' and trashed = false"
    if len(args) >= 3 and args[1] == "--in":
        folder = check_id(args[2])
        ensure_in_allowed(service, profile, folder)
        query += f" and '{folder}' in parents"
    elif profile["allowed_folders"]:
        sys.exit("error: このプロファイルでは search に --in <folder_id> が必要です")
    for f in list_files(service, query, limit=50):
        print(fmt(f))


def cmd_meta(service, profile, args):
    file_id = check_id(args[0])
    ensure_in_allowed(service, profile, file_id)
    f = service.files().get(fileId=file_id, fields=FIELDS, supportsAllDrives=True).execute()
    print(json.dumps(f, ensure_ascii=False, indent=2))


def download_bytes(request):
    from googleapiclient.http import MediaIoBaseDownload

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def cmd_read(service, profile, args):
    file_id = check_id(args[0])
    ensure_in_allowed(service, profile, file_id)
    meta = service.files().get(fileId=file_id, fields="name, mimeType", supportsAllDrives=True).execute()
    mime = meta["mimeType"]
    if mime in EXPORT_TYPES:
        data = download_bytes(service.files().export_media(fileId=file_id, mimeType=EXPORT_TYPES[mime]))
    elif mime.startswith("text/") or mime in ("application/json", "application/xml"):
        data = download_bytes(service.files().get_media(fileId=file_id, supportsAllDrives=True))
    else:
        sys.exit(f"error: {meta['name']!r} is {mime}, not readable as text; use `download` instead")
    text = data.decode("utf-8", errors="replace")
    print(text[:MAX_READ_CHARS])
    if len(text) > MAX_READ_CHARS:
        print(f"\n... (truncated, {len(text)} chars total)")


def safe_name(name):
    return re.sub(r"[^\w.\-()　-鿿]+", "_", name).strip("._") or "file"


INSTALL_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def project_dir():
    """カレントディレクトリ = プロジェクトのルート。インストール先の中で実行されたときは拒否する
    （そこへ scratch/ を作ってしまい、エージェントも読めない。プロジェクトのディレクトリで実行させる）。"""
    cwd = os.path.realpath(os.getcwd())
    if os.path.commonpath([cwd, INSTALL_ROOT]) == INSTALL_ROOT:
        sys.exit(f"error: the current directory ({cwd}) is inside the bridge install directory; "
                 "run this command from the project directory (the one agy opened)")
    return cwd


def cmd_download(service, profile, args):
    root = project_dir()
    file_id = check_id(args[0])
    ensure_in_allowed(service, profile, file_id)
    meta = service.files().get(fileId=file_id, fields="name, mimeType", supportsAllDrives=True).execute()
    mime, name = meta["mimeType"], safe_name(meta["name"])
    if mime in EXPORT_TYPES:
        # Google 形式は Office/PDF 形式で書き出す
        target = {
            "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
            "application/vnd.google-apps.spreadsheet": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
            "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
        }[mime]
        data = download_bytes(service.files().export_media(fileId=file_id, mimeType=target[0]))
        if not name.endswith(target[1]):
            name += target[1]
    elif mime.startswith("application/vnd.google-apps."):
        sys.exit(f"error: {mime} cannot be downloaded")
    else:
        data = download_bytes(service.files().get_media(fileId=file_id, supportsAllDrives=True))
    out_dir = os.path.join(root, DOWNLOAD_DIR)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)
    with open(path, "wb") as fh:
        fh.write(data)
    print(path)                                # 絶対パス（エージェントが read_file にそのまま使える）


# --- 書き込み系（毎回承認が要る）-----------------------------------------------

def resolve_upload_source(path, profile):
    """アップロード元を検証する。プロジェクト（カレント）配下の通常ファイルで、
    プロファイルの拒否リストに当たらないもの。"""
    root = project_dir()
    real = os.path.realpath(path)
    if os.path.commonpath([root, real]) != root:
        sys.exit(f"error: {path!r} is outside the project directory")
    if not os.path.isfile(real):
        sys.exit(f"error: {path!r} is not a regular file")
    rel_parts = os.path.relpath(real, root).split(os.sep)
    if profile["deny_dirs"].intersection(rel_parts[:-1]):
        sys.exit(f"error: refusing to upload from {os.sep.join(rel_parts[:-1])!r}")
    base = os.path.basename(real).lower()
    if any(fnmatch.fnmatch(base, pat.lower()) for pat in profile["deny_names"]):
        sys.exit(f"error: refusing to upload {base!r} (matches the profile's upload_deny)")
    return real


def guess_mime(path):
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def cmd_mkdir(service, profile, args):
    if len(args) < 2:
        sys.exit("usage: mkdir <parent_id> <name>")
    parent, name = check_id(args[0]), args[1]
    ensure_in_allowed(service, profile, parent)
    existing = list_files(
        service,
        f"name = '{q_escape(name)}' and '{parent}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false",
        limit=1)
    if existing:
        print(fmt(existing[0]) + "\t(already exists)")
        return
    f = service.files().create(
        body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]},
        fields=FIELDS, supportsAllDrives=True).execute()
    print(fmt(f))


def cmd_upload(service, profile, args):
    from googleapiclient.http import MediaFileUpload

    if len(args) < 2:
        sys.exit("usage: upload <local_file> <folder_id> [--name <name>]")
    local = resolve_upload_source(args[0], profile)
    folder = check_id(args[1])
    ensure_in_allowed(service, profile, folder)
    name = args[3] if len(args) >= 4 and args[2] == "--name" else os.path.basename(local)
    media = MediaFileUpload(local, mimetype=guess_mime(local), resumable=True)
    f = service.files().create(
        body={"name": name, "parents": [folder]}, media_body=media,
        fields=FIELDS, supportsAllDrives=True).execute()
    print(fmt(f))


def cmd_update(service, profile, args):
    from googleapiclient.http import MediaFileUpload

    if len(args) < 2:
        sys.exit("usage: update <file_id> <local_file>")
    file_id = check_id(args[0])
    local = resolve_upload_source(args[1], profile)
    ensure_in_allowed(service, profile, file_id)
    meta = service.files().get(fileId=file_id, fields="name, mimeType", supportsAllDrives=True).execute()
    if meta["mimeType"] == FOLDER_MIME:
        sys.exit("error: that ID is a folder")
    media = MediaFileUpload(local, mimetype=guess_mime(local), resumable=True)
    f = service.files().update(
        fileId=file_id, media_body=media, fields=FIELDS, supportsAllDrives=True).execute()
    print(fmt(f))


COMMANDS = {"list": cmd_list, "search": cmd_search, "meta": cmd_meta, "read": cmd_read,
            "download": cmd_download, "mkdir": cmd_mkdir, "upload": cmd_upload, "update": cmd_update}


def _error_reason(content):
    """Drive API のエラー本文(JSON)から reason を取り出す。取れなければ None。"""
    try:
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        err = json.loads(content).get("error", {})
        if err.get("errors"):
            return err["errors"][0].get("reason")
        return err.get("status")
    except (ValueError, AttributeError, TypeError):
        return None


NEW_FILES_DOC_URL = "https://github.com/so5/agy-slack-bridge/blob/master/tools/gdrive-profile.md#create-new-files"


def upload_target_name(args):
    """upload の引数から、Drive に作ろうとしたファイル名を求める(エラーメッセージ用)。"""
    if len(args) >= 4 and args[2] == "--name":
        return args[3]
    return os.path.basename(args[0]) if args else None


def http_error_message(status, content, write, sa_email="?", file_name=None):
    """HttpError(status と本文)から、利用者向けのメッセージを作る。"""
    reason = _error_reason(content)
    if reason == "storageQuotaExceeded":
        if sa_email == "?":
            return (f"error: Drive API {status} (storageQuotaExceeded): この認証情報のアカウントの Drive の容量が"
                    "いっぱいです。容量を空けてください")
        name = f"「{file_name}」" if file_name else ""
        return (f"error: Drive API {status} (storageQuotaExceeded): サービスアカウントは自分のストレージ容量を持たないため、"
                "新しいファイルを作れません。\n"
                f"Google Drive でファイル{name}を作って、サービスアカウント {sa_email} に編集者として共有してください"
                "（共有されたファイルには update で内容を書き込めます）。\n"
                f"自動的に新規ファイルを作らせる方法はこちら: {NEW_FILES_DOC_URL}")
    hint = ""
    if status in (403, 404):
        who = f"サービスアカウント {sa_email}" if sa_email != "?" else "この認証情報のアカウント"
        hint = f" (見つからない、または共有されていません。{who} に{'編集' if write else '閲覧'}権限が必要です)"
    return f"error: Drive API {status}{hint}"


def main(argv):
    if len(argv) < 2 or argv[0] != "--profile":
        sys.exit("usage: gdrive_cli.py --profile <name> <command> [args...]\n\n" + __doc__)
    rest = argv[2:]
    if len(rest) < 1 or rest[0] not in COMMANDS:
        sys.exit(__doc__)
    from googleapiclient.errors import HttpError
    from google.auth.exceptions import RefreshError

    write = rest[0] in WRITE_COMMAND_NAMES
    try:
        profile = load_profile(argv[1])
        if rest[0] not in profile["commands"]:
            sys.exit(f"error: このプロファイルでは {rest[0]} は無効です")
        COMMANDS[rest[0]](get_service(profile, write=write), profile, rest[1:])
    except ProfileError as e:
        sys.exit(f"error: {e}")
    except RefreshError as e:
        reason = e.args[0] if e.args else "refresh failed"
        sys.exit(f"error: アクセストークンを取得できません（{reason}）。OAuth の場合は、プロファイルの "
                 "scopes が、リフレッシュトークンの同意時のスコープと一致しているか、"
                 "トークンが失効していないか確認してください")
    except HttpError as e:
        sys.exit(http_error_message(getattr(e.resp, "status", "?"), getattr(e, "content", b""), write,
                                    SERVICE_ACCOUNT["email"],
                                    upload_target_name(rest[1:]) if rest[0] == "upload" else None))
    except IndexError:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
