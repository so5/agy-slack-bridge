#!/usr/bin/env python3
"""agy-slack-bridge

Bridges one or more Slack channels to Antigravity CLI (`agy`) projects.

Design:
- Each configured Slack channel maps 1:1 to one agy project.
- A new top-level message in the channel starts a fresh agy conversation.
  The bot's reply is posted as a thread reply on that message, so the Slack
  thread and the agy conversation come into existence together.
- A reply inside an existing Slack thread continues the agy conversation
  already associated with that thread (looked up by thread_ts).
- A reply inside a thread the bridge has no record of (e.g. a thread that
  predates this bot, or after state was cleared) falls back to starting a
  new agy conversation, keyed from that point on to the same thread_ts.

See README.md for Slack app setup (Socket Mode, scopes, event subscriptions)
and systemd/agy-slack-bridge.service for how this is meant to run.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Optional

import yaml
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("agy-slack-bridge")

CONFIG_PATH = Path(os.environ.get("AGY_BRIDGE_CONFIG", "config.yaml"))
STATE_PATH = Path(os.environ.get("AGY_BRIDGE_STATE_DB", "state.sqlite3"))
AGY_BIN = os.environ.get("AGY_BIN", "agy")
# Safety cap so a stuck agy invocation can't wedge the bridge forever.
AGY_TIMEOUT_SEC = int(os.environ.get("AGY_TIMEOUT_SEC", "1200"))
# agy's own global permission file (see README's "Tool permissions" section)
# - not owned by this bridge, so every write here preserves whatever else is
# already in it (trustedWorkspaces, etc.) and only touches permissions.allow.
AGY_SETTINGS_PATH = Path(
    os.environ.get("AGY_SETTINGS_PATH", "~/.gemini/antigravity-cli/settings.json")
).expanduser()


_CODE_SPAN_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_TABLE_SEP_RE = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s)]+)\)")
_HEADER_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*$", re.MULTILINE)

# Lets a *new* top-level Slack message graft onto an agy conversation that
# already exists elsewhere (e.g. started in the Antigravity web UI), instead
# of always starting a fresh one. Only checked on new messages, not thread
# replies - once grafted, the resulting Slack thread continues normally.
# Example: "resume db68d299-5a1c-475a-a1ed-09604f5537e4: what's next?"
# The ID (optionally wrapped in a single backtick, e.g. "resume `<id>: ...`"
# - a natural thing to type in Slack, and easy to do without noticing, since
# a whole message ending in "?`" doesn't look obviously different from one
# ending in "?") is captured separately so a stray trailing backtick can be
# stripped back off the message text below.
_RESUME_RE = re.compile(
    r"^\s*resume\s+(`)?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})`?"
    r"\s*[:,\-]?\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)


def _convert_markdown_tables(text: str) -> str:
    """Slack mrkdwn has no table syntax; render markdown tables as a
    monospace code block instead of leaving the raw `| a | b |` pipes."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if (
            _TABLE_ROW_RE.match(lines[i])
            and i + 1 < len(lines)
            and _TABLE_SEP_RE.match(lines[i + 1])
        ):
            def plain_cell(cell: str) -> str:
                # Table cells end up inside a ``` code block (Slack mrkdwn
                # has no table syntax), where Slack won't render any nested
                # formatting anyway - so flatten common markdown down to
                # plain text instead of leaving literal ** and [text](url).
                cell = _LINK_RE.sub(lambda m: m.group(1), cell)
                cell = _BOLD_RE.sub(lambda m: m.group(1), cell)
                cell = _STRIKE_RE.sub(lambda m: m.group(1), cell)
                cell = cell.replace("`", "")
                return cell.strip()

            def split_row(line: str) -> list[str]:
                return [plain_cell(c) for c in line.strip().strip("|").split("|")]

            rows = [split_row(lines[i])]
            i += 2  # header + separator consumed
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i]):
                rows.append(split_row(lines[i]))
                i += 1
            ncols = max(len(r) for r in rows)
            rows = [r + [""] * (ncols - len(r)) for r in rows]
            widths = [max(len(r[c]) for r in rows) for c in range(ncols)]
            rendered = []
            for ridx, row in enumerate(rows):
                rendered.append("  ".join(cell.ljust(widths[c]) for c, cell in enumerate(row)))
                if ridx == 0:
                    rendered.append("  ".join("-" * widths[c] for c in range(ncols)))
            out.append("```\n" + "\n".join(rendered) + "\n```")
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def markdown_to_mrkdwn(text: str) -> str:
    """Best-effort conversion of the GitHub-flavored Markdown agy returns
    into Slack's "mrkdwn" dialect, so bold/links/tables actually render
    instead of showing up as literal '**' and '[text](url)' in Slack."""
    if not text:
        return text

    text = _convert_markdown_tables(text)

    # Protect code spans/blocks so the substitutions below don't mangle
    # markdown-looking characters that appear inside code.
    code_spans: list[str] = []

    def stash_code(m: re.Match) -> str:
        code_spans.append(m.group(0))
        return f"\x00CODE{len(code_spans) - 1}\x00"

    text = _CODE_SPAN_RE.sub(stash_code, text)

    text = _HEADER_RE.sub(lambda m: f"*{m.group(1)}*", text)
    text = _LINK_RE.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", text)
    text = _STRIKE_RE.sub(lambda m: f"~{m.group(1)}~", text)

    # Bold uses the same character Slack uses for italics (*), so pull **
    # out first (as a placeholder) before touching single-* italics -
    # otherwise the italic pass would immediately re-match the new *bold*.
    bolds: list[str] = []

    def stash_bold(m: re.Match) -> str:
        bolds.append(m.group(1))
        return f"\x00BOLD{len(bolds) - 1}\x00"

    text = _BOLD_RE.sub(stash_bold, text)
    text = _ITALIC_STAR_RE.sub(lambda m: f"_{m.group(1)}_", text)
    for idx, content in enumerate(bolds):
        text = text.replace(f"\x00BOLD{idx}\x00", f"*{content}*")

    for idx, code in enumerate(code_spans):
        text = text.replace(f"\x00CODE{idx}\x00", code)

    return text


def _persona_kwargs(channel_cfg: dict) -> dict:
    """Per-channel display name/icon override for chat.postMessage, so the
    same bot token can look like a different persona in each channel (e.g.
    "secretary" vs "accountant"). Requires the chat:write.customize scope;
    silently has no effect without it if channel_cfg sets nothing."""
    kwargs = {}
    if channel_cfg.get("display_name"):
        kwargs["username"] = channel_cfg["display_name"]
    if channel_cfg.get("icon_emoji"):
        kwargs["icon_emoji"] = channel_cfg["icon_emoji"]
    elif channel_cfg.get("icon_url"):
        kwargs["icon_url"] = channel_cfg["icon_url"]
    return kwargs


def load_channel_map() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    channel_map = cfg.get("channels") or {}
    if not channel_map:
        raise SystemExit(f"No channels configured in {CONFIG_PATH}")
    for channel_id, entry in channel_map.items():
        if "project" not in entry:
            raise SystemExit(f"channels.{channel_id} is missing a 'project' id in {CONFIG_PATH}")
    return channel_map


class ThreadStore:
    """Maps (channel_id, thread_ts) -> agy conversation_id, and holds
    short-lived "pending retry" records for the permission-grant buttons
    (see _build_reply_blocks / grant_once / grant_permanent below).

    Backed by sqlite (not a plain JSON file) because Slack Bolt dispatches
    events from a worker thread pool, so writes can race.
    """

    def __init__(self, path: Path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS threads (
                channel_id TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (channel_id, thread_ts)
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS pending_retries (
                retry_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                project_id TEXT NOT NULL,
                conversation_id TEXT,
                prompt_text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        self._conn.commit()

    def get(self, channel_id: str, thread_ts: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT conversation_id FROM threads WHERE channel_id=? AND thread_ts=?",
                (channel_id, thread_ts),
            ).fetchone()
            return row[0] if row else None

    def put(self, channel_id: str, thread_ts: str, conversation_id: str, project_id: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO threads (channel_id, thread_ts, conversation_id, project_id, updated_at)
                   VALUES (?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(channel_id, thread_ts) DO UPDATE SET
                     conversation_id=excluded.conversation_id,
                     project_id=excluded.project_id,
                     updated_at=excluded.updated_at""",
                (channel_id, thread_ts, conversation_id, project_id),
            )
            self._conn.commit()

    def put_retry(self, retry_id: str, channel_id: str, thread_ts: str, project_id: str,
                   conversation_id: Optional[str], prompt_text: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO pending_retries
                   (retry_id, channel_id, thread_ts, project_id, conversation_id, prompt_text, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                (retry_id, channel_id, thread_ts, project_id, conversation_id, prompt_text),
            )
            self._conn.commit()

    def get_retry(self, retry_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                """SELECT channel_id, thread_ts, project_id, conversation_id, prompt_text
                   FROM pending_retries WHERE retry_id=?""",
                (retry_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "channel_id": row[0], "thread_ts": row[1], "project_id": row[2],
            "conversation_id": row[3], "prompt_text": row[4],
        }

    def delete_retry(self, retry_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM pending_retries WHERE retry_id=?", (retry_id,))
            self._conn.commit()


class PermissionsFile:
    """Read-modify-write helper for agy's global
    ~/.gemini/antigravity-cli/settings.json permissions.allow list.

    Every write reloads the file fresh and only touches the allow list, so
    whatever else is in there (trustedWorkspaces, etc.) - and any change a
    human made by hand in between - survives untouched."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            with self._path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save(self, data: dict) -> None:
        tmp = self._path.with_name(self._path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        tmp.replace(self._path)

    @staticmethod
    def _entry(command: str) -> str:
        return f"command({command})"

    def list_commands(self) -> list[str]:
        with self._lock:
            allow = (self._load().get("permissions") or {}).get("allow") or []
        return [a[len("command("):-1] for a in allow if a.startswith("command(") and a.endswith(")")]

    def add(self, command: str) -> bool:
        entry = self._entry(command)
        with self._lock:
            data = self._load()
            allow = data.setdefault("permissions", {}).setdefault("allow", [])
            if entry in allow:
                return False
            allow.append(entry)
            self._save(data)
        log.info("permissions.allow += %r", entry)
        return True

    def remove(self, command: str) -> bool:
        entry = self._entry(command)
        with self._lock:
            data = self._load()
            allow = (data.get("permissions") or {}).get("allow") or []
            if entry not in allow:
                return False
            allow.remove(entry)
            self._save(data)
        log.info("permissions.allow -= %r", entry)
        return True


class KeyedLocks:
    """One lock per (channel_id, thread_ts) so two quick messages in the same
    thread can't run agy concurrently and cross-talk on the same conversation.
    Different threads/channels still run fully in parallel."""

    def __init__(self):
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, key: tuple[str, str]) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())


# Slack auto-clears an assistant status after 2 minutes of silence; agy can
# easily run longer than that (we've seen many minutes on real tasks), so
# it needs to be refreshed periodically rather than set once.
STATUS_REFRESH_SEC = 90


def _run_with_status(client, channel_id: str, thread_ts: str, status_text: str,
                      persona_kwargs: dict, fn, *args, **kwargs):
    """Show a Slack "is thinking..." style status in the thread for as long
    as fn() is running. Best-effort: a failure to set/refresh the status
    never blocks or fails the actual agy call."""
    def set_status():
        try:
            client.assistant_threads_setStatus(
                channel_id=channel_id, thread_ts=thread_ts, status=status_text,
                **persona_kwargs,
            )
        except Exception:
            log.warning("failed to set assistant status", exc_info=True)

    stop_event = threading.Event()

    def keep_alive():
        while not stop_event.wait(STATUS_REFRESH_SEC):
            set_status()

    set_status()
    refresher = threading.Thread(target=keep_alive, daemon=True)
    refresher.start()
    try:
        return fn(*args, **kwargs)
    finally:
        stop_event.set()
        refresher.join(timeout=1)


def _describe_tool_error(err: dict) -> str:
    """Render one entry of run_agy()'s _tool_errors as a short, readable line
    (backtick-quoted so Slack shows it as inline code, not mangled markdown)."""
    tool_name = err.get("tool_name") or "?"
    params = err.get("parameters") or {}
    detail = params.get("CommandLine") or params.get("FilePath") or params.get("Path")
    if detail is None and params:
        detail = json.dumps(params, ensure_ascii=False)
    if detail:
        return f"`{tool_name}`: `{detail}`"
    return f"`{tool_name}`"


def run_agy(text: str, project_id: str, conversation_id: Optional[str]) -> dict:
    # --output-format json only gives denied_actions as a bare
    # {"action": "command", "display_name": "RunCommand"} - not what was
    # actually denied. stream-json emits a step_update per tool call, so a
    # denied one carries the exact command line / file path / etc. Parse
    # that stream ourselves and fold the detail into the final result dict
    # under "_tool_errors" for the caller to report.
    cmd = [
        AGY_BIN, "-p", text,
        "--project", project_id,
        "--output-format", "stream-json",
        "--print-timeout", "0",
        "--mode", "accept-edits",
    ]
    if conversation_id:
        cmd += ["--conversation", conversation_id]
    log.info("running agy for project=%s conversation=%s", project_id, conversation_id or "(new)")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=AGY_TIMEOUT_SEC)
    if proc.returncode != 0:
        raise RuntimeError(f"agy exited {proc.returncode}: {proc.stderr.strip()[-2000:]}")

    result = None
    tool_errors = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # stray non-JSON line; the real payload is line-delimited JSON
        if event.get("event") == "result":
            result = event.get("result")
        elif event.get("event") == "step_update":
            step = event.get("step_update", {})
            if step.get("state") == "ERROR" and step.get("step_type") == "tool":
                info = step.get("tool_info", {})
                tool_errors.append({
                    "tool_name": step.get("tool_name"),
                    "parameters": info.get("parameters"),
                    "message": (info.get("error") or {}).get("message"),
                })

    if result is None:
        raise RuntimeError(f"agy produced no result event: {proc.stdout[-2000:]}")
    result["_tool_errors"] = tool_errors
    return result


def _text_to_section_blocks(text: str, chunk_size: int = 2900) -> list[dict]:
    """Slack section blocks cap out around 3000 chars; split long replies
    into several sections rather than truncating them."""
    text = text or "(empty response)"
    chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)] or [text]
    return [{"type": "section", "text": {"type": "mrkdwn", "text": c}} for c in chunks]


def _build_reply_blocks(outgoing_text: str, tool_errors: list[dict], retry_id: str) -> list[dict]:
    """Reply blocks for a turn that hit at least one run_command denial: the
    text as usual, plus one pair of grant/retry buttons per distinct denied
    command line. Only run_command denials get buttons - other tool types
    (file edits, etc.) still just show up as text, since there's no
    permissions.allow syntax for this bridge to grant on their behalf."""
    blocks = _text_to_section_blocks(outgoing_text)
    seen: set[str] = set()
    for err in tool_errors:
        if err.get("tool_name") != "run_command":
            continue
        cmd = (err.get("parameters") or {}).get("CommandLine")
        if not cmd or cmd in seen:
            continue
        seen.add(cmd)
        value = json.dumps({"retry_id": retry_id, "cmd": cmd})
        blocks.append({
            "type": "actions",
            "block_id": f"perm_{len(seen)}_{retry_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "今回だけ許可して再実行"},
                    "style": "primary",
                    "action_id": "grant_once",
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "恒久的に許可して再実行"},
                    "style": "danger",
                    "action_id": "grant_permanent",
                    "value": value,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "恒久的に許可しますか?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": f"以下のコマンドを今後ずっと許可します:\n`{cmd}`",
                        },
                        "confirm": {"type": "plain_text", "text": "許可する"},
                        "deny": {"type": "plain_text", "text": "キャンセル"},
                    },
                },
            ],
        })
        if len(seen) >= 10:  # stay well under Slack's block-count limit
            break
    return blocks


def _run_and_reply(client, store: "ThreadStore", permissions: "PermissionsFile",
                    channel_id: str, thread_key: str, project_id: str,
                    conversation_id: Optional[str], text: str, persona_kwargs: dict,
                    status_text: str = "考え中です...") -> None:
    """Runs one agy turn and posts the reply, attaching permission-grant
    buttons if a run_command call got denied. Shared by the normal message
    handler and by the grant_once/grant_permanent retry flow."""
    try:
        result = _run_with_status(
            client, channel_id, thread_key, status_text, persona_kwargs,
            run_agy, text, project_id, conversation_id,
        )
    except Exception:
        log.exception("agy invocation failed")
        client.chat_postMessage(
            channel=channel_id, thread_ts=thread_key,
            text=":x: agy invocation failed. Check the bridge's logs.",
            **persona_kwargs,
        )
        return

    log.info("agy result=%r", result)
    if result.get("status") != "SUCCESS":
        log.error("agy returned non-SUCCESS status: %r", result)
        client.chat_postMessage(
            channel=channel_id, thread_ts=thread_key,
            text=f":x: agy status={result.get('status')}: {result.get('error') or '(no error detail)'}",
            **persona_kwargs,
        )
        return

    conversation_id = result["conversation_id"]
    store.put(channel_id, thread_key, conversation_id, project_id)
    outgoing_text = markdown_to_mrkdwn(result.get("response")) or "(empty response)"

    tool_errors = result.get("_tool_errors") or []
    blocks = None
    if tool_errors:
        log.warning("agy tool errors: %r", tool_errors)
        lines = "\n".join(f"- {_describe_tool_error(e)}" for e in tool_errors)
        outgoing_text += (
            "\n\n:warning: 一部のツール実行が権限不足で拒否されました:\n"
            f"{lines}\n"
            "下のボタンで許可して再実行するか、`~/.gemini/antigravity-cli/settings.json` の "
            "`permissions.allow` に直接追加してください。"
        )
        retry_id = uuid.uuid4().hex[:12]
        store.put_retry(retry_id, channel_id, thread_key, project_id, conversation_id, text)
        blocks = _build_reply_blocks(outgoing_text, tool_errors, retry_id)
    elif result.get("denied_actions"):
        # Fallback in case we couldn't pull step-level detail out of the
        # stream for some reason - no command line to grant, so no buttons.
        denied_desc = ", ".join(
            d.get("display_name") or d.get("action") or "?" for d in result["denied_actions"]
        )
        outgoing_text += f"\n\n:warning: 一部のツール実行が権限不足で拒否されました: {denied_desc}"

    log.info("posting to slack text=%r", outgoing_text)
    client.chat_postMessage(
        channel=channel_id, thread_ts=thread_key,
        text=outgoing_text, blocks=blocks, **persona_kwargs,
    )


def build_app() -> App:
    channel_map = load_channel_map()
    store = ThreadStore(STATE_PATH)
    permissions = PermissionsFile(AGY_SETTINGS_PATH)
    locks = KeyedLocks()

    app = App(token=os.environ["SLACK_BOT_TOKEN"])
    self_user_id = app.client.auth_test()["user_id"]
    mention_re = re.compile(rf"<@{re.escape(self_user_id)}>\s*")
    log.info("bridge bot user id=%s (mention required to start a new conversation)", self_user_id)

    @app.event("message")
    def handle_message(event, client, logger):  # noqa: ANN001 - Bolt signature
        # Ignore edits, deletes, bot messages, thread-broadcast echoes, etc.
        # A plain human message has no "subtype". This also quietly ignores
        # messages posted via an Incoming Webhook (e.g. a batch job posting
        # its results into the same channel) - those arrive with
        # subtype="bot_message".
        if event.get("subtype") is not None:
            return

        channel_id = event.get("channel")
        channel_cfg = channel_map.get(channel_id)
        if not channel_cfg:
            return  # not one of our bridged channels

        project_id = channel_cfg["project"]
        text = event.get("text", "")
        ts = event["ts"]
        incoming_thread_ts = event.get("thread_ts")
        is_reply = incoming_thread_ts is not None and incoming_thread_ts != ts
        thread_key = incoming_thread_ts if is_reply else ts

        if not is_reply:
            # Only start a *new* conversation when explicitly @-mentioned,
            # so other traffic landing in the channel (a webhook's batch
            # results, ordinary chatter) isn't treated as a prompt. Once a
            # thread exists, replies in it don't need to repeat the mention.
            if not mention_re.search(text):
                return
            text = mention_re.sub("", text).strip()

        conversation_id = store.get(channel_id, thread_key) if is_reply else None

        resume_match = None if is_reply else _RESUME_RE.match(text)
        if resume_match:
            had_leading_backtick = resume_match.group(1) is not None
            conversation_id = resume_match.group(2)
            text = resume_match.group(3).strip()
            if had_leading_backtick and text.endswith("`"):
                text = text[:-1].rstrip()
            if not text:
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_key,
                    text="resumeの後にメッセージ本文も書いてや（例: `resume <会話ID> 続きをお願い`）",
                    **_persona_kwargs(channel_cfg),
                )
                return

        log.info("incoming text=%r channel=%s thread_key=%s conversation=%s%s",
                  text, channel_id, thread_key, conversation_id,
                  " (resumed)" if resume_match else "")

        with locks.get((channel_id, thread_key)):
            _run_and_reply(
                client, store, permissions, channel_id, thread_key, project_id,
                conversation_id, text, _persona_kwargs(channel_cfg),
            )

    def _grant_and_retry(body: dict, client, permanent: bool) -> None:
        action = body["actions"][0]
        try:
            payload = json.loads(action["value"])
        except (KeyError, json.JSONDecodeError):
            log.error("bad button value: %r", action.get("value"))
            return
        retry_id = payload.get("retry_id")
        cmd = payload.get("cmd")
        ctx = store.get_retry(retry_id) if retry_id else None

        message = body.get("message") or {}
        origin_channel_id = body["channel"]["id"]
        message_ts = message.get("ts")

        if not ctx or not cmd:
            if message_ts:
                client.chat_update(
                    channel=origin_channel_id, ts=message_ts,
                    text=":warning: このボタンは期限切れです。もう一度メッセージを送ってください。",
                    blocks=[],
                )
            return

        clicker = (body.get("user") or {}).get("username") or (body.get("user") or {}).get("id") or "?"
        log.info("%s permission grant for command=%r by user=%s",
                  "permanent" if permanent else "one-time", cmd, clicker)

        channel_cfg = channel_map.get(ctx["channel_id"], {})
        persona = _persona_kwargs(channel_cfg)
        label = "恒久的に許可" if permanent else "今回だけ許可"

        added = permissions.add(cmd)
        if message_ts:
            client.chat_update(
                channel=origin_channel_id, ts=message_ts,
                text=f":gear: {label}して再実行しています... (`{cmd}`)",
                blocks=[],
            )

        try:
            _run_and_reply(
                client, store, permissions, ctx["channel_id"], ctx["thread_ts"],
                ctx["project_id"], ctx["conversation_id"], ctx["prompt_text"], persona,
                status_text="許可して再実行しています...",
            )
        finally:
            if not permanent and added:
                permissions.remove(cmd)
            store.delete_retry(retry_id)

    @app.action("grant_once")
    def handle_grant_once(ack, body, client):  # noqa: ANN001 - Bolt signature
        ack()
        _grant_and_retry(body, client, permanent=False)

    @app.action("grant_permanent")
    def handle_grant_permanent(ack, body, client):  # noqa: ANN001 - Bolt signature
        ack()
        _grant_and_retry(body, client, permanent=True)

    @app.action("revoke_entry")
    def handle_revoke_entry(ack, body, respond):  # noqa: ANN001 - Bolt signature
        ack()
        cmd = body["actions"][0]["value"]
        removed = permissions.remove(cmd)
        respond(
            text=(f":wastebasket: `command({cmd})` を削除しました。" if removed
                  else f"`command({cmd})` は既に存在しませんでした。"),
            replace_original=False,
            response_type="ephemeral",
        )

    @app.command("/agy-permissions")
    def handle_permissions_command(ack, respond, command):  # noqa: ANN001 - Bolt signature
        ack()
        if command.get("channel_id") not in channel_map:
            respond(text="このチャンネルは agy-slack-bridge の対象外です。", response_type="ephemeral")
            return

        text = (command.get("text") or "").strip()
        parts = text.split(maxsplit=1)
        sub = parts[0].lower() if parts else "list"
        arg = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("", "list"):
            cmds = permissions.list_commands()
            if not cmds:
                respond(text="permissions.allow は現在空です。", response_type="ephemeral")
                return
            blocks = [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"`{c}`"},
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "削除"},
                        "style": "danger",
                        "action_id": "revoke_entry",
                        "value": c,
                        "confirm": {
                            "title": {"type": "plain_text", "text": "削除しますか?"},
                            "text": {"type": "mrkdwn", "text": f"`{c}` を permissions.allow から削除します。"},
                            "confirm": {"type": "plain_text", "text": "削除する"},
                            "deny": {"type": "plain_text", "text": "キャンセル"},
                        },
                    },
                }
                for c in cmds
            ]
            respond(text=f"現在の permissions.allow ({len(cmds)}件):", blocks=blocks, response_type="ephemeral")
        elif sub == "add":
            if not arg:
                respond(text="使い方: `/agy-permissions add <コマンド>`", response_type="ephemeral")
                return
            added = permissions.add(arg)
            respond(
                text=(f":white_check_mark: `command({arg})` を追加しました。" if added
                      else f"`command({arg})` はすでに登録されています。"),
                response_type="ephemeral",
            )
        elif sub == "remove":
            if not arg:
                respond(text="使い方: `/agy-permissions remove <コマンド>`", response_type="ephemeral")
                return
            removed = permissions.remove(arg)
            respond(
                text=(f":wastebasket: `command({arg})` を削除しました。" if removed
                      else f"`command({arg})` は登録されていません。"),
                response_type="ephemeral",
            )
        else:
            respond(
                text="使い方: `/agy-permissions [list|add <コマンド>|remove <コマンド>]`",
                response_type="ephemeral",
            )

    return app


def main() -> None:
    app = build_app()
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()


if __name__ == "__main__":
    main()
