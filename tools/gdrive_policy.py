"""Drive CLI プロファイルの「方針」部分(有効なコマンド、許可フォルダ、アップロードの
拒否パターン)の読み書きと差分(setup_agy_project.py の対話設定が使う)。

プロファイルのうち資格情報(kind / credentials)とスコープは、新規作成のときだけ書き、
既存のプロファイルでは触らない。方針は、既定値と違う項目だけをファイルに書く
(既定値が将来増えたとき、上書きしていない項目には自動で反映されるように)。"""
import json
import os
import stat
from pathlib import Path

import gdrive_cli as g

ALL_COMMANDS = list(g.READ_COMMAND_NAMES + g.WRITE_COMMAND_NAMES)


def default_policy() -> dict:
    return {"commands": list(ALL_COMMANDS), "allowed_folders": [],
            "deny_names": list(g.DENY_UPLOAD_NAMES_DEFAULT), "deny_dirs": list(g.DENY_UPLOAD_DIRS_DEFAULT)}


def policy_from_data(data: dict) -> dict:
    """プロファイル(JSON の dict)から方針を取り出す。書かれていない項目は既定値。"""
    deny = data.get("upload_deny") or {}
    return {"commands": list(data.get("commands", ALL_COMMANDS)),
            "allowed_folders": list(data.get("allowed_folders", [])),
            "deny_names": list(deny.get("names", g.DENY_UPLOAD_NAMES_DEFAULT)),
            "deny_dirs": list(deny.get("dirs", g.DENY_UPLOAD_DIRS_DEFAULT))}


def apply_policy(data: dict, policy: dict) -> dict:
    """data(既存のプロファイル)に方針を反映した新しい dict。既定値と同じ項目は書かない。"""
    commands, folders, names, dirs = g.validate_policy(
        policy["commands"], policy["allowed_folders"], policy["deny_names"], policy["deny_dirs"])
    new = dict(data)
    ordered = [c for c in ALL_COMMANDS if c in set(commands)]
    if ordered == ALL_COMMANDS:
        new.pop("commands", None)
    else:
        new["commands"] = ordered
    if folders:
        new["allowed_folders"] = folders
    else:
        new.pop("allowed_folders", None)
    deny = dict(new.get("upload_deny") or {})
    for key, value, default in (("names", names, g.DENY_UPLOAD_NAMES_DEFAULT),
                                ("dirs", dirs, g.DENY_UPLOAD_DIRS_DEFAULT)):
        if value == list(default):
            deny.pop(key, None)
        else:
            deny[key] = value
    if deny:
        new["upload_deny"] = deny
    else:
        new.pop("upload_deny", None)
    return new


def describe(policy: dict) -> list[str]:
    folders = policy["allowed_folders"]
    return [
        "有効なコマンド: " + ", ".join(policy["commands"]),
        "許可フォルダ: " + (", ".join(folders) if folders else "(制限なし = 認証情報で見える範囲すべて)"),
        "アップロード拒否(ファイル名): " + ", ".join(policy["deny_names"]),
        "アップロード拒否(ディレクトリ): " + ", ".join(policy["deny_dirs"]),
    ]


def _delta(label: str, old: list, new: list) -> "str | None":
    plus, minus = [x for x in new if x not in old], [x for x in old if x not in new]
    if not plus and not minus:
        return None
    parts = [f"+{x}" for x in plus] + [f"-{x}" for x in minus]
    return f"{label}: " + " ".join(parts)


def diff(old: dict, new: dict) -> list[str]:
    lines = [_delta("コマンド", old["commands"], new["commands"]),
             _delta("許可フォルダ", old["allowed_folders"], new["allowed_folders"]),
             _delta("拒否ファイル名", old["deny_names"], new["deny_names"]),
             _delta("拒否ディレクトリ", old["deny_dirs"], new["deny_dirs"])]
    return [l for l in lines if l]


def loosening(old: dict, new: dict) -> list[str]:
    """old より権限が広がる変更(new が old より緩い点)の説明。"""
    out = []
    added = [c for c in new["commands"] if c not in old["commands"]]
    if added:
        out.append("コマンドを有効にする: " + ", ".join(added))
    if old["allowed_folders"] and not new["allowed_folders"]:
        out.append("許可フォルダの制限を解除する(認証情報で見える範囲すべてが対象になる)")
    elif old["allowed_folders"]:
        extra = [f for f in new["allowed_folders"] if f not in old["allowed_folders"]]
        if extra:
            out.append("許可フォルダを増やす: " + ", ".join(extra))
    for label, key in (("拒否ファイル名", "deny_names"), ("拒否ディレクトリ", "deny_dirs")):
        gone = [x for x in old[key] if x not in new[key]]
        if gone:
            out.append(f"{label}のパターンを外す: " + ", ".join(gone))
    return out


def new_profile(kind: str, credential_argv: list[str], scopes: "dict | None" = None) -> dict:
    data = {"kind": kind, "credentials": {"command": credential_argv}}
    if scopes:
        data["scopes"] = scopes
    return data


def read_profile(path: Path) -> "dict | None":
    """JSON のまま読む(無ければ None)。壊れていれば ValueError。"""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_profile(path: Path, data: dict) -> None:
    """検証してから、アトミックに 0600 で書く(置き場は 0700)。gdrive_cli.py は、自分以外が
    書き込める場所のプロファイルを拒否するので、ディレクトリの group/other 書き込みも外す。"""
    g.parse_profile(data)  # 不正なら ProfileError。書かない
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = stat.S_IMODE(os.stat(path.parent).st_mode)
    if mode & 0o022:
        os.chmod(path.parent, mode & ~0o022)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)
