#!/usr/bin/env python3
"""
agy プロジェクトのブートストラップ(新規作成・修復とも、このコマンド1本)
================================================================================
Slack ブリッジ経由で動かす agy プロジェクトに要る設定一式(共有の安全規則、
script-integrity フック、machine-wide の deny/trustedWorkspaces、
Google Drive の許可リストなど)を、このコマンド1回で揃える。

冪等です。何度実行しても安全で、何かが壊れた・ずれた場合は、このコマンドを
もう一度実行するだけで直ります。

使い方:
    python3 tools/setup_agy_project.py <対象プロジェクトのパス> [オプション]

    --source-dir DIR     共有ファイル(agent-safety-rules.md, hooks.json, hooks/ ...)
                          の置き場。既定はこのスクリプト自身のあるディレクトリ。
                          tools/ を別の場所にコピーして運用する場合は、そのコピーの
                          ディレクトリを指定(またはコピー先からこのスクリプトを実行)。
    --bridge-dir DIR      ブリッジ本体(bridge.py)のチェックアウト。エージェントに
                          読ませない deny ルールの対象。既定はこのスクリプトの親
                          ディレクトリ(bridge.py がそこにある場合のみ)。
    --bridge-config-dir DIR
                          ブリッジの設定・トークン・状態DBのディレクトリ(既定:
                          ~/.config/agy-slack-bridge)。これも deny ルールの対象。
    --with-gdrive         Google Drive 読み書き用の固定コマンド(source-dir の
                          gdrive_cli.py)を共有し、その読み取り系サブコマンドを
                          対象プロジェクトの許可リストに恒久登録する。
                          --gdrive-profile が必須。
    --gdrive-profile NAME gdrive_cli.py に渡す --profile の値。許可エントリの一部になるので、
                          プロジェクトごとに別の名前を使えば、あるプロジェクトの許可が
                          別プロジェクトの認証情報を使う権限にならない。プロファイル本体
                          (<bridge-config-dir>/gdrive/NAME.json、書き方は tools/gdrive-profile.md)
                          は運用者が作る。無ければ警告する(許可の登録自体は行う)。
    --gdrive-script REL   対象プロジェクトでの配置先パス(既定: scripts/gdrive_cli.py)。

このコマンドが揃えるもの:
  1. 対象の .agents/ 配下に、source-dir の共有ファイル(安全規則・hooks 定義・
     hooks/、あれば local-tools.md、--with-gdrive ならあれば gdrive-cli.md)への
     シンボリックリンク。AGENTS.md への読み込み行。
     (local-tools.md と gdrive-cli.md は、各運用者が自分のマシン用に置くファイルで、
     このリポジトリには含まれない。source-dir に無ければスキップする。)
  2. scratch/ を丸ごと除外する .gitignore の調整
     (そこが .gitignore に一切出てきていない場合のみ - 既に何らかの scratch/
     ルールがあれば、それは導入者の意図したものとして触らない)。
     scratch/ は使い捨て置き場であり、恒久的な許可(下記3・4)の対象には
     ならないので、ここは git で追跡する必要が無い。
  3. machine-wide の ~/.gemini/antigravity-cli/settings.json:
     - ブリッジ/agy 自身の内部を読ませない deny ルール。
     - --with-gdrive 時、対象プロジェクトの Drive CLI スクリプトと .venv への
       write_file deny。
     - trustedWorkspaces に対象ディレクトリを追加。
     (ブリッジ本体はこのファイルを書かない。ここで書くのは絶対パスで書かれた
     deny / trustedWorkspaces だけで、プロジェクトを問わず安全に共有できるもの。
     許可(allow)は、ここには決して書かない。)
  4. --with-gdrive 時:
     - .agents/.permanent-commands.json(script-integrity フックが
       「git commit 必須」と判定する対象の一覧。gitignore 対象、git には
       乗らない)に、Drive CLI を登録(中身の sha256 で seal)。
     - 対象プロジェクトが agy に登録済みなら
       (~/.gemini/config/projects/*.json から folderUri で特定)、
       読み取り系サブコマンドを、そのプロジェクト自身の permissionGrants にだけ
       恒久許可として追加。

このコマンドの対象外(別の理由で自動化していないもの):
  - agy へのプロジェクトの初回登録そのもの(`agy --project <名前> -p "hi"` 等)。
    対象プロジェクトが未登録のときは、その旨を表示して終わる
    (--with-gdrive の許可登録だけがスキップされ、他は適用される)。
    登録後にこのコマンドを再実行すれば、許可登録も反映される。
  - ブリッジの config.yaml へのチャンネル追加(Slack チャンネルIDが要るため)。
    README の「Slack app setup」を参照。
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

GLOBAL_SETTINGS_PATH = Path("~/.gemini/antigravity-cli/settings.json").expanduser()
AGY_PROJECTS_DIR = Path("~/.gemini/config/projects").expanduser()
DEFAULT_BRIDGE_CONFIG_DIR = Path("~/.config/agy-slack-bridge").expanduser()
PERMANENT_COMMANDS_REL = Path(".agents") / ".permanent-commands.json"

GDRIVE_READONLY_SUBCOMMANDS = ["list", "search", "meta", "read", "download"]
GDRIVE_SOURCE_NAME = "gdrive_cli.py"
DEFAULT_GDRIVE_SCRIPT = "scripts/gdrive_cli.py"
# --profile は許可エントリの一部になるので、空白・引用符・パス区切りを含む名前は受け付けない
# (gdrive_cli.py 側の PROFILE_NAME_RE と同じ)。
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# (ファイル名, 必須か)。source-dir にあるものを .agents/<名前> としてリンクする。
# 必須でないものは、運用者がマシンごとに用意するファイルなので、無ければスキップ。
SHARED_FILES = [
    ("agent-safety-rules.md", True),
    ("hooks.json", True),
    ("hooks", True),
    ("local-tools.md", False),
]
# AGENTS.md の読み込み行(ファイル名, ラベル)。リンクしたものだけ追記する。
AGENTS_MD_INCLUDES = {
    "agent-safety-rules.md": "エージェント安全規則",
    "local-tools.md": "このマシンで使えるツール",
    "gdrive-cli.md": "Google Drive の読み書き",
}


def global_deny_rules(bridge_dir: Path, bridge_config_dir: Path, home: Path) -> list[str]:
    """machine-wide: 対象を問わず、常にこの内容を禁止する(ブリッジ/agy自身の内部)。
    settings.json の既存エントリと同じ展開済みの絶対パスで持つ(でないと別文字列として
    重複登録されてしまう)。"""
    return [
        f"read_file({bridge_dir})",
        f"read_file({bridge_config_dir})",
        f"read_file({home}/.gemini/antigravity-cli/settings.json)",
        f"read_file({home}/.gemini/config)",
    ]


def relative_symlink(target_dir: Path, dest_rel_path: str, source: Path) -> None:
    """target_dir/<dest_rel_path> を、source への相対シンボリックリンクにする。"""
    dest = target_dir / dest_rel_path
    if not source.exists():
        print(f"  [スキップ] 正本が無い: {source}")
        return
    if dest.is_symlink():
        if dest.resolve() == source.resolve():
            print(f"  [OK] 既にリンク済み: {dest_rel_path}")
            return
        sys.exit(f"エラー: {dest} は既に別のリンク先を指しています（{os.readlink(dest)}）。"
                 "意図した切り替えなら、そのリンクを手動で削除してから再実行してください。")
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


def update_global_settings(target_dir: Path, deny_rules: list[str], gdrive_script: "str | None") -> None:
    data = load_json(GLOBAL_SETTINGS_PATH)
    permissions = data.setdefault("permissions", {})

    deny_wanted = list(deny_rules)
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
    hooks/script_integrity.py (the PreToolUse hook) checks before every run. Runs
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


def gdrive_allow_entries(gdrive_script: str, profile: str) -> list[str]:
    # --profile はサブコマンドより前に固定する(末尾の自由引数にすると、エージェントが
    # 別プロジェクトのプロファイルを選べてしまう)。
    return [f"command(.venv/bin/python -I {gdrive_script} --profile {profile} {sub})"
            for sub in GDRIVE_READONLY_SUBCOMMANDS]


def warn_if_profile_missing(bridge_config_dir: Path, profile: str) -> None:
    path = bridge_config_dir / "gdrive" / f"{profile}.json"
    if not path.exists():
        print(f"  [警告] プロファイルがまだありません: {path}（tools/gdrive-profile.md を参照。"
              "作るまで gdrive_cli.py は動きません）")
    elif path.stat().st_mode & 0o022:
        print(f"  [警告] {path} が group/other から書き込み可能です（chmod 600）。"
              "gdrive_cli.py はこの状態では実行を拒否します")


def update_project_gdrive_allow(target_dir: Path, gdrive_script: str, profile: str) -> None:
    wanted = gdrive_allow_entries(gdrive_script, profile)
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


def main(argv: "list[str] | None" = None) -> None:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="初期設定したいプロジェクトのディレクトリ")
    ap.add_argument("--source-dir", type=Path, default=here)
    ap.add_argument("--bridge-dir", type=Path, default=None)
    ap.add_argument("--bridge-config-dir", type=Path, default=DEFAULT_BRIDGE_CONFIG_DIR)
    ap.add_argument("--with-gdrive", action="store_true")
    ap.add_argument("--gdrive-profile", metavar="NAME")
    ap.add_argument("--gdrive-script", metavar="REL_PATH", default=DEFAULT_GDRIVE_SCRIPT)
    args = ap.parse_args(argv)

    source_dir = args.source_dir.expanduser().resolve()
    target_dir = Path(args.target).expanduser().resolve()
    bridge_dir = args.bridge_dir
    if bridge_dir is None and (source_dir.parent / "bridge.py").exists():
        bridge_dir = source_dir.parent
    if bridge_dir is None:
        sys.exit("エラー: ブリッジ本体のディレクトリを特定できません。--bridge-dir で指定してください。")
    bridge_dir = bridge_dir.expanduser().resolve()
    bridge_config_dir = args.bridge_config_dir.expanduser().resolve()

    if not source_dir.is_dir():
        sys.exit(f"エラー: source-dir が無い: {source_dir}")
    if not target_dir.is_dir():
        sys.exit(f"エラー: ディレクトリが無い: {target_dir}")
    if not (target_dir / ".git").exists():
        sys.exit(f"エラー: git リポジトリではない: {target_dir}（恒久許可コマンドの"
                 "commit-before-run 判定の前提なので必須）")
    if target_dir in (source_dir, bridge_dir):
        sys.exit("このディレクトリは共有ファイルの置き場(またはブリッジ本体)です。"
                 "対象には別のプロジェクトを指定してください。")

    if args.with_gdrive:
        if not args.gdrive_profile:
            sys.exit("エラー: --with-gdrive には --gdrive-profile <プロファイル名> が必要です。")
        if not _PROFILE_NAME_RE.match(args.gdrive_profile):
            sys.exit("エラー: --gdrive-profile に使えるのは英数字と _ - だけです。")
        if not (source_dir / GDRIVE_SOURCE_NAME).exists():
            sys.exit(f"エラー: {source_dir / GDRIVE_SOURCE_NAME} がありません。")
    gdrive_script = args.gdrive_script if args.with_gdrive else None

    print(f"=== {target_dir} をブートストラップ ===")
    linked = []
    for name, required in SHARED_FILES:
        if not required and not (source_dir / name).exists():
            print(f"  [スキップ] 任意ファイルが source-dir に無い: {name}")
            continue
        relative_symlink(target_dir, f".agents/{name}", source_dir / name)
        linked.append(name)
    if gdrive_script:
        relative_symlink(target_dir, gdrive_script, source_dir / GDRIVE_SOURCE_NAME)
        if (source_dir / "gdrive-cli.md").exists():
            relative_symlink(target_dir, ".agents/gdrive-cli.md", source_dir / "gdrive-cli.md")
            linked.append("gdrive-cli.md")
        else:
            print("  [スキップ] 任意ファイルが source-dir に無い: gdrive-cli.md")

    ensure_agents_md_includes(target_dir, [(n, AGENTS_MD_INCLUDES[n]) for n in linked
                                           if n in AGENTS_MD_INCLUDES])

    ensure_scratch_ignored(target_dir)

    print("-- machine-wide settings --")
    update_global_settings(target_dir,
                           global_deny_rules(bridge_dir, bridge_config_dir, Path.home()),
                           gdrive_script)

    if gdrive_script:
        print("-- agy プロジェクトの許可登録 --")
        update_project_gdrive_allow(target_dir, gdrive_script, args.gdrive_profile)
        warn_if_profile_missing(bridge_config_dir, args.gdrive_profile)

    print(
        "\n完了。残っているのは、ブリッジの config.yaml へのチャンネル追加だけです"
        "（README の「Slack app setup」参照）。"
    )


if __name__ == "__main__":
    main()
