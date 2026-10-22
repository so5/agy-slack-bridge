#!/usr/bin/env python3
"""
ブリッジ・ツール・フックを、固定のインストール先にインストールする(要・人間が実行)
================================================================================
エージェントが書き換えられる場所(各 agy プロジェクトの中、git のチェックアウト)に
権限モデルの要を置かないための、インストール手順です。インストール先は、
agy の deny ルールで読み書きを禁じる対象になります。

使い方(リポジトリのチェックアウトから):
    python3 tools/install.py [--prefix DIR] [--config-dir DIR] [--with-gdrive]

  --prefix DIR       インストール先(既定: ~/.local/share/agy-slack-bridge)。
                     root で /opt 以下に入れる場合など、別の場所も指定できる。
  --config-dir DIR   設定ディレクトリ(既定: ~/.config/agy-slack-bridge)。
                     config.yaml / env のひな形を作る(既にあれば上書きしない)。
  --with-gdrive      tools/gdrive_cli.py 用の Google 系パッケージも venv に入れる。
  --no-venv          venv の作成とパッケージのインストールを行わない。
  --no-config        設定ディレクトリを作らない。
  --config-only      インストール先には触れず、設定ディレクトリだけ作る
                     (root で --prefix /opt/... に入れた後、通常ユーザーで実行する)。

インストール先のレイアウトはリポジトリと同じです:
    <prefix>/bridge.py, requirements*.txt, config.example.yaml, env.example
    <prefix>/tools/              (setup_agy_project.py, gdrive_cli.py, hooks/ ...)
    <prefix>/.venv/
    <prefix>/systemd/agy-slack-bridge.service   (このインストール先を指す unit)

冪等です。再実行するとコードを入れ替えます(アップグレード)。設定ディレクトリの
既存ファイルは決して上書きしません。インストールしたファイルは読み取り専用(0444)で
置きます(同じユーザーなら chmod できるので、事故防止であって防御ではありません。
防御は deny ルールと、root 所有のインストール先です)。
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PREFIX = Path("~/.local/share/agy-slack-bridge").expanduser()
DEFAULT_CONFIG_DIR = Path("~/.config/agy-slack-bridge").expanduser()

# リポジトリ直下から、そのままコピーするファイル。
ROOT_FILES = ["bridge.py", "requirements.txt", "requirements-gdrive.txt",
              "config.example.yaml", "env.example", "LICENSE"]
# ディレクトリごと同期するもの(インストール先に残った古いファイルは消す)。
SYNC_DIRS = ["tools"]
IGNORED_NAMES = {"__pycache__", ".pytest_cache"}

SYSTEMD_UNIT = """\
[Unit]
Description=Slack <-> Antigravity CLI (agy) bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={prefix}
EnvironmentFile={config_dir}/env
Environment=AGY_BRIDGE_CONFIG={config_dir}/config.yaml
Environment=AGY_BRIDGE_STATE_DB={config_dir}/state.sqlite3
ExecStart={prefix}/.venv/bin/python {prefix}/bridge.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


def install_file(src: Path, dest: Path) -> bool:
    """src を dest へ 0444 でコピーする。内容が同じなら何もしない。変えたら True。"""
    if dest.exists() and dest.read_bytes() == src.read_bytes():
        os.chmod(dest, 0o444)
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        dest.unlink()  # 0444 のファイルは、置き場のディレクトリが書ければ消せる
    shutil.copyfile(src, dest)
    os.chmod(dest, 0o444)
    return True


def sync_tree(src: Path, dest: Path) -> list[str]:
    """src の木を dest に写す。dest にしか無いファイルは消す。変えたパスの一覧を返す。"""
    changed = []
    wanted = set()
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(part in IGNORED_NAMES for part in rel.parts) or path.suffix == ".pyc":
            continue
        if path.is_dir():
            (dest / rel).mkdir(parents=True, exist_ok=True)
        else:
            wanted.add(rel)
            if install_file(path, dest / rel):
                changed.append(str(rel))
    if dest.is_dir():
        for path in sorted(dest.rglob("*"), reverse=True):
            rel = path.relative_to(dest)
            if path.is_dir():
                if not any(path.iterdir()):
                    path.rmdir()
            elif rel not in wanted:
                path.unlink()
                changed.append(f"{rel} (削除)")
    return changed


def install_code(prefix: Path) -> None:
    if prefix.resolve() == REPO_ROOT:
        sys.exit("エラー: インストール先がこのチェックアウト自身です。別の --prefix を指定してください。")
    prefix.mkdir(parents=True, exist_ok=True)
    for name in ROOT_FILES:
        if (REPO_ROOT / name).exists() and install_file(REPO_ROOT / name, prefix / name):
            print(f"  [更新] {name}")
    for d in SYNC_DIRS:
        for rel in sync_tree(REPO_ROOT / d, prefix / d):
            print(f"  [更新] {d}/{rel}")


def install_venv(prefix: Path, with_gdrive: bool) -> None:
    venv = prefix / ".venv"
    if not (venv / "bin" / "python").exists():
        print("  [作成] .venv")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    reqs = ["-r", str(prefix / "requirements.txt")]
    if with_gdrive:
        reqs += ["-r", str(prefix / "requirements-gdrive.txt")]
    subprocess.run([str(venv / "bin" / "pip"), "install", "-q", *reqs], check=True)
    print("  [OK] パッケージ" + (" (gdrive 含む)" if with_gdrive else ""))


def write_systemd_unit(prefix: Path, config_dir: Path) -> None:
    unit = prefix / "systemd" / "agy-slack-bridge.service"
    text = SYSTEMD_UNIT.format(prefix=prefix, config_dir=config_dir)
    unit.parent.mkdir(parents=True, exist_ok=True)
    if unit.exists():
        unit.unlink()
    unit.write_text(text, encoding="utf-8")
    os.chmod(unit, 0o444)
    print(f"  [生成] {unit}")


def create_config(prefix: Path, config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config_dir, 0o700)
    for sample, name, mode in (("config.example.yaml", "config.yaml", 0o600),
                               ("env.example", "env", 0o600)):
        dest = config_dir / name
        if dest.exists():
            print(f"  [OK] {dest} は既にある（変更しない）")
            continue
        shutil.copyfile(prefix / sample, dest)
        os.chmod(dest, mode)
        print(f"  [作成] {dest}")


def main(argv: "list[str] | None" = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prefix", type=Path, default=DEFAULT_PREFIX)
    ap.add_argument("--config-dir", type=Path, default=None)
    ap.add_argument("--with-gdrive", action="store_true")
    ap.add_argument("--no-venv", action="store_true")
    ap.add_argument("--no-config", action="store_true")
    ap.add_argument("--config-only", action="store_true")
    args = ap.parse_args(argv)

    prefix = args.prefix.expanduser().resolve()
    config_dir = (args.config_dir or DEFAULT_CONFIG_DIR).expanduser().resolve()
    as_root = hasattr(os, "geteuid") and os.geteuid() == 0
    skip_config = args.no_config
    if as_root and args.config_dir is None and not args.config_only:
        print("  [注意] root で実行しているため、設定ディレクトリは作りません"
              "（通常ユーザーで `install.py --config-only` を実行してください）。")
        skip_config = True

    if not args.config_only:
        print(f"=== インストール: {prefix} ===")
        install_code(prefix)
        if not args.no_venv:
            install_venv(prefix, args.with_gdrive)
        write_systemd_unit(prefix, config_dir)
    elif not (prefix / "config.example.yaml").exists():
        sys.exit(f"エラー: {prefix} にインストールされていません。先に install.py を実行してください。")

    if not skip_config:
        print(f"=== 設定ディレクトリ: {config_dir} ===")
        create_config(prefix, config_dir)

    print(f"\n完了。次は、{prefix}/tools/setup_agy_project.py でプロジェクトを設定してください。")


if __name__ == "__main__":
    main()
