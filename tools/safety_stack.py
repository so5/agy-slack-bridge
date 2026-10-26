"""安全機構の確認と自動修復。bridge.py(agy を呼ぶ前)と setup_agy_project.py(状態の表示)が共有する。

確認するのは「在るかどうか」だけ。安全規則の中身や許可フォルダを、ユーザーが意図して緩めることは
妨げない。修復するのは、セットアップが作るものと同じ内容を、無いところに戻すことだけ(deny の追加、
フックの登録、安全規則へのリンク、状態ディレクトリ)。ユーザーの実体の安全規則は、
`trigger: always_on` の行だけを直し(他の内容はそのまま)、リンクではなく別の場所を指す
シンボリックリンクのように、触れないものは修復できない問題として報告する。"""
import contextlib
import io
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import agy_settings

RULES_RELPATH = Path(".agents") / "rules" / "agent-safety-rules.md"
HOOK_NAME = agy_settings.HOOK_NAME


# install.py が作る、安全機構の本体。設定ディレクトリ(エージェントは読み書きできない)に原本の
# コピーを持ち、インストール先から消えたら、そこから戻す。
SAFETY_FILES = ("tools/hooks/script_integrity.py", "tools/hooks.json", "tools/agent-safety-rules.md")
BACKUP_DIRNAME = "safety-backup"


def backup_safety_files(prefix: Path, config_dir: Path) -> "list[str]":
    """インストール先の SAFETY_FILES を、設定ディレクトリに控える(install.py が呼ぶ)。"""
    copied = []
    for rel in SAFETY_FILES:
        src = prefix / rel
        if not src.is_file():
            continue
        dest = config_dir / BACKUP_DIRNAME / rel
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(src, dest)
        dest.chmod(0o600)
        copied.append(rel)
    return copied


def _available(prefix: Path, config_dir: Path, rel: str) -> bool:
    return (prefix / rel).is_file() or (config_dir / BACKUP_DIRNAME / rel).is_file()


def restore_missing(prefix: Path, config_dir: Path) -> "list[str]":
    """インストール先から消えた SAFETY_FILES を、控えから戻す。戻したものを返す。"""
    restored = []
    for rel in SAFETY_FILES:
        dest, backup = prefix / rel, config_dir / BACKUP_DIRNAME / rel
        if dest.is_file() or not backup.is_file():
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(backup, dest)
            dest.chmod(0o444)
            restored.append(rel)
        except OSError:
            pass
    return restored


@dataclass(frozen=True)
class Problem:
    code: str          # rules / rules_frontmatter / state_dir / hook / hook_script / deny_prefix / deny_config
    message: str
    fixable: bool


_TRIGGER_RE = re.compile(r"^trigger:.*$", re.MULTILINE)


def with_always_on(text: str) -> str:
    """frontmatter に `trigger: always_on` を入れた本文を返す(他の内容は変えない)。
    frontmatter が無ければ先頭に付け、あって trigger が別の値・無しなら、その行だけ直す。"""
    m = re.match(r"---[ \t]*\r?\n(?:(.*?)\r?\n)?---[ \t]*(?:\r?\n|$)", text, re.DOTALL)
    if not m:
        return "---\ntrigger: always_on\n---\n" + text
    head = m.group(1)
    if head is None:                                    # 空の frontmatter
        i = text.index("\n") + 1
        return text[:i] + "trigger: always_on\n" + text[i:]
    if _TRIGGER_RE.search(head):
        head = _TRIGGER_RE.sub("trigger: always_on", head, count=1)
    else:
        head = "trigger: always_on\n" + head
    return text[:m.start(1)] + head + text[m.end(1):]


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
                                    _available(prefix, config_dir, "tools/agent-safety-rules.md")
                                    and not (rules.exists() and not rules.is_symlink())))
        else:
            head = text.split("\n---", 1)[0] if text.startswith("---") else ""
            if not re.search(r"^trigger:\s*always_on\s*$", head, re.MULTILINE):
                problems.append(Problem("rules_frontmatter",
                                        f"安全規則の先頭に `trigger: always_on` がありません: {rules}",
                                        not rules.is_symlink()))
        if state_dir is not None and not state_dir.is_dir():
            problems.append(Problem("state_dir", f"フックの状態ディレクトリがありません: {state_dir}", True))
    script = prefix / "tools" / "hooks" / "script_integrity.py"
    try:
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        hooks = {}
    if not (isinstance(hooks, dict) and hooks.get(HOOK_NAME)):
        problems.append(Problem("hook", f"フック {HOOK_NAME} が登録されていません: {hooks_path}",
                                _available(prefix, config_dir, "tools/hooks/script_integrity.py")
                                and _available(prefix, config_dir, "tools/hooks.json")))
    if not script.is_file():
        problems.append(Problem("hook_script", f"フックのスクリプトがありません: {script}",
                                _available(prefix, config_dir, "tools/hooks/script_integrity.py")))
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
    for rel in restore_missing(prefix, config_dir):          # 先に原本を戻す(以降の修復が使うため)
        done.append(f"インストール先のファイルを控えから戻しました: {prefix / rel}")
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
    if "rules_frontmatter" in codes and root is not None:
        dest = root / RULES_RELPATH
        try:
            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_text(with_always_on(dest.read_text(encoding="utf-8")), encoding="utf-8")
            tmp.chmod(dest.stat().st_mode & 0o777)
            tmp.replace(dest)
            done.append(f"安全規則の先頭に `trigger: always_on` を入れました（他の内容はそのまま）: {dest}")
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
