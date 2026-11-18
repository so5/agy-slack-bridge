"""カレンダー・メール用プロファイル(<設定ディレクトリ>/google/NAME.json)の「方針」部分の読み書きと差分
(setup_agy_project.py の対話設定が使う)。方針は、既定値と違う項目だけをファイルに書く
(既定値が将来増えたとき、上書きしていない項目には自動で反映されるように)。トークンは、新規作成のときだけ書く。"""
import json
import os
import stat
from pathlib import Path

import google_common as gc

ALL_COMMANDS = list(gc.ALL_COMMANDS)
READ_COMMANDS = list(gc.READ_COMMANDS)
WRITE_COMMANDS = list(gc.WRITE_COMMANDS)


def default_policy() -> dict:
    return {"commands": list(ALL_COMMANDS), "write_mode": "confirm", "mail_filter": "", "timezone": "Asia/Tokyo"}


def policy_from_data(data: dict) -> dict:
    """プロファイル(JSON の dict)から方針を取り出す。書かれていない項目は既定値。"""
    d = default_policy()
    return {"commands": list(data.get("commands", d["commands"])), "write_mode": data.get("write_mode", d["write_mode"]),
            "mail_filter": data.get("mail_filter", d["mail_filter"]), "timezone": data.get("timezone", d["timezone"])}


def apply_policy(data: dict, policy: dict) -> dict:
    """data(既存のプロファイル)に方針を反映した新しい dict。既定値と同じ項目は書かない。"""
    commands, write_mode, mail_filter, timezone = gc.validate_policy(
        policy["commands"], policy["write_mode"], policy["mail_filter"], policy["timezone"])
    new = dict(data)
    ordered = [c for c in ALL_COMMANDS if c in set(commands)]
    for key, value, default in (("commands", ordered, ALL_COMMANDS), ("write_mode", write_mode, "confirm"),
                                ("mail_filter", mail_filter.strip(), ""), ("timezone", timezone, "Asia/Tokyo")):
        if value == default:
            new.pop(key, None)
        else:
            new[key] = value
    return new


def describe(policy: dict) -> list[str]:
    mode = {"confirm": "毎回、Slack の許可ボタンで確認する", "allow": "恒久許可（実行した全件を、エージェントがユーザーに報告する）"}
    return [
        "有効なコマンド: " + ", ".join(policy["commands"]),
        "書き込み系（予定の作成・更新・削除、メールの下書き）: " + mode[policy["write_mode"]],
        "メールの検索条件: " + (policy["mail_filter"] or "(なし = アカウントのメール全部)"),
        "タイムゾーン: " + policy["timezone"],
    ]


def diff(old: dict, new: dict) -> list[str]:
    out = []
    plus = [c for c in new["commands"] if c not in old["commands"]]
    minus = [c for c in old["commands"] if c not in new["commands"]]
    if plus or minus:
        out.append("コマンド: " + " ".join([f"+{c}" for c in plus] + [f"-{c}" for c in minus]))
    for label, key in (("書き込み系の扱い", "write_mode"), ("メールの検索条件", "mail_filter"), ("タイムゾーン", "timezone")):
        if old[key] != new[key]:
            out.append(f"{label}: {old[key] or '(なし)'} → {new[key] or '(なし)'}")
    return out


def loosening(old: dict, new: dict) -> list[str]:
    """old より権限が広がる変更の説明。"""
    out = []
    added = [c for c in new["commands"] if c not in old["commands"]]
    if added:
        out.append("コマンドを有効にする: " + ", ".join(added))
    if old["write_mode"] == "confirm" and new["write_mode"] == "allow":
        out.append("書き込み系を、毎回の確認なしで実行できるようにする（恒久許可）")
    if old["mail_filter"] and old["mail_filter"] != new["mail_filter"]:
        out.append("メールの検索条件を、外す・変える（見えるメールの範囲が変わる）")
    return out


def new_profile(token: "dict | None" = None, token_from: "str | None" = None) -> dict:
    """token(client_id / client_secret / refresh_token / scopes)か、token_from(別のプロファイル名)から作る。"""
    if token_from:
        return {"token_from": token_from}
    return {"token": {k: token[k] for k in ("client_id", "client_secret", "refresh_token", "scopes") if k in token}}


def read_token_file(path: Path) -> dict:
    """google-auth の authorized_user 形式のトークンファイル(token.json)から、必要なキーを取り出す。
    不正なら ValueError(値は出さない)。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("トークンのファイルを読めません（JSON ではない、または存在しない）") from None
    if not isinstance(data, dict):
        raise ValueError("トークンのファイルが JSON のオブジェクトではありません")
    token = {k: data[k] for k in ("client_id", "client_secret", "refresh_token") if isinstance(data.get(k), str) and data.get(k)}
    missing = [k for k in ("client_id", "client_secret", "refresh_token") if k not in token]
    if missing:
        raise ValueError("トークンのファイルに足りないキーがあります: " + ", ".join(missing))
    scopes = data.get("scopes")
    if isinstance(scopes, list) and all(isinstance(s, str) for s in scopes):
        token["scopes"] = scopes
    return token


def read_profile(path: Path) -> "dict | None":
    """JSON のまま読む(無ければ None)。壊れていれば ValueError。"""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_profile(path: Path, data: dict) -> None:
    """検証してから、アトミックに 0600 で書く(置き場は 0700)。CLI は、自分以外が書き込める場所の
    プロファイルを拒否するので、ディレクトリの group/other 書き込みも外す。"""
    gc.parse_profile(data)  # 不正なら ProfileError。書かない
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
