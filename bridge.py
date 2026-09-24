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


def run_agy(text: str, project_id: str, conversation_id: Optional[str]) -> dict:
    cmd = [
        AGY_BIN, "-p", text,
        "--project", project_id,
        "--output-format", "json",
        "--print-timeout", "0",
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

    @app.event("message")
    def handle_message(event, client, logger):  # noqa: ANN001 - Bolt signature
        # Ignore edits, deletes, bot messages, thread-broadcast echoes, etc.
        # A plain human message has no "subtype".
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

        conversation_id = store.get(channel_id, thread_key) if is_reply else None

        with locks.get((channel_id, thread_key)):
            try:
                result = run_agy(text, project_id, conversation_id)
            except Exception:
                logger.exception("agy invocation failed")
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_key,
                    text=":x: agy invocation failed. Check the bridge's logs.",
                )
                return

            store.put(channel_id, thread_key, result["conversation_id"], project_id)
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_key,
                text=result.get("response") or "(empty response)",
            )

    return app


def main() -> None:
    app = build_app()
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()


if __name__ == "__main__":
    main()
