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

import difflib
import hashlib
import json
import logging
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

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
# Safety-net upper bound on how long a grant_once entry can outlive its own
# retry, for a conversation that's abandoned mid-task and never comes back
# to a clean turn (see _run_and_reply / ThreadStore.sweep_stale_temp_grants).
AGY_TEMP_GRANT_MAX_AGE_SEC = int(os.environ.get("AGY_TEMP_GRANT_MAX_AGE_SEC", "3600"))
AGY_TEMP_GRANT_SWEEP_INTERVAL_SEC = int(os.environ.get("AGY_TEMP_GRANT_SWEEP_INTERVAL_SEC", "300"))
AGY_PROJECTS_DIR = Path("~/.gemini/config/projects").expanduser()
AGY_BRAIN_DIR = Path("~/.gemini/antigravity-cli/brain").expanduser()
# Extensions worth auto-uploading a local file agy mentions by file:// link
# (a generated chart, report, etc.) - deliberately narrow so this can't turn
# into a generic "fetch me any file on the box" primitive.
UPLOADABLE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".csv", ".xlsx", ".md", ".txt"}
UPLOAD_MAX_BYTES = 20 * 1024 * 1024

# --- Locally-operated config drift detection -------------------------------
# "Category B" files (local-tools.md's content, a project's own Drive/API
# access setup, ...) are deliberately NOT distributed from a shared
# template - they're specific to whoever runs this bridge and what they've
# installed/authorized. There's nothing upstream to diff against, so the
# trusted baseline is whatever a human last explicitly confirmed via the
# config_seal/config_restore Slack buttons (see _check_config_drift).
AGY_CONFIG_BACKUP_DIR = Path(
    os.environ.get("AGY_CONFIG_BACKUP_DIR", str(Path.home() / ".config" / "agy-slack-bridge" / "config-backups"))
)
AGY_CONFIG_DRIFT_CHECK_INTERVAL_SEC = int(os.environ.get("AGY_CONFIG_DRIFT_CHECK_INTERVAL_SEC", "1800"))


def _file_hash(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _config_backup_path(abs_path: str) -> Path:
    name = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:20]
    return AGY_CONFIG_BACKUP_DIR / f"{name}.bak"


def _config_seal_blocks(rel_path: str, abs_path: str, text: str) -> list[dict]:
    value = json.dumps({"path": abs_path, "rel_path": rel_path})
    blocks = _text_to_section_blocks(text)
    blocks.append({
        "type": "actions",
        "elements": [{
            "type": "button",
            "text": {"type": "plain_text", "text": "この内容を正として確定"},
            "style": "primary",
            "action_id": "config_seal",
            "value": value,
        }],
    })
    return blocks


def _config_drift_blocks(rel_path: str, abs_path: str, text: str) -> list[dict]:
    value = json.dumps({"path": abs_path, "rel_path": rel_path})
    blocks = _text_to_section_blocks(text)
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "今の内容を新しく確定"},
                "style": "primary",
                "action_id": "config_seal",
                "value": value,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "確定済みの内容に戻す"},
                "style": "danger",
                "action_id": "config_restore",
                "value": value,
            },
        ],
    })
    return blocks


CONFIG_PREVIEW_MAX_CHARS = 2500  # leaves room for the notice line so the
# whole message (notice + fence + preview) stays under _text_to_section_blocks'
# own 2900-char chunk size - otherwise a long preview could get split mid-fence.


def _read_text_preview(path: Path, max_chars: int = CONFIG_PREVIEW_MAX_CHARS) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(読み取り失敗: {exc})"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... (省略、全{len(text)}文字)"
    return text


def _config_diff_preview(old_text: str, new_text: str, max_chars: int = CONFIG_PREVIEW_MAX_CHARS) -> str:
    diff = "\n".join(difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(),
        fromfile="確定済み", tofile="現在", lineterm="",
    ))
    if not diff:
        return "(差分なし - 改行コード等、見た目に出ない違いの可能性があります)"
    if len(diff) > max_chars:
        diff = diff[:max_chars] + f"\n... (省略、全{len(diff)}文字)"
    return diff


def _check_config_drift(store: "ThreadStore", client, channel_id: str, channel_cfg: dict) -> None:
    """Hashes each of this channel's `watched_config_files` (relative to its
    agy project's workspace root) against the last sealed baseline, and
    posts a one-time (per hash) Slack message - with the actual content (or
    a diff) attached, not just a bare notice, so sealing isn't a blind click
    - with seal/restore buttons if it's unsealed or has drifted. A clean
    match is silent."""
    rel_paths = channel_cfg.get("watched_config_files") or []
    if not rel_paths:
        return
    proj_root = _project_root(channel_cfg["project"])
    if not proj_root:
        return
    persona = _persona_kwargs(channel_cfg)
    for rel_path in rel_paths:
        abs_path = proj_root / rel_path
        cur_hash = _file_hash(abs_path)
        if cur_hash is None:
            continue  # doesn't exist (yet) - nothing to protect
        abs_str = str(abs_path)
        baseline = store.get_config_baseline(abs_str)
        if baseline and baseline["content_hash"] == cur_hash:
            continue  # matches the sealed baseline - clean
        if store.get_last_notified_hash(abs_str) == cur_hash:
            continue  # already notified about this exact state; don't spam
        try:
            if baseline is None:
                notice = f":new: `{rel_path}` はまだ内容が確定(seal)されていません。内容:"
                preview = _read_text_preview(abs_path)
                text = f"{notice}\n```\n{preview}\n```"
                blocks = _config_seal_blocks(rel_path, abs_str, text)
            else:
                notice = (f":warning: `{rel_path}` の内容が、最後に確定した内容と一致しません"
                          f"（{baseline['sealed_at']} に {baseline['sealed_by'] or '?'} が確定）。差分:")
                old_text = _read_text_preview(Path(baseline["backup_path"]))
                new_text = _read_text_preview(abs_path)
                diff = _config_diff_preview(old_text, new_text)
                text = f"{notice}\n```\n{diff}\n```"
                blocks = _config_drift_blocks(rel_path, abs_str, text)
            client.chat_postMessage(channel=channel_id, text=text, blocks=blocks, **persona)
            store.mark_config_notified(abs_str, cur_hash)
        except Exception:
            log.exception("failed to post config-drift notice for %s", abs_str)


# --- Mermaid diagram rendering --------------------------------------------
# agy is fond of putting diagrams in a ```mermaid fenced block, which Slack
# can only ever show as literal text - nobody reading Slack has a Mermaid
# renderer. Render each one to a PNG with a headless Chromium (already on
# this box for the accountant project's browser automation) and upload the
# image instead; requires the optional `playwright` package (see README).
_MERMAID_FENCE_RE = re.compile(r"```mermaid[ \t]*\r?\n(.*?)```", re.IGNORECASE | re.DOTALL)
MERMAID_JS_URL = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"
MERMAID_JS_CACHE = Path(
    os.environ.get("AGY_BRIDGE_CACHE_DIR", str(Path.home() / ".cache" / "agy-slack-bridge"))
) / "mermaid.min.js"
MERMAID_RENDER_TIMEOUT_SEC = 20


def _get_mermaid_js() -> str:
    """Fetched once and cached on disk - every later render reuses the
    cached copy instead of hitting the CDN again."""
    if MERMAID_JS_CACHE.exists():
        return MERMAID_JS_CACHE.read_text(encoding="utf-8")
    import urllib.request
    data = urllib.request.urlopen(MERMAID_JS_URL, timeout=30).read().decode("utf-8")
    MERMAID_JS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    MERMAID_JS_CACHE.write_text(data, encoding="utf-8")
    return data


def _render_mermaid_png(diagram: str, out_path: Path) -> None:
    """Renders one Mermaid diagram to a PNG via `mermaid.render()` (returns
    an SVG string - never parses `diagram` as HTML, so labels containing
    `<`/`>`/`&` or an intentional `<br/>` all come through correctly) run
    inside a headless Chromium, then screenshots just that SVG element."""
    from playwright.sync_api import sync_playwright  # optional dependency
    js = _get_mermaid_js()
    html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        "<style>body{margin:0;background:#fff;}</style>"
        f"<script>{js}</script></head><body><div id=\"d\"></div></body></html>"
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1200}, device_scale_factor=2)
            page.set_content(html)
            page.evaluate(
                "async (src) => { mermaid.initialize({startOnLoad:false}); "
                "const {svg} = await mermaid.render('g1', src); "
                "document.getElementById('d').innerHTML = svg; }",
                diagram,
            )
            svg = page.query_selector("#d svg")
            if svg is None:
                raise RuntimeError("mermaid.render() produced no <svg>")
            svg.screenshot(path=str(out_path))
        finally:
            browser.close()


def _extract_and_render_mermaid(response_text: str, out_dir: Path) -> tuple[str, list[Path]]:
    """Pulls every ```mermaid block out of agy's response, renders each to a
    PNG under `out_dir`, and returns (text with those blocks replaced by a
    short marker, the list of rendered PNGs) - a block that fails to render
    (bad syntax, no playwright installed, ...) is left as plain text instead
    of silently vanishing."""
    images: list[Path] = []

    def repl(m: "re.Match") -> str:
        diagram = m.group(1)
        idx = len(images) + 1
        out_path = out_dir / f"mermaid_{idx}.png"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            _render_mermaid_png(diagram, out_path)
        except Exception:
            log.warning("mermaid render failed, leaving as text", exc_info=True)
            return m.group(0)
        images.append(out_path)
        return f"_(図を画像として添付しました: {out_path.name})_"

    new_text = _MERMAID_FENCE_RE.sub(repl, response_text or "")
    return new_text, images


_CODE_SPAN_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_TABLE_SEP_RE = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s)]+)\)")
_HEADER_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_FILE_LINK_RE = re.compile(r"file://(/[^\s)>\]]+)")

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


_FENCE_RE = re.compile(r"```[ \t]*([\w.+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
# A fence tagged with one of these isn't really code - it's agy using a
# fenced block just to set off a block of prose (often the whole answer).
# GitHub hides the tag; Slack's mrkdwn code blocks don't support a language
# tag at all, so it used to show up as a literal first line inside the
# block (the exact "markdown"/"text" residue this fixes). Unwrap these
# entirely instead, so headers/bold/tables inside render normally rather
# than sitting frozen in monospace.
_PROSE_FENCE_LANGS = {"", "markdown", "md", "text", "txt", "plain", "plaintext"}


def _normalize_fences(text: str) -> str:
    def repl(m: "re.Match") -> str:
        lang, body = m.group(1).strip().lower(), m.group(2)
        if lang in _PROSE_FENCE_LANGS:
            return body
        return f"```\n{body}```"  # real code - keep the block, drop the tag
    return _FENCE_RE.sub(repl, text)


def markdown_to_mrkdwn(text: str) -> str:
    """Best-effort conversion of the GitHub-flavored Markdown agy returns
    into Slack's "mrkdwn" dialect, so bold/links/tables actually render
    instead of showing up as literal '**' and '[text](url)' in Slack."""
    if not text:
        return text

    text = _normalize_fences(text)
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
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS temp_grants (
                conversation_id TEXT NOT NULL,
                entry TEXT NOT NULL,
                granted_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (conversation_id, entry)
            )"""
        )
        # Every permission ask and its outcome, args included, so that
        # entries asked for over and over can be reviewed (see
        # `/agy-permissions stats`) and promoted to a permanent grant.
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS permission_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (datetime('now')),
                channel_id TEXT,
                conversation_id TEXT,
                event TEXT NOT NULL,
                entry TEXT NOT NULL,
                user TEXT
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS channel_settings (
                channel_id TEXT PRIMARY KEY,
                model TEXT,
                effort TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        # "Category B" locally-operated config files (local-tools.md, Drive
        # access config, ...) have no published upstream to re-fetch and
        # diff against, unlike the GitHub-distributed safety rules/hooks -
        # so the trusted baseline here is whatever a human last explicitly
        # "sealed" via the config_seal/config_restore buttons (see
        # _check_config_drift). last_notified_hash exists only so an
        # unresolved drift doesn't re-post the same Slack message every
        # sweep interval - it's set whenever we post, and compared against
        # on the next sweep so only an actual *change* re-notifies.
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS config_baselines (
                path TEXT PRIMARY KEY,
                content_hash TEXT,
                backup_path TEXT,
                sealed_at TEXT,
                sealed_by TEXT,
                last_notified_hash TEXT
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

    def latest_conversation(self, channel_id: str) -> Optional[str]:
        """The most recently active conversation in a channel - what
        `/agy-link` means by "current", since Slack won't deliver a slash
        command from inside a thread."""
        with self._lock:
            row = self._conn.execute(
                "SELECT conversation_id FROM threads WHERE channel_id=? "
                "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                (channel_id,),
            ).fetchone()
            return row[0] if row else None

    def get_config_baseline(self, path: str) -> Optional[dict]:
        """None only if this path has never been sealed - it may still have
        a row (just to remember `last_notified_hash` pre-seal; see
        mark_config_notified) with content_hash NULL, which doesn't count
        as a real baseline."""
        with self._lock:
            row = self._conn.execute(
                "SELECT content_hash, backup_path, sealed_at, sealed_by, last_notified_hash "
                "FROM config_baselines WHERE path=? AND content_hash IS NOT NULL",
                (path,),
            ).fetchone()
        if not row:
            return None
        return {"content_hash": row[0], "backup_path": row[1], "sealed_at": row[2],
                "sealed_by": row[3], "last_notified_hash": row[4]}

    def seal_config(self, path: str, content_hash: str, backup_path: str, sealed_by: Optional[str]) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO config_baselines (path, content_hash, backup_path, sealed_at, sealed_by, last_notified_hash)
                   VALUES (?, ?, ?, datetime('now'), ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                     content_hash=excluded.content_hash, backup_path=excluded.backup_path,
                     sealed_at=excluded.sealed_at, sealed_by=excluded.sealed_by,
                     last_notified_hash=excluded.last_notified_hash""",
                (path, content_hash, backup_path, sealed_by, content_hash),
            )
            self._conn.commit()

    def get_last_notified_hash(self, path: str) -> Optional[str]:
        """Works pre-seal too (unlike get_config_baseline), so an unsealed
        file's "still not sealed" notice doesn't repeat every sweep."""
        with self._lock:
            row = self._conn.execute(
                "SELECT last_notified_hash FROM config_baselines WHERE path=?", (path,),
            ).fetchone()
            return row[0] if row else None

    def mark_config_notified(self, path: str, content_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO config_baselines (path, last_notified_hash) VALUES (?, ?)
                   ON CONFLICT(path) DO UPDATE SET last_notified_hash=excluded.last_notified_hash""",
                (path, content_hash),
            )
            self._conn.commit()

    def log_permission(self, channel_id: Optional[str], conversation_id: Optional[str],
                       event: str, entries: list, user: Optional[str] = None) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT INTO permission_log (channel_id, conversation_id, event, entry, user) "
                "VALUES (?, ?, ?, ?, ?)",
                [(channel_id, conversation_id, event, e, user) for e in entries],
            )
            self._conn.commit()

    def permission_stats(self, days: int = 30, limit: int = 15) -> list:
        """(entry, requested, granted_once, granted_permanent, denied) for the
        most-asked-for entries in the last `days` days."""
        with self._lock:
            return self._conn.execute(
                """SELECT entry,
                          SUM(event IN ('requested', 'throttled')),
                          SUM(event = 'granted_once'),
                          SUM(event = 'granted_permanent'),
                          SUM(event = 'denied')
                   FROM permission_log
                   WHERE ts >= datetime('now', ?)
                   GROUP BY entry
                   ORDER BY SUM(event IN ('requested', 'throttled')) DESC, MAX(id) DESC
                   LIMIT ?""",
                (f"-{int(days)} days", limit),
            ).fetchall()

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

    def add_temp_grant(self, conversation_id: str, entry: str) -> None:
        """Records that `entry` is temporarily granted for `conversation_id`,
        so release can be deferred (see clear_temp_grants /
        sweep_stale_temp_grants) instead of happening right after the one
        retry that needed it - a second grant for a *different* entry in
        the same conversation must not undo this one."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO temp_grants (conversation_id, entry, granted_at)
                   VALUES (?, ?, datetime('now'))
                   ON CONFLICT(conversation_id, entry) DO UPDATE SET granted_at=excluded.granted_at""",
                (conversation_id, entry),
            )
            self._conn.commit()

    def clear_temp_grants(self, conversation_id: str) -> list[str]:
        """Releases every temp grant recorded for this conversation, and
        returns the subset now safe to actually remove from
        permissions.allow - i.e. no *other* conversation still holds the
        same entry, since that file is shared machine-wide."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT entry FROM temp_grants WHERE conversation_id=?",
                (conversation_id,),
            ).fetchall()
            entries = [r[0] for r in rows]
            if not entries:
                return []
            self._conn.execute("DELETE FROM temp_grants WHERE conversation_id=?", (conversation_id,))
            self._conn.commit()
            to_revoke = []
            for entry in entries:
                remaining = self._conn.execute(
                    "SELECT COUNT(*) FROM temp_grants WHERE entry=?", (entry,)
                ).fetchone()[0]
                if remaining == 0:
                    to_revoke.append(entry)
            return to_revoke

    def sweep_stale_temp_grants(self, max_age_seconds: int) -> list[str]:
        """Safety net for a conversation that never comes back to a clean
        turn (abandoned mid-task, etc.) - force-releases anything older
        than max_age_seconds, with the same cross-conversation check."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT conversation_id, entry FROM temp_grants
                   WHERE (strftime('%s','now') - strftime('%s', granted_at)) > ?""",
                (max_age_seconds,),
            ).fetchall()
            if not rows:
                return []
            self._conn.executemany(
                "DELETE FROM temp_grants WHERE conversation_id=? AND entry=?", rows
            )
            self._conn.commit()
            to_revoke = []
            seen: set[str] = set()
            for _, entry in rows:
                if entry in seen:
                    continue
                seen.add(entry)
                remaining = self._conn.execute(
                    "SELECT COUNT(*) FROM temp_grants WHERE entry=?", (entry,)
                ).fetchone()[0]
                if remaining == 0:
                    to_revoke.append(entry)
            return to_revoke

    def get_channel_settings(self, channel_id: str) -> Optional[dict]:
        """The per-channel model/effort override set via `/agy-model set`,
        if any - takes precedence over that channel's static `model`/`effort`
        in config.yaml. None means "no override, use the config default"."""
        with self._lock:
            row = self._conn.execute(
                "SELECT model, effort FROM channel_settings WHERE channel_id=?",
                (channel_id,),
            ).fetchone()
        return {"model": row[0], "effort": row[1]} if row else None

    def set_channel_settings(self, channel_id: str, model: Optional[str], effort: Optional[str]) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO channel_settings (channel_id, model, effort, updated_at)
                   VALUES (?, ?, ?, datetime('now'))
                   ON CONFLICT(channel_id) DO UPDATE SET
                     model=excluded.model, effort=excluded.effort, updated_at=excluded.updated_at""",
                (channel_id, model, effort),
            )
            self._conn.commit()

    def clear_channel_settings(self, channel_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM channel_settings WHERE channel_id=?", (channel_id,))
            self._conn.commit()
            return cur.rowcount > 0


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

    def list_entries(self) -> list[str]:
        with self._lock:
            return list((self._load().get("permissions") or {}).get("allow") or [])

    def add_entry(self, entry: str) -> bool:
        with self._lock:
            data = self._load()
            allow = data.setdefault("permissions", {}).setdefault("allow", [])
            if entry in allow:
                return False
            allow.append(entry)
            self._save(data)
        log.info("permissions.allow += %r", entry)
        return True

    def remove_entry(self, entry: str) -> bool:
        with self._lock:
            data = self._load()
            allow = (data.get("permissions") or {}).get("allow") or []
            if entry not in allow:
                return False
            allow.remove(entry)
            self._save(data)
        log.info("permissions.allow -= %r", entry)
        return True

    # Backward-compatible helpers for a bare shell command (used by
    # /agy-permissions add/remove when given a plain command instead of a
    # full "<kind>(...)" entry like the grant buttons pass).
    def list_commands(self) -> list[str]:
        return [e[len("command("):-1] for e in self.list_entries()
                if e.startswith("command(") and e.endswith(")")]

    def add(self, command: str) -> bool:
        return self.add_entry(self._entry(command))

    def remove(self, command: str) -> bool:
        return self.remove_entry(self._entry(command))


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
    detail = (params.get("CommandLine") or params.get("FilePath") or params.get("Path")
              or params.get("TargetFile") or params.get("Url"))
    if detail is None and params:
        detail = json.dumps(params, ensure_ascii=False)
    head = f"`{tool_name}`: `{detail}`" if detail else f"`{tool_name}`"

    message = err.get("message") or ""
    if "denied permission" in message.lower():
        return head  # the grant button / surrounding text already covers this one

    # Not a permission issue (e.g. a file edit whose target text no longer
    # matches the file) - the reason itself is the useful part here, so
    # include it instead of leaving just the target name.
    reason = message.split("\nDo not attempt to circumvent")[0].strip()
    return f"{head} — {reason}" if reason else head


# Some tool kinds spell out the exact grantable permissions.allow entry
# right in their own denial message, e.g. read_url_content's:
#   'user denied permission for read_url(support.yayoi-kk.co.jp)'
# (confirmed by testing against a live denial) - trust that instead of
# guessing the syntax per tool kind ourselves.
_ENTRY_IN_MESSAGE_RE = re.compile(r"\b([a-z_]+\([^()]*\))")
# Whether a string typed into /agy-permissions add/remove already looks like
# a full "<kind>(...)" entry (e.g. "read_url(example.com)") rather than a
# bare shell command that should get wrapped as command(<that>).
_FULL_ENTRY_RE = re.compile(r"^[a-z_]+\(.*\)$")


def _is_permission_error(err: dict) -> bool:
    """Distinguishes a genuine permission denial (grantable via
    permissions.allow) from some other tool failure - e.g.
    replace_file_content failing because its target text didn't match the
    file's *current* content, or view_file failing because the model
    itself gave a wrong/nonexistent path, which are real bugs elsewhere,
    not a permission problem.

    Checks for the specific phrase "denied permission", not just
    "permission" - confirmed live that a bare substring check is too loose
    and produces false positives: a view_file failure's own message
    ("declaring permissions: ... convert tool call for permissions: ...
    failed to read file: stat ...: no such file or directory") mentions
    "permissions" in its internal plumbing description, with nothing to do
    with an actual denial, and got wrongly classified as one - shown with
    the "権限不足で拒否されました" wording and a "click the button below"
    instruction, but no button, since _grantable_entry correctly found
    nothing grantable in it. Every confirmed real denial's message
    literally says "user denied permission ..." (to run a command, or for
    read_file(...)/read_url(...)), so anchor on that instead."""
    return "denied permission" in (err.get("message") or "").lower()


def _is_deny_rule_error(err: dict) -> bool:
    """A hard block from a permissions.deny rule (e.g. the bridge's own
    source/config, off limits to agents). Not grantable - deny beats allow -
    so it must get neither buttons nor the "not a permission problem" text."""
    return "deny rule" in (err.get("message") or "").lower()


def _stuck_denial_reason(err: dict, permissions: "PermissionsFile") -> Optional[str]:
    """For a run_command denial that *looks* like an ordinary permission
    gap (passes _is_permission_error) but almost certainly isn't - clicking
    "grant and retry" can't fix either case, so it shouldn't get a button:

    - The command doesn't even parse as a shell command (e.g. an unclosed
      quote). Confirmed by testing: agy reports this with the exact same
      "auto-denied, needs permission" wording as a genuine denial - it
      can't tell "malformed" from "not yet granted" apart either. Almost
      always means the model's own output got cut off mid-string while
      writing a long argument (e.g. a long commit message); re-running the
      same (truncated) text verbatim can never succeed.
    - The exact command is already in permissions.allow. Whatever's wrong,
      it isn't a missing grant - re-granting what's already granted does
      nothing. (Confirmed live: a previously-granted, unmodified command
      kept failing identically across multiple grant/retry cycles.)
    """
    if err.get("tool_name") != "run_command":
        return None
    cmd = (err.get("parameters") or {}).get("CommandLine")
    if not cmd:
        return None
    try:
        list(shlex.shlex(cmd, posix=True, punctuation_chars=True))
    except ValueError:
        return ("コマンドの構文が壊れています（例: 引用符が閉じていない）。"
                "モデルの出力が途中で切れた可能性が高く、同じ内容を再試行しても直りません。")
    grantable = _grantable_entry(err)
    if grantable and grantable["entry"] in permissions.list_entries():
        return "このコマンドはすでに許可リストに入っています。権限不足ではなく、別の理由で失敗しています。"
    return None


def _grantable_entry(err: dict) -> Optional[dict]:
    """Figures out the literal permissions.allow entry that would have let
    one run_agy() tool error through, plus a short human-readable
    description of what it was trying to do (for confirm dialogs / retry
    prompts). Returns None if we can't tell - that error just stays
    text-only, no button."""
    tool_name = err.get("tool_name")
    params = err.get("parameters") or {}

    if tool_name == "run_command":
        cmd = params.get("CommandLine")
        if not cmd:
            return None
        return {"entry": f"command({cmd})", "describe": cmd}

    m = _ENTRY_IN_MESSAGE_RE.search(err.get("message") or "")
    if not m:
        return None
    entry = m.group(1)
    describe = params.get("Url") or params.get("FilePath") or params.get("Path") or entry
    return {"entry": entry, "describe": describe}


def _project_root(project_id: str) -> Optional[Path]:
    """Best-effort: the git working directory agy registered for this
    project, read from ~/.gemini/config/projects/<id>.json. Used to scope
    which local files this bridge is willing to auto-upload to Slack (see
    _extract_uploadable_files) - never anywhere outside a project's own
    workspace or its own conversation's brain/artifact directory."""
    try:
        data = json.loads((AGY_PROJECTS_DIR / f"{project_id}.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    for res in (data.get("projectResources") or {}).get("resources", []):
        # A project can be registered either as a plain folder or a git
        # checkout - confirmed by inspection: this bridge's own two
        # projects use different forms (one bare `folderUri`, the other
        # wrapped in `gitFolder`), so both need checking.
        uri = res.get("folderUri") or (res.get("gitFolder") or {}).get("folderUri")
        if uri and uri.startswith("file://"):
            return Path(uri[len("file://"):])
    return None


PERMANENT_COMMANDS_REL = Path(".agents") / ".permanent-commands.json"

_SCRIPT_INTERPRETER_BASENAMES = {
    "python", "python3", "python3.10", "python3.11", "python3.12", "python3.13",
    "python3.14", "bash", "sh", "zsh", "node", "ruby", "perl",
}
_SCRIPT_EXTS = (".py", ".sh", ".js", ".mjs", ".rb", ".pl")


def _resolve_permanent_script_path(entry: str, root: Path) -> Optional[str]:
    """Extracts the script a `command(...)` permission entry would run and
    resolves it to an absolute, normalized path under `root` - mirrors
    script_integrity.py's own find_script_targets so the two stay in
    agreement. Matching between the hook and this manifest is by resolved
    script path, not raw command text (text would miss the same script
    invoked a different way - absolute path, `./`-relative, ...). Returns
    None for anything that isn't "run this script" (e.g. a bare command
    with no script argument, or a non-command() entry kind like
    read_url(...)) - there's no script path to track for those."""
    if not (entry.startswith("command(") and entry.endswith(")")):
        return None
    cmdline = entry[len("command("):-1]
    try:
        tokens = shlex.split(cmdline)
    except ValueError:
        return None
    if not tokens:
        return None
    head = tokens[0]
    head_base = os.path.basename(head)
    candidate = None
    if head_base in _SCRIPT_INTERPRETER_BASENAMES:
        for tok in tokens[1:]:
            if tok.startswith("-"):
                continue
            candidate = tok
            break
    elif head.endswith(_SCRIPT_EXTS):
        candidate = head
    if not candidate or not candidate.endswith(_SCRIPT_EXTS):
        return None
    return os.path.normpath(candidate if os.path.isabs(candidate) else os.path.join(str(root), candidate))


def _mark_permanent_command(project_id: str, entry: str, present: bool) -> None:
    """Keeps a project's `.agents/.permanent-commands.json` in sync with
    its *permanent* permissions.allow entries (never its one-time/temp
    grants) - the project-local manifest script_integrity.py (the
    PreToolUse hook) reads to decide whether a run_command target needs
    git-commit enforcement or just the lighter hash-ledger check. Stores
    resolved script paths (see _resolve_permanent_script_path), not the
    raw entry text. Called from /agy-permissions add/remove and from
    setup_agy_project.py's own direct settings.json edits (gdrive
    grants), so either path keeps this manifest accurate regardless of
    how the permanent grant was made."""
    root = _project_root(project_id)
    if not root:
        return
    script_path = _resolve_permanent_script_path(entry, root)
    if script_path is None:
        return
    path = root / PERMANENT_COMMANDS_REL
    try:
        entries = set(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else set()
    except (json.JSONDecodeError, OSError):
        entries = set()
    if present:
        entries.add(script_path)
    else:
        entries.discard(script_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(entries), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        log.exception("failed to update %s", path)


def _extract_uploadable_files(response_text: str, project_id: str, conversation_id: str) -> list[Path]:
    """Finds file:// links in agy's response (e.g. "generated this chart:
    file:///.../chart.png") that point at real, safely-scoped local files,
    so the bridge can upload them to Slack directly instead of leaving a
    dead file:// link nothing in Slack can open. Deliberately narrow:
    - only image/document-ish extensions (UPLOADABLE_EXTS), never an
      arbitrary file agy happens to mention;
    - only under this project's own workspace (per _project_root) or this
      conversation's own brain/artifact directory - never an arbitrary
      absolute path, which could otherwise leak something sensitive
      elsewhere on the box if a response ever named one;
    - must actually exist, be a plain file, and be under the size cap.
    """
    roots = []
    proj_root = _project_root(project_id)
    if proj_root and proj_root.is_dir():
        roots.append(proj_root.resolve())
    brain_dir = AGY_BRAIN_DIR / conversation_id
    if brain_dir.is_dir():
        roots.append(brain_dir.resolve())
    if not roots:
        return []

    found: list[Path] = []
    seen: set[str] = set()
    for m in _FILE_LINK_RE.finditer(response_text or ""):
        raw = m.group(1)
        if raw in seen:
            continue
        seen.add(raw)
        try:
            p = Path(raw).resolve()
        except (OSError, ValueError):
            continue
        if p.suffix.lower() not in UPLOADABLE_EXTS:
            continue
        if not any(p == root or root in p.parents for root in roots):
            continue
        try:
            if not p.is_file() or p.stat().st_size > UPLOAD_MAX_BYTES:
                continue
        except OSError:
            continue
        found.append(p)
        if len(found) >= 5:  # sane cap per reply
            break
    return found


# How long a fetched `agy models` list is trusted before refetching, for
# /agy-model's validation and listing - that call shells out and can be slow,
# and the model catalog doesn't change within the lifetime of one bridge run.
AGY_MODELS_CACHE_TTL_SEC = 300
_agy_models_cache: dict = {"ts": 0.0, "models": []}


def _fetch_agy_models() -> list[tuple[str, str]]:
    """Runs `agy models` and parses its tab-separated "id<TAB>label" stdout
    lines. Best-effort: on any failure, returns the last good cached list
    (possibly empty) rather than raising, since this only feeds a UX nicety
    (listing/validating choices in /agy-model), not the actual --model flag
    passed to run_agy."""
    now = time.time()
    if _agy_models_cache["models"] and now - _agy_models_cache["ts"] < AGY_MODELS_CACHE_TTL_SEC:
        return _agy_models_cache["models"]
    try:
        proc = subprocess.run([AGY_BIN, "models"], capture_output=True, text=True, timeout=30)
        models = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            model_id, _, label = line.partition("\t")
            models.append((model_id, label.strip() or model_id))
        if models:
            _agy_models_cache["ts"] = now
            _agy_models_cache["models"] = models
        return models
    except Exception:
        log.warning("failed to fetch agy models", exc_info=True)
        return _agy_models_cache["models"]


def _effective_model_effort(store: "ThreadStore", channel_id: str, channel_cfg: dict) -> tuple:
    """A /agy-model override for this channel, if set, otherwise the
    channel's static model/effort from config.yaml."""
    override = store.get_channel_settings(channel_id)
    if override:
        return override.get("model"), override.get("effort")
    return channel_cfg.get("model"), channel_cfg.get("effort")


_AGY_INSTALL_UUID_RE = re.compile(r'installation_uuid:\s*"([0-9a-fA-F-]+)"')
AGY_STATE_PBTXT = Path.home() / ".gemini" / "antigravity-cli" / "antigravity_state.pbtxt"


def _remote_instance_id() -> Optional[str]:
    """The <instance> in antigravity.google.com/r/<instance>. It's not the
    human-readable name `agy remote-control status` prints; it's the CLI's
    installation uuid plus "-v2" (the id the daemon logs as its
    remote-control connection). AGY_REMOTE_INSTANCE overrides it."""
    override = os.environ.get("AGY_REMOTE_INSTANCE")
    if override:
        return override
    try:
        m = _AGY_INSTALL_UUID_RE.search(AGY_STATE_PBTXT.read_text())
        return f"{m.group(1)}-v2" if m else None
    except Exception:
        log.warning("failed to read agy installation uuid", exc_info=True)
        return None


def _web_ui_link(conversation_id: str) -> Optional[str]:
    instance = _remote_instance_id()
    if not instance:
        return None
    return f"https://antigravity.google.com/r/{instance}?p={quote('c/' + conversation_id, safe='')}"


_NO_INSTANCE_TEXT = ":x: agy のインスタンスID（installation_uuid）が取得できません。"


# agy's built-in slash commands (the ones handled locally by the CLI itself,
# as opposed to skill commands like /plan or /boost, which are just prompts
# for the agent) come back instantly with a `command` field in the result
# and no conversation. `/agy <name>` uses this set to decide between
# "answer right here, ephemerally" and "run it as a normal agent turn".
# Fetched from `/help` so it tracks the installed agy; this is only the
# fallback for when that fetch fails.
_AGY_BUILTIN_FALLBACK = frozenset({
    "agents", "changelog", "config", "settings", "credits", "effort", "help",
    "hooks", "model", "permissions", "skills", "usage", "quota",
})
_agy_builtin_cache: dict = {"ts": 0.0, "names": frozenset()}
_HELP_LINE_RE = re.compile(r"^/(\S+)(?: \(([^)]*)\))?\t")


def _fetch_agy_builtin_commands(project_id: str) -> frozenset:
    now = time.time()
    if _agy_builtin_cache["names"] and now - _agy_builtin_cache["ts"] < AGY_MODELS_CACHE_TTL_SEC:
        return _agy_builtin_cache["names"]
    try:
        result = run_agy("/help", project_id, None)
        names = set()
        for line in (result.get("response") or "").splitlines():
            m = _HELP_LINE_RE.match(line)
            if m:
                names.add(m.group(1).lower())
                names.update(a.strip().lower() for a in (m.group(2) or "").split(",") if a.strip())
        if names:
            _agy_builtin_cache["ts"] = now
            _agy_builtin_cache["names"] = frozenset(names)
            return _agy_builtin_cache["names"]
    except Exception:
        log.warning("failed to fetch agy builtin slash commands", exc_info=True)
    return _agy_builtin_cache["names"] or _AGY_BUILTIN_FALLBACK


def run_agy(text: str, project_id: str, conversation_id: Optional[str],
            model: Optional[str] = None, effort: Optional[str] = None,
            permissions: Optional["PermissionsFile"] = None) -> dict:
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
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    log.info("running agy for project=%s conversation=%s", project_id, conversation_id or "(new)")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=AGY_TIMEOUT_SEC)
    if proc.returncode != 0:
        raise RuntimeError(f"agy exited {proc.returncode}: {proc.stderr.strip()[-2000:]}")

    result = None
    tool_errors = []
    tool_steps = set()
    raw_tool_events = []  # kept only for diagnostics (see _run_and_reply)
    last_tool_step: dict = {}
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
            if step.get("step_type") == "tool":
                raw_tool_events.append(step)
                last_tool_step[step.get("step_index")] = step
                # Only steps that actually finished count as progress -
                # denied/cancelled ones (an agent probing for what it can
                # get away with) must not keep the auto-retry going.
                if step.get("state") == "DONE":
                    tool_steps.add(step.get("step_index"))
                else:
                    tool_steps.discard(step.get("step_index"))
            if step.get("state") == "ERROR" and step.get("step_type") == "tool":
                info = step.get("tool_info", {})
                tool_errors.append({
                    "tool_name": step.get("tool_name"),
                    "parameters": info.get("parameters"),
                    "message": (info.get("error") or {}).get("message"),
                })

    if result is None:
        raise RuntimeError(f"agy produced no result event: {proc.stdout[-2000:]}")

    # Confirmed by testing: a denied command normally comes back as an ERROR
    # step carrying the command, but *sometimes* (same command, seemingly a
    # race in agy's async permission check) as a step that just ends DONE
    # with no error and - unlike every command that actually ran, which
    # carries "output" - no output either, while agy's stderr says the
    # command was "auto-denied". That silent form used to leave us with
    # neither a button nor even the command. It's recoverable from the
    # step's own parameters, so report it like the loud form.
    #
    # But "no output" is structurally ambiguous: a command that's silent on
    # *success* (git add, git status -s/git diff with nothing to report,
    # rm -f, ...) looks identical to one that never ran because it was
    # denied - both simply lack an "output" key. Confirmed live: granting
    # an already-silent command and retrying kept reporting it as denied
    # again, forever, even though (being silent on success) there's no way
    # to tell from this step alone that it didn't just quietly succeed. If
    # the command is already in permissions.allow, a fresh denial of the
    # exact same text is far less likely than it simply having run and said
    # nothing - so skip the inference for those and let them count as the
    # progress they almost certainly are, instead of looping a grant that
    # can't fix a command that was never actually the problem.
    already_allowed = set(permissions.list_entries()) if permissions else set()
    if "auto-denied" in proc.stderr:
        for idx, step in last_tool_step.items():
            info = step.get("tool_info") or {}
            cmd = (info.get("parameters") or {}).get("CommandLine")
            if not (step.get("tool_name") == "run_command" and step.get("state") == "DONE"
                    and "output" not in info and cmd):
                continue
            if f"command({cmd})" in already_allowed:
                continue  # almost certainly a silent success, not a denial
            tool_errors.append({
                "tool_name": "run_command",
                "parameters": info.get("parameters"),
                "message": f'permission check failed for unsandboxed "{cmd}": '
                           f"user denied permission to run command:\n{cmd}\n"
                           "(inferred: agy ended the step DONE with no output, and auto-denied it)",
            })
            tool_steps.discard(idx)  # a denied command is not progress

    result["_tool_errors"] = tool_errors
    result["_tool_steps"] = len(tool_steps)
    result["_tool_events"] = raw_tool_events
    result["_stderr"] = proc.stderr.strip()[-1500:]
    return result


def _text_to_section_blocks(text: str, chunk_size: int = 2900) -> list[dict]:
    """Slack section blocks cap out around 3000 chars; split long replies
    into several sections rather than truncating them."""
    text = text or "(empty response)"
    chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)] or [text]
    return [{"type": "section", "text": {"type": "mrkdwn", "text": c}} for c in chunks]


def _build_reply_blocks(outgoing_text: str, tool_errors: list[dict], retry_id: str) -> list[dict]:
    """Reply blocks for a turn that hit at least one grantable denial: the
    text as usual, plus one pair of grant/retry buttons per distinct
    denied entry, and - only when 2 or more distinct entries showed up in
    this *same* turn - one more pair of "grant all of these at once"
    buttons, since clicking the individual ones one at a time would only
    ever grant one and immediately re-run the whole prompt, possibly
    hitting the others all over again. Denials we can't map to a
    permissions.allow entry (see _grantable_entry) stay text-only."""
    blocks = _text_to_section_blocks(outgoing_text)
    entries: list[dict] = []
    seen: set[str] = set()
    for err in tool_errors:
        grantable = _grantable_entry(err)
        if not grantable or grantable["entry"] in seen:
            continue
        seen.add(grantable["entry"])
        entries.append(grantable)
        if len(entries) >= 9:  # stay well under Slack's block-count limit
            break

    for i, g in enumerate(entries):
        entry, describe = g["entry"], g["describe"]
        value = json.dumps({"retry_id": retry_id, "entries": [entry], "describe": describe})
        blocks.append({
            "type": "actions",
            "block_id": f"perm_{i}_{retry_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "許可して再実行"},
                    "style": "primary",
                    "action_id": "grant_once",
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "拒否"},
                    "action_id": "deny_grant",
                    "value": value,
                },
            ],
        })

    if len(entries) >= 2:
        all_entries = [g["entry"] for g in entries]
        all_describe = " / ".join(g["describe"] for g in entries)
        value = json.dumps({"retry_id": retry_id, "entries": all_entries, "describe": all_describe})
        blocks.append({
            "type": "actions",
            "block_id": f"perm_all_{retry_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"表示された{len(entries)}件を全部許可して再実行"},
                    "style": "primary",
                    "action_id": "grant_once",
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"表示された{len(entries)}件を全部拒否"},
                    "action_id": "deny_grant",
                    "value": value,
                },
            ],
        })

    return blocks


GREY_QUESTION_AUTO_RETRIES = 2
# The model (esp. the Flash tier) sometimes ends a turn with an empty response
# right after a tool result, mid-task. If the turn still ran tools, the task
# is progressing and the nudge is doing its job, so that doesn't spend the
# budget above - only a hard cap of its own.
GREY_QUESTION_PROGRESS_RETRIES = 5
# Permission buttons are a way for the agent to get a human to approve
# something, so an agent can (and one did: it read this file, then ran
# throwaway commands purely to make buttons appear) provoke denials on
# purpose. After this many button-bearing denials in a conversation within
# the window - without the user granting anything in between (see
# _reset_denial_throttle) - stop offering buttons and say so instead.
DENIAL_BUTTON_LIMIT = 3
DENIAL_BUTTON_WINDOW_SEC = 600
_denial_log: dict = {}


def _denial_buttons_throttled(conversation_id: str) -> bool:
    now = time.time()
    recent = [t for t in _denial_log.get(conversation_id, []) if now - t < DENIAL_BUTTON_WINDOW_SEC]
    throttled = len(recent) >= DENIAL_BUTTON_LIMIT
    if not throttled:
        recent.append(now)
    _denial_log[conversation_id] = recent
    return throttled


def _reset_denial_throttle(conversation_id: Optional[str]) -> None:
    """A human just clicked a grant in this conversation: they're watching
    and engaged, so it isn't an unattended agent provoking prompts. Restart
    the count (a legit trial-and-error session used to trip the limit while
    the user was approving every step)."""
    if conversation_id:
        _denial_log.pop(conversation_id, None)


AUTO_RETRY_TEXT = (
    "作業の続きをお願いします。直前のターンが途中で終わっているので、そのまま続行してください。"
    "ツールが「キャンセル」された場合は、権限が無いだけです。原因調査のために、"
    "ブリッジやagyの内部（ソース・設定・ログ）を調べないでください。"
    "必要な操作を実行しようとして、拒否されたらそれを報告してください。"
)


# --- usage (model quota) -------------------------------------------------
# `/usage` is answered locally by agy (no model call, no quota spent), so it
# is cheap - but still a ~4s process start, hence the short cache.
USAGE_CACHE_TTL_SEC = 60
USAGE_WARN_PCT = 10            # warn under a reply once a window drops to this
USAGE_WARN_CACHE_SEC = 300     # ...checked against data at most this old
USAGE_WARN_INTERVAL_SEC = 3600  # ...and at most once per hour per model group
JST = timezone(timedelta(hours=9))
_agy_usage_cache: dict = {"ts": 0.0, "rows": []}
_usage_warned: dict = {}
_USAGE_GROUP_LABEL = {"Gemini Models": "Gemini", "Claude and GPT models": "Claude / GPT"}


def _fetch_agy_usage(project_id: str, max_age: float = USAGE_CACHE_TTL_SEC) -> list[dict]:
    now = time.time()
    if _agy_usage_cache["rows"] and now - _agy_usage_cache["ts"] < max_age:
        return _agy_usage_cache["rows"]
    try:
        res = run_agy("/usage", project_id, None)
        rows = []
        for line in (res.get("response") or "").splitlines():
            cols = line.split("\t")
            if len(cols) < 4:
                continue
            m = re.match(r"(\d+)", cols[2])
            rows.append({"group": cols[0], "window": cols[1],
                          "pct": int(m.group(1)) if m else None, "reset": cols[3]})
        if rows:
            _agy_usage_cache["ts"] = now
            _agy_usage_cache["rows"] = rows
            return rows
    except Exception:
        log.warning("failed to fetch agy usage", exc_info=True)
    return _agy_usage_cache["rows"]


def _usage_group_for_model(model: Optional[str]) -> str:
    # agy's own default (no --model) is a Gemini model.
    return "Claude and GPT models" if (model or "").lower().startswith(("claude", "gpt")) else "Gemini Models"


def _usage_window_label(window: str) -> str:
    return "5時間枠" if "Five Hour" in window else "週次枠" if "Weekly" in window else window


def _format_reset(reset_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(reset_iso.replace("Z", "+00:00"))
    except ValueError:
        return reset_iso
    jst = dt.astimezone(JST).strftime("%m/%d %H:%M")
    secs = int((dt - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return f"リセット {jst}（経過済み・表示は更新待ちの可能性）"
    hours, minutes = divmod(secs // 60, 60)
    rel = f"{hours // 24}日{hours % 24}時間" if hours >= 48 else f"{hours}時間{minutes}分"
    return f"リセットまで {rel}（{jst} JST）"


def _format_usage(rows: list[dict], highlight_group: Optional[str] = None) -> str:
    if not rows:
        return "(利用状況を取得できませんでした)"
    groups: dict = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)
    out = []
    for group, items in groups.items():
        mark = "  ← このチャンネルのモデル" if group == highlight_group else ""
        out.append(f"*{_USAGE_GROUP_LABEL.get(group, group)}*{mark}")
        for r in items:
            pct = r["pct"]
            icon = (":white_circle:" if pct is None else ":large_green_circle:" if pct >= 30
                    else ":large_yellow_circle:" if pct >= USAGE_WARN_PCT else ":red_circle:")
            out.append(f"{icon} {_usage_window_label(r['window'])}: *{'?' if pct is None else pct}%*"
                       f"　{_format_reset(r['reset'])}")
        out.append("")
    return "\n".join(out).strip()


def _low_quota_warning(model: Optional[str], project_id: str) -> Optional[str]:
    group = _usage_group_for_model(model)
    low = [r for r in _fetch_agy_usage(project_id, max_age=USAGE_WARN_CACHE_SEC)
           if r["group"] == group and r["pct"] is not None and r["pct"] <= USAGE_WARN_PCT]
    now = time.time()
    if not low or now - _usage_warned.get(group, 0.0) < USAGE_WARN_INTERVAL_SEC:
        return None
    _usage_warned[group] = now
    detail = " / ".join(f"{_usage_window_label(r['window'])} {r['pct']}%" for r in low)
    return (f":warning: {_USAGE_GROUP_LABEL.get(group, group)} の残り枠が少なくなっています（{detail}）。"
            "`/agy-usage` で詳細、`/agy-model` で別系統のモデルに切り替えられます。")


_QUOTA_RESET_RE = re.compile(r"Resets in (\w+)")


def _agy_failure_text(err: str) -> str:
    """Slack text for a failed agy invocation. A model-quota 429 is common
    enough (and fixable from Slack) to deserve its own explanation instead
    of the generic "check the logs"."""
    if "RESOURCE_EXHAUSTED" in err:
        m = _QUOTA_RESET_RE.search(err)
        reset = f"（回復まであと {m.group(1)}）" if m else ""
        return (f":x: モデルの利用枠を使い切りました{reset}。\n"
                "別系統の枠のモデルに切り替えれば続けられます（例: `/agy-model set claude-sonnet-4-6`）。"
                "枠の残りは `/agy usage` で確認できます。")
    return ":x: agy invocation failed. Check the bridge's logs."


def _run_and_reply(client, store: "ThreadStore", permissions: "PermissionsFile",
                    channel_id: str, thread_key: str, project_id: str,
                    conversation_id: Optional[str], text: str, persona_kwargs: dict,
                    status_text: str = "考え中です...",
                    remember_text: Optional[str] = None,
                    auto_retries_left: int = GREY_QUESTION_AUTO_RETRIES,
                    progress_retries_left: int = GREY_QUESTION_PROGRESS_RETRIES,
                    model: Optional[str] = None, effort: Optional[str] = None) -> None:
    """Runs one agy turn and posts the reply, attaching permission-grant
    buttons if a run_command call got denied. Shared by the normal message
    handler and by the grant_once/grant_permanent retry flow.

    `remember_text` is what gets saved as the prompt to replay on a *future*
    grant-and-retry, if this turn hits another denial - defaults to `text`
    itself, but the retry flow passes the original, unwrapped prompt here so
    a chain of several denials in the same message doesn't nest another
    "please run <cmd> now" instruction inside the last one every time."""
    try:
        result = _run_with_status(
            client, channel_id, thread_key, status_text, persona_kwargs,
            run_agy, text, project_id, conversation_id,
            model=model, effort=effort, permissions=permissions,
        )
    except Exception as exc:
        log.exception("agy invocation failed")
        client.chat_postMessage(
            channel=channel_id, thread_ts=thread_key,
            text=_agy_failure_text(str(exc)),
            **persona_kwargs,
        )
        return

    log.info("agy result=%r", {k: v for k, v in result.items() if k not in ("_tool_events", "_stderr")})
    if result.get("denied_actions") and not result.get("_tool_errors"):
        # agy says something was denied, yet no tool step came back as an
        # error we can read (the "Tool execution was canceled" case): dump
        # the raw tool events and stderr, to find out whether the denied
        # command is recoverable from them (so a grant button could be
        # offered) or agy simply doesn't say.
        log.info("DIAG denied_actions without tool_errors: tool_events=%s stderr=%r",
                  json.dumps(result.get("_tool_events", [])[-12:], ensure_ascii=False)[:6000],
                  result.get("_stderr"))
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
    # Pull out ```mermaid blocks and render them to PNGs *before* markdown
    # conversion - the placeholder text _extract_and_render_mermaid leaves
    # behind is plain prose, not something markdown_to_mrkdwn needs to
    # touch, and the images themselves are uploaded below, after the text
    # post, same as any other generated-file upload.
    raw_response, mermaid_images = _extract_and_render_mermaid(
        result.get("response"), AGY_BRAIN_DIR / conversation_id / "scratch" / "mermaid"
    )
    # Built up as separate parts and joined at the end, so "(empty
    # response)" only ever shows up when there's truly nothing else to
    # say - not as noise sitting above a warning/error section that
    # already explains why the response was empty.
    parts: list[str] = []
    response_text = markdown_to_mrkdwn(raw_response)
    if response_text:
        parts.append(response_text)

    tool_errors = result.get("_tool_errors") or []
    if not tool_errors:
        # A turn with no fresh tool denials is our signal that whatever
        # this conversation needed permissions for is done (for now) -
        # release any temp grants it's been holding since an earlier
        # grant_once click (see _grant_and_retry), instead of tearing one
        # down the moment a *different* one gets granted. Safe across
        # conversations: clear_temp_grants only returns entries no other
        # conversation still needs.
        for entry in store.clear_temp_grants(conversation_id):
            log.info("auto-releasing temp grant (clean turn, conversation=%s): %r",
                      conversation_id, entry)
            permissions.remove_entry(entry)

    # Not every tool-step error is a permission denial - e.g.
    # replace_file_content can fail because its target text no longer
    # matches the file (a real edit bug, nothing a grant would fix).
    # Reporting that as "denied due to insufficient permission" with a
    # grant button would be actively misleading, so split them apart.
    # Each list is also de-duplicated by its rendered text - agy sometimes
    # reports the exact same failure more than once in one turn (e.g. two
    # identical replace_file_content attempts), which otherwise shows up
    # as the same line repeated verbatim.
    def _dedupe(errors: list[dict]) -> list[dict]:
        seen_lines: set[str] = set()
        out = []
        for e in errors:
            line = _describe_tool_error(e)
            if line in seen_lines:
                continue
            seen_lines.add(line)
            out.append(e)
        return out

    permission_candidates = _dedupe([e for e in tool_errors if _is_permission_error(e)])
    blocked_errors = _dedupe([e for e in tool_errors if _is_deny_rule_error(e)])
    other_errors = _dedupe([e for e in tool_errors
                             if not _is_permission_error(e) and not _is_deny_rule_error(e)])

    # Split off denials that only *look* like an ordinary permission gap -
    # a button offering to "grant and retry" would be actively misleading
    # for these, since granting (again) can't fix either cause (see
    # _stuck_denial_reason).
    stuck_errors: list[tuple[dict, str]] = []
    permission_errors: list[dict] = []
    for e in permission_candidates:
        reason = _stuck_denial_reason(e, permissions)
        if reason:
            stuck_errors.append((e, reason))
        else:
            permission_errors.append(e)

    # other_errors (no buttons) is deliberately placed *before*
    # permission_errors (which gets buttons) in the text, not after -
    # confirmed by testing: the buttons always render at the very end of
    # the message, below every text section, regardless of which section
    # they logically belong to. Putting the button-less section last would
    # leave the buttons visually stuck right below unrelated text, making
    # it look like they applied to that instead of the (possibly much
    # earlier) permission-denied section.
    if other_errors:
        log.warning("agy non-permission tool errors: %r", other_errors)
        lines = "\n".join(f"- {_describe_tool_error(e)}" for e in other_errors)
        parts.append(
            ":x: 一部のツール実行が失敗しました（権限の問題ではありません。"
            "内容を確認してagyに指示し直してください）:\n"
            f"{lines}"
        )
    if blocked_errors:
        log.warning("agy hit deny rules: %r", blocked_errors)
        lines = "\n".join(f"- {_describe_tool_error(e)}" for e in blocked_errors)
        parts.append(
            ":no_entry_sign: 次の操作は拒否ルール（`permissions.deny`）で禁止されているため実行できません"
            "（許可はできません）:\n"
            f"{lines}"
        )
        store.log_permission(channel_id, conversation_id, "blocked",
                              [_describe_tool_error(e) for e in blocked_errors])
    if stuck_errors:
        log.warning("agy denials that aren't really a permission gap: %r", stuck_errors)
        lines = "\n".join(f"- {_describe_tool_error(e)}\n  → {reason}" for e, reason in stuck_errors)
        parts.append(
            ":grey_question: 権限不足のように見えますが、許可しても直らないと判断したため、"
            "ボタンは出していません:\n"
            f"{lines}\n"
            "内容を確認し、別の指示を出してください。"
        )
        store.log_permission(channel_id, conversation_id, "stuck",
                              [_describe_tool_error(e) for e, _ in stuck_errors])
    buttons_throttled = bool(permission_errors) and _denial_buttons_throttled(conversation_id)
    if permission_errors:
        log.warning("agy permission errors: %r (buttons throttled=%s)", permission_errors, buttons_throttled)
        lines = "\n".join(f"- {_describe_tool_error(e)}" for e in permission_errors)
        if buttons_throttled:
            parts.append(
                ":no_entry: 短時間に権限拒否が続いたため、許可ボタンを一時停止しました"
                "（エージェントが権限確認を繰り返している可能性があります）。拒否された操作:\n"
                f"{lines}\n"
                "本当に必要な操作だけ `/agy-permissions add` で明示的に許可してください。"
            )
        else:
            parts.append(
                ":warning: 一部のツール実行が権限不足で拒否されました:\n"
                f"{lines}\n"
                "下のボタンでこの会話だけ許可して再実行するか、今後もずっと許可したい場合は "
                "`/agy-permissions add <エントリ>` で恒久的に登録してください。"
            )
    if not tool_errors and result.get("denied_actions") and not result.get("response"):
        # Confirmed by testing (see GREY_QUESTION_AUTO_RETRIES and the log
        # analysis behind its value): this situation usually clears up on
        # its own if you just ask again, since it's often an async
        # permission check that hadn't resolved yet rather than a real,
        # persistent denial - so try that automatically first, silently,
        # a bounded number of times, before bothering a human. Bounded
        # deliberately (never looped until it stops happening) to cap the
        # cost/time of a run that's stuck for a real reason. This can
        # never fire back-to-back with a genuine denial in the *same*
        # turn - tool_errors must be empty to get here at all, so the
        # moment a real, actionable denial shows up, this whole branch is
        # skipped in favor of the permission_errors/other_errors handling
        # above, buttons and all, with no further auto-retry.
        made_progress = (result.get("_tool_steps") or 0) > 0
        if (progress_retries_left > 0) if made_progress else (auto_retries_left > 0):
            log.info("empty response with stale denied_actions (num_turns=%s, tool_steps=%s) - "
                      "auto-retrying (no-progress left=%d, progress left=%d)",
                      result.get("num_turns"), result.get("_tool_steps"),
                      auto_retries_left, progress_retries_left)
            _run_and_reply(
                client, store, permissions, channel_id, thread_key, project_id,
                conversation_id, AUTO_RETRY_TEXT, persona_kwargs,
                status_text="反応が無かったので自動的に再試行しています...",
                remember_text=remember_text if remember_text is not None else text,
                auto_retries_left=auto_retries_left if made_progress else auto_retries_left - 1,
                progress_retries_left=progress_retries_left - 1 if made_progress else progress_retries_left,
                model=model, effort=effort,
            )
            return

        # `denied_actions` (like duration_seconds/usage in the same result)
        # is cumulative for the whole conversation, not scoped to this turn,
        # and never clears once set - confirmed by testing: it stays set on
        # every later turn even when that turn has real, substantive
        # content (agy did make genuine progress and reported back). So
        # this note is only useful/accurate on a turn that *also* came back
        # with an empty response - pairing it with real response text would
        # misleadingly suggest nothing happened when something clearly did.
        denied_desc = ", ".join(
            d.get("display_name") or d.get("action") or "?" for d in result["denied_actions"]
        )
        if made_progress:
            # Empty response, but tools did run this turn: the model just
            # keeps ending its turns early mid-task, and the automatic
            # nudges ran out. The old "past denial" wording would be
            # misleading here - the denial flag is stale, not the cause.
            parts.append(
                ":hourglass: モデルが作業の途中で空の応答を返してターンを終えることを繰り返しました"
                "（自動で続行を促しましたが、まだ作業の途中のようです）。"
                "「続けて」と送ると再開します。"
            )
        elif result.get("num_turns") == 1:
            # Confirmed by testing: this can happen on a conversation's
            # very first turn, when a tool call ran async (WaitMsBeforeAsync)
            # and its permission check only resolved *after* this print-mode
            # call had already captured its stream and returned - so there's
            # no earlier turn for anything to be "past" from; saying so
            # would be flatly wrong, not just imprecise.
            parts.append(
                f":grey_question: 今回の処理中に非同期実行されていた操作({denied_desc})が"
                "権限不足で拒否されたようですが、結果が確定する前にこのターンが終了したため詳細は表示できません。"
                "もう一度はっきり「実行して」と頼むか、`/agy-permissions` で先に許可しておいてください。"
            )
        else:
            parts.append(
                f":grey_question: この会話には過去に拒否された操作({denied_desc})が残っていますが、"
                "今回のターンでは新たなツール実行は発生しませんでした。"
                "もう一度はっきり「実行して」と頼むか、`/agy-permissions` で先に許可しておいてください。"
            )

    outgoing_text = "\n\n".join(parts) if parts else "(empty response)"

    if permission_errors:
        asked = []
        for e in permission_errors:
            g = _grantable_entry(e)
            if g and g["entry"] not in asked:
                asked.append(g["entry"])
        store.log_permission(channel_id, conversation_id,
                              "throttled" if buttons_throttled else "requested", asked)

    blocks = None
    if permission_errors and not buttons_throttled:
        retry_id = uuid.uuid4().hex[:12]
        store.put_retry(retry_id, channel_id, thread_key, project_id, conversation_id,
                         remember_text if remember_text is not None else text)
        blocks = _build_reply_blocks(outgoing_text, permission_errors, retry_id)

    log.info("posting to slack text=%r", outgoing_text)
    try:
        client.chat_postMessage(
            channel=channel_id, thread_ts=thread_key,
            text=outgoing_text, blocks=blocks, **persona_kwargs,
        )
    except Exception:
        # Confirmed by testing: a malformed block (e.g. a confirm dialog
        # that used to embed an arbitrarily long command and blew past
        # Slack's 300-char limit) makes this call fail outright and the
        # *entire* reply silently never reaches Slack - no buttons, no
        # text, nothing. A plain-text-only retry is worth far more than
        # losing real progress to a formatting bug - kept as a backstop
        # even now that the confirm dialogs are fixed-length by design.
        log.exception("failed to post reply with blocks, retrying as plain text")
        try:
            client.chat_postMessage(
                channel=channel_id, thread_ts=thread_key,
                text=outgoing_text, **persona_kwargs,
            )
        except Exception:
            log.exception("plain-text fallback post also failed")

    # agy often reports a generated artifact (a chart, a report PDF, ...)
    # as a file:// link, which nothing in Slack can actually open - upload
    # the file itself so it's viewable right in the thread instead.
    for path in _extract_uploadable_files(result.get("response"), project_id, conversation_id):
        try:
            client.files_upload_v2(
                channel=channel_id, thread_ts=thread_key,
                file=str(path), filename=path.name, title=path.name,
            )
            log.info("uploaded generated file to slack: %s", path)
        except Exception:
            log.exception("failed to upload generated file %s", path)

    for path in mermaid_images:
        try:
            client.files_upload_v2(
                channel=channel_id, thread_ts=thread_key,
                file=str(path), filename=path.name, title="diagram",
            )
            log.info("uploaded rendered mermaid diagram to slack: %s", path)
        except Exception:
            log.exception("failed to upload mermaid diagram %s", path)

    try:
        warning = _low_quota_warning(model, project_id)
        if warning:
            client.chat_postMessage(channel=channel_id, thread_ts=thread_key,
                                     text=warning, **persona_kwargs)
    except Exception:
        log.exception("low-quota check failed")


def build_app() -> App:
    channel_map = load_channel_map()
    store = ThreadStore(STATE_PATH)
    permissions = PermissionsFile(AGY_SETTINGS_PATH)
    locks = KeyedLocks()

    def _sweep_temp_grants() -> None:
        while True:
            time.sleep(AGY_TEMP_GRANT_SWEEP_INTERVAL_SEC)
            try:
                for entry in store.sweep_stale_temp_grants(AGY_TEMP_GRANT_MAX_AGE_SEC):
                    permissions.remove_entry(entry)
                    log.info("swept stale temp grant %r (older than %ss without a clean turn)",
                              entry, AGY_TEMP_GRANT_MAX_AGE_SEC)
            except Exception:
                log.exception("temp-grant sweep failed")

    threading.Thread(target=_sweep_temp_grants, daemon=True).start()

    app = App(token=os.environ["SLACK_BOT_TOKEN"])
    self_user_id = app.client.auth_test()["user_id"]
    mention_re = re.compile(rf"<@{re.escape(self_user_id)}>\s*")
    log.info("bridge bot user id=%s (mention required to start a new conversation)", self_user_id)

    def _check_all_config_drift() -> None:
        for channel_id, channel_cfg in channel_map.items():
            try:
                _check_config_drift(store, app.client, channel_id, channel_cfg)
            except Exception:
                log.exception("config-drift check failed for channel=%s", channel_id)

    def _config_drift_loop() -> None:
        _check_all_config_drift()  # once at startup, not just after the first interval
        while True:
            time.sleep(AGY_CONFIG_DRIFT_CHECK_INTERVAL_SEC)
            _check_all_config_drift()

    threading.Thread(target=_config_drift_loop, daemon=True).start()

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

        conversation_id = store.get(channel_id, thread_key) if is_reply else None

        # Confirmed by testing: replying to *any* message opens a Slack
        # thread regardless of whether its root (or anything else in it)
        # was ever mentioned, ignored or not - so gating only on `is_reply`
        # let a reply to a completely un-mentioned message quietly start a
        # conversation, defeating the whole "mention to activate" premise.
        # A reply only skips the mention gate once its thread is already
        # *activated* (has a stored conversation) - which only happens once
        # some message in it passed this same gate - so replying in a
        # brand-new/never-activated thread needs its own mention, exactly
        # like a new top-level message; every later reply in that thread is
        # then mention-free as before.
        is_activating = not is_reply or conversation_id is None
        if is_activating:
            if not mention_re.search(text):
                return
            text = mention_re.sub("", text).strip()
            if not text:
                # Confirmed by testing: a mention with nothing else in the
                # message leaves an empty prompt, which agy itself rejects
                # outright ("error: Error: empty prompt") - run_agy() then
                # raises, and the generic exception handler further down
                # can only report a vague "invocation failed". Catch it
                # here instead, with a message that actually explains it.
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_key,
                    text="メンションだけじゃなくて、本文も書いてや。",
                    **_persona_kwargs(channel_cfg),
                )
                return

        resume_match = _RESUME_RE.match(text) if is_activating else None
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

        model, effort = _effective_model_effort(store, channel_id, channel_cfg)
        with locks.get((channel_id, thread_key)):
            _run_and_reply(
                client, store, permissions, channel_id, thread_key, project_id,
                conversation_id, text, _persona_kwargs(channel_cfg),
                model=model, effort=effort,
            )

    def _grant_and_retry(body: dict, client, permanent: bool) -> None:
        action = body["actions"][0]
        try:
            payload = json.loads(action["value"])
        except (KeyError, json.JSONDecodeError):
            log.error("bad button value: %r", action.get("value"))
            return
        retry_id = payload.get("retry_id")
        entries = payload.get("entries") or []
        describe = payload.get("describe") or ", ".join(entries)
        ctx = store.get_retry(retry_id) if retry_id else None

        message = body.get("message") or {}
        origin_channel_id = body["channel"]["id"]
        message_ts = message.get("ts")

        if not ctx or not entries:
            if message_ts:
                client.chat_update(
                    channel=origin_channel_id, ts=message_ts,
                    text=":warning: このボタンは期限切れです。もう一度メッセージを送ってください。",
                    blocks=[],
                )
            return

        clicker = (body.get("user") or {}).get("username") or (body.get("user") or {}).get("id") or "?"
        log.info("%s permission grant for entries=%r by user=%s",
                  "permanent" if permanent else "one-time", entries, clicker)

        channel_cfg = channel_map.get(ctx["channel_id"], {})
        persona = _persona_kwargs(channel_cfg)
        label = "恒久的に許可" if permanent else "今回だけ許可"
        conversation_id = ctx["conversation_id"]
        store.log_permission(ctx["channel_id"], conversation_id,
                              "granted_permanent" if permanent else "granted_once", entries, clicker)
        _reset_denial_throttle(conversation_id)

        for entry in entries:
            newly_added = permissions.add_entry(entry)
            if newly_added and not permanent:
                # Deferred release (see _run_and_reply's clean-turn check
                # and the sweep thread below) instead of tearing this down
                # the moment *this one* retry finishes - so granting a
                # different entry later in the same conversation doesn't
                # undo it.
                store.add_temp_grant(conversation_id, entry)

        if message_ts:
            client.chat_update(
                channel=origin_channel_id, ts=message_ts,
                text=f":gear: {label}して再実行しています... (`{describe}`)",
                blocks=[],
            )

        # Confirmed by testing: agy doesn't always actually retry a
        # previously-denied action just because you resend the same
        # prompt - it can non-deterministically choose to give up instead
        # (an empty response, no fresh tool call at all), especially since
        # the tool's own denial message tells the model not to try to work
        # around it. Prefix an explicit instruction naming what was just
        # allowed, so the retry actually exercises the grant.
        retry_prompt = (
            f"(先ほど拒否された次の操作が許可されました。実行してください: `{describe}`)\n\n"
            f"{ctx['prompt_text']}"
        )

        model, effort = _effective_model_effort(store, ctx["channel_id"], channel_cfg)
        try:
            _run_and_reply(
                client, store, permissions, ctx["channel_id"], ctx["thread_ts"],
                ctx["project_id"], conversation_id, retry_prompt, persona,
                status_text="許可して再実行しています...",
                remember_text=ctx["prompt_text"],
                model=model, effort=effort,
            )
        finally:
            store.delete_retry(retry_id)

    def _deny_and_retry(body: dict, client) -> None:
        action = body["actions"][0]
        try:
            payload = json.loads(action["value"])
        except (KeyError, json.JSONDecodeError):
            log.error("bad button value: %r", action.get("value"))
            return
        retry_id = payload.get("retry_id")
        describe = payload.get("describe") or "?"
        ctx = store.get_retry(retry_id) if retry_id else None

        message = body.get("message") or {}
        origin_channel_id = body["channel"]["id"]
        message_ts = message.get("ts")

        if not ctx:
            if message_ts:
                client.chat_update(
                    channel=origin_channel_id, ts=message_ts,
                    text=":warning: このボタンは期限切れです。もう一度メッセージを送ってください。",
                    blocks=[],
                )
            return

        clicker = (body.get("user") or {}).get("username") or (body.get("user") or {}).get("id") or "?"
        log.info("denied grant for %r by user=%s", describe, clicker)
        store.log_permission(ctx["channel_id"], ctx.get("conversation_id"), "denied",
                              payload.get("entries") or [describe], clicker)

        # Deliberately no agy turn here: the permission_errors text this
        # button was attached to already told agy everything it knows about
        # the denial. Sending it a "you were denied" prompt just invited it
        # to guess at a workaround on its own initiative (exactly what
        # agent-safety-rules.md #2 tells it not to do) instead of waiting
        # for one from the user - so the thread simply sits here until the
        # user replies with what to do next.
        if message_ts:
            client.chat_update(
                channel=origin_channel_id, ts=message_ts,
                text=f":no_entry_sign: 拒否しました (`{describe}`)。"
                     "このスレッドに返信して、次の指示を送ってください。",
                blocks=[],
            )
        store.delete_retry(retry_id)

    @app.action("grant_once")
    def handle_grant_once(ack, body, client):  # noqa: ANN001 - Bolt signature
        ack()
        _grant_and_retry(body, client, permanent=False)

    @app.action("grant_permanent")
    def handle_grant_permanent(ack, body, client):  # noqa: ANN001 - Bolt signature
        ack()
        _grant_and_retry(body, client, permanent=True)

    @app.action("deny_grant")
    def handle_deny_grant(ack, body, client):  # noqa: ANN001 - Bolt signature
        ack()
        _deny_and_retry(body, client)

    @app.action("config_seal")
    def handle_config_seal(ack, body, client):  # noqa: ANN001 - Bolt signature
        """Confirms the file's *current* on-disk content as the trusted
        baseline - the same action whether this is the first-ever seal or
        accepting a deliberate edit after a drift notice."""
        ack()
        action = body["actions"][0]
        try:
            payload = json.loads(action["value"])
        except (KeyError, json.JSONDecodeError):
            log.error("bad config_seal button value: %r", action.get("value"))
            return
        abs_path, rel_path = payload["path"], payload["rel_path"]
        clicker = (body.get("user") or {}).get("username") or (body.get("user") or {}).get("id") or "?"
        message, channel_id, ts = body.get("message") or {}, body["channel"]["id"], (body.get("message") or {}).get("ts")
        try:
            content = Path(abs_path).read_bytes()
        except OSError as exc:
            client.chat_update(channel=channel_id, ts=ts, text=f":x: 読み取りに失敗しました: {exc}", blocks=[])
            return
        content_hash = hashlib.sha256(content).hexdigest()
        backup_path = _config_backup_path(abs_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path.write_bytes(content)
        store.seal_config(abs_path, content_hash, str(backup_path), clicker)
        log.info("config sealed: %s by %s", abs_path, clicker)
        client.chat_update(
            channel=channel_id, ts=ts,
            text=f":white_check_mark: `{rel_path}` の現在の内容を正として確定しました（{clicker}）。",
            blocks=[],
        )

    @app.action("config_restore")
    def handle_config_restore(ack, body, client):  # noqa: ANN001 - Bolt signature
        """Overwrites the live file with the last sealed backup - for when
        the drift was unwanted rather than a deliberate edit to accept."""
        ack()
        action = body["actions"][0]
        try:
            payload = json.loads(action["value"])
        except (KeyError, json.JSONDecodeError):
            log.error("bad config_restore button value: %r", action.get("value"))
            return
        abs_path, rel_path = payload["path"], payload["rel_path"]
        clicker = (body.get("user") or {}).get("username") or (body.get("user") or {}).get("id") or "?"
        channel_id, ts = body["channel"]["id"], (body.get("message") or {}).get("ts")
        baseline = store.get_config_baseline(abs_path)
        if not baseline:
            client.chat_update(channel=channel_id, ts=ts,
                                text=":warning: 確定済みの内容がありません（先に「正として確定」してください）。",
                                blocks=[])
            return
        try:
            backup_content = Path(baseline["backup_path"]).read_bytes()
            Path(abs_path).write_bytes(backup_content)
        except OSError as exc:
            client.chat_update(channel=channel_id, ts=ts, text=f":x: 復元に失敗しました: {exc}", blocks=[])
            return
        store.mark_config_notified(abs_path, baseline["content_hash"])
        log.info("config restored from backup: %s by %s", abs_path, clicker)
        client.chat_update(
            channel=channel_id, ts=ts,
            text=f":leftwards_arrow_with_hook: `{rel_path}` を確定済みの内容（{baseline['sealed_at']} / "
                 f"{baseline['sealed_by'] or '?'}）に復元しました（{clicker}）。",
            blocks=[],
        )

    @app.command("/agy-permissions")
    def handle_permissions_command(ack, respond, command):  # noqa: ANN001 - Bolt signature
        ack()
        channel_cfg = channel_map.get(command.get("channel_id"))
        if not channel_cfg:
            respond(text="このチャンネルは agy-slack-bridge の対象外です。", response_type="ephemeral")
            return
        project_id = channel_cfg["project"]

        text = (command.get("text") or "").strip()
        parts = text.split(maxsplit=1)
        sub = parts[0].lower() if parts else "list"
        arg = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("", "list"):
            entries = permissions.list_entries()
            if not entries:
                respond(text="permissions.allow は現在空です。", response_type="ephemeral")
                return
            # Plain text, no per-entry buttons - simpler and doesn't depend
            # on Slack's per-block length/count limits (a button+confirm
            # per entry hit both of those already; see git history).
            # Delete via `/agy-permissions remove <entry>` instead.
            listing = "\n".join(f"{i}. `{e}`" for i, e in enumerate(entries, 1))
            respond(
                text=f"現在の permissions.allow ({len(entries)}件):\n{listing}\n\n"
                     "削除するには `/agy-permissions remove <エントリ>` を使ってください。",
                response_type="ephemeral",
            )
        elif sub == "stats":
            days = int(arg) if arg.isdigit() else 30
            rows = store.permission_stats(days)
            if not rows:
                respond(text=f"直近{days}日の許可リクエストはありません。", response_type="ephemeral")
                return
            listing = "\n".join(
                f"{i}. 要求{req} / 今回{once} / 恒久{perm} / 拒否{den}  `{entry}`"
                for i, (entry, req, once, perm, den) in enumerate(rows, 1)
            )
            respond(
                text=f"直近{days}日に許可を求められた回数の多い順:\n{listing}\n\n"
                     "頻繁に「今回だけ許可」しているものは、`/agy-permissions add <エントリ>` で恒久許可を検討できます。",
                response_type="ephemeral",
            )
        elif sub in ("add", "remove"):
            if not arg:
                respond(text=f"使い方: `/agy-permissions {sub} <コマンド または kind(引数)>`"
                             + ("（`remove 1 3 5` のように `list` の番号を複数指定も可）" if sub == "remove" else ""),
                        response_type="ephemeral")
                return

            if sub == "remove":
                # Accept one or more list numbers (as shown by `list`),
                # space/comma-separated, so removing several entries
                # doesn't mean retyping long command strings by hand on
                # mobile. Resolved against a single fresh snapshot, then
                # removed by exact entry string - safe regardless of
                # order, even if the list changes mid-way.
                tokens = [t for t in re.split(r"[,\s]+", arg.strip()) if t]
                if tokens and all(t.isdigit() for t in tokens):
                    snapshot = permissions.list_entries()
                    removed, invalid = [], []
                    for i in sorted({int(t) for t in tokens}):
                        if 1 <= i <= len(snapshot):
                            entry = snapshot[i - 1]
                            if permissions.remove_entry(entry):
                                removed.append(entry)
                                _mark_permanent_command(project_id, entry, present=False)
                        else:
                            invalid.append(i)
                    text = (":wastebasket: 削除しました:\n" + "\n".join(f"- `{e}`" for e in removed)
                            if removed else "該当エントリはありませんでした。")
                    if invalid:
                        text += f"\n無効な番号: {', '.join(map(str, invalid))}（`list` を確認してください）"
                    respond(text=text, response_type="ephemeral")
                    return

            # A bare shell command (no "kind(...)" wrapper typed) gets
            # wrapped as command(<that>), same as the old command-only
            # /agy-permissions; typing a full entry like read_url(example.com)
            # is used as-is.
            entry = arg if _FULL_ENTRY_RE.match(arg) else f"command({arg})"
            if sub == "add":
                ok = permissions.add_entry(entry)
                _mark_permanent_command(project_id, entry, present=True)
                respond(
                    text=(f":white_check_mark: `{entry}` を追加しました。" if ok
                          else f"`{entry}` はすでに登録されています。"),
                    response_type="ephemeral",
                )
            else:
                ok = permissions.remove_entry(entry)
                _mark_permanent_command(project_id, entry, present=False)
                respond(
                    text=(f":wastebasket: `{entry}` を削除しました。" if ok
                          else f"`{entry}` は登録されていません。"),
                    response_type="ephemeral",
                )
        else:
            respond(
                text="使い方: `/agy-permissions [list|stats [日数]|add <コマンド>|remove <コマンド>]`",
                response_type="ephemeral",
            )

    @app.command("/agy-model")
    def handle_model_command(ack, respond, command):  # noqa: ANN001 - Bolt signature
        ack()
        channel_id = command.get("channel_id")
        channel_cfg = channel_map.get(channel_id)
        if not channel_cfg:
            respond(text="このチャンネルは agy-slack-bridge の対象外です。", response_type="ephemeral")
            return

        tokens = (command.get("text") or "").split()
        sub = tokens[0].lower() if tokens else "list"
        rest = tokens[1:]

        if sub in ("", "list"):
            override = store.get_channel_settings(channel_id)
            if override:
                current = override.get("model") or "(agyのデフォルト)"
                if override.get("effort"):
                    current += f" (effort={override['effort']})"
                note = "このチャンネル専用に切り替え中。config.yaml の既定に戻すには `/agy-model clear`"
            else:
                current = channel_cfg.get("model") or "(agyのデフォルト)"
                if channel_cfg.get("effort"):
                    current += f" (effort={channel_cfg['effort']})"
                note = "config.yaml の既定のまま"
            models = _fetch_agy_models()
            listing = "\n".join(f"- `{mid}`  {label}" for mid, label in models) if models else "(一覧の取得に失敗しました)"
            respond(
                text=f"現在のモデル: `{current}`\n({note})\n\n選べるモデル:\n{listing}\n\n"
                     "切り替え: `/agy-model set <モデルID> [low|medium|high|max]`\n"
                     "config.yaml の既定に戻す: `/agy-model clear`",
                response_type="ephemeral",
            )
        elif sub == "set":
            if not rest:
                respond(text="使い方: `/agy-model set <モデルID> [low|medium|high|max]`", response_type="ephemeral")
                return
            model_id = rest[0]
            effort = rest[1].lower() if len(rest) > 1 else None
            models = _fetch_agy_models()
            valid_ids = {mid for mid, _ in models}
            if models and model_id not in valid_ids:
                respond(
                    text=f":x: `{model_id}` は `agy models` に無いモデルIDです。`/agy-model list` で確認してください。",
                    response_type="ephemeral",
                )
                return
            if effort and effort not in ("low", "medium", "high", "max"):
                respond(text=":x: effort は low/medium/high/max のいずれかで指定してください。",
                         response_type="ephemeral")
                return
            store.set_channel_settings(channel_id, model_id, effort)
            respond(
                text=f":white_check_mark: このチャンネルのモデルを `{model_id}`"
                     + (f" (effort={effort})" if effort else "")
                     + " に切り替えました。次のメッセージから反映されます。",
                response_type="ephemeral",
            )
        elif sub == "clear":
            had = store.clear_channel_settings(channel_id)
            respond(
                text=(":leftwards_arrow_with_hook: config.yaml の既定モデルに戻しました。" if had
                      else "このチャンネルにモデルの切り替えはありませんでした（すでに既定のままです）。"),
                response_type="ephemeral",
            )
        else:
            respond(
                text="使い方: `/agy-model [list|set <モデルID> [effort]|clear]`",
                response_type="ephemeral",
            )

    @app.command("/agy")
    def handle_agy_command(ack, respond, command, client):  # noqa: ANN001 - Bolt signature
        """Transparent passthrough to agy's own slash commands:
        `/agy usage`, `/agy plan <task>`, `/agy boost <task>` ...

        agy's built-ins (usage/credits/skills/...) are answered ephemerally
        right here; anything else (skill commands like /plan) is sent to the
        agent as a normal turn in a fresh thread, exactly as if the same
        text had been typed after an @mention - so replies, permission
        buttons and the per-channel model all behave the same."""
        ack()
        channel_id = command.get("channel_id")
        channel_cfg = channel_map.get(channel_id)
        if not channel_cfg:
            respond(text="このチャンネルは agy-slack-bridge の対象外です。", response_type="ephemeral")
            return

        project_id = channel_cfg["project"]
        raw = (command.get("text") or "").strip().lstrip("/").strip()
        model, effort = _effective_model_effort(store, channel_id, channel_cfg)

        def _ephemeral_agy(cmd_text: str) -> str:
            try:
                res = run_agy(cmd_text, project_id, None, model=model, effort=effort)
            except Exception:
                log.exception("agy slash command failed: %r", cmd_text)
                return ":x: agy の実行に失敗しました。bridge のログを確認してください。"
            if res.get("status") != "SUCCESS":
                return f":x: {res.get('error') or res.get('status')}"
            out = (res.get("response") or "").strip() or "(出力なし)"
            if len(out) > 3500:
                out = out[:3500] + "\n... (省略)"
            return f"```\n{out}\n```"

        if not raw:
            respond(
                text="*agy のスラッシュコマンド*\n"
                     f"{_ephemeral_agy('/help')}\n"
                     f"スキル:\n{_ephemeral_agy('/skills')}\n"
                     "使い方: `/agy <コマンド> [引数]`（例: `/agy usage`, `/agy plan 〇〇を整理して`）\n"
                     "上の組み込みコマンドはここに表示、それ以外は通常の会話として新しいスレッドで実行します。"
                     "モデル切り替えは `/agy-model`。",
                response_type="ephemeral",
            )
            return

        name, _, args = raw.partition(" ")
        if name.lower() in _fetch_agy_builtin_commands(project_id):
            if name.lower() in ("model", "effort") and args.strip():
                respond(text="モデル/effort の切り替えは `/agy-model set <モデルID> [effort]` を使ってください。",
                         response_type="ephemeral")
                return
            respond(text=f"`/{raw}`\n{_ephemeral_agy('/' + raw)}", response_type="ephemeral")
            return

        shown = raw if len(raw) <= 200 else raw[:200] + "..."
        root = client.chat_postMessage(
            channel=channel_id,
            text=f"<@{command.get('user_id')}> が `/{shown}` を実行します",
            **_persona_kwargs(channel_cfg),
        )
        thread_key = root["ts"]
        log.info("slash passthrough text=%r channel=%s thread_key=%s", "/" + raw, channel_id, thread_key)
        with locks.get((channel_id, thread_key)):
            _run_and_reply(
                client, store, permissions, channel_id, thread_key, project_id,
                None, "/" + raw, _persona_kwargs(channel_cfg),
                model=model, effort=effort,
            )

    @app.command("/agy-link")
    def handle_link_command(ack, respond, command):  # noqa: ANN001 - Bolt signature
        ack()
        channel_id = command.get("channel_id")
        if channel_id not in channel_map:
            respond(text="このチャンネルは agy-slack-bridge の対象外です。", response_type="ephemeral")
            return
        # Slack doesn't allow slash commands inside a thread, so "current"
        # = this channel's most recently active conversation; an explicit
        # conversation id (as shown by `resume <id>`) picks any other one.
        conversation_id = (command.get("text") or "").strip() or store.latest_conversation(channel_id)
        if not conversation_id:
            respond(text="このチャンネルにはまだ会話がありません。", response_type="ephemeral")
            return
        url = _web_ui_link(conversation_id)
        if not url:
            respond(text=_NO_INSTANCE_TEXT, response_type="ephemeral")
            return
        respond(text=f"<{url}|Web UI で開く>\n会話ID: `{conversation_id}`", response_type="ephemeral")

    @app.message_shortcut("agy_open_web_ui")
    def handle_open_web_ui_shortcut(ack, shortcut, client):  # noqa: ANN001 - Bolt signature
        """Message shortcut ("..." menu on a message in a thread): unlike
        /agy-link this knows exactly which thread it was invoked on."""
        ack()
        channel_id = (shortcut.get("channel") or {}).get("id")
        user_id = (shortcut.get("user") or {}).get("id")
        message = shortcut.get("message") or {}
        thread_key = message.get("thread_ts") or message.get("ts")
        if channel_id not in channel_map or not thread_key:
            text = "このチャンネルは agy-slack-bridge の対象外です。"
        else:
            conversation_id = store.get(channel_id, thread_key)
            if not conversation_id:
                text = "このスレッドにはまだ agy の会話がありません（メンションして会話を始めると作られます）。"
            else:
                url = _web_ui_link(conversation_id)
                text = (f"<{url}|Web UI で開く>\n会話ID: `{conversation_id}`" if url
                        else _NO_INSTANCE_TEXT)
        client.chat_postEphemeral(channel=channel_id, user=user_id, thread_ts=thread_key, text=text)

    @app.command("/agy-usage")
    def handle_usage_command(ack, respond, command):  # noqa: ANN001 - Bolt signature
        ack()
        channel_id = command.get("channel_id")
        channel_cfg = channel_map.get(channel_id)
        if not channel_cfg:
            respond(text="このチャンネルは agy-slack-bridge の対象外です。", response_type="ephemeral")
            return
        model, _ = _effective_model_effort(store, channel_id, channel_cfg)
        rows = _fetch_agy_usage(channel_cfg["project"])
        respond(text=f"*モデル利用枠*（このチャンネル: `{model or 'agyのデフォルト'}`）\n"
                     + _format_usage(rows, _usage_group_for_model(model)),
                response_type="ephemeral")

    @app.event("app_home_opened")
    def handle_home_opened(event, client):  # noqa: ANN001 - Bolt signature
        if event.get("tab") != "home" or not channel_map:
            return
        try:
            rows = _fetch_agy_usage(next(iter(channel_map.values()))["project"])
            lines = []
            for cid, cfg in channel_map.items():
                model, effort = _effective_model_effort(store, cid, cfg)
                lines.append(f"<#{cid}>  `{model or 'agyのデフォルト'}`" + (f"  (effort={effort})" if effort else ""))
            client.views_publish(user_id=event["user"], view={"type": "home", "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "agy 利用状況"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": _format_usage(rows)}},
                {"type": "divider"},
                {"type": "section", "text": {"type": "mrkdwn",
                                              "text": "*チャンネルごとの現在のモデル*\n" + "\n".join(lines)}},
                {"type": "context", "elements": [{"type": "mrkdwn",
                    "text": f"取得: {datetime.now(JST):%H:%M} JST（開くたびに更新、最大1分キャッシュ）"}]},
            ]})
        except Exception:
            log.exception("failed to publish app home")

    return app


def main() -> None:
    app = build_app()
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()


if __name__ == "__main__":
    main()
