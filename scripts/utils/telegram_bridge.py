"""telegram_bridge.py - the Telegram bridge's logic, without network or Herdr calls (HOS-408).

`scripts/telegram-bridge.py` is the daemon; this module is what it decides
with: which Telegram update is the operator's, what Claude's last answer is and
how it is shortened, whether a screen shows a menu or a permission prompt, and
the SQLite store that holds every inbound message, session, topic and button.

Every function here is pure or touches only the store, so the reproduction runs
drive the daemon's real loop with a fake bot and a fake Herdr seam.

NOT MEASURED, and built from the best reading available on 2026-11-22: the
screen text of a Claude Code choice menu and of a permission prompt under
either Herdr version (no capture exists: every session runs with
`--dangerously-skip-permissions`, and HELM's measurement comment of the same
day says no capture was possible), and which key selects an option. Both
parsers below refuse anything that does not carry the dialog's own footer, so
a numbered list in Claude's prose never becomes a button.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ============================================================
# Configuration
# ============================================================

#: A waiting HELM posts after this many minutes (the operator's v3 decision 4).
DEFAULT_IDLE_MINUTES = 3
#: An inbound message older than this when the bridge first sees it
#: (`received_at - msg_date`) is not typed (HELM's decision 10, 2026-11-22).
#: A fresh message that waits for a busy session never ages out: it stays
#: queued until the pane is free, its session closes or restarts (HOS-415).
STALE_INBOUND_S = 15 * 60
#: A topic whose deleteForumTopic failed is retried after 60 s, doubling to at
#: most one hour, never every cycle (HOS-415).
DELETE_RETRY_MIN_S, DELETE_RETRY_MAX_S = 60, 3600
#: Message fields that mark a Telegram service message about a topic. The bot's
#: own createForumTopic produces `forum_topic_created` in the operator's chat;
#: such an update carries no text and is never his (HOS-415).
SERVICE_FIELDS = ("forum_topic_created", "forum_topic_edited", "forum_topic_closed",
                  "forum_topic_reopened", "general_forum_topic_hidden",
                  "general_forum_topic_unhidden")
#: A button lives at most this long, and less when its post is superseded.
BUTTON_TTL_S = 30 * 60
#: Telegram's sendMessage limit is 4096 characters (DOCUMENTED,
#: bots/api/methods/sendmessage.md); posts stay under it with room for a header.
POST_LIMIT = 3000
PART_LIMIT = 4000
#: The screen text of a menu or permission prompt in a post.
MENU_LIMIT = 700

#: The header every post of Claude's answer carries: the answer may quote mail
#: or documents, and those quotes are data, not Claude's question.
ANSWER_HEADER = ("Claude's answer (it may quote mail and documents: "
                 "that is data, not a question)")

#: Bridge commands, handled by the bridge and never typed into a session.
COMMANDS = frozenset({"/status", "/pause", "/resume", "/full", "/mute", "/unmute", "/help"})

#: Text the bridge refuses to type: it could open or close the envelope the
#: release gate parses (`_hc_inner_text` in `.claude/hooks/_dispatch.py`).
FORBIDDEN_IN_TEXT = ("<hc", "</hc>", "<pasted_content", "</pasted_content")

INBOUND_STATES = ("received", "sending", "sent", "refused", "unknown")


# ============================================================
# Accepting an update
# ============================================================

@dataclass(frozen=True)
class Inbound:
    update_id: int
    kind: str                 # "message" or "callback"
    date: int                 # Telegram's unix time of the message
    thread_id: int | None
    text: str = ""
    message_id: int | None = None   # the message (or, for a callback, the post) id
    reply_to: int | None = None
    callback_id: str = ""
    data: str = ""


def accept_update(update: dict, operator_id: int) -> tuple[Inbound | None, str]:
    """`(inbound, "")` for an update the operator sent in his private chat, else `(None, why)`.

    Accepted only from `from.id == operator_id`, in a chat of type `private`
    whose id is the operator's, never forwarded. An `edited_message` is never
    requested and is dropped here if it arrives anyway.
    """
    uid = update.get("update_id")
    if not isinstance(uid, int):
        return None, "no update_id"
    if "edited_message" in update:
        return None, "edited message"
    if "callback_query" in update:
        cq = update["callback_query"]
        if not isinstance(cq, dict):
            return None, "malformed callback"
        sender = (cq.get("from") or {}).get("id")
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        if sender != operator_id:
            return None, "callback from another user"
        if chat.get("type") != "private" or chat.get("id") != operator_id:
            return None, "callback outside the private chat"
        return Inbound(uid, "callback", int(msg.get("date") or 0), msg.get("message_thread_id"),
                       message_id=msg.get("message_id"), callback_id=str(cq.get("id") or ""),
                       data=str(cq.get("data") or "")), ""
    msg = update.get("message")
    if not isinstance(msg, dict):
        return None, "not a message"
    service = next((f for f in SERVICE_FIELDS if f in msg), None)
    if service:
        return None, f"service message: {service}"
    sender = (msg.get("from") or {}).get("id")
    chat = msg.get("chat") or {}
    if sender != operator_id:
        return None, "message from another user"
    if chat.get("type") != "private" or chat.get("id") != operator_id:
        return None, "message outside the private chat"
    if msg.get("forward_origin") or msg.get("forward_from") or msg.get("forward_date"):
        return None, "forwarded message"
    text = msg.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, "no text (voice and media are not bridged)"
    reply = msg.get("reply_to_message") or {}
    return Inbound(uid, "message", int(msg.get("date") or 0), msg.get("message_thread_id"),
                   text=text, message_id=msg.get("message_id"),
                   reply_to=reply.get("message_id") if isinstance(reply, dict) else None), ""


def command_of(text: str) -> tuple[str, str] | None:
    """`(command, rest)` when `text` is a bridge command, else None. `/full@botname` counts."""
    head, _, rest = text.strip().partition(" ")
    name = head.split("@", 1)[0].lower()
    return (name, rest.strip()) if name in COMMANDS else None


def refuse_text(text: str) -> str | None:
    """Why `text` may not be typed, or None."""
    for bad in FORBIDDEN_IN_TEXT:
        if bad in text:
            return f"the text contains {bad!r}, which is not typed"
    return None


# ============================================================
# Claude's last answer
# ============================================================

def _records(path: Path) -> list[dict]:
    out = []
    with path.open("rb") as fh:
        for raw in fh:
            try:
                rec = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                # The harness appends while this reads: a torn last line is ordinary.
                logger.debug("telegram_bridge: unparseable transcript line in %s skipped", path.name)
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def last_turn(path: Path) -> tuple[str | None, str]:
    """`(turn uuid, answer text)` of the newest turn in a transcript.

    The turn is the newest `user` record with a `promptSource`; the answer is
    the text blocks of every `assistant` record after it, joined. `(None, "")`
    when the transcript has no turn yet.
    """
    turn, parts = None, []
    for rec in _records(path):
        if rec.get("type") == "user" and isinstance(rec.get("promptSource"), str):
            turn, parts = rec.get("uuid"), []
            continue
        if rec.get("type") != "assistant" or turn is None:
            continue
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list):
            parts += [b.get("text", "") for b in content
                      if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
    return turn, "\n\n".join(p.strip() for p in parts if p.strip())


def turn_answer(path: Path, turn: str) -> str:
    """The answer of one named turn, for `/full`, pinned to the turn that was posted."""
    current, parts, found = None, [], False
    for rec in _records(path):
        if rec.get("type") == "user" and isinstance(rec.get("promptSource"), str):
            if found:
                break
            current = rec.get("uuid")
            found = current == turn
            continue
        if found and rec.get("type") == "assistant":
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, list):
                parts += [b.get("text", "") for b in content
                          if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
    return "\n\n".join(p.strip() for p in parts if p.strip())


def shorten_answer(text: str, limit: int = POST_LIMIT) -> tuple[str, bool]:
    """`(short, cut)`: the whole text when it fits, else the question and the last paragraphs.

    The first paragraph ending in "?" (when there is one) and then the last
    paragraphs, as many as fit, newest kept; `cut` says `/full` has more.
    """
    text = text.strip()
    if len(text) <= limit:
        return text, False
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    question = next((p for p in paras if p.rstrip().endswith("?")), None)
    if question is not None and len(question) > limit // 2:
        # A question paragraph keeps its end, where the question is, within half the budget.
        paras = [p for p in paras if p is not question]
        question = "..." + question[-(limit // 2 - 3):]
    kept: list[str] = []
    room = limit - (len(question) + 10 if question else 0)
    for p in reversed(paras):
        if p is question:
            continue
        if len(p) + 2 > room:
            if not kept:
                kept.append("..." + p[-(room - 5):] if room > 50 else "...")
            break
        kept.insert(0, p)
        room -= len(p) + 2
    head = [question, "[...]"] if question and question not in kept else ["[...]"]
    return "\n\n".join(head + kept), True


def split_parts(text: str, limit: int = PART_LIMIT) -> list[str]:
    """`text` in pieces Telegram accepts, cut at a newline where one is near."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts + ([text] if text else [])


# ============================================================
# The screen: a menu or a permission prompt
# ============================================================

#: An option line of a Claude Code select menu: an optional cursor, a digit, a
#: dot, the label. NOT MEASURED against a real screen (module docstring).
_OPTION = re.compile(r"^\s*(?:[❯>]\s*)?(?P<n>[1-9])\.\s+(?P<label>\S.*?)\s*$")
#: The dialog footer that tells a live menu from a numbered list in prose. NOT
#: MEASURED; from the best reading of Claude Code's select dialog.
_MENU_FOOTER = re.compile(r"(?i)\b(enter to (select|confirm)|esc to (cancel|go back))\b")
_PERMISSION_Q = re.compile(r"(?i)^\s*do you want to (proceed|make this edit|create|run)\b.*\?\s*$")


_BOX = "│╭╮╰╯─┌┐└┘├┤ \t"


def _clean(line: str) -> str:
    """A screen line without the dialog's box-drawing frame; a frame-only line becomes empty."""
    return line.strip(_BOX).strip()


@dataclass(frozen=True)
class Menu:
    kind: str                       # "menu" or "permission"
    question: str
    options: tuple[tuple[str, str], ...]   # (digit, label)
    block: str                      # the lines the digest covers
    detail: str = ""                # for a permission prompt: the command shown

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.block.encode("utf-8")).hexdigest()


def parse_screen(screen: str) -> Menu | None:
    """The menu or permission prompt at the bottom of `screen`, or None.

    Requires, in the last 30 lines: a dialog footer (`_MENU_FOOTER`), at least
    two consecutive option lines numbered from 1 directly above it, and a
    question line above the options. A permission prompt is a menu whose
    question matches `_PERMISSION_Q`; its `detail` is the non-empty lines
    between the dialog's top and the question. Anything else is None, so prose
    never becomes buttons (Kimi finding 9).
    """
    lines = [_clean(ln) for ln in screen.splitlines()[-30:]]
    footer = next((i for i in range(len(lines) - 1, -1, -1) if _MENU_FOOTER.search(lines[i])), None)
    if footer is None:
        return None
    options: list[tuple[str, str]] = []
    i = footer - 1
    while i >= 0 and not lines[i]:
        i -= 1
    end = i
    while i >= 0:
        m = _OPTION.match(lines[i])
        if not m:
            break
        options.insert(0, (m.group("n"), m.group("label")))
        i -= 1
    if len(options) < 2 or [n for n, _ in options] != [str(k) for k in range(1, len(options) + 1)]:
        return None
    while i >= 0 and not lines[i]:
        i -= 1
    if i < 0:
        return None
    question = lines[i]
    if not question:
        return None
    top = i
    block = "\n".join(lines[top:end + 1])
    if _PERMISSION_Q.match(question):
        start = top - 1
        while start >= 0 and lines[start]:
            start -= 1
        detail = "\n".join(ln for ln in lines[start + 1:top] if ln)
        return Menu("permission", question, tuple(options),
                    "\n".join(lines[start + 1:end + 1]), detail)
    return Menu("menu", question, tuple(options), block)


def allow_key(menu: Menu) -> str | None:
    """The digit of a permission prompt's plain "Yes" option; never "don't ask again"."""
    for digit, label in menu.options:
        low = label.lower()
        if low.startswith("yes") and "don't ask" not in low and "always" not in low:
            return digit
    return None


# ============================================================
# The store
# ============================================================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS inbound (
    update_id INTEGER PRIMARY KEY, received_at REAL NOT NULL, msg_date INTEGER NOT NULL,
    kind TEXT NOT NULL, thread_id INTEGER, message_id INTEGER, reply_to INTEGER,
    text TEXT NOT NULL DEFAULT '', callback_id TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '', session_key TEXT, state TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '', journal_id TEXT, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    key TEXT PRIMARY KEY, name TEXT NOT NULL, backend TEXT NOT NULL, pane TEXT NOT NULL,
    checkout TEXT NOT NULL, incarnation TEXT, thread_id INTEGER, is_helm INTEGER NOT NULL,
    waiting_since REAL, posted_turn TEXT, post_message_id INTEGER, post_kind TEXT,
    posted_at REAL, muted INTEGER NOT NULL DEFAULT 0, closed_at REAL, seen_at REAL);
CREATE TABLE IF NOT EXISTS buttons (
    token TEXT PRIMARY KEY, session_key TEXT NOT NULL, post_message_id INTEGER NOT NULL,
    incarnation TEXT, kind TEXT NOT NULL, key TEXT NOT NULL, menu_sha TEXT NOT NULL,
    created_at REAL NOT NULL, dead_at REAL, used_at REAL);
"""

#: Columns added after the first store was created live (HOS-415): added by
#: ALTER TABLE when missing, so a store from HOS-408 keeps its rows.
_ADDED_COLUMNS = (
    ("inbound", "told_queued", "INTEGER NOT NULL DEFAULT 0"),
    ("inbound", "queued_tries", "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "topic_deleted_at", "REAL"),
    ("sessions", "delete_tries", "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "delete_next_at", "REAL"),
)


_SESSION_COLS = frozenset({
    "name", "backend", "pane", "checkout", "incarnation", "thread_id", "is_helm",
    "waiting_since", "posted_turn", "post_message_id", "post_kind", "posted_at", "muted",
    "closed_at", "seen_at", "topic_deleted_at", "delete_tries", "delete_next_at"})


class Store:
    """One SQLite file, WAL mode; every write commits before the caller acts on it."""

    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        for table, column, decl in _ADDED_COLUMNS:
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                # Names from the constant above only, never from a value.
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        self.db.close()

    # kv
    def get(self, name: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE name=?", (name,)).fetchone()
        return row["value"] if row else default

    def put(self, name: str, value: str) -> None:
        self.db.execute("INSERT INTO kv(name, value) VALUES(?, ?) ON CONFLICT(name) "
                        "DO UPDATE SET value=excluded.value", (name, value))

    # inbound
    def add_inbound(self, item: Inbound, now: float) -> bool:
        """Store one accepted update as `received`; False when its update_id is already stored."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO inbound(update_id, received_at, msg_date, kind, thread_id, "
            "message_id, reply_to, text, callback_id, data, state, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'received',?)",
            (item.update_id, now, item.date, item.kind, item.thread_id, item.message_id,
             item.reply_to, item.text, item.callback_id, item.data, now))
        return cur.rowcount == 1

    def pending(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM inbound WHERE state='received' ORDER BY update_id").fetchall()

    def set_state(self, update_id: int, state: str, detail: str = "", *,
                  session_key: str | None = None, journal_id: str | None = None) -> None:
        if state not in INBOUND_STATES:
            raise ValueError(state)
        self.db.execute(
            "UPDATE inbound SET state=?, detail=?, updated_at=?, "
            "session_key=COALESCE(?, session_key), journal_id=COALESCE(?, journal_id) "
            "WHERE update_id=?",
            (state, detail[:300], time.time(), session_key, journal_id, update_id))

    def mark_queued_notice(self, update_id: int, *, told: bool, tries: int) -> None:
        """Record one attempt at the queued notice: `told` only when the post went out."""
        self.db.execute("UPDATE inbound SET told_queued=?, queued_tries=? WHERE update_id=?",
                        (int(told), tries, update_id))

    def orphaned_sends(self) -> list[sqlite3.Row]:
        """Rows left in `sending` by a crash: delivery unknown, never retried."""
        return self.db.execute("SELECT * FROM inbound WHERE state='sending'").fetchall()

    def sent_since(self, session_key: str, since: float) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM inbound WHERE session_key=? AND state='sent' AND updated_at>=? LIMIT 1",
            (session_key, since)).fetchone()
        return row is not None

    # sessions
    def sessions(self, *, open_only: bool = True) -> list[sqlite3.Row]:
        if open_only:
            return self.db.execute("SELECT * FROM sessions WHERE closed_at IS NULL "
                                   "ORDER BY is_helm DESC, name").fetchall()
        return self.db.execute("SELECT * FROM sessions ORDER BY is_helm DESC, name").fetchall()

    def session(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM sessions WHERE key=?", (key,)).fetchone()

    def session_by_thread(self, thread_id: int | None, *, open_only: bool = True) -> sqlite3.Row | None:
        if thread_id is None:
            return None
        if open_only:
            return self.db.execute("SELECT * FROM sessions WHERE thread_id=? AND closed_at IS NULL",
                                   (thread_id,)).fetchone()
        return self.db.execute("SELECT * FROM sessions WHERE thread_id=? ORDER BY seen_at DESC "
                               "LIMIT 1", (thread_id,)).fetchone()

    def reusable_topic(self, checkout: str) -> sqlite3.Row | None:
        """The newest closed session of `checkout` whose topic still exists, for a session that came back.

        A yard whose agent exited and started again (in the same pane or
        another) keeps its topic; so does HELM, whose checkout never changes.
        """
        return self.db.execute(
            "SELECT * FROM sessions WHERE checkout=? AND closed_at IS NOT NULL AND "
            "thread_id IS NOT NULL AND topic_deleted_at IS NULL ORDER BY seen_at DESC LIMIT 1",
            (checkout,)).fetchone()

    def topics_to_delete(self, now: float) -> list[sqlite3.Row]:
        """Closed, non-HELM sessions holding a topic that is due for a delete attempt.

        The caller still checks that the checkout is gone and is not HELM's.
        """
        return self.db.execute(
            "SELECT * FROM sessions WHERE closed_at IS NOT NULL AND is_helm=0 AND "
            "thread_id IS NOT NULL AND topic_deleted_at IS NULL AND "
            "(delete_next_at IS NULL OR delete_next_at<=?)", (now,)).fetchall()

    def upsert_session(self, key: str, **fields) -> None:
        """Insert or update one session row; every column name is checked against `_SESSION_COLS`.

        The SQL is assembled from those checked names only, never from a value,
        and every value is a bound parameter.
        """
        bad = set(fields) - _SESSION_COLS
        if bad:
            raise ValueError(f"unknown session columns {sorted(bad)}")
        if self.session(key) is None:
            cols = ["key", *fields]
            sql = "INSERT INTO sessions(%s) VALUES(%s)" % (  # noqa: S608 # nosec B608 - checked names
                ", ".join(cols), ", ".join("?" for _ in cols))
            self.db.execute(sql, (key, *fields.values()))
            return
        if fields:
            sql = "UPDATE sessions SET %s WHERE key=?" % ", ".join(  # noqa: S608 # nosec B608 - checked names
                f"{k}=?" for k in fields)
            self.db.execute(sql, (*fields.values(), key))

    # buttons
    def add_button(self, token: str, session_key: str, post_message_id: int, incarnation: str,
                   kind: str, key: str, menu_sha: str, now: float) -> None:
        self.db.execute("INSERT INTO buttons VALUES(?,?,?,?,?,?,?,?,NULL,NULL)",
                        (token, session_key, post_message_id, incarnation, kind, key, menu_sha, now))

    def kill_buttons(self, session_key: str, now: float) -> None:
        self.db.execute("UPDATE buttons SET dead_at=? WHERE session_key=? AND dead_at IS NULL",
                        (now, session_key))

    def claim_button(self, token: str, post_message_id: int | None, now: float) -> sqlite3.Row | None:
        """The button row when it is alive, unused, unexpired and from this post; claimed atomically."""
        cur = self.db.execute(
            "UPDATE buttons SET used_at=? WHERE token=? AND used_at IS NULL AND dead_at IS NULL "
            "AND post_message_id=? AND created_at>=?",
            (now, token, post_message_id, now - BUTTON_TTL_S))
        if cur.rowcount != 1:
            return None
        return self.db.execute("SELECT * FROM buttons WHERE token=?", (token,)).fetchone()
