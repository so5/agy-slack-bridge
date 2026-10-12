#!/usr/bin/env python3
"""
agy プロジェクトのブートストラップ(新規作成・修復とも、このコマンド1本)
================================================================================
Slack ブリッジ経由で動かす agy プロジェクトに要る設定一式(共有の安全規則、
script-integrity フック、machine-wide の deny/trustedWorkspaces、
Google Drive の許可リストなど)を、このコマンド1回で揃える。

冪等です。何度実行しても安全で、何かが壊れた・ずれた場合は、このコマンドを
もう一度実行するだけで直ります。今後セーフガードを追加・変更するときも、
このスクリプト自体を直して、対象プロジェクトに対して再実行するだけで済みます
(シンボリックリンク先の `.agents/` 配下の共有ファイルを直す場合も同様)。

使い方:
    .venv/bin/python scripts/setup_agy_project.py <対象プロジェクトのパス> [--with-gdrive]

    --with-gdrive   accountant に実在する Google Drive 読み書き用の固定コマンド
                     (既定では scripts/gdrive_cli.py。"gdrive_cli.py" という名前は
                     このリポジトリでそう呼んでいるだけの一スクリプト名であり、
                     標準的な名称ではない)を共有し、その読み取り系サブコマンドを
                     対象プロジェクトの許可リストに恒久登録する。
                     配置先の名前/パスを変えたい場合は --gdrive-script で指定する。

このコマンドが揃えるもの:
  1. .agents/ 配下の共有ファイル(安全規則・ツール一覧・hooks・任意でDrive CLI)
     へのシンボリックリンクと、AGENTS.md への読み込み行。
  2. scratch/ を丸ごと除外する .gitignore の調整
     (そこが .gitignore に一切出てきていない場合のみ - 既に何らかの scratch/
     ルールがあれば、それは導入者の意図したものとして触らない)。
     scratch/ は使い捨て置き場であり、恒久的な許可（下記3・4）の対象には
     ならないので、ここは git で追跡する必要が無い。
  3. machine-wide の ~/.gemini/antigravity-cli/settings.json:
     - ブリッジ/agy 自身の内部を読ませない deny ルール。
     - --with-gdrive 時、対象プロジェクトの Drive CLI スクリプトと .venv への
       write_file deny。
     - trustedWorkspaces に対象ディレクトリを追加。
  4. --with-gdrive 時:
     - .agents/.permanent-commands.json（script-integrity フックが
       「git commit 必須」と判定する対象の一覧。gitignore 対象、git には
       乗らない）に、Drive CLI の読み取り系サブコマンドを登録。
     - 対象プロジェクトが agy に登録済みなら
       (~/.gemini/config/projects/*.json から folderUri で特定)、
       同じサブコマンドを恒久許可として permissionGrants にも追加。

このコマンドの対象外(別の理由で自動化していないもの):
  - agy へのプロジェクトの初回登録そのもの(`agy --project <名前> -p "hi"` 等)。
    対象プロジェクトが未登録のときは、その旨を表示して終わる
    (--with-gdrive の許可登録だけがスキップされ、他は適用される)。
    登録後にこのコマンドを再実行すれば、許可登録も反映される。
  - agy-slack-bridge の config.yaml へのチャンネル追加(別リポジトリ。
    Slack チャンネルIDが要るため)。agy-slack-bridge の README を参照。
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

SELF_DIR = Path(__file__).resolve().parent.parent  # accountant/ itself
CANONICAL_AGENTS_DIR = SELF_DIR / ".agents"
GLOBAL_SETTINGS_PATH = Path("~/.gemini/antigravity-cli/settings.json").expanduser()
AGY_PROJECTS_DIR = Path("~/.gemini/config/projects").expanduser()
PERMANENT_COMMANDS_REL = Path(".agents") / ".permanent-commands.json"

# machine-wide: 対象を問わず、常にこの内容を禁止する(ブリッジ/agy自身の内部)。
# settings.json の既存エントリに ~ 展開前の文字列が使われていないため、ここも
# 展開済みの絶対パスで持つ(でないと別文字列として重複登録されてしまう)。
_HOME = Path.home()
GLOBAL_DENY_ALWAYS = [
    f"read_file({_HOME}/WORK/agy-slack-bridge)",
    f"read_file({_HOME}/.config/agy-slack-bridge)",
    f"read_file({_HOME}/.gemini/antigravity-cli/settings.json)",
    f"read_file({_HOME}/.gemini/config)",
]
GDRIVE_READONLY_SUBCOMMANDS = ["list", "search", "meta", "read", "download"]
# このリポジトリでの固定名。他プロジェクトに展開する際の既定値に過ぎず、
# --gdrive-script で上書きできる(「gdrive_cli.py」は標準名ではない)。
DEFAULT_GDRIVE_SCRIPT = "scripts/gdrive_cli.py"

# (リンク先の相対パス)
SHARED_LINKS = [
    ".agents/agent-safety-rules.md",
    ".agents/local-tools.md",
    ".agents/hooks.json",
    ".agents/hooks",
    ".agents/README.md",
]
AGENTS_MD_INCLUDES = [
    ("agent-safety-rules.md", "エージェント安全規則"),
    ("local-tools.md", "このマシンで使えるツール"),
]
GDRIVE_INCLUDE = ("gdrive-cli.md", "Google Drive の読み書き")


def relative_symlink(target_dir: Path, dest_rel_path: str, source_rel_path: "str | None" = None) -> None:
    """target_dir/<dest_rel_path> を、このリポジトリの <source_rel_path>
    (省略時は dest_rel_path と同じ)への相対シンボリックリンクにする。
    gdrive スクリプトのように、展開先の名前をソースと変えたい場合に
    source_rel_path を使う。"""
    source_rel_path = source_rel_path or dest_rel_path
    dest = target_dir / dest_rel_path
    source = CANONICAL_AGENTS_DIR.parent / source_rel_path
    if not source.exists():
        print(f"  [スキップ] 正本が無い: {source}")
        return
    if dest.is_symlink():
        if dest.resolve() == source.resolve():
            print(f"  [OK] 既にリンク済み: {dest_rel_path}")
            return
        sys.exit(f"エラー: {dest} は既に別のリンク先を指しています（{os.readlink(dest)}）。"
                 "手動で確認してください。")
    if dest.exists():
        sys.exit(f"エラー: {dest} は既に実体のあるファイル/ディレクトリです。"
                 "上書きしないので、手動で確認してください。")
    dest.parent.mkdir(parents=True, exist_ok=True)
    rel_target = os.path.relpath(source, dest.parent)
    dest.symlink_to(rel_target, target_is_directory=source.is_dir())
    print(f"  [作成] {dest_rel_path} -> {rel_target}")


def ensure_agents_md_includes(target_dir: Path, includes: list[tuple[str, str]]) -> None:
    agents_md = target_dir / "AGENTS.md"
    existing = agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""
    missing = [(fname, label) for fname, label in includes
               if f"](.agents/{fname})" not in existing]
    if not missing:
        print("  [OK] AGENTS.md は読み込み済み")
        return
    new_lines = "".join(f"@[{label}](.agents/{fname})\n" for fname, label in missing)
    agents_md.write_text(new_lines + existing, encoding="utf-8")
    for fname, _ in missing:
        print(f"  [追記] AGENTS.md に @[...](.agents/{fname}) を追加")


def ensure_scratch_ignored(target_dir: Path) -> None:
    """scratch/ は使い捨て置き場(agent-safety-rules.md #8)で、git で追跡する
    必要は無い（恒久的に許可したコマンドだけが script-integrity フックの
    git-commit 対象になり、それらは scripts/ 等に置く前提）。.gitignore に
    scratch について一切記載が無いプロジェクトにだけ、素朴な除外を足す。
    既に何らかの scratch ルールがあれば、導入者の意図したものとして触らない。"""
    gi_path = target_dir / ".gitignore"
    if not gi_path.exists():
        print("  [スキップ] .gitignore が無い")
        return
    lines = gi_path.read_text(encoding="utf-8").splitlines()
    if any("scratch" in l for l in lines):
        print("  [OK] .gitignore に scratch の記載が既にある（変更しない）")
        return
    with gi_path.open("a", encoding="utf-8") as f:
        f.write("\n# 使い捨て作業ファイル置き場（恒久的な許可の対象にはしないこと）\nscratch/\n")
    print("  [追加] .gitignore: scratch/")


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


def update_global_settings(target_dir: Path, gdrive_script: "str | None") -> None:
    data = load_json(GLOBAL_SETTINGS_PATH)
    permissions = data.setdefault("permissions", {})

    deny_wanted = list(GLOBAL_DENY_ALWAYS)
    if gdrive_script:
        deny_wanted += [
            f"write_file({target_dir}/{gdrive_script})",
            f"write_file({target_dir}/.venv)",
        ]
    added_deny = ensure_list_entries(permissions, "deny", deny_wanted)

    added_ws = ensure_list_entries(data, "trustedWorkspaces", [str(target_dir)])

    if added_deny or added_ws:
        save_json(GLOBAL_SETTINGS_PATH, data)
    for e in added_deny:
        print(f"  [追加] settings.json permissions.deny: {e}")
    for e in added_ws:
        print(f"  [追加] settings.json trustedWorkspaces: {e}")
    if not added_deny and not added_ws:
        print("  [OK] settings.json は対応済み")


def find_agy_project_json(target_dir: Path) -> "Path | None":
    target_uri = f"file://{target_dir}"
    if not AGY_PROJECTS_DIR.is_dir():
        return None
    for p in AGY_PROJECTS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for res in (data.get("projectResources") or {}).get("resources", []):
            uri = res.get("folderUri") or (res.get("gitFolder") or {}).get("folderUri")
            if uri and uri.rstrip("/") == target_uri.rstrip("/"):
                return p
    return None


def ensure_permanent_commands_manifest(target_dir: Path, wanted_paths: list[str]) -> None:
    """Seals a permanent grant into .agents/.permanent-commands.json: resolved
    absolute script path -> sha256 of its current content, which
    script_integrity.py (the PreToolUse hook) checks before every run. Runs
    here (re-)seal the current content of each wanted script; the bridge's
    /agy-permissions add does the same for grants made from Slack."""
    path = target_dir / PERMANENT_COMMANDS_REL
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (json.JSONDecodeError, OSError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    changed = []
    for p in wanted_paths:
        digest = hashlib.sha256(Path(p).read_bytes()).hexdigest()
        if current.get(p) != digest:
            current[p] = digest
            changed.append(p)
    if not changed:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(current.items())), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    for p in changed:
        print(f"  [seal] {PERMANENT_COMMANDS_REL}: {p}")


def update_project_gdrive_allow(target_dir: Path, gdrive_script: str) -> None:
    wanted = [f"command(.venv/bin/python -I {gdrive_script} {sub})"
              for sub in GDRIVE_READONLY_SUBCOMMANDS]
    script_abs = os.path.normpath(target_dir / gdrive_script)
    ensure_permanent_commands_manifest(target_dir, [script_abs])

    project_json = find_agy_project_json(target_dir)
    if project_json is None:
        print("  [未登録] このプロジェクトはまだ agy に登録されていません。"
              "`agy --project <名前> -p \"hi\"` 等で登録後、このコマンドを再実行してください"
              "（gdrive の許可登録だけが保留されます）。")
        return
    data = load_json(project_json)
    grants = data.setdefault("permissionGrants", {}).setdefault("permissionGrants", {})
    added = ensure_list_entries(grants, "allow", wanted)
    if added:
        save_json(project_json, data)
        for e in added:
            print(f"  [追加] {project_json.name} permissionGrants.allow: {e}")
    else:
        print(f"  [OK] {project_json.name} は対応済み")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="初期設定したいプロジェクトのディレクトリ")
    ap.add_argument("--with-gdrive", action="store_true",
                     help="Google Drive 読み書き用の固定コマンドも共有し、許可登録する")
    ap.add_argument("--gdrive-script", metavar="REL_PATH", default=None,
                     help=f"対象プロジェクトでの配置先パス/名前(既定: {DEFAULT_GDRIVE_SCRIPT}。"
                          "--with-gdrive 指定時のみ有効。ソース自体は常に accountant の"
                          f"{DEFAULT_GDRIVE_SCRIPT})")
    args = ap.parse_args()

    target_dir = Path(args.target).resolve()
    if not target_dir.is_dir():
        sys.exit(f"エラー: ディレクトリが無い: {target_dir}")
    if not (target_dir / ".git").exists():
        sys.exit(f"エラー: git リポジトリではない: {target_dir}（恒久許可コマンドの"
                 "commit-before-run 判定の前提なので必須）")
    if target_dir == SELF_DIR:
        sys.exit("このリポジトリ自体が正本です。対象には別のプロジェクトを指定してください。")

    gdrive_script = (args.gdrive_script or DEFAULT_GDRIVE_SCRIPT) if args.with_gdrive else None

    print(f"=== {target_dir} をブートストラップ ===")
    for rel_path in SHARED_LINKS:
        relative_symlink(target_dir, rel_path)
    if gdrive_script:
        relative_symlink(target_dir, ".agents/gdrive-cli.md")
        relative_symlink(target_dir, gdrive_script, source_rel_path=DEFAULT_GDRIVE_SCRIPT)

    includes = AGENTS_MD_INCLUDES + ([GDRIVE_INCLUDE] if gdrive_script else [])
    ensure_agents_md_includes(target_dir, includes)

    ensure_scratch_ignored(target_dir)

    print("-- machine-wide settings --")
    update_global_settings(target_dir, gdrive_script)

    if gdrive_script:
        print("-- agy プロジェクトの許可登録 --")
        update_project_gdrive_allow(target_dir, gdrive_script)

    print(
        "\n完了。残っているのは agy-slack-bridge の config.yaml へのチャンネル追加だけです"
        "（別リポジトリ。README の「Slack app setup」参照）。"
    )


if __name__ == "__main__":
    main()
