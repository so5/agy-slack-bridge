"""安全機構の確認と自動修復。bridge.py(agy を呼ぶ前)と setup_agy_project.py(状態の表示)が共有する。

確認するのは「在るかどうか」だけ。安全規則の中身や許可フォルダを、ユーザーが意図して緩めることは
妨げない。修復するのは、セットアップが作るものと同じ内容を、無いところに戻すことだけ(deny の追加、
フックの登録、安全規則へのリンク、状態ディレクトリ)。ユーザーの実体ファイル(frontmatter の無い
安全規則など)は上書きしないので、そういうものは修復できない問題として報告する。"""
import contextlib
import io
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import agy_settings

RULES_RELPATH = Path(".agents") / "rules" / "agent-safety-rules.md"
HOOK_NAME = agy_settings.HOOK_NAME


@dataclass(frozen=True)
class Problem:
    code: str          # rules / rules_frontmatter / state_dir / hook / hook_script / deny_prefix / deny_config
    message: str
    fixable: bool


def _deny_forms(path: Path) -> set:
    return {str(path), str(path.resolve()), os.path.realpath(path)}


def check(prefix: Path, config_dir: Path, root: "Path | None", state_dir: "Path | None",
          hooks_path: Path, settings_path: Path) -> "list[Problem]":
    """欠けている安全機構。空なら問題なし。root が None(ワークスペースの無いプロジェクト)なら、
    プロジェクト側(安全規則・状態ディレクトリ)は見ない。"""
    problems = []
    if root is not None:
        rules = root / RULES_RELPATH
        try:
            text = rules.read_text(encoding="utf-8")
        except OSError:
            problems.append(Problem("rules", f"安全規則がありません: {rules}",
                                    (prefix / "tools" / "agent-safety-rules.md").is_file()
                                    and not (rules.exists() and not rules.is_symlink())))
        else:
            head = text.split("\n---", 1)[0] if text.startswith("---") else ""
            if not re.search(r"^trigger:\s*always_on\s*$", head, re.MULTILINE):
                problems.append(Problem("rules_frontmatter",
                                        f"安全規則の先頭に `trigger: always_on` がありません: {rules}", False))
        if state_dir is not None and not state_dir.is_dir():
            problems.append(Problem("state_dir", f"フックの状態ディレクトリがありません: {state_dir}", True))
    script = prefix / "tools" / "hooks" / "script_integrity.py"
    try:
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        hooks = {}
    if not (isinstance(hooks, dict) and hooks.get(HOOK_NAME)):
        problems.append(Problem("hook", f"フック {HOOK_NAME} が登録されていません: {hooks_path}",
                                script.is_file() and (prefix / "tools" / "hooks.json").is_file()))
    if not script.is_file():
        problems.append(Problem("hook_script", f"フックのスクリプトがありません: {script}", False))
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        settings = {}
    deny = set(((settings.get("permissions") or {}).get("deny") or []) if isinstance(settings, dict) else [])
    for code, label, path in (("deny_prefix", "インストール先", prefix), ("deny_config", "設定ディレクトリ", config_dir)):
        if not any(f"write_file({f})" in deny for f in _deny_forms(path)):
            problems.append(Problem(code, f"{label}への書き込みを禁じる deny が settings.json にありません: write_file({path})",
                                    True))
    return problems


def repair(prefix: Path, config_dir: Path, root: "Path | None", state_dir: "Path | None",
           hooks_path: Path, settings_path: Path, problems: "list[Problem]") -> "list[str]":
    """修復できる問題を直す。直したことの説明を返す(失敗したものは含まない)。"""
    done = []
    codes = {p.code for p in problems if p.fixable}
    quiet = contextlib.redirect_stdout(io.StringIO())
    if "rules" in codes and root is not None:
        dest = root / RULES_RELPATH
        try:
            if dest.is_symlink():
                dest.unlink()                      # リンク切れ
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(prefix / "tools" / "agent-safety-rules.md")
            done.append(f"安全規則のリンクを作り直しました: {dest}")
        except OSError:
            pass
    if "state_dir" in codes and state_dir is not None:
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            done.append(f"フックの状態ディレクトリを作り直しました: {state_dir}")
        except OSError:
            pass
    if "hook" in codes:
        try:
            with quiet:
                wrote = agy_settings.install_global_hook(prefix, config_dir, hooks_path)
            if wrote:
                done.append(f"フック {HOOK_NAME} を登録し直しました: {hooks_path}")
        except (OSError, KeyError, json.JSONDecodeError):
            pass
    if codes & {"deny_prefix", "deny_config"}:
        try:
            with quiet:
                added = agy_settings.update_settings(
                    agy_settings.machine_wide_deny_rules(prefix, config_dir, Path.home()), None, settings_path)
            if added:
                done.append("settings.json の deny を戻しました: " + ", ".join(added))
        except (OSError, json.JSONDecodeError):
            pass
    return done
