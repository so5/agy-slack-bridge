#!/usr/bin/env python3
"""
agy プロジェクトのセットアップ(新規作成・修復とも、このコマンド1本。要・人間が実行)
================================================================================
Slack ブリッジ経由で動かす agy プロジェクトに要る設定一式を、対話形式で揃える。
エージェントが実行するものではない(settings.json と許可リストを書き換えるため)。

事前に tools/install.py でインストールしておくこと。このスクリプトは、インストール先
(<prefix>/tools/setup_agy_project.py)から実行する。インストール先と設定ディレクトリは
エージェントに読み書きさせない(下記3の deny ルール)ので、安全規則・フック・Drive CLI は
プロジェクトにコピーせず、インストール先のものを直接使う。

冪等です。何度実行しても安全で、現在の設定が既定値として表示されるので、設定の変更・修復も
このコマンドをもう一度実行するだけでよい。質問は Enter で現在値のまま、`-` で値を消す。

使い方:
    <prefix>/.venv/bin/python <prefix>/tools/setup_agy_project.py [対象のパス] [オプション]

    --prefix DIR           インストール先(既定: このスクリプトの2つ上)。
    --bridge-config-dir DIR
                           ブリッジの設定ディレクトリ(既定: ~/.config/agy-slack-bridge)。

このコマンドが行うこと:
  1. 対象ディレクトリの用意(無ければ作成、git リポジトリでなければ git init)。
  2. agy へのプロジェクト登録(未登録なら `agy --new-project -p hi` を対象ディレクトリで実行。
     プロジェクト名は、そのディレクトリ名になる)。
  3. machine-wide の ~/.gemini/antigravity-cli/settings.json:
     - ブリッジ/agy 自身の内部(インストール先・設定ディレクトリ)の読み取り・書き込みを
       禁じる deny ルール、対象プロジェクトの .agents/hooks.json と
       .agents/.permanent-commands.json への書き込みを禁じる deny ルール。
     - trustedWorkspaces に対象ディレクトリを追加。
     (許可(allow)は、ここには決して書かない。)
  4. 対象の .agents/ に、安全規則(インストール先のファイルへのシンボリックリンク)と、
     インストール先のフックを指す hooks.json を置き、AGENTS.md に読み込み行を追記。
     .gitignore に scratch/ の除外を追記(scratch について何も書かれていない場合のみ)。
  5. Google Drive CLI を使う場合: プロファイル名を決め、読み取り系サブコマンドを、
     そのプロジェクト自身の permissionGrants にだけ許可として追加
     (プロファイル本体は運用者が作る。tools/gdrive-profile.md)。
  6. Slack チャンネルとの対応づけ(config.yaml の channels): チャンネル ID、表示名、アイコン、
     モデル、effort、監視する設定ファイル、統計。任意の項目は飛ばせる。
  7. 未設定なら Slack のトークン(env)。

設定ファイルを変えたあとは、ブリッジの再起動が必要な場合があります。
"""
import argparse
import getpass
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bridge_config  # noqa: E402

GLOBAL_SETTINGS_PATH = Path("~/.gemini/antigravity-cli/settings.json").expanduser()
AGY_PROJECTS_DIR = Path("~/.gemini/config/projects").expanduser()
DEFAULT_BRIDGE_CONFIG_DIR = Path("~/.config/agy-slack-bridge").expanduser()

GDRIVE_READONLY_SUBCOMMANDS = ["list", "search", "meta", "read", "download"]
# --profile は許可エントリの一部になるので、空白・引用符・パス区切りを含む名前は受け付けない
# (gdrive_cli.py 側の PROFILE_NAME_RE と同じ)。
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CHANNEL_ID_RE = re.compile(r"^[CG][A-Z0-9]{5,}$")
_EMOJI_RE = re.compile(r"^:[a-z0-9_+\-]+:$")
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# 運用者がマシンごとに用意する任意のファイル(<設定ディレクトリ>/shared/ に置く)。
# あれば .agents/ にリンクして AGENTS.md から読み込ませる。
OPTIONAL_SHARED_FILES = ["local-tools.md", "gdrive-cli.md"]
AGENTS_MD_INCLUDES = {
    "agent-safety-rules.md": "エージェント安全規則",
    "local-tools.md": "このマシンで使えるツール",
    "gdrive-cli.md": "Google Drive の読み書き",
}


# --- 対話 ---------------------------------------------------------------------

class Prompter:
    """質問は Enter で既定値(現在値)、`-` で値を消す(None を返す)。"""

    def __init__(self, input_fn=input, secret_fn=getpass.getpass, out=print):
        self._input, self._secret, self.out = input_fn, secret_fn, out

    def text(self, label, default=None, validate=None):
        hint = f" [{default}]" if default not in (None, "") else "（空欄でスキップ）"
        while True:
            raw = self._input(f"  {label}{hint}: ").strip()
            if raw == "":
                return default if default != "" else None
            if raw == "-":
                return None
            err = validate(raw) if validate else None
            if not err:
                return raw
            self.out(f"    {err}")

    def yes_no(self, label, default=True):
        while True:
            raw = self._input(f"  {label} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
            if raw == "":
                return default
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False

    def secret(self, label):
        return self._secret(f"  {label}（空欄でスキップ）: ").strip()


# --- 共通の小物 ------------------------------------------------------------------

def global_deny_rules(prefix: Path, bridge_config_dir: Path, home: Path, target_dir: Path) -> list[str]:
    """machine-wide: 対象を問わず、常にこの内容を禁止する(ブリッジ/agy自身の内部)。
    settings.json の既存エントリと同じ展開済みの絶対パスで持つ(でないと別文字列として
    重複登録されてしまう)。"""
    return [
        f"read_file({prefix})",
        f"write_file({prefix})",
        f"read_file({bridge_config_dir})",
        f"write_file({bridge_config_dir})",
        f"read_file({home}/.gemini/antigravity-cli/settings.json)",
        f"read_file({home}/.gemini/config)",
        # フックの配線と恒久許可の台帳は、書き換えられると保護が外れる
        f"write_file({target_dir}/.agents/hooks.json)",
        f"write_file({target_dir}/.agents/.permanent-commands.json)",
    ]


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


def absolute_symlink(target_dir: Path, dest_rel_path: str, source: Path) -> None:
    """target_dir/<dest_rel_path> を、source への絶対シンボリックリンクにする。"""
    dest = target_dir / dest_rel_path
    if not source.exists():
        print(f"  [スキップ] 元のファイルが無い: {source}")
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
    dest.symlink_to(source)
    print(f"  [作成] {dest_rel_path} -> {source}")


def write_hooks_json(target_dir: Path, prefix: Path) -> None:
    """インストール先のフックを指す .agents/hooks.json(絶対パス)を置く。"""
    template = json.loads((prefix / "tools" / "hooks.json").read_text(encoding="utf-8"))
    hook = prefix / "tools" / "hooks" / "script_integrity.py"
    for entry in template.values():
        for group in entry.get("PreToolUse", []):
            for h in group.get("hooks", []):
                h["command"] = f"python3 {hook}"
    dest = target_dir / ".agents" / "hooks.json"
    text = json.dumps(template, indent=2, ensure_ascii=False) + "\n"
    if dest.is_symlink():
        sys.exit(f"エラー: {dest} は以前のセットアップのシンボリックリンクです。"
                 "削除してから再実行してください（フックはインストール先を直接指す形に変わりました）。")
    if dest.exists() and dest.read_text(encoding="utf-8") == text:
        print("  [OK] .agents/hooks.json は対応済み")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    print(f"  [書込] .agents/hooks.json（フック: {hook}）")


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
    必要は無い。.gitignore に scratch について一切記載が無いプロジェクトにだけ、
    素朴な除外を足す。既に何らかの scratch ルールがあれば、導入者の意図したものとして触らない。"""
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


def update_global_settings(target_dir: Path, deny_rules: list[str]) -> None:
    data = load_json(GLOBAL_SETTINGS_PATH)
    permissions = data.setdefault("permissions", {})
    added_deny = ensure_list_entries(permissions, "deny", deny_rules)
    added_ws = ensure_list_entries(data, "trustedWorkspaces", [str(target_dir)])
    if added_deny or added_ws:
        save_json(GLOBAL_SETTINGS_PATH, data)
    for e in added_deny:
        print(f"  [追加] settings.json permissions.deny: {e}")
    for e in added_ws:
        print(f"  [追加] settings.json trustedWorkspaces: {e}")
    if not added_deny and not added_ws:
        print("  [OK] settings.json は対応済み")


# --- agy プロジェクト ------------------------------------------------------------

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


def register_agy_project(target_dir: Path) -> None:
    """`agy --new-project` はカレントディレクトリをプロジェクトとして登録する
    (名前はそのディレクトリ名)。モデルを 1 回呼ぶので、未ログインだと -p は失敗するが、
    登録自体は済む。結果は呼び出し側が find_agy_project_json で確かめる。"""
    agy = os.environ.get("AGY_BIN", "agy")
    try:
        subprocess.run([agy, "--new-project", "-p", "hi", "--print-timeout", "120s"],
                       cwd=target_dir, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  [警告] agy の実行に失敗しました: {e}")


def gdrive_allow_entries(prefix: Path, profile: str) -> list[str]:
    # --profile はサブコマンドより前に固定する(末尾の自由引数にすると、エージェントが
    # 別プロジェクトのプロファイルを選べてしまう)。
    py = prefix / ".venv" / "bin" / "python"
    script = prefix / "tools" / "gdrive_cli.py"
    return [f"command({py} -I {script} --profile {profile} {sub})" for sub in GDRIVE_READONLY_SUBCOMMANDS]


def current_gdrive_profile(project_json: Path, prefix: Path) -> "str | None":
    grants = ((load_json(project_json).get("permissionGrants") or {}).get("permissionGrants") or {})
    marker = f"{prefix}/tools/gdrive_cli.py --profile "
    for e in grants.get("allow", []):
        if marker in e:
            return e.split(marker, 1)[1].split()[0]
    return None


def warn_if_profile_missing(bridge_config_dir: Path, profile: str) -> None:
    path = bridge_config_dir / "gdrive" / f"{profile}.json"
    if not path.exists():
        print(f"  [警告] プロファイルがまだありません: {path}（tools/gdrive-profile.md を参照。"
              "作るまで gdrive_cli.py は動きません）")
    elif path.stat().st_mode & 0o022:
        print(f"  [警告] {path} が group/other から書き込み可能です（chmod 600）。"
              "gdrive_cli.py はこの状態では実行を拒否します")


def warn_if_gdrive_packages_missing(prefix: Path) -> None:
    if not list((prefix / ".venv").glob("lib/python*/site-packages/googleapiclient")):
        print("  [警告] インストール先の venv に Google 系パッケージがありません。"
              "`install.py --with-gdrive` で入れてください。")


def update_project_gdrive_allow(project_json: Path, prefix: Path, profile: str) -> None:
    data = load_json(project_json)
    grants = data.setdefault("permissionGrants", {}).setdefault("permissionGrants", {})
    added = ensure_list_entries(grants, "allow", gdrive_allow_entries(prefix, profile))
    if added:
        save_json(project_json, data)
        for e in added:
            print(f"  [追加] {project_json.name} permissionGrants.allow: {e}")
    else:
        print(f"  [OK] {project_json.name} は対応済み")


# --- ブリッジの設定(config.yaml / env) ----------------------------------------------

def _validate_channel_id(v):
    return None if _CHANNEL_ID_RE.match(v) else "Slack のチャンネル ID（C… または G…）を入力してください"


def _validate_emoji(v):
    return None if _EMOJI_RE.match(v) else "`:name:` の形で入力してください"


def _validate_url(v):
    return None if v.startswith("https://") else "https:// で始まる URL を入力してください"


def _validate_effort(v):
    return None if v in EFFORTS else f"{' / '.join(EFFORTS)} のどれか"


def _validate_rel_paths(v):
    for p in (s.strip() for s in v.split(",")):
        if not p or p.startswith("/") or ".." in Path(p).parts:
            return "プロジェクトからの相対パスをカンマ区切りで（絶対パスと .. は不可）"
    return None


def _set_or_pop(entry: dict, key: str, value) -> None:
    if value in (None, ""):
        entry.pop(key, None)
    else:
        entry[key] = value


def configure_channel(ask: Prompter, config_path: Path, project_id: str, project_name: str) -> None:
    if not config_path.exists():
        print(f"  [スキップ] {config_path} がありません（先に install.py を実行してください）")
        return
    text = config_path.read_text(encoding="utf-8")
    channels = bridge_config.read_channels(text)
    mine = [cid for cid, e in channels.items() if (e or {}).get("project") == project_id]
    if mine:
        print(f"  このプロジェクトに割り当て済みのチャンネル: {', '.join(mine)}")
    if not ask.yes_no("Slack チャンネルとの対応づけを設定・変更しますか?", default=not mine):
        return
    channel_id = ask.text("チャンネル ID", default=mine[0] if len(mine) == 1 else None,
                          validate=_validate_channel_id)
    if not channel_id:
        print("  [スキップ] チャンネル ID が未入力")
        return
    entry = dict(channels.get(channel_id) or {})
    if entry.get("project") not in (None, project_id):
        if not ask.yes_no(f"{channel_id} は別のプロジェクト（{entry['project']}）に割り当て済みです。置き換えますか?",
                          default=False):
            return
    entry["project"] = project_id
    _set_or_pop(entry, "name", ask.text("名前（自分用のメモ。ブリッジは読まない）",
                                        default=entry.get("name") or project_name))
    _set_or_pop(entry, "display_name", ask.text("Slack での表示名", default=entry.get("display_name")))
    emoji = ask.text("アイコン（絵文字 :name:）", default=entry.get("icon_emoji"), validate=_validate_emoji)
    _set_or_pop(entry, "icon_emoji", emoji)
    if emoji:
        entry.pop("icon_url", None)
    else:
        _set_or_pop(entry, "icon_url", ask.text("アイコン（画像 URL）", default=entry.get("icon_url"),
                                                validate=_validate_url))
    _set_or_pop(entry, "model", ask.text("モデル（`agy models` の名前。空欄は agy の既定）",
                                         default=entry.get("model")))
    _set_or_pop(entry, "effort", ask.text(f"effort（{' / '.join(EFFORTS)}）", default=entry.get("effort"),
                                          validate=_validate_effort))
    watched = ask.text("監視する設定ファイル（プロジェクトからの相対パス、カンマ区切り）",
                       default=",".join(entry.get("watched_config_files") or []) or None,
                       validate=_validate_rel_paths)
    _set_or_pop(entry, "watched_config_files", [s.strip() for s in watched.split(",")] if watched else None)
    telemetry = dict(entry.get("telemetry") or {})
    stats = ask.yes_no("権限ボタンの統計を取りますか?（telemetry.permission_stats）",
                       default=bool(telemetry.get("permission_stats")))
    if stats or "permission_stats" in telemetry:
        telemetry["permission_stats"] = stats
    _set_or_pop(entry, "telemetry", telemetry or None)

    new_text = bridge_config.upsert_channel(text, channel_id, entry)
    if new_text == text:
        print("  [OK] config.yaml は対応済み")
        return
    backup = config_path.with_name(config_path.name + ".bak")
    backup.write_text(text, encoding="utf-8")
    os.chmod(backup, 0o600)
    config_path.write_text(new_text, encoding="utf-8")
    print(f"  [更新] {config_path} の channels.{channel_id}（元のファイル: {backup.name}）")


def configure_tokens(ask: Prompter, env_path: Path) -> None:
    if not env_path.exists():
        return
    text = env_path.read_text(encoding="utf-8")
    changed = False
    for key, prefix_hint in (("SLACK_BOT_TOKEN", "xoxb-"), ("SLACK_APP_TOKEN", "xapp-")):
        current = bridge_config.get_env_value(text, key)
        if current and current != f"{prefix_hint}...":
            print(f"  [OK] {key} は設定済み")
            continue
        value = ask.secret(f"{key}（{prefix_hint}…）")
        if value:
            text = bridge_config.set_env_value(text, key, value)
            changed = True
    if changed:
        env_path.write_text(text, encoding="utf-8")
        os.chmod(env_path, 0o600)
        print(f"  [更新] {env_path}")


# --- main -----------------------------------------------------------------------

def main(argv: "list[str] | None" = None, ask: "Prompter | None" = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", help="セットアップするプロジェクトのディレクトリ（省略すると質問する）")
    ap.add_argument("--prefix", type=Path, default=HERE.parent)
    ap.add_argument("--bridge-config-dir", type=Path, default=DEFAULT_BRIDGE_CONFIG_DIR)
    args = ap.parse_args(argv)

    if ask is None:
        if not sys.stdin.isatty():
            sys.exit("エラー: 対話形式のコマンドです。端末から実行してください。")
        ask = Prompter()

    prefix = args.prefix.expanduser().resolve()
    config_dir = args.bridge_config_dir.expanduser().resolve()
    if not (prefix / "bridge.py").exists() or not (prefix / "tools" / "hooks.json").exists():
        sys.exit(f"エラー: {prefix} にインストールされていません。先に tools/install.py を実行してください。")

    raw_target = args.target or ask.text("プロジェクトのディレクトリ")
    if not raw_target:
        sys.exit("エラー: 対象のディレクトリが必要です。")
    target_dir = Path(raw_target).expanduser().resolve()
    if prefix == target_dir or prefix in target_dir.parents or config_dir in (target_dir, *target_dir.parents):
        sys.exit("エラー: インストール先・設定ディレクトリの中をプロジェクトにはできません。")

    print(f"=== {target_dir} をセットアップ ===")
    if not target_dir.is_dir():
        if not ask.yes_no(f"{target_dir} がありません。作成しますか?"):
            sys.exit("中止しました。")
        target_dir.mkdir(parents=True)
    if not (target_dir / ".git").exists():
        if not ask.yes_no("git リポジトリではありません。git init しますか?（恒久許可コマンドの"
                          "commit-before-run 判定の前提）"):
            sys.exit("中止しました。")
        subprocess.run(["git", "init", "-q", str(target_dir)], check=True)

    print("-- agy プロジェクト --")
    project_json = find_agy_project_json(target_dir)
    if project_json is None:
        if ask.yes_no("agy にまだ登録されていません。`agy --new-project` で登録しますか?（モデルを 1 回呼びます）"):
            register_agy_project(target_dir)
            project_json = find_agy_project_json(target_dir)
        if project_json is None:
            print("  [未登録] 登録されていないので、許可の登録とチャンネルの対応づけは行えません"
                  "（登録してから、このコマンドを再実行してください）。")
    if project_json is not None:
        project = load_json(project_json)
        project_id, project_name = project["id"], project.get("name") or target_dir.name
        print(f"  [OK] {project_name}（{project_id}）")

    print("-- 共有ファイル --")
    absolute_symlink(target_dir, ".agents/agent-safety-rules.md", prefix / "tools" / "agent-safety-rules.md")
    linked = ["agent-safety-rules.md"]
    for name in OPTIONAL_SHARED_FILES:
        src = config_dir / "shared" / name
        if src.exists():
            absolute_symlink(target_dir, f".agents/{name}", src)
            linked.append(name)
    write_hooks_json(target_dir, prefix)
    ensure_agents_md_includes(target_dir, [(n, AGENTS_MD_INCLUDES[n]) for n in linked])
    ensure_scratch_ignored(target_dir)

    print("-- machine-wide settings --")
    update_global_settings(target_dir, global_deny_rules(prefix, config_dir, Path.home(), target_dir))

    if project_json is not None:
        print("-- Google Drive CLI --")
        current = current_gdrive_profile(project_json, prefix)
        if ask.yes_no("Drive CLI（gdrive_cli.py）を使いますか?", default=current is not None):
            if re.search(r"\s", str(prefix)):
                print("  [スキップ] インストール先のパスに空白があると許可エントリを安全に作れません。")
            else:
                default = current or re.sub(r"[^A-Za-z0-9_-]", "-", target_dir.name)
                profile = ask.text("プロファイル名（プロジェクトごとに別の名前にする）", default=default,
                                   validate=lambda v: None if _PROFILE_NAME_RE.match(v)
                                   else "英数字と _ - だけ使えます")
                if profile:
                    update_project_gdrive_allow(project_json, prefix, profile)
                    warn_if_profile_missing(config_dir, profile)
                    warn_if_gdrive_packages_missing(prefix)

        print("-- Slack チャンネル --")
        configure_channel(ask, config_dir / "config.yaml", project_id, project_name)

    print("-- Slack トークン --")
    configure_tokens(ask, config_dir / "env")

    print("\n完了。設定を変えた場合は、ブリッジを再起動してください。")


if __name__ == "__main__":
    main()
