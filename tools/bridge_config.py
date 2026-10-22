"""config.yaml / env を、コメントを残したままテキストで書き換える小さな部品
(setup_agy_project.py が使う)。PyYAML の dump で全体を書き直すと、利用者が書いた
コメントが消えるため、書き換える範囲(`channels:` 配下の 1 エントリ、env の 1 行)だけを
置き換える。書き換えた結果は必ず読み戻して、意図した内容になっているか確かめる。"""
import re

import yaml

# bridge.py が読むチャンネル設定のキー(順序は書き出し順)。
ENTRY_KEY_ORDER = ["project", "name", "display_name", "icon_emoji", "icon_url",
                   "model", "effort", "watched_config_files", "telemetry"]


class ConfigEditError(Exception):
    pass


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def render_entry(channel_id: str, entry: dict, indent: int = 2) -> list[str]:
    ordered = {k: entry[k] for k in ENTRY_KEY_ORDER if k in entry}
    ordered.update({k: v for k, v in entry.items() if k not in ordered})
    body = yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False, default_flow_style=False)
    pad = " " * indent
    return [f"{pad}{channel_id}:"] + [f"{pad}  {l}" if l else l for l in body.splitlines()]


def read_channels(text: str) -> dict:
    data = yaml.safe_load(text) or {}
    return data.get("channels") or {}


def upsert_channel(text: str, channel_id: str, entry: dict) -> str:
    lines = text.splitlines()
    head = next((i for i, l in enumerate(lines) if re.match(r"^channels:", l)), None)
    new_entry = render_entry(channel_id, entry)
    if head is None:
        out = lines + ([""] if lines and lines[-1].strip() else []) + ["channels:"] + new_entry
    else:
        rest = lines[head][len("channels:"):].split("#")[0].strip()
        if rest == "{}":
            lines[head] = "channels:"
        elif rest:
            raise ConfigEditError("config.yaml の channels がブロック形式ではありません。手で編集してください。")
        end = next((j for j in range(head + 1, len(lines)) if lines[j].strip() and _indent(lines[j]) == 0),
                   len(lines))
        while end > head + 1 and not _is_content(lines[end - 1]):
            end -= 1  # 末尾の空行・コメントは、次のトップレベル項目のもの
        start = next((i for i in range(head + 1, end)
                      if re.match(rf"^\s+(['\"]?){re.escape(channel_id)}\1\s*:", lines[i])), None)
        if start is None:
            out = lines[:end] + new_entry + lines[end:]
        else:
            base = _indent(lines[start])
            stop = next((j for j in range(start + 1, end)
                         if _is_content(lines[j]) and _indent(lines[j]) <= base), end)
            while stop > start + 1 and not _is_content(lines[stop - 1]):
                stop -= 1
            out = lines[:start] + render_entry(channel_id, entry, base) + lines[stop:]
    result = "\n".join(out) + "\n"
    if read_channels(result).get(channel_id) != entry:
        raise ConfigEditError("書き換えた config.yaml を読み戻すと意図した内容になりませんでした。書き込みを中止します。")
    return result


def set_env_value(text: str, key: str, value: str) -> str:
    """`KEY=...` の行を置き換える(コメントアウトされた `# KEY=...` の行は、その下に
    実際の行を足す)。無ければ末尾に足す。"""
    if "\n" in value or "\r" in value:
        raise ConfigEditError("env の値に改行は使えません。")
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if re.match(rf"^{re.escape(key)}=", l):
            lines[i] = f"{key}={value}"
            break
    else:
        at = next((i + 1 for i, l in enumerate(lines) if re.match(rf"^#\s*{re.escape(key)}=", l)), len(lines))
        lines.insert(at, f"{key}={value}")
    return "\n".join(lines) + "\n"


def get_env_value(text: str, key: str) -> "str | None":
    for l in text.splitlines():
        m = re.match(rf"^{re.escape(key)}=(.*)$", l)
        if m:
            return m.group(1)
    return None
