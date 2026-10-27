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
  --config-only      インストール先には触れず、設定ディレクトリと settings.json だけ
                     設定する(root で --prefix /opt/... に入れた後、通常ユーザーで実行する)。
  --no-settings      ~/.gemini/antigravity-cli/settings.json と ~/.gemini/config/hooks.json を
                     書き換えない。
  --no-setup         最後に setup_agy_project.py を実行するかの確認を出さない。

settings.json には、インストール先と設定ディレクトリを agy のエージェントに読み書き
させない deny ルール(と、agy 自身の設定を読ませない deny)を追加します。許可(allow)は
書きません。hooks.json(全プロジェクトに効く置き場)には、script-integrity フックを
登録します(他のフックは残す)。これはプロジェクトに依らない machine-wide の設定なので、ここで行います
(root で実行したときは、root のホームの settings.json を書いてしまうので行いません。
通常ユーザーでの --config-only で行います)。

最後に、続けて setup_agy_project.py を実行するか確認します(端末のときだけ)。

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
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_settings  # noqa: E402
import safety_stack  # noqa: E402

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
# AGY_BRIDGE_CONFIG's directory ({config_dir}) must be the same directory the
# script-integrity hook gets as --config-dir (install.py sets both): the hook's
# seals live in <config dir>/integrity/. Logs go to the journal:
#   journalctl --user -u agy-slack-bridge.service
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
{agy_line}ExecStart={prefix}/.venv/bin/python {prefix}/bridge.py
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


def agy_environment_line() -> str:
    """systemd --user の PATH は最小限(~/.local/bin を含まない)なので、agy の絶対パスを
    AGY_BIN として unit に書く。見つからなければ書かずに警告する。"""
    found = shutil.which("agy")
    if found:
        return f"Environment=AGY_BIN={Path(found).resolve()}\n"
    print("  [警告] agy が PATH に見つかりません。unit に AGY_BIN を書けませんでした。"
          "agy の絶対パスを env ファイルの AGY_BIN に設定してください（systemd の PATH は最小限です）。")
    return ""


def write_systemd_unit(prefix: Path, config_dir: Path) -> None:
    unit = prefix / "systemd" / "agy-slack-bridge.service"
    text = SYSTEMD_UNIT.format(prefix=prefix, config_dir=config_dir, agy_line=agy_environment_line())
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
    saved = safety_stack.backup_safety_files(prefix, config_dir)
    if saved:
        print(f"  [控え] 安全機構の原本を {config_dir / safety_stack.BACKUP_DIRNAME} に保存"
              "（インストール先から消えたら、ブリッジがここから戻します）")


def apply_settings(prefix: Path, config_dir: Path) -> None:
    agy_settings.update_settings(agy_settings.machine_wide_deny_rules(prefix, config_dir, Path.home()))
    agy_settings.install_global_hook(prefix, config_dir)


def setup_command(prefix: Path, config_dir: Path) -> list[str]:
    python = prefix / ".venv" / "bin" / "python"
    return [str(python if python.exists() else sys.executable),
            str(prefix / "tools" / "setup_agy_project.py"),
            "--prefix", str(prefix), "--bridge-config-dir", str(config_dir)]


def offer_setup(prefix: Path, config_dir: Path, input_fn=input, runner=subprocess.run) -> None:
    cmd = setup_command(prefix, config_dir)
    print("\n続けて agy プロジェクトを設定します(チャンネルとの対応づけ、権限など)。")
    while True:
        try:
            raw = input_fn("続けてセットアップを実行しますか? [Y/n]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n中断しました。後で設定する場合は、端末で次のコマンドを実行してください:\n  " + " ".join(cmd))
            return
        if raw in ("", "y", "yes"):
            # Ctrl-C は端末が前面のプロセス全員に送る。親がここで落ちると、実行中のセットアップも
            # 道連れに止まり、終了の確認ダイアログも出せない。親は受けて何もしない(子は exec で
            # 既定の動作に戻る。SIG_IGN だと子にも引き継がれてしまうので、関数を指定する)。
            old = signal.signal(signal.SIGINT, lambda *_: None)
            try:
                runner(cmd)
            finally:
                signal.signal(signal.SIGINT, old)
            return
        if raw in ("n", "no"):
            print("後で設定する場合は、端末で次のコマンドを実行してください:\n  " + " ".join(cmd))
            return


def main(argv: "list[str] | None" = None, input_fn=input, runner=subprocess.run) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prefix", type=Path, default=DEFAULT_PREFIX)
    ap.add_argument("--config-dir", type=Path, default=None)
    ap.add_argument("--with-gdrive", action="store_true")
    ap.add_argument("--no-venv", action="store_true")
    ap.add_argument("--no-config", action="store_true")
    ap.add_argument("--config-only", action="store_true")
    ap.add_argument("--no-settings", action="store_true")
    ap.add_argument("--no-setup", action="store_true")
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

    if as_root and not args.config_only:
        print("  [注意] root で実行しているため、settings.json は書き換えません。")
    elif not args.no_settings:
        print("=== agy の settings.json と hooks.json ===")
        apply_settings(prefix, config_dir)

    if as_root and not args.config_only:
        print(f"\n次に、通常ユーザーで `python3 {prefix}/tools/install.py --prefix {prefix} --config-only` を実行し、"
              f"続けて {prefix}/tools/setup_agy_project.py でプロジェクトを設定してください。")
    elif args.no_setup or not sys.stdin.isatty() and input_fn is input:
        print(f"\n完了。次は、次のコマンドでプロジェクトを設定してください:\n  "
              + " ".join(setup_command(prefix, config_dir)))
    else:
        offer_setup(prefix, config_dir, input_fn, runner)


if __name__ == "__main__":
    main()
