"""カレンダー・メールの CLI(google_calendar.py / google_mail.py)が共有する部分:
プロファイル(<設定ディレクトリ>/google/NAME.json)の読み込みと検証、資格情報、実行記録。

プロファイルの形(未知のキーはエラー。綴り間違いで制限が黙って効かなくなるのを防ぐ):
  {
    "token": {"client_id": ..., "client_secret": ..., "refresh_token": ...,
              "scopes": [...] (任意)},
    "token_from": "<別のプロファイル名>",   # token の代わりに、別のプロファイルのトークンを使う
    "mail_filter": "(to:a@example.com OR from:example.com)",   # 任意。検索と、メールの読み出しに足す条件
    "commands": ["calendar.list", "mail.read", ...],            # 任意。省略すると全部
    "write_mode": "confirm" | "allow",                          # 書き込み系の扱い(既定 confirm)
    "timezone": "Asia/Tokyo"                                    # 任意
  }

設計上の制約(エージェントからは書き込み不可にしてある。変更しないこと):
  - トークンはプロファイルの中だけにあり、エージェントは読めない(設定ディレクトリは deny)。
  - 実行のたびに、<設定ディレクトリ>/google/actions.log に 1 行(JSON)で記録する
    (何を実行したか。本文などの中身は記録しない)。
  - mail_filter は絞り込みであって、権限の境界ではない(同じトークンを使う別のプロファイルは、
    条件なしで同じメールを読める)。
"""
import json
import os
import re
import stat
import sys
import time

PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
OAUTH_TOKEN_URI_DEFAULT = "https://oauth2.googleapis.com/token"
MAX_FILTER_CHARS = 1000

CALENDAR_READ = ("list", "search")
CALENDAR_WRITE = ("create", "update", "delete")
MAIL_READ = ("search", "read", "attachment")
MAIL_WRITE = ("draft-reply", "draft-new")
READ_COMMANDS = [f"calendar.{c}" for c in CALENDAR_READ] + [f"mail.{c}" for c in MAIL_READ]
WRITE_COMMANDS = [f"calendar.{c}" for c in CALENDAR_WRITE] + [f"mail.{c}" for c in MAIL_WRITE]
ALL_COMMANDS = READ_COMMANDS + WRITE_COMMANDS
WRITE_MODES = ("confirm", "allow")

DEFAULT_SCOPES = {
    "calendar": ["https://www.googleapis.com/auth/calendar",
                 "https://www.googleapis.com/auth/meetings.space.settings"],
    "mail": ["https://www.googleapis.com/auth/gmail.modify"],
}
_SCOPE_PREFIX = {"calendar": ("https://www.googleapis.com/auth/calendar",
                              "https://www.googleapis.com/auth/meetings"),
                 "mail": ("https://www.googleapis.com/auth/gmail",)}


class ProfileError(Exception):
    """プロファイル/資格情報まわりの失敗。メッセージに秘密を含めないこと。"""


def config_dir() -> str:
    return os.environ.get("AGY_BRIDGE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".config", "agy-slack-bridge")


def profile_dir() -> str:
    return os.path.join(config_dir(), "google")


def log_path() -> str:
    return os.path.join(profile_dir(), "actions.log")


def _check_profile_file_mode(path: str) -> None:
    """ポリシーの改ざん防止: 自分以外が書けるプロファイルは拒否。読めるだけなら警告(トークンが入っている)。"""
    for p in (path, os.path.dirname(path)):
        if os.stat(p).st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ProfileError(f"{p} が group/other から書き込み可能です。chmod go-w してください")
    if os.stat(path).st_mode & (stat.S_IRGRP | stat.S_IROTH):
        print(f"warning: {path} が group/other から読めます（トークンが入っています。chmod 600 を推奨）", file=sys.stderr)


def _str_list(value, what: str) -> list:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ProfileError(f"プロファイルの {what} は文字列のリストにしてください")
    return value


def validate_token(token) -> dict:
    if not isinstance(token, dict):
        raise ProfileError("プロファイルの token は JSON のオブジェクトにしてください")
    missing = [k for k in ("client_id", "client_secret", "refresh_token")
               if not isinstance(token.get(k), str) or not token.get(k)]
    if missing:
        raise ProfileError(f"プロファイルの token に足りないキーがあります: {', '.join(missing)}")
    if "scopes" in token:
        _str_list(token["scopes"], "token.scopes")
    return token


def validate_policy(commands, write_mode, mail_filter, timezone) -> tuple:
    commands = _str_list(commands, "commands")
    bad = set(commands) - set(ALL_COMMANDS)
    if bad:
        raise ProfileError(f"プロファイルの commands に未知のコマンドがあります: {sorted(bad)}")
    if write_mode not in WRITE_MODES:
        raise ProfileError(f"プロファイルの write_mode は {' か '.join(WRITE_MODES)} のどちらかにしてください")
    if not isinstance(mail_filter, str) or len(mail_filter) > MAX_FILTER_CHARS:
        raise ProfileError(f"プロファイルの mail_filter は {MAX_FILTER_CHARS} 文字以内の文字列にしてください")
    if not isinstance(timezone, str) or not timezone:
        raise ProfileError("プロファイルの timezone は文字列にしてください")
    return commands, write_mode, mail_filter, timezone


def parse_profile(data: dict) -> dict:
    """プロファイル(dict)を検証し、既定値を補って返す(token_from の解決は load_profile)。"""
    if not isinstance(data, dict):
        raise ProfileError("プロファイルは JSON のオブジェクトにしてください")
    unknown = set(data) - {"token", "token_from", "mail_filter", "commands", "write_mode", "timezone"}
    if unknown:
        raise ProfileError(f"プロファイルに未知のキーがあります: {sorted(unknown)}")
    if ("token" in data) == ("token_from" in data):
        raise ProfileError("プロファイルには token か token_from のどちらか一方を指定してください")
    token_from = data.get("token_from")
    if token_from is not None and not (isinstance(token_from, str) and PROFILE_NAME_RE.match(token_from)):
        raise ProfileError("プロファイルの token_from は、プロファイル名(英数字と _ -)にしてください")
    token = validate_token(data["token"]) if "token" in data else None
    commands, write_mode, mail_filter, timezone = validate_policy(
        data.get("commands", list(ALL_COMMANDS)), data.get("write_mode", "confirm"),
        data.get("mail_filter", ""), data.get("timezone", "Asia/Tokyo"))
    return {"token": token, "token_from": token_from, "commands": set(commands), "write_mode": write_mode,
            "mail_filter": mail_filter.strip(), "timezone": timezone}


def _read_profile_file(name: str) -> dict:
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


def load_profile(name: str) -> dict:
    profile = _read_profile_file(name)
    profile["name"] = name
    if profile["token_from"]:
        other = _read_profile_file(profile["token_from"])
        if not other["token"]:
            raise ProfileError("token_from で参照するプロファイルは、自分のトークンを持つものにしてください"
                               "（参照の連鎖はできません）")
        profile["token"] = other["token"]
    return profile


def scopes_for(profile: dict, tool: str) -> list:
    """要求するスコープ: 同意で与えられたもの(token.scopes)のうち、そのツールのものだけ(無ければ既定)。"""
    return [s for s in profile["token"].get("scopes", []) if s.startswith(_SCOPE_PREFIX[tool])] or DEFAULT_SCOPES[tool]


def credentials(profile: dict, tool: str):
    """tool('calendar' か 'mail')用の資格情報。アクセストークンは保存せず、実行ごとにリフレッシュで取る。"""
    from google.oauth2.credentials import Credentials

    token = profile["token"]
    scopes = scopes_for(profile, tool)
    return Credentials(None, refresh_token=token["refresh_token"], client_id=token["client_id"],
                       client_secret=token["client_secret"], token_uri=OAUTH_TOKEN_URI_DEFAULT, scopes=scopes)


def require_command(profile: dict, tool: str, command: str) -> None:
    if f"{tool}.{command}" not in profile["commands"]:
        sys.exit(f"error: このプロファイルでは {tool} の {command} は無効です")


def log_action(profile: dict, tool: str, command: str, ok: bool, detail: "dict | None" = None) -> None:
    """実行の記録。失敗しても本体の処理は止めない(標準エラーに出すだけ)。"""
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "profile": profile["name"], "tool": tool,
             "command": command, "ok": ok, "detail": detail or {}}
    try:
        os.makedirs(profile_dir(), mode=0o700, exist_ok=True)
        fd = os.open(log_path(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"warning: 実行の記録を書けませんでした（{e.__class__.__name__}）", file=sys.stderr)


INSTALL_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def project_dir() -> str:
    """カレントディレクトリ = プロジェクトのルート。インストール先の中で実行されたときは拒否する。"""
    cwd = os.path.realpath(os.getcwd())
    if os.path.commonpath([cwd, INSTALL_ROOT]) == INSTALL_ROOT:
        sys.exit(f"error: the current directory ({cwd}) is inside the bridge install directory; "
                 "run this command from the project directory (the one agy opened)")
    return cwd


def safe_name(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r"[^\w.\-぀-ヿ㐀-鿿]+", "_", name).strip("._")
    return name or "attachment"


def http_error_message(e) -> str:
    """HttpError のメッセージ(本文は出さない。ステータスと、見つからない・権限が無いの案内だけ)。"""
    status = getattr(getattr(e, "resp", None), "status", "?")
    hint = ""
    if status in (403, 404):
        hint = " (見つからない、またはこのアカウントに権限がありません)"
    elif status == 401:
        hint = " (認証に失敗しました。トークンが失効していないか確認してください)"
    return f"error: Google API {status}{hint}"
