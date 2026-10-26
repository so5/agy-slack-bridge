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

  プロジェクトの一覧のメニューが開く。プロジェクトを選ぶか新規作成すると、そのプロジェクトの
  メニュー(全部設定 / ルールとフック / Drive CLI / Slack チャンネル / 状態)が出る。Slack の
  トークンは一覧のメニューから設定する(全体で共通)。--all を付けると、メニューを出さず、全部の
  段階を順に実行して終わる。

  対象のパスを省略すると、agy に登録済みのプロジェクトの一覧から選ぶか、新しい
  ディレクトリのパスを入力する。既存のプロジェクトを選ぶと、そのディレクトリを使う
  (ディレクトリの無いプロジェクトは、Drive の許可とチャンネルの対応づけだけ行う)。

    --prefix DIR           インストール先(既定: このスクリプトの2つ上)。
    --bridge-config-dir DIR
                           ブリッジの設定ディレクトリ(既定: ~/.config/agy-slack-bridge)。

このコマンドが行うこと:
  1. 対象ディレクトリの用意(無ければ作成。git 管理は必須なので、git リポジトリでなければ
     git init する)。
  2. agy へのプロジェクト登録(未登録なら `agy --new-project -p hi` を対象ディレクトリで実行。
     プロジェクト名は、そのディレクトリ名になる)。
  3. ~/.gemini/antigravity-cli/settings.json の trustedWorkspaces に対象ディレクトリを追加し、
     安全規則のリンク(.agents/rules/agent-safety-rules.md)への書き込みを禁じる deny を足す。
     (許可(allow)は、ここには決して書かない。インストール先・設定ディレクトリを守る
     machine-wide の deny と、グローバルの hooks.json へのフック登録は、install.py が行う。)
     併せて、フックの状態ディレクトリ(<設定ディレクトリ>/integrity/…、恒久許可の seal
     と実行履歴の置き場)を作る。これがあるプロジェクトだけが、フックの対象になる。
     以前のセットアップが置いたプロジェクトごとの .agents/hooks.json のフックは外す
     (グローバルのフックと二重に動くため)。
  4. 対象の .agents/rules/ に、安全規則(インストール先のファイルへのシンボリックリンク。
     frontmatter 付き)と、あれば <設定ディレクトリ>/shared/ の local-tools.md / gdrive-cli.md
     へのリンクを置く。AGENTS.md は触らない(エージェントが編集してよい)。以前のセットアップが
     作った .agents/ 直下のリンクと AGENTS.md の読み込み行は取り除く。
     .gitignore に scratch/ の除外を追記(scratch について何も書かれていない場合のみ)。
  5. Google Drive CLI を使う場合: プロファイル名を決め、プロファイル
     (<設定ディレクトリ>/gdrive/NAME.json、tools/gdrive-profile.md)を対話で作成・設定する。
     無ければ認証の種類と資格情報を取り出すコマンド(とスコープ)を聞いて作り、方針
     (有効なコマンド、許可フォルダ、アップロード拒否パターン)は、現在値(新規なら既定値)を
     見せて、変えるかを聞く。権限が広がる変更は、差分を見せたうえで別に確認する(既定は no)。
     既存のプロファイルの資格情報とスコープには触らない。有効な読み取り系サブコマンドを、
     そのプロジェクト自身の permissionGrants にだけ許可として追加し、無効にしたものの許可は外す。
  6. Slack チャンネルとの対応づけ(config.yaml の channels): チャンネル ID、表示名、アイコン、
     モデル、effort、監視する設定ファイル、統計。任意の項目は飛ばせる。
  7. 未設定なら Slack のトークン(env)。

設定ファイルを変えたあとは、ブリッジの再起動が必要な場合があります。
"""
import argparse
import getpass
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "hooks"))
import agy_settings  # noqa: E402
import bridge_config  # noqa: E402
import gdrive_cli  # noqa: E402
import gdrive_policy  # noqa: E402
import script_integrity  # noqa: E402
from agy_settings import ensure_list_entries, load_json, save_json  # noqa: E402

AGY_PROJECTS_DIR = Path("~/.gemini/config/projects").expanduser()
DEFAULT_BRIDGE_CONFIG_DIR = Path("~/.config/agy-slack-bridge").expanduser()

GDRIVE_READONLY_SUBCOMMANDS = ["list", "search", "meta", "read", "download"]
# --profile は許可エントリの一部になるので、空白・引用符・パス区切りを含む名前は受け付けない
# (gdrive_cli.py 側の PROFILE_NAME_RE と同じ)。
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CHANNEL_ID_RE = re.compile(r"^[CG][A-Z0-9]{5,}$")
_EMOJI_RE = re.compile(r"^:?[^\s:]+:?$")   # 名前の文字種は絞らない（Slack 側が日本語・結合記号つきの名前なども許すため）。空白と途中の : だけ弾く
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# プロジェクトのルールは .agents/rules/*.md に置く(AGENTS.md は、エージェントが自由に
# 編集できるように、触らない)。agy は frontmatter(`trigger: always_on` など)の無い
# ルールファイルを、エラーも出さずに無視する(実機で確認済み)。
RULES_DIR = Path(".agents") / "rules"
SAFETY_RULES = "agent-safety-rules.md"
# 運用者がマシンごとに用意する任意のファイル(<設定ディレクトリ>/shared/ に置く)。
# あれば .agents/rules/ にリンクする。frontmatter を付けておくこと。
OPTIONAL_SHARED_FILES = ["local-tools.md", "gdrive-cli.md"]
# 以前のセットアップが作ったもの(.agents/<名前> のリンクと、AGENTS.md の読み込み行)。
LEGACY_INCLUDE_LABELS = {
    SAFETY_RULES: "エージェント安全規則",
    "local-tools.md": "このマシンで使えるツール",
    "gdrive-cli.md": "Google Drive の読み書き",
}


def project_deny_rules(target_dir: Path) -> list[str]:
    """プロジェクトごとの deny。安全規則のリンクだけを守る(.agents/rules/ の他のルールや
    AGENTS.md は、エージェントが編集できるままにする)。インストール先・設定ディレクトリを
    守る machine-wide の deny は install.py が書く。"""
    return [f"write_file({target_dir}/{RULES_DIR / SAFETY_RULES})"]


# --- 対話 ---------------------------------------------------------------------

def complete_path(text: str) -> list[str]:
    """タブ補完用: ~ を展開して一致するパスを返す(ディレクトリには / を付ける)。"""
    found = glob.glob(os.path.expanduser(text) + "*")
    return sorted(f + "/" if os.path.isdir(f) else f for f in found)


class path_completion:
    """入力の間だけ、readline のタブ補完をパス補完にする(readline が無ければ何もしない)。"""

    def __init__(self, enabled: bool):
        self.enabled = enabled and sys.stdin.isatty()

    def __enter__(self):
        if not self.enabled:
            return self
        try:
            import readline
        except ImportError:
            self.enabled = False
            return self
        self._rl, self._old = readline, (readline.get_completer(), readline.get_completer_delims())
        matches: list[str] = []

        def completer(text, state):
            nonlocal matches
            if state == 0:
                matches = complete_path(text)
            return matches[state] if state < len(matches) else None

        readline.set_completer(completer)
        readline.set_completer_delims(" \t\n")
        return self

    def __exit__(self, *exc):
        if self.enabled:
            self._rl.set_completer(self._old[0])
            self._rl.set_completer_delims(self._old[1])


class Prompter:
    """質問は Enter で既定値(現在値)、`-` で値を消す(None を返す)。"""

    def __init__(self, input_fn=input, secret_fn=getpass.getpass, out=print):
        self._input, self._secret, self.out = input_fn, secret_fn, out

    def text(self, label, default=None, validate=None, paths=False):
        hint = f" [{default}]" if default not in (None, "") else "（空欄でスキップ）"
        while True:
            with path_completion(paths):
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


def remove_project_hook(target_dir: Path) -> None:
    """以前のセットアップが置いた、プロジェクトごとの .agents/hooks.json のフックを外す。
    フックは今はグローバル(~/.gemini/config/hooks.json)なので、残すと二重に動き、
    先に実行履歴が更新されて検知が効かなくなる。他のフックは残す。"""
    path = target_dir / ".agents" / "hooks.json"
    if not (path.exists() or path.is_symlink()):
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print(f"  [警告] {path} を読めません。script-integrity フックが入っていれば手で外してください"
              "（グローバルのフックと二重に動きます）。")
        return
    if agy_settings.HOOK_NAME not in data:
        return
    data.pop(agy_settings.HOOK_NAME)
    if path.is_symlink() and data:
        sys.exit(f"エラー: {path} はシンボリックリンクで、他のフックも含みます。手で確認してください。")
    if data:
        save_json(path, data)
        print(f"  [更新] {path} から {agy_settings.HOOK_NAME} を外しました（グローバルのフックに移行）")
    else:
        path.unlink()
        print(f"  [削除] {path}（フックはグローバルの hooks.json に移行）")


def remove_legacy_includes(target_dir: Path, prefix: Path, config_dir: Path) -> None:
    """以前のセットアップが作った .agents/<名前> のリンク(自分が作ったもの、つまりインストール先/
    設定ディレクトリの shared を指すものだけ)と、AGENTS.md の読み込み行を取り除く。
    残すと、同じルールが二重に読まれる。"""
    owned = (prefix.resolve(), (config_dir / "shared").resolve())
    removed_names = []
    for name in LEGACY_INCLUDE_LABELS:
        link = target_dir / ".agents" / name
        if link.is_symlink() and any(o == link.resolve().parent or o in link.resolve().parents for o in owned):
            link.unlink()
            removed_names.append(name)
            print(f"  [削除] 以前のリンク .agents/{name}（.agents/rules/ に移行）")
    agents_md = target_dir / "AGENTS.md"
    if removed_names and agents_md.exists():
        lines = agents_md.read_text(encoding="utf-8").splitlines(keepends=True)
        keep = [l for l in lines if not any(f"](.agents/{n})" in l for n in removed_names)]
        if len(keep) != len(lines):
            agents_md.write_text("".join(keep), encoding="utf-8")
            print("  [更新] AGENTS.md から以前の読み込み行を外しました")


def warn_if_no_frontmatter(path: Path) -> None:
    try:
        head = path.read_text(encoding="utf-8")[:4]
    except OSError:
        return
    if not head.startswith("---"):
        print(f"  [警告] {path} に frontmatter（先頭の --- ... ---、trigger: always_on）がありません。"
              "エラーにならずに無視され、エージェントには渡りません（実機で確認済み）。")


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


def register_with_hook(target_dir: Path, config_dir: Path) -> None:
    """フックが状態(恒久許可の seal、実行履歴)を持つ場所を用意する。この場所がある
    プロジェクトだけが、フックの対象になる。置き場は設定ディレクトリ(エージェントは読み書きできない)。"""
    state = Path(script_integrity.state_dir(str(config_dir), str(target_dir)))
    if state.is_dir():
        print("  [OK] フックの状態ディレクトリは作成済み")
        return
    state.mkdir(parents=True)
    print(f"  [作成] フックの状態ディレクトリ: {state}")


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


def project_folder(data: dict) -> "Path | None":
    for res in (data.get("projectResources") or {}).get("resources", []):
        uri = res.get("folderUri") or (res.get("gitFolder") or {}).get("folderUri")
        if uri and uri.startswith("file://"):
            return Path(uri[len("file://"):])
    return None


def list_agy_projects() -> list[dict]:
    """agy に登録済みのプロジェクト(名前順)。folder は無いこともある(ワークスペース無し)。"""
    found = []
    for p in sorted(AGY_PROJECTS_DIR.glob("*.json")) if AGY_PROJECTS_DIR.is_dir() else []:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and data.get("id"):
            found.append({"id": data["id"], "name": data.get("name") or data["id"],
                          "folder": project_folder(data), "json": p})
    return sorted(found, key=lambda x: x["name"])


def confirm_existing_dir(ask: "Prompter", path: Path) -> bool:
    """入力されたパスが既にあるとき、ルール・設定の書き込み（上書き）を確認する。"""
    return not path.exists() or ask.yes_no(f"{path} は既に存在します。上書きしますか?", default=False)


def choose_target(ask: "Prompter") -> "tuple[Path | None, dict | None]":
    """(ディレクトリ, 既存プロジェクト)。既存を選ぶか、ディレクトリのパスを直接入力する。"""
    projects = list_agy_projects()
    if projects:
        print("  agy に登録済みのプロジェクト:")
        for i, pr in enumerate(projects, 1):
            where = pr["folder"] or "(ディレクトリ無し)"
            print(f"    {i}) {pr['name']}  {where}  [{pr['id']}]")
        print("    0) 新しく作る")
    while True:
        raw = ask.text("番号（0=新規）またはパス" if projects else "ディレクトリのパス", paths=True)
        if not raw:
            sys.exit("エラー: 対象が必要です。")
        if raw.isdigit() and projects:
            n = int(raw)
            if 1 <= n <= len(projects):
                pr = projects[n - 1]
                return (pr["folder"].resolve() if pr["folder"] else None), pr
            if n == 0:
                raw = ask.text("ディレクトリのパス", paths=True)
                if not raw:
                    sys.exit("エラー: 対象が必要です。")
            else:
                print("    一覧にある番号を入力してください")
                continue
        path = Path(raw).expanduser().resolve()
        if not confirm_existing_dir(ask, path):
            sys.exit("中止しました。")
        return path, None


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


def gdrive_allow_entries(prefix: Path, profile: str, subs: "list[str] | None" = None) -> list[str]:
    # --profile はサブコマンドより前に固定する(末尾の自由引数にすると、エージェントが
    # 別プロジェクトのプロファイルを選べてしまう)。
    py = prefix / ".venv" / "bin" / "python"
    script = prefix / "tools" / "gdrive_cli.py"
    return [f"command({py} -I {script} --profile {profile} {sub})"
            for sub in (GDRIVE_READONLY_SUBCOMMANDS if subs is None else subs)]


def current_gdrive_profile(project_json: Path, prefix: Path) -> "str | None":
    grants = ((load_json(project_json).get("permissionGrants") or {}).get("permissionGrants") or {})
    marker = f"{prefix}/tools/gdrive_cli.py --profile "
    for e in grants.get("allow", []):
        if marker in e:
            return e.split(marker, 1)[1].split()[0]
    return None


def default_profile_name(project_id: str) -> str:
    """プロファイルの既定名は agy のプロジェクト ID（一意）。他のプロジェクトと重ならない。"""
    return re.sub(r"[^A-Za-z0-9_-]", "-", project_id)


def profile_users(prefix: Path, profile: str, exclude_id: "str | None" = None) -> list[str]:
    """このプロファイルを許可エントリに使っているプロジェクトの名前。"""
    return [pr["name"] for pr in list_agy_projects()
            if pr["id"] != exclude_id and current_gdrive_profile(pr["json"], prefix) == profile]


def remove_project_gdrive_allow(project_json: Path, prefix: Path, profile: str) -> None:
    """このプロジェクトの許可から、そのプロファイルの Drive CLI のエントリを全部外す。"""
    head = gdrive_allow_entries(prefix, profile, ["x"])[0].rsplit(" x)", 1)[0] + " "
    data = load_json(project_json)
    allow = ((data.get("permissionGrants") or {}).get("permissionGrants") or {}).get("allow")
    gone = [e for e in (allow or []) if e.startswith(head)]
    if not gone:
        return
    for e in gone:
        allow.remove(e)
        print(f"  [削除] {project_json.name} permissionGrants.allow: {e}")
    save_json(project_json, data)


def delete_profile_files(config_dir: Path, profile: str) -> None:
    for name in (f"{profile}.json", f"{profile}.sa.json"):
        f = config_dir / "gdrive" / name
        if f.exists():
            f.unlink()
            print(f"  [削除] {f}")


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


def update_project_gdrive_allow(project_json: Path, prefix: Path, profile: str,
                                enabled: "set[str] | None" = None) -> None:
    """読み取り系サブコマンドの許可を、そのプロジェクト自身の permissionGrants にだけ入れる。
    enabled(プロファイルで有効なコマンド)が分かっているときは、無効なコマンドの、この
    スクリプトが以前入れた許可を取り除く。書き込み系は許可に入れない(毎回の承認)。"""
    data = load_json(project_json)
    grants = data.setdefault("permissionGrants", {}).setdefault("permissionGrants", {})
    subs = [c for c in GDRIVE_READONLY_SUBCOMMANDS if enabled is None or c in enabled]
    added = ensure_list_entries(grants, "allow", gdrive_allow_entries(prefix, profile, subs))
    stale = [e for e in gdrive_allow_entries(prefix, profile, [c for c in GDRIVE_READONLY_SUBCOMMANDS if c not in subs])
             if e in grants.get("allow", [])]
    for e in stale:
        grants["allow"].remove(e)
    if added or stale:
        save_json(project_json, data)
    for e in added:
        print(f"  [追加] {project_json.name} permissionGrants.allow: {e}")
    for e in stale:
        print(f"  [削除] {project_json.name} permissionGrants.allow: {e}（プロファイルで無効）")
    if not added and not stale:
        print(f"  [OK] {project_json.name} は対応済み")


# --- Drive CLI のプロファイル(方針の対話設定) -----------------------------------------

_SCOPE_PREFIX = "https://www.googleapis.com/auth/"


def _csv(value: "str | None") -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def _validate_commands(v):
    names = _csv(v)
    if v.strip() == "all":
        return None
    bad = [n for n in names if n not in gdrive_policy.ALL_COMMANDS]
    return f"未知のコマンド: {', '.join(bad)}（{', '.join(gdrive_policy.ALL_COMMANDS)}、または all）" if bad or not names else None


def _validate_folders(v):
    bad = [x for x in _csv(v) if gdrive_cli.extract_drive_id(x) is None]
    return f"Drive のフォルダ URL または ID ではありません: {', '.join(bad)}" if bad else None


def _validate_scope(v):
    return None if v.startswith(_SCOPE_PREFIX) else f"{_SCOPE_PREFIX}… の形で入力してください"


def _validate_kind(v):
    return None if v in ("service_account", "oauth") else "service_account か oauth"


def _validate_credential_command(v):
    try:
        return None if shlex.split(v) else "コマンドを入力してください"
    except ValueError as e:
        return f"引用符が閉じていません: {e}"


def _validate_upload_mode(v):
    return None if v in ("k", "a", "r") else "k / a / r のどれか"


def _validate_sa_key_file(v):
    path = Path(v).expanduser()
    try:
        key = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return f"読めません: {e}"
    if not isinstance(key, dict) or key.get("type") != "service_account" \
            or not key.get("client_email") or not key.get("private_key"):
        return "サービスアカウントの JSON 鍵ではありません（type / client_email / private_key が必要）"
    return None


def store_sa_key(src: Path, config_dir: Path, profile: str) -> "tuple[Path, str]":
    """鍵を <設定ディレクトリ>/gdrive/<プロファイル名>.sa.json(0600)に置く。
    設定ディレクトリは agy の deny 対象で、エージェントは読めない。(置き場, client_email)を返す。"""
    key = json.loads(src.read_text(encoding="utf-8"))
    dest = config_dir / "gdrive" / f"{profile}.sa.json"
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(key, indent=2) + "\n")
    os.chmod(dest, 0o600)
    return dest, key["client_email"]


def ask_new_profile(ask: Prompter, config_dir: Path, profile: str) -> "dict | None":
    kind = ask.text("認証の種類（service_account / oauth）", default="service_account", validate=_validate_kind)
    credentials_paths = None
    if kind == "service_account":
        key_file = ask.text("サービスアカウントの JSON 鍵のパス（空欄で別の保管先）", validate=_validate_sa_key_file, paths=True)
        if key_file:
            dest, email = store_sa_key(Path(key_file).expanduser(), config_dir, profile)
            print(f"  [保存] {dest}（0600）。元の鍵ファイルは削除してください")
            print(f"  Drive で使わせたいフォルダを、このアドレスに共有してください: {email}")
            cat = shutil.which("cat") or "cat"
            return _finish_profile(kind, [cat, str(dest)], [str(dest)])
    command = ask.text("資格情報を取り出すコマンド",
                       validate=_validate_credential_command)
    if not command:
        print("  [スキップ] コマンドが未入力")
        return None
    head = shlex.split(command)[0]
    if not (shutil.which(head) or Path(head).expanduser().is_file()):
        if not ask.yes_no(f"`{head}` というコマンドが見つかりません。このまま使いますか?", default=False):
            print("  [スキップ] コマンドを確認してから、もう一度実行してください")
            return None
    return _finish_profile(kind, shlex.split(command), None)


def _finish_profile(kind: str, argv: list[str], paths: "list[str] | None") -> dict:
    data = gdrive_policy.new_profile(kind, argv)
    if paths:
        data["credentials"]["paths"] = paths
    return data


def ask_scopes(ask: Prompter, current: "dict | None" = None) -> "dict | None":
    """既定と同じなら None(プロファイルには書かない)。current は今の設定（入力の既定にする）。"""
    current = current or {}
    read = ask.text("読み取りに使うスコープ", default=current.get("read") or gdrive_cli.SCOPE_READ_DEFAULT,
                    validate=_validate_scope)
    write = ask.text("書き込みに使うスコープ", default=current.get("write") or gdrive_cli.SCOPE_WRITE_DEFAULT,
                     validate=_validate_scope)
    if (read, write) == (gdrive_cli.SCOPE_READ_DEFAULT, gdrive_cli.SCOPE_WRITE_DEFAULT):
        return None
    return {"read": read, "write": write}


def ask_policy(ask: Prompter, old: dict) -> dict:
    new = {k: list(v) for k, v in old.items()}
    cmds = ask.text("有効にするコマンド（カンマ区切り or all）", default=",".join(old["commands"]),
                    validate=_validate_commands)
    new["commands"] = gdrive_policy.ALL_COMMANDS[:] if (cmds or "").strip() == "all" else _csv(cmds)
    folders = ask.text("許可フォルダ（URL か ID、カンマ区切り。`-` で制限なし）", default=",".join(old["allowed_folders"]) or None,
                       validate=_validate_folders)
    new["allowed_folders"] = [gdrive_cli.extract_drive_id(x) for x in _csv(folders)]
    mode = ask.text("アップロード拒否パターン（k=今のまま / a=追加 / r=置換）", default="k",
                    validate=_validate_upload_mode)
    if mode == "a":
        for key, label in (("deny_names", "追加する拒否ファイル名パターン（カンマ区切り）"),
                           ("deny_dirs", "追加する拒否ディレクトリ名（カンマ区切り）")):
            extra = _csv(ask.text(label))
            new[key] = new[key] + [x for x in extra if x not in new[key]]
    elif mode == "r":
        names = ask.text("拒否ファイル名パターン（カンマ区切り）", default=",".join(old["deny_names"]) or None)
        dirs = ask.text("拒否ディレクトリ名（カンマ区切り）", default=",".join(old["deny_dirs"]) or None)
        new["deny_names"], new["deny_dirs"] = _csv(names), _csv(dirs)
    return new


def configure_gdrive_profile(ask: Prompter, config_dir: Path, profile: str) -> "set[str] | None":
    """プロファイル(<設定ディレクトリ>/gdrive/NAME.json)の方針を対話で設定して書く。
    有効な読み取り系を含むコマンドの集合を返す。プロファイルを扱えなかったとき
    (作らない・壊れている等)は None。"""
    path = config_dir / "gdrive" / f"{profile}.json"
    try:
        data = gdrive_policy.read_profile(path)
    except (ValueError, OSError) as e:
        print(f"  [警告] {path} を読めません（{e}）。方針の設定はスキップします（直してから再実行してください）。")
        return None
    created = data is None
    if created:
        data = ask_new_profile(ask, config_dir, profile)
        if data is None:
            return None
    else:
        try:
            gdrive_cli.parse_profile(data)
        except gdrive_cli.ProfileError as e:
            print(f"  [警告] {path} が不正です（{e}）。方針の設定はスキップします。")
            return None

    old = gdrive_policy.policy_from_data(data)
    print("    現在の方針:" if not created else "    方針（既定値）:")
    for line in gdrive_policy.describe(old):
        print(f"      {line}")
    new = old
    if ask.yes_no("詳細設定（スコープ・方針）を変更しますか?" if created else "詳細設定（方針）を変更しますか?",
                  default=False):
        if created:
            scopes = ask_scopes(ask)
            if scopes:
                data["scopes"] = scopes
        new = ask_policy(ask, old)
        for line in gdrive_policy.diff(old, new):
            print(f"      変更: {line}")
        looser = gdrive_policy.loosening(old, new)
        if looser:
            print("    [注意] 権限が広がる変更が含まれます:")
            for line in looser:
                print(f"      - {line}")
            if not ask.yes_no("緩める変更を適用しますか?", default=False):
                print("    [取消] 方針の変更を取り消しました")
                new = old
    if created or new != old:
        gdrive_policy.write_profile(path, gdrive_policy.apply_policy(data, new))
        print(f"  [書込] {path}")
    else:
        print(f"  [OK] {path} は変更なし")
    return {c for c in new["commands"] if c in GDRIVE_READONLY_SUBCOMMANDS}


# --- ブリッジの設定(config.yaml / env) ----------------------------------------------

def _validate_channel_id(v):
    return None if _CHANNEL_ID_RE.match(v) else "Slack のチャンネル ID（C… または G…）を入力してください"


def _validate_emoji(v):
    return None if _EMOJI_RE.match(v) else "絵文字の名前（例 robot_face または :robot_face:）を入力してください（空白は使えません）"


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


def configure_channel(ask: Prompter, config_path: Path, project_id: str, project_name: str,
                      gate: bool = False) -> None:
    if not config_path.exists():
        print(f"  [スキップ] {config_path} がありません（先に install.py を実行してください）")
        return
    text = config_path.read_text(encoding="utf-8")
    channels = bridge_config.read_channels(text)
    mine = [cid for cid, e in channels.items() if (e or {}).get("project") == project_id]
    if mine:
        print(f"  このプロジェクトに割り当て済みのチャンネル: {', '.join(mine)}")
    if gate and not ask.yes_no("Slack チャンネルを対応づけますか?", default=not mine):
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
    _set_or_pop(entry, "display_name", ask.text("表示名", default=entry.get("display_name")))
    emoji = ask.text("アイコン絵文字", default=entry.get("icon_emoji"), validate=_validate_emoji)
    if emoji:
        emoji = f":{emoji.strip(':')}:"
    _set_or_pop(entry, "icon_emoji", emoji)
    if emoji:
        entry.pop("icon_url", None)
    else:
        _set_or_pop(entry, "icon_url", ask.text("アイコン画像 URL", default=entry.get("icon_url"),
                                                validate=_validate_url))
    _set_or_pop(entry, "model", ask.text("モデル",
                                         default=entry.get("model")))
    _set_or_pop(entry, "effort", ask.text(f"effort（{' / '.join(EFFORTS)}）", default=entry.get("effort"),
                                          validate=_validate_effort))
    watched = ask.text("監視するファイル（相対パス、カンマ区切り）",
                       default=",".join(entry.get("watched_config_files") or []) or None,
                       validate=_validate_rel_paths)
    _set_or_pop(entry, "watched_config_files", [s.strip() for s in watched.split(",")] if watched else None)
    telemetry = dict(entry.get("telemetry") or {})
    stats = ask.yes_no("権限ボタンの統計を取りますか?", default=bool(telemetry.get("permission_stats")))
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

class Session:
    """セットアップの状態(対象プロジェクトなど)と、段階ごとの処理。メニューの各項目と
    `--all`(順に全部)が、同じ処理を使う。"""

    def __init__(self, ask: Prompter, prefix: Path, config_dir: Path):
        self.ask, self.prefix, self.config_dir = ask, prefix, config_dir
        self.target_dir: "Path | None" = None
        self.existing: "dict | None" = None
        self.project_json: "Path | None" = None
        self.project_id = self.project_name = None
        self.selected = False

    # --- 対象のプロジェクト ---
    def open_project(self, target_dir: "Path | None", existing: "dict | None" = None) -> None:
        """対象を決める。ディレクトリが無ければ作り、git リポジトリでなければ git init し、
        agy に未登録なら登録する(いずれも確認なし。結果を表示する)。ルールやフックの配置は setup_files。"""
        ask, prefix, config_dir = self.ask, self.prefix, self.config_dir
        if target_dir is not None:
            if prefix == target_dir or prefix in target_dir.parents or config_dir in (target_dir, *target_dir.parents):
                sys.exit("エラー: インストール先・設定ディレクトリの中をプロジェクトにはできません。")
            print(f"=== {target_dir} ===")
            if not target_dir.is_dir():
                target_dir.mkdir(parents=True)
                print(f"  [作成] {target_dir}")
            if not (target_dir / ".git").exists():
                # git 管理は必須(恒久許可コマンドの commit-before-run 判定の前提)
                subprocess.run(["git", "init", "-q", str(target_dir)], check=True)
                print(f"  [git init] {target_dir} は git リポジトリではなかったので初期化しました（git 管理は必須です。コミットはしていません）")
        else:
            print(f"=== {existing['name']}（ディレクトリ無しのプロジェクト） ===")
        self.target_dir, self.existing, self.selected = target_dir, existing, True

        print("-- agy プロジェクト --")
        project_json = existing["json"] if existing else find_agy_project_json(target_dir)
        if project_json is None:
            register_agy_project(target_dir)
            project_json = find_agy_project_json(target_dir)
            if project_json is None:
                print("  [失敗] agy への登録ができませんでした。許可の登録とチャンネルの対応づけは行えません"
                      "（agy にログインしているか確認して、もう一度やり直してください）。")
            else:
                print("  [登録] agy に登録しました（agy --new-project）")
        self.project_json = project_json
        self.project_id = self.project_name = None
        if project_json is not None:
            project = load_json(project_json)
            self.project_id = project["id"]
            self.project_name = project.get("name") or (target_dir.name if target_dir else self.project_id)
            print(f"  [OK] {self.project_name}（{self.project_id}）")

    def setup_files(self) -> None:
        """ルール・フック・agy の設定(deny / trustedWorkspaces)をプロジェクトに配置・更新する。"""
        target_dir, prefix, config_dir = self.target_dir, self.prefix, self.config_dir
        if target_dir is None:
            print("  [スキップ] ディレクトリが無いので、ファイルの配置は行いません")
            return
        print("-- 共有ファイル --")
        remove_legacy_includes(target_dir, prefix, config_dir)
        absolute_symlink(target_dir, f"{RULES_DIR / SAFETY_RULES}", prefix / "tools" / SAFETY_RULES)
        for name in OPTIONAL_SHARED_FILES:
            src = config_dir / "shared" / name
            if src.exists():
                absolute_symlink(target_dir, f"{RULES_DIR / name}", src)
                warn_if_no_frontmatter(src)
        remove_project_hook(target_dir)
        register_with_hook(target_dir, config_dir)
        ensure_scratch_ignored(target_dir)

        print("-- machine-wide settings --")
        agy_settings.update_settings(project_deny_rules(target_dir), [str(target_dir)])

    def project(self, target_arg: "str | None" = None) -> None:
        """対象を選んで(または指定されて)決め、ファイルを配置する(`--all` 用)。"""
        if target_arg:
            self.open_project(Path(target_arg).expanduser().resolve())
        else:
            self.open_project(*choose_target(self.ask))
        self.setup_files()

    def ready(self) -> bool:
        if not self.selected or self.project_json is None:
            print("  agy に登録されたプロジェクトが必要です。")
            return False
        return True

    # --- Drive CLI ---
    def drive(self, gate: bool = False) -> None:
        if not self.ready():
            return
        ask, prefix, config_dir = self.ask, self.prefix, self.config_dir
        print("-- Google Drive CLI --")
        current = current_gdrive_profile(self.project_json, prefix)
        if gate and not ask.yes_no("Drive CLI を使いますか?", default=current is not None):
            return
        if re.search(r"\s", str(prefix)):
            print("  [スキップ] インストール先のパスに空白があると許可エントリを安全に作れません。")
            return
        if current and not gate:
            self.drive_menu()
            return
        self.configure_profile(current or self.pick_profile())

    def configure_profile(self, profile: str) -> None:
        """プロファイルの作成・方針の設定と、このプロジェクトへの許可の登録。"""
        ask, prefix, config_dir = self.ask, self.prefix, self.config_dir
        print(f"  プロファイル: {profile}（{config_dir / 'gdrive' / (profile + '.json')}）")
        others = profile_users(prefix, profile, self.project_id)
        if others:
            print(f"  [注意] このプロファイルは {', '.join(others)} と共有です。方針を変えると、どちらにも反映されます。")
        enabled = configure_gdrive_profile(ask, config_dir, profile)
        update_project_gdrive_allow(self.project_json, prefix, profile, enabled)
        warn_if_profile_missing(config_dir, profile)
        warn_if_gdrive_packages_missing(prefix)

    def drive_menu(self) -> None:
        """設定済みの Drive CLI の更新メニュー。"""
        ask, prefix, config_dir = self.ask, self.prefix, self.config_dir
        while True:
            current = current_gdrive_profile(self.project_json, prefix)
            if not current:
                return
            others = profile_users(prefix, current, self.project_id)
            shared = f"  共有: {', '.join(others)}" if others else ""
            key = choose(ask, f"Google Drive CLI — プロファイル {current}{shared}",
                         [("1", "方針（コマンド・許可フォルダ・アップロード拒否）を変更する"),
                          ("2", "認証情報を入れ直す（鍵・取り出しコマンド・スコープ）"),
                          ("3", "別のプロファイルに切り替える／専用に作り直す"),
                          ("4", "このプロジェクトの Drive 設定を外す"),
                          ("b", "戻る")], "b")
            if key == "b":
                return
            if key == "1":
                self.configure_profile(current)
            elif key == "2":
                self.reset_credentials(current, others)
            elif key == "3":
                self.switch_profile(current)
            elif key == "4":
                self.detach_drive(current)
                return

    def reset_credentials(self, profile: str, others: "list[str]") -> None:
        ask, config_dir = self.ask, self.config_dir
        path = config_dir / "gdrive" / f"{profile}.json"
        try:
            data = gdrive_policy.read_profile(path)
            gdrive_cli.parse_profile(data or {})
        except (ValueError, OSError, gdrive_cli.ProfileError) as e:
            print(f"  [警告] {path} を読めません（{e}）。")
            return
        if data is None:
            print(f"  [警告] {path} がありません。方針の変更から作り直してください。")
            return
        if others and not ask.yes_no(f"このプロファイルは {', '.join(others)} と共有です。入れ直すと、どちらにも反映されます。続けますか?",
                                    default=False):
            return
        new = ask_new_profile(ask, config_dir, profile)
        if new is None:
            return
        data["kind"], data["credentials"] = new["kind"], new["credentials"]
        if ask.yes_no("スコープを変更しますか?", default=False):
            scopes = ask_scopes(ask, data.get("scopes"))
            if scopes:
                data["scopes"] = scopes
            else:
                data.pop("scopes", None)
        gdrive_policy.write_profile(path, data)
        print(f"  [書込] {path}")
        sa = config_dir / "gdrive" / f"{profile}.sa.json"
        if sa.exists() and str(sa) not in json.dumps(data["credentials"]):
            sa.unlink()
            print(f"  [削除] {sa}（使わなくなった鍵）")

    def switch_profile(self, current: str) -> None:
        ask, prefix, config_dir = self.ask, self.prefix, self.config_dir
        own = default_profile_name(self.project_id)
        options, names = [], {}
        if current != own:
            exists = (config_dir / "gdrive" / f"{own}.json").exists()
            options.append(("n", f"このプロジェクト専用のプロファイル（{own}）を{'使う' if exists else '新しく作る'}"))
            names["n"] = own
        found = sorted(f.stem for f in (config_dir / "gdrive").glob("*.json")
                       if _PROFILE_NAME_RE.match(f.stem) and f.stem not in (current, own)) \
            if (config_dir / "gdrive").is_dir() else []
        for i, name in enumerate(found, 1):
            users = profile_users(prefix, name, self.project_id)
            who = f"「{'」「'.join(users)}」プロジェクト" if users else f"プロファイル {name}"
            options.append((str(i), f"{who}と同じ設定・権限を使う（{name}）"))
            names[str(i)] = name
        if not options:
            print("  切り替え先がありません（専用のプロファイルを使用中で、他のプロファイルもありません）。")
            return
        options.append(("b", "戻る"))
        key = choose(ask, "切り替え先", options, "b")
        if key == "b":
            return
        new = names[key]
        remove_project_gdrive_allow(self.project_json, prefix, current)
        self.configure_profile(new)
        self.offer_delete_if_unused(current)

    def detach_drive(self, current: str) -> None:
        remove_project_gdrive_allow(self.project_json, self.prefix, current)
        print("  [OK] このプロジェクトでは Drive CLI を使えなくなりました。")
        self.offer_delete_if_unused(current)

    def offer_delete_if_unused(self, profile: str) -> None:
        if profile_users(self.prefix, profile, self.project_id) or current_gdrive_profile(self.project_json, self.prefix) == profile:
            return
        path = self.config_dir / "gdrive" / f"{profile}.json"
        if path.exists() and self.ask.yes_no(
                f"プロファイル {profile} はどのプロジェクトからも使われていません。認証情報ごと削除しますか?", default=False):
            delete_profile_files(self.config_dir, profile)

    def pick_profile(self) -> str:
        """新しいプロファイルを作るか、他のプロジェクトのプロファイル(同じ認証情報・方針)を使うかを選ぶ。"""
        new = default_profile_name(self.project_id)
        existing = sorted(p.stem for p in (self.config_dir / "gdrive").glob("*.json")
                          if _PROFILE_NAME_RE.match(p.stem) and p.stem != new) \
            if (self.config_dir / "gdrive").is_dir() else []
        if not existing:
            return new
        options = [("n", f"新しいプロファイルを作る（{new}）")]
        names = {}
        for i, name in enumerate(existing, 1):
            users = profile_users(self.prefix, name, self.project_id)
            who = f"「{'」「'.join(users)}」プロジェクト" if users else f"プロファイル {name}"
            options.append((str(i), f"{who}と同じ設定・権限を使う（{name}）"))
            names[str(i)] = name
        key = choose(self.ask, "Google Drive CLI のプロファイル", options, "n")
        return names.get(key, new)

    # --- Slack ---
    def channel(self, gate: bool = False) -> None:
        if not self.ready():
            return
        print("-- Slack チャンネル --")
        configure_channel(self.ask, self.config_dir / "config.yaml", self.project_id, self.project_name, gate)

    def tokens(self) -> None:
        print("-- Slack トークン --")
        configure_tokens(self.ask, self.config_dir / "env")

    # --- 状態 ---
    def mapped_channels(self, project_id: str) -> list[str]:
        cfg = self.config_dir / "config.yaml"
        if not cfg.exists():
            return []
        try:
            channels = bridge_config.read_channels(cfg.read_text(encoding="utf-8"))
        except Exception:
            return []
        return [cid for cid, e in channels.items() if (e or {}).get("project") == project_id]

    def status(self) -> None:
        print("-- 現在の状態 --")
        if not self.selected:
            print("  プロジェクト: 未選択")
        else:
            print(f"  プロジェクト: {self.project_name or '未登録'}  {self.target_dir or '(ディレクトリ無し)'}")
        if self.project_json is not None:
            profile = current_gdrive_profile(self.project_json, self.prefix)
            print(f"  Drive CLI: {'プロファイル ' + profile if profile else '使わない'}")
            mine = self.mapped_channels(self.project_id)
            print(f"  Slack チャンネル: {', '.join(mine) if mine else '未設定'}")
        env = self.config_dir / "env"
        if env.exists():
            text = env.read_text(encoding="utf-8")
            for key, hint in (("SLACK_BOT_TOKEN", "xoxb-"), ("SLACK_APP_TOKEN", "xapp-")):
                value = bridge_config.get_env_value(text, key)
                print(f"  {key}: {'設定済み' if value and value != hint + '...' else '未設定'}")

    def run_all(self, target_arg: "str | None" = None) -> None:
        self.project(target_arg)
        if self.project_json is not None:
            self.drive(gate=True)
            self.channel(gate=True)
        self.tokens()
        print("\n完了。設定を変えた場合は、ブリッジを再起動してください。")


# --- メニュー ------------------------------------------------------------------------

def choose(ask: Prompter, title: str, options: "list[tuple[str, str]]", default: str) -> str:
    """番号・キーを選ぶメニュー。Enter で default。"""
    print(f"\n{title}")
    for key, label in options:
        print(f"    {key}) {label}")
    keys = [k for k, _ in options]
    return ask.text("選択", default=default,
                    validate=lambda v: None if v in keys else "表示されている番号かキーを入力してください")


def project_menu(session: Session) -> bool:
    """プロジェクトのメニュー。True を返したときは、セットアップ全体を終了する。"""
    ask = session.ask
    options = [("1", "順に全部設定する（初めて・更新）"),
               ("2", "ルールとフックを配置・更新する"),
               ("3", "Google Drive CLI を設定する"),
               ("4", "Slack チャンネルとの対応づけを設定する"),
               ("5", "現在の状態を表示する"),
               ("b", "プロジェクトの一覧に戻る"),
               ("q", "終了")]
    while True:
        key = choose(ask, f"プロジェクト: {session.project_name or '未登録'}  {session.target_dir or ''}", options, "b")
        try:
            if key == "b":
                return False
            if key == "q":
                return True
            if key == "1":
                session.setup_files()
                if session.project_json is not None:
                    session.drive(gate=True)
                    session.channel(gate=True)
            elif key == "2":
                session.setup_files()
            elif key == "3":
                session.drive()
            elif key == "4":
                session.channel()
            elif key == "5":
                session.status()
        except SystemExit as e:           # 段階の中の sys.exit(メッセージ) でメニューを閉じない
            if e.code not in (None, 0):
                print(e.code)


def main_menu(session: Session) -> None:
    ask = session.ask
    while True:
        projects = list_agy_projects()
        options = []
        for i, pr in enumerate(projects, 1):
            marks = session.mapped_channels(pr["id"])
            options.append((str(i), f"{pr['name']}  {pr['folder'] or '(ディレクトリ無し)'}"
                                    + (f"  [Slack: {', '.join(marks)}]" if marks else "")))
        options += [("n", "新しいプロジェクトを作る"), ("t", "Slack のトークンを設定する（全体で共通）"), ("q", "終了")]
        key = choose(ask, "agy-slack-bridge セットアップ — 設定するプロジェクトを選んでください",
                     options, "q")
        try:
            if key == "q":
                print("設定を変えた場合は、ブリッジを再起動してください。")
                return
            if key == "t":
                session.tokens()
            elif key == "n":
                raw = ask.text("新しいプロジェクトのディレクトリのパス", paths=True)
                path = Path(raw).expanduser().resolve() if raw else None
                if path and confirm_existing_dir(ask, path):
                    session.open_project(path)
                    if project_menu(session):
                        key = "q"
            else:
                pr = projects[int(key) - 1]
                session.open_project(pr["folder"].resolve() if pr["folder"] else None, pr)
                if project_menu(session):
                    key = "q"
            if key == "q":
                print("設定を変えた場合は、ブリッジを再起動してください。")
                return
        except SystemExit as e:
            if e.code not in (None, 0):
                print(e.code)
        except (KeyboardInterrupt, EOFError):
            print("\n中断しました。")
            return


def main(argv: "list[str] | None" = None, ask: "Prompter | None" = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", help="セットアップするプロジェクトのディレクトリ"
                    "（省略すると、登録済みのプロジェクトから選ぶか、パスを入力する）")
    ap.add_argument("--prefix", type=Path, default=HERE.parent)
    ap.add_argument("--bridge-config-dir", type=Path, default=DEFAULT_BRIDGE_CONFIG_DIR)
    ap.add_argument("--all", action="store_true", help="メニューを出さず、全部の段階を順に実行して終わる")
    args = ap.parse_args(argv)

    if ask is None:
        if not sys.stdin.isatty():
            sys.exit("エラー: 対話形式のコマンドです。端末から実行してください。")
        ask = Prompter()

    prefix = args.prefix.expanduser().resolve()
    config_dir = args.bridge_config_dir.expanduser().resolve()
    if not (prefix / "bridge.py").exists() or not (prefix / "tools" / "hooks.json").exists():
        sys.exit(f"エラー: {prefix} にインストールされていません。先に tools/install.py を実行してください。")

    session = Session(ask, prefix, config_dir)
    if args.all or type(ask) is not Prompter:   # 自動実行(テストの Prompter の差し替えを含む)は一本道
        session.run_all(args.target)
    elif args.target:
        session.open_project(Path(args.target).expanduser().resolve())
        project_menu(session)
    else:
        main_menu(session)


if __name__ == "__main__":
    main()
