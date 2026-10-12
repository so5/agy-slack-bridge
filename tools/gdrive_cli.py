#!/usr/bin/env python3
"""
Google Drive CLI（エージェント用の固定コマンド。複数プロジェクトで共有する前提）
================================================================================
Slack ブリッジ経由のエージェントが Drive を読み書きするための、承認済みコマンド。
毎回違うワンライナーを書かせると、その都度承認が要り、危険でもあるため、これに集約している。

このスクリプトは1つのプロジェクトに固有の認証情報を持たない。どの AWS Secrets
Manager のシークレット（Google サービスアカウントの鍵）を使うかは、必ず
`--secret <名前>` で明示する。デフォルト値は無い（誤ったデフォルトは、他の
プロジェクトの認証情報を黒黒に借用してしまう事故になるため、意図的に持たせていない）。

実行方法（許可リストのエントリと一致させること。-I は必須）:
  読み取り系（恒久許可・承認不要）:
    .venv/bin/python -I gdrive_cli.py --secret <aws-secret-name> list <folder_id>
    .venv/bin/python -I gdrive_cli.py --secret <aws-secret-name> search <name_substring> [--in <folder_id>]
    .venv/bin/python -I gdrive_cli.py --secret <aws-secret-name> meta <file_id>
    .venv/bin/python -I gdrive_cli.py --secret <aws-secret-name> read <file_id>       # テキスト化して標準出力
    .venv/bin/python -I gdrive_cli.py --secret <aws-secret-name> download <file_id>   # scratch/gdrive/ に保存
  書き込み系（毎回ユーザーの承認が要る。許可リストには入れない）:
    .venv/bin/python -I gdrive_cli.py --secret <aws-secret-name> mkdir <parent_id> <name>
    .venv/bin/python -I gdrive_cli.py --secret <aws-secret-name> upload <local_file> <folder_id> [--name <name>]
    .venv/bin/python -I gdrive_cli.py --secret <aws-secret-name> update <file_id> <local_file>

  `--secret <名前>` は固定の許可リストエントリの一部になる（プロジェクトごとに
  異なる文字列になるので、1つのプロジェクトへの恒久許可が別プロジェクトの
  認証情報を使う権限にはならない）。例:
    command(.venv/bin/python -I gdrive_cli.py --secret accountant-local-google-credentials list)

設計上の制約（変更しないこと。エージェントからは書き込み不可にしてある）:
  - 読み取り系は読み取り専用スコープ (drive.readonly) のみで動く。書き込み系だけが drive スコープを使う。
  - 削除・移動・共有設定の変更は提供しない。
  - 他のスクリプトを import しない（エージェントが書き換え可能なコードを実行しないため）。
  - ローカルの読み書きは、カレント（プロジェクト）配下に限る。
      * 書き出し: scratch/gdrive/ のみ。保存先は指定できない。
      * アップロード元: プロジェクト配下の通常ファイルのみ。.env・鍵・トークン類・.git・.venv は不可。
  - -I（isolated）で起動し、カレント/スクリプトのディレクトリを import パスに含めない。
"""

import fnmatch
import io
import json
import mimetypes
import os
import re
import subprocess
import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPE_READ = "https://www.googleapis.com/auth/drive.readonly"
SCOPE_WRITE = "https://www.googleapis.com/auth/drive"
DOWNLOAD_DIR = os.path.join("scratch", "gdrive")
MAX_READ_CHARS = 200_000
FOLDER_MIME = "application/vnd.google-apps.folder"

ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")

# Google 形式 -> テキストとして export する mimeType
EXPORT_TYPES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

# アップロード元として拒否するファイル名/パス（機密になり得るもの）
DENY_UPLOAD_NAMES = [".env*", "*.pem", "*.key", "*.p12", "id_rsa*", "id_ed25519*",
                     "token*.json", "*credentials*.json", "*secret*", "*.kdbx"]
DENY_UPLOAD_DIRS = {".git", ".venv", "node_modules", ".ssh", ".aws", ".gnupg"}

SERVICE_ACCOUNT = {"email": "?"}


def get_service(secret_name, write=False):
    res = subprocess.check_output(
        ["aws", "secretsmanager", "get-secret-value", "--secret-id", secret_name,
         "--query", "SecretString", "--output", "text"],
        text=True,
    )
    info = json.loads(res)
    SERVICE_ACCOUNT["email"] = info.get("client_email", "?")
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=[SCOPE_WRITE if write else SCOPE_READ])
    return build("drive", "v3", credentials=creds, cache_discovery=False)


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


# --- 読み取り系 -------------------------------------------------------------

def cmd_list(service, args):
    folder = check_id(args[0])
    for f in list_files(service, f"'{folder}' in parents and trashed = false"):
        print(fmt(f))


def cmd_search(service, args):
    if not args:
        sys.exit("usage: search <name_substring> [--in <folder_id>]")
    query = f"name contains '{q_escape(args[0])}' and trashed = false"
    if len(args) >= 3 and args[1] == "--in":
        query += f" and '{check_id(args[2])}' in parents"
    for f in list_files(service, query, limit=50):
        print(fmt(f))


def cmd_meta(service, args):
    f = service.files().get(fileId=check_id(args[0]), fields=FIELDS, supportsAllDrives=True).execute()
    print(json.dumps(f, ensure_ascii=False, indent=2))


def download_bytes(request):
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def cmd_read(service, args):
    file_id = check_id(args[0])
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


def cmd_download(service, args):
    file_id = check_id(args[0])
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
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    path = os.path.join(DOWNLOAD_DIR, name)
    with open(path, "wb") as fh:
        fh.write(data)
    print(path)


# --- 書き込み系（毎回承認が要る）---------------------------------------------

def resolve_upload_source(path):
    """アップロード元を検証する。プロジェクト（カレント）配下の通常ファイルで、機密になり得るものは不可。"""
    root = os.path.realpath(os.getcwd())
    real = os.path.realpath(path)
    if os.path.commonpath([root, real]) != root:
        sys.exit(f"error: {path!r} is outside the project directory")
    if not os.path.isfile(real):
        sys.exit(f"error: {path!r} is not a regular file")
    rel_parts = os.path.relpath(real, root).split(os.sep)
    if DENY_UPLOAD_DIRS.intersection(rel_parts[:-1]):
        sys.exit(f"error: refusing to upload from {os.sep.join(rel_parts[:-1])!r}")
    base = os.path.basename(real).lower()
    if any(fnmatch.fnmatch(base, pat) for pat in DENY_UPLOAD_NAMES):
        sys.exit(f"error: refusing to upload {base!r} (looks like a secret/credential file)")
    return real


def guess_mime(path):
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def cmd_mkdir(service, args):
    if len(args) < 2:
        sys.exit("usage: mkdir <parent_id> <name>")
    parent, name = check_id(args[0]), args[1]
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


def cmd_upload(service, args):
    if len(args) < 2:
        sys.exit("usage: upload <local_file> <folder_id> [--name <name>]")
    local = resolve_upload_source(args[0])
    folder = check_id(args[1])
    name = args[3] if len(args) >= 4 and args[2] == "--name" else os.path.basename(local)
    media = MediaFileUpload(local, mimetype=guess_mime(local), resumable=True)
    f = service.files().create(
        body={"name": name, "parents": [folder]}, media_body=media,
        fields=FIELDS, supportsAllDrives=True).execute()
    print(fmt(f))


def cmd_update(service, args):
    if len(args) < 2:
        sys.exit("usage: update <file_id> <local_file>")
    file_id = check_id(args[0])
    local = resolve_upload_source(args[1])
    meta = service.files().get(fileId=file_id, fields="name, mimeType", supportsAllDrives=True).execute()
    if meta["mimeType"] == FOLDER_MIME:
        sys.exit("error: that ID is a folder")
    media = MediaFileUpload(local, mimetype=guess_mime(local), resumable=True)
    f = service.files().update(
        fileId=file_id, media_body=media, fields=FIELDS, supportsAllDrives=True).execute()
    print(fmt(f))


READ_COMMANDS = {"list": cmd_list, "search": cmd_search, "meta": cmd_meta,
                 "read": cmd_read, "download": cmd_download}
WRITE_COMMANDS = {"mkdir": cmd_mkdir, "upload": cmd_upload, "update": cmd_update}


def main(argv):
    if len(argv) < 2 or argv[0] != "--secret":
        sys.exit("usage: gdrive_cli.py --secret <aws-secret-name> <command> [args...]\n\n" + __doc__)
    secret_name = argv[1]
    rest = argv[2:]
    if len(rest) < 1 or rest[0] not in {**READ_COMMANDS, **WRITE_COMMANDS}:
        sys.exit(__doc__)
    write = rest[0] in WRITE_COMMANDS
    handler = (WRITE_COMMANDS if write else READ_COMMANDS)[rest[0]]
    try:
        handler(get_service(secret_name, write=write), rest[1:])
    except HttpError as e:
        status = getattr(e.resp, "status", "?")
        hint = ""
        if status in (403, 404):
            hint = (f" (見つからない、または共有されていません。サービスアカウント "
                    f"{SERVICE_ACCOUNT['email']} に{'編集' if write else '閲覧'}権限が必要です)")
        sys.exit(f"error: Drive API {status}{hint}")
    except IndexError:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
