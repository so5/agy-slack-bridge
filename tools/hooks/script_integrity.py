#!/usr/bin/env python3
"""PreToolUse hook for `run_command` (see .agents/hooks.json).

Enforces agent-safety-rules.md #7: closes the gap where a command string
once granted a *permanent* permissions.allow entry (e.g. a fixed CLI like
gdrive_cli.py) would otherwise keep running whatever its script file
currently contains, not just what it held when the grant was made - by
requiring the file to be git-committed (so every version that ever ran is
in history) before each run.

Two different checks, depending on whether the command's resolved script
path (see `find_script_targets`) is in this project's own
`.agents/.permanent-commands.json` (absolute script path -> sha256 of the
content that was approved, written by the bridge's `/agy-permissions add`,
never by hand). Matching is by resolved path, not by
raw command-line text: a permanent grant is for a *script*, and the same
script can be invoked many different ways (absolute path, `./`-relative,
a different leading interpreter flag, ...) that would never agree as
strings but all resolve to the same file.

- **Permanent commands**: the referenced script's current content must hash
  to the value recorded when the grant was approved (the seal), and it must
  also be git-committed and clean (see `blocked_reason`). Being committed
  alone is not approval - anyone can commit a changed script - so the pin is
  what gates the run. Re-approving (`/agy-permissions add` again) re-seals
  the current content.
- **Everything else** (scratch/ one-off scripts, ordinary one-time Slack
  grants): no git involvement at all - scratch/ is meant to be disposable,
  and forcing a commit for something that's granted for one conversation
  and then expires was solving a much smaller problem than the permanent
  case. Instead, a lightweight local content ledger
  (`.agents/.command-hashes.json`, gitignored, hook-owned) remembers each
  exact command string's script content the first time it's seen; a *later*
  run of the identical command string with *different* script content is
  denied once (forcing a visible pause instead of a silent swap, with a
  unified diff of what changed in the deny message, so whoever's approving
  can actually judge it), and the ledger is updated to the new content at
  the same time, so simply asking again succeeds.

Anything this can't confidently identify as "running a script" (most
commands) falls through as "ask" - i.e. exactly the normal permission flow,
unchanged. Never invents a "deny" for a command that isn't actually
executing a script; on any internal error, also falls back to "ask" rather
than blocking work over a bug in this hook itself.
"""
import difflib
import hashlib
import json
import os
import shlex
import subprocess
import sys

MAX_DIFF_CHARS = 4000

INTERPRETER_BASENAMES = {
    "python", "python3", "python3.10", "python3.11", "python3.12", "python3.13",
    "python3.14", "bash", "sh", "zsh", "node", "ruby", "perl",
}
SCRIPT_EXTS = (".py", ".sh", ".js", ".mjs", ".rb", ".pl")
SHELL_OPERATORS = {";", "&&", "||", "|", "&"}

PERMANENT_COMMANDS_REL = os.path.join(".agents", ".permanent-commands.json")
COMMAND_HASHES_REL = os.path.join(".agents", ".command-hashes.json")


def split_segments(cmdline: str) -> list[list[str]]:
    """Splits a shell command line into its individual pipeline/sequence
    segments (on ; && || | &), each already shlex-tokenized. Best-effort -
    falls back to treating the whole line as one segment if it doesn't
    parse as a shell command at all (e.g. genuinely malformed quoting)."""
    try:
        lexer = shlex.shlex(cmdline, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return [cmdline.split()]
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in SHELL_OPERATORS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def find_script_targets(cmdline: str, root: str) -> list[str]:
    """The script file(s) a command line actually executes - a direct
    `./foo.py`, or an interpreter (python/bash/node/...) followed by a
    script-looking path - resolved to absolute paths under `root`. Deliberately
    narrow: a .py path that's merely an *argument* to some other command
    (`grep`, `cat`, `ls`, ...) is not "executing" anything and is ignored."""
    targets: list[str] = []
    for seg in split_segments(cmdline):
        if not seg:
            continue
        head = seg[0]
        head_base = os.path.basename(head)
        candidate = None
        if head_base in INTERPRETER_BASENAMES:
            for tok in seg[1:]:
                if tok.startswith("-"):
                    continue
                candidate = tok
                break
        elif head.endswith(SCRIPT_EXTS):
            candidate = head
        if candidate and candidate.endswith(SCRIPT_EXTS):
            path = os.path.normpath(candidate if os.path.isabs(candidate) else os.path.join(root, candidate))
            if os.path.isfile(path):
                targets.append(path)
    # dedupe, keep order
    seen = set()
    out = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def load_permanent_pins(root: str) -> dict:
    """`.agents/.permanent-commands.json`: resolved absolute script path ->
    sha256 of the content approved at grant time. A bare string entry (the
    old unpinned format) maps to None, so it's treated as permanent but
    unapproved until re-sealed."""
    try:
        data = json.loads(open(os.path.join(root, PERMANENT_COMMANDS_REL)).read())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if isinstance(data, list):
        return {e: None for e in data if isinstance(e, str)}
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if isinstance(k, str)
                and (v is None or isinstance(v, str))}
    return {}


def file_hash(path: str) -> "str | None":
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def pin_reason(path: str, pinned: "str | None", root: str) -> "str | None":
    rel = os.path.relpath(path, root)
    if pinned is None:
        return f"{rel} は恒久許可の承認（seal）がまだ無い"
    if file_hash(path) != pinned:
        return f"{rel} は承認時から中身が変わっている（再承認＝seal が必要）"
    return None


def blocked_reason(path: str, root: str) -> "str | None":
    """None if safe to run; otherwise a human-readable reason (permanent-
    command path: requires git).

    A single `git status --porcelain --ignored=matching -- <path>` call
    distinguishes all three states: empty output means clean *and* tracked
    (plain `--porcelain`, without `--ignored`, can't tell that apart from a
    gitignored file - both print nothing); `??` means untracked; `!!` means
    gitignored."""
    rel = os.path.relpath(path, root)
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain", "--ignored=matching", "--", rel],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        line = res.stdout.strip()
        if not line:
            return None
        if line.startswith("!!"):
            return f"{rel} is excluded via .gitignore (outside git's management)"
        return f"{rel} is not committed ({line.split()[0]})"
    except Exception:
        return None  # not a git repo / git missing - don't block over our own failure


def read_text(path: str) -> "str | None":
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def load_hashes(root: str) -> dict:
    try:
        return json.loads(open(os.path.join(root, COMMAND_HASHES_REL)).read())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def save_hashes(root: str, data: dict) -> None:
    path = os.path.join(root, COMMAND_HASHES_REL)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=1)


def changed_reason(cmdline: str, targets: list[str], root: str) -> "str | None":
    """None if safe to run; otherwise a reason (non-permanent-command path:
    a lightweight, bridge-independent content ledger instead of git). First
    sighting of a command string just records its script(s)' current
    content and allows it - nothing to compare against yet. A later run of
    the *identical* command string whose script content has since changed
    is denied once, with a unified diff of the change in the deny message
    (so the human approving can actually tell what changed, not just that
    something did), and the ledger is refreshed to the new content in the
    same step, so asking again (after confirming the change is intentional)
    succeeds without a second denial."""
    all_entries = load_hashes(root)
    prior = all_entries.get(cmdline)
    current = {os.path.relpath(p, root): read_text(p) for p in targets}

    if prior is not None and prior != current:
        all_entries[cmdline] = current
        save_hashes(root, all_entries)
        changed = [p for p in current if prior.get(p) != current.get(p)]
        diff_chunks = []
        for relpath in changed:
            old = prior.get(relpath) or ""
            new = current.get(relpath) or ""
            diff = "".join(difflib.unified_diff(
                old.splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile=f"{relpath} (前回実行時)", tofile=f"{relpath} (今回)", n=2,
            ))
            diff_chunks.append(diff or f"--- {relpath}: 差分生成に失敗（バイナリ等）")
        diff_text = "\n".join(diff_chunks)
        if len(diff_text) > MAX_DIFF_CHARS:
            diff_text = diff_text[:MAX_DIFF_CHARS] + "\n...(長いため省略)"
        return (f"{', '.join(changed)} の中身が、前回この同じコマンドを実行した時から変わっています。\n"
                f"変更内容:\n{diff_text}\n"
                "変更が意図したものか確認し、問題なければ同じ指示をもう一度送ってください。")

    if prior is None:
        all_entries[cmdline] = current
        save_hashes(root, all_entries)
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        cmdline = ((payload.get("toolCall") or {}).get("args") or {}).get("CommandLine") or ""
        roots = payload.get("workspacePaths") or []
        root = roots[0] if roots else os.getcwd()

        targets = find_script_targets(cmdline, root)
        if not targets:
            print(json.dumps({"decision": "ask"}))
            return

        pins = load_permanent_pins(root)
        if any(p in pins for p in targets):
            reasons = []
            for p in targets:
                if p not in pins:
                    continue
                r = pin_reason(p, pins[p], root) or blocked_reason(p, root)
                if r:
                    reasons.append(r)
            if reasons:
                print(json.dumps({
                    "decision": "deny",
                    "reason": ("; ".join(reasons) + ". このコマンドは恒久的に許可されているため、"
                               "内容を確認して再承認（seal）し、git commit してから実行してください"
                               "（.agents/agent-safety-rules.md #7）。"),
                }))
                return
        else:
            reason = changed_reason(cmdline, targets, root)
            if reason:
                print(json.dumps({"decision": "deny", "reason": reason}))
                return
    except Exception as exc:  # never let a bug in this hook block real work
        print(json.dumps({"decision": "ask", "reason": f"script_integrity hook error (ignored): {exc}"}))
        return

    print(json.dumps({"decision": "ask"}))


if __name__ == "__main__":
    main()
