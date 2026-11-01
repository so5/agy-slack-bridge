"""Grant scopes: how wide the permission is that a "allow" button grants for a command.

agy matches a `command(...)` allow entry as a prefix of the command line. Granting a
denied command's full text therefore pins every argument, including ones the person
does not care about (which local file to upload). A rule in `tools/grant_scopes.json`
says how many leading arguments identify what is being allowed:

    {"prefix": ["{prefix}/.venv/bin/python", "-I", "{prefix}/tools/gdrive_cli.py", "--profile", "*", "upload"],
     "flags": ["--folder"],
     "label": "このフォルダへのアップロード"}

`prefix` are token patterns (fnmatch; `{prefix}` is the install directory), `flags` are
flags that must follow immediately, each with a value. The entry is the command up to
and including the last value; `label` is what the person is asked to allow. The file is
part of the installed code (the agent cannot write it). A command that matches no rule,
or that contains shell syntax, keeps its full text as the entry. Used for the buttons
only (temporary grants), never for permanent ones.
"""
import fnmatch
import json
import re
import shlex
from pathlib import Path

_SHELL_SYNTAX = re.compile(r"[<>|&;`$(){}\n]")


def load_rules(path, install_prefix) -> list:
    """The rules in `path` with {prefix} filled in. A missing or broken file means no rules
    (full-text entries: the narrow side), never an error."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        rules = []
        for r in data:
            rules.append({"prefix": [str(t).replace("{prefix}", str(install_prefix)) for t in r["prefix"]],
                          "flags": [str(f) for f in r["flags"]], "label": str(r["label"])})
        return rules
    except (OSError, ValueError, KeyError, TypeError):
        return []


def match(command: str, rules: list):
    """{"entry_command", "label"} for the first rule the command matches, else None."""
    if not command or _SHELL_SYNTAX.search(command):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    for rule in rules:
        pat = rule["prefix"]
        n = len(pat)
        if len(tokens) < n or not all(fnmatch.fnmatchcase(t, p) for t, p in zip(tokens, pat)):
            continue
        for flag in rule["flags"]:
            if len(tokens) < n + 2 or tokens[n] != flag or not tokens[n + 1] or tokens[n + 1].startswith("-"):
                break
            n += 2
        else:
            entry = " ".join(shlex.quote(t) for t in tokens[:n])
            if command.startswith(entry):
                return {"entry_command": entry, "label": rule["label"]}
    return None


def is_scope_entry(entry_command: str, rules: list) -> bool:
    """True if `entry_command` is itself what a rule produces (so a grant of it is a scoped one)."""
    m = match(entry_command, rules)
    return bool(m) and m["entry_command"] == entry_command
