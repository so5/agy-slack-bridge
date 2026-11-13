#!/usr/bin/env python3
"""Runs a command with a credential profile's environment variables, and masks their values in its output.

    cred_run.py --profile NAME -- <command> [args...]

The profile is `<bridge config dir>/credentials/NAME.json` (tools/cred_profiles.py): a place the
agent cannot read, so the token does not have to sit in the project's .env. The command gets the
profile's variables in its environment and nothing else is changed, e.g.

    cred_run.py --profile op -- op run --env-file=.env -- python scratch/fetch.py

(`op` reads its own token from the environment, not from .env).

The command is the agent's, and so are the scripts behind it - script_integrity.py and the
script review look past this prefix at the script it runs. What this wrapper adds is the
masking of the profile's values in stdout/stderr and a refusal of the obvious ways to print the
environment (`env`, `printenv`, `python -c ...`, `sh -c ...`). That refusal is best effort,
not a sandbox: a script can still print what it reads, which is why the review looks for it.
Error messages never contain a value. Exit status is the command's (2: refused/usage, 127: not found).
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cred_profiles  # noqa: E402

ENV_PRINTERS = {"env", "printenv", "export", "declare", "typeset", "set", "eval", "exec", "xargs"}
INTERPRETERS = {"python", "python3", "bash", "sh", "zsh", "dash", "node", "ruby", "perl", "php", "pwsh"}
CODE_FLAGS = {"-c", "-lc", "-ic", "-e", "-p", "-E", "-r", "--eval", "--print", "-Command", "-command"}


def refusal(argv: list) -> "str | None":
    """Why this command is not run with credentials, or None. Looks at every program position:
    the command itself and what follows a `--` (op run ... -- python x.py)."""
    starts = [0] + [i + 1 for i, t in enumerate(argv) if t == "--"]
    for s in starts:
        if s >= len(argv):
            continue
        base = os.path.basename(argv[s])
        if base in ENV_PRINTERS:
            return f"`{base}` は、環境変数を表示できるため、認証情報を付けては実行できません"
        stem = base.rstrip("0123456789.")
        if base in INTERPRETERS or stem in INTERPRETERS:
            for tok in argv[s + 1:]:
                if not tok.startswith("-"):
                    break
                if tok in CODE_FLAGS:
                    return (f"`{base} {tok}` のようなコードの直接指定は、認証情報を付けては実行できません。"
                            "スクリプトを scratch/ に書いて実行してください")
    if any("/proc/" in t or t.endswith("/environ") for t in argv):
        return "プロセスの環境（/proc）を指すコマンドは、認証情報を付けては実行できません"
    return None


def main(argv: "list[str] | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 4 or argv[0] != "--profile" or argv[2] != "--":
        print("usage: cred_run.py --profile NAME -- <command> [args...]", file=sys.stderr)
        return 2
    name, cmd = argv[1], argv[3:]
    try:
        env = cred_profiles.read_profile(cred_profiles.profile_path(name))
    except cred_profiles.ProfileError as e:
        print(f"cred_run: {e}", file=sys.stderr)
        return 2
    why = refusal(cmd)
    if why:
        print(f"cred_run: {why}", file=sys.stderr)
        return 2
    secrets = cred_profiles.secrets_of(env)
    try:
        proc = subprocess.run(cmd, env={**os.environ, **env}, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        print(f"cred_run: コマンドが見つかりません: {cmd[0]}", file=sys.stderr)
        return 127
    except OSError as e:
        print(f"cred_run: 実行できません: {cmd[0]}（{e.strerror}）", file=sys.stderr)
        return 126
    for data, stream in ((proc.stdout, sys.stdout), (proc.stderr, sys.stderr)):
        stream.write(cred_profiles.mask(data.decode("utf-8", errors="replace"), secrets))
        stream.flush()
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
