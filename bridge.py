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
    """Maps (channel_id, thread_ts) -> agy conversation_id.

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


def run_agy(text: str, project_id: str, conversation_id: Optional[str]) -> dict:
    cmd = [
        AGY_BIN, "-p", text,
        "--project", project_id,
        "--output-format", "json",
        "--print-timeout", "0",
        "--dangerously-skip-permissions",
    ]
    if conversation_id:
        cmd += ["--conversation", conversation_id]
    log.info("running agy for project=%s conversation=%s", project_id, conversation_id or "(new)")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=AGY_TIMEOUT_SEC)
    if proc.returncode != 0:
        raise RuntimeError(f"agy exited {proc.returncode}: {proc.stderr.strip()[-2000:]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"agy returned non-JSON output: {proc.stdout[-2000:]}") from e


def build_app() -> App:
    channel_map = load_channel_map()
    store = ThreadStore(STATE_PATH)
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
            try:
                result = _run_with_status(
                    client, channel_id, thread_key, "考え中です...",
                    _persona_kwargs(channel_cfg),
                    run_agy, text, project_id, conversation_id,
                )
            except Exception:
                logger.exception("agy invocation failed")
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_key,
                    text=":x: agy invocation failed. Check the bridge's logs.",
                    **_persona_kwargs(channel_cfg),
                )
                return

            log.info("agy result=%r", result)
            if result.get("status") != "SUCCESS":
                logger.error("agy returned non-SUCCESS status: %r", result)
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_key,
                    text=f":x: agy status={result.get('status')}: {result.get('error') or '(no error detail)'}",
                    **_persona_kwargs(channel_cfg),
                )
                return

            store.put(channel_id, thread_key, result["conversation_id"], project_id)
            outgoing_text = markdown_to_mrkdwn(result.get("response")) or "(empty response)"
            log.info("posting to slack text=%r", outgoing_text)
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_key,
                text=outgoing_text,
                **_persona_kwargs(channel_cfg),
            )

    return app


def main() -> None:
    app = build_app()
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()


if __name__ == "__main__":
    main()
