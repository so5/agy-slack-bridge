"""agy の machine-wide な settings.json(~/.gemini/antigravity-cli/settings.json)の
扱い。install.py(インストール先・設定ディレクトリを守る deny)と
setup_agy_project.py(プロジェクトごとの trustedWorkspaces など)が共有する。

ここで書くのは deny と trustedWorkspaces だけ。許可(allow)は決して書かない
(ブリッジの方針: プロジェクトをまたいで使える許可を作らない)。"""
import json
from pathlib import Path

GLOBAL_SETTINGS_PATH = Path("~/.gemini/antigravity-cli/settings.json").expanduser()
# 全プロジェクトに効く agy の hooks.json。ここにフックを一つ置く(プロジェクトごとの
# .agents/hooks.json は使わない)。フック自身は、設定ディレクトリに状態ディレクトリがある
# プロジェクトにしか働かない(setup_agy_project.py が作る)。
GLOBAL_HOOKS_PATH = Path("~/.gemini/config/hooks.json").expanduser()
HOOK_NAME = "script-integrity"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def ensure_list_entries(container: dict, key: str, entries: list[str]) -> list[str]:
    """container[key] にリストを用意し、無いものだけ末尾に足す。足したものを返す。"""
    current = container.setdefault(key, [])
    added = [e for e in entries if e not in current]
    current.extend(added)
    return added


def machine_wide_deny_rules(prefix: Path, config_dir: Path, home: Path) -> list[str]:
    """プロジェクトを問わず、常にエージェントに禁じる内容(ブリッジ/agy 自身の内部)。
    settings.json の既存エントリと同じ展開済みの絶対パスで持つ(でないと別文字列として
    重複登録されてしまう)。"""
    return [
        f"read_file({prefix})",
        f"write_file({prefix})",
        f"read_file({config_dir})",
        f"write_file({config_dir})",
        f"read_file({home}/.gemini/antigravity-cli/settings.json)",
        f"read_file({home}/.gemini/config)",
    ]


def install_global_hook(prefix: Path, config_dir: Path) -> None:
    """インストール先のフックを、グローバルの hooks.json に登録する(他のフックは残す)。"""
    template = json.loads((prefix / "tools" / "hooks.json").read_text(encoding="utf-8"))
    entry = template[HOOK_NAME]
    hook = prefix / "tools" / "hooks" / "script_integrity.py"
    for group in entry.get("PreToolUse", []):
        for h in group.get("hooks", []):
            h["command"] = f"python3 {hook} --config-dir {config_dir}"
    data = load_json(GLOBAL_HOOKS_PATH)
    if data.get(HOOK_NAME) == entry:
        print(f"  [OK] {GLOBAL_HOOKS_PATH} は対応済み")
        return
    data[HOOK_NAME] = entry
    save_json(GLOBAL_HOOKS_PATH, data)
    print(f"  [書込] {GLOBAL_HOOKS_PATH} にフック {HOOK_NAME} を登録（{hook}）")


def update_settings(deny_rules: list[str], trusted_workspaces: "list[str] | None" = None) -> None:
    data = load_json(GLOBAL_SETTINGS_PATH)
    permissions = data.setdefault("permissions", {})
    added_deny = ensure_list_entries(permissions, "deny", deny_rules)
    added_ws = ensure_list_entries(data, "trustedWorkspaces", trusted_workspaces) if trusted_workspaces else []
    if added_deny or added_ws:
        save_json(GLOBAL_SETTINGS_PATH, data)
    for e in added_deny:
        print(f"  [追加] settings.json permissions.deny: {e}")
    for e in added_ws:
        print(f"  [追加] settings.json trustedWorkspaces: {e}")
    if not added_deny and not added_ws:
        print("  [OK] settings.json は対応済み")
