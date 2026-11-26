#!/usr/bin/env python3
"""telegram-bridge.py - the operator's private chat with his notification bot, two ways, into every session (HOS-408).

Usage:

    .venv/bin/python scripts/telegram-bridge.py daemon          # the loop; HELM only, run by the unit
    .venv/bin/python scripts/telegram-bridge.py run-once        # one cycle, short poll; HELM only
    .venv/bin/python scripts/telegram-bridge.py status          # state from the store; no network call
    .venv/bin/python scripts/telegram-bridge.py status --live   # plus getMe and getWebhookInfo; HELM only
    .venv/bin/python scripts/telegram-bridge.py off             # the kill switch: nothing is typed from now on; HELM only
    .venv/bin/python scripts/telegram-bridge.py on              # remove it; HELM only, on the machine
    .venv/bin/python scripts/telegram-bridge.py journal -n 20   # the last lines of the input journal
    .venv/bin/python scripts/telegram-bridge.py enable-topics   # setMySettings(has_topics_enabled); HELM only

What it does. The operator decided on 2026-11-22 that a notification he cannot
answer is of no use to him, so the bridge works both ways. From his private
chat with the bot (the bot of TELEGRAM_NOTIFY_BOT_TOKEN), one forum topic per Claude Code session (HELM and
every yard, under herdr-core and the old Herdr TUI):

1. A message in a session's topic is typed into that session unmarked, as his
   own word (his decision, 2026-11-22: text Herdr sends in his name counts as
   his, as `/compact` does). Typed only when the pane is idle or done and its
   input line is empty, under the per-pane input lock every typist shares
   (`herdr_agent.input_lock`). Before typing, the message is stored with its
   text, target and state (`received`, `sending`, `sent`, `refused`,
   `unknown`); a send that crashed or timed out is `unknown`, is never
   retried, and the bot says "not sure it arrived, please check". A message
   older than 15 minutes when first seen, or with no session topic, is never
   typed anywhere. A fresh message for a busy session is queued, said once in
   its topic, and typed when the pane is free, one message per turn; it is
   refused only if the session restarts or ends first (HOS-415). A button
   press is never queued: it is acted on in the cycle it arrives or answered
   "stale", because the screen it answered may have changed.
5. Topics. One per checkout: a session that comes back (its agent restarted,
   in the same pane or another) keeps its checkout's topic. When a yard's
   session is gone AND its checkout directory no longer exists (the yard was
   removed), its topic is renamed to its name plus " · removed" and then
   deleted with deleteForumTopic, retried with backoff on failure; the rename
   (HOS-416) is for Telegram Android, which keeps showing a deleted topic
   until the app restarts. HELM's topic is never deleted, and is created again if a post
   finds it missing.
2. HELM, once per finished turn left unanswered for TELEGRAM_BRIDGE_IDLE_MINUTES
   (default 3), posts Claude's last answer in its topic, shortened, with
   `/full`. A yard posts only when it shows a choice menu or a permission
   prompt (HELM's decision 10, 2026-11-22), never for being idle.
3. A menu or permission prompt read from the SCREEN, never from Claude's prose,
   becomes buttons. Each button is single-use, bound to its post, the session
   incarnation and the SHA-256 of the dialog block; the block is re-read under
   the input lock immediately before the key press, and a changed screen
   answers "stale".
4. `/status`, `/pause`, `/resume`, `/mute`, `/unmute`, `/full`; and the kill
   switch, a file `off` in `runtime:telegram-bridge/state`, created by `off` in
   HELM and checked immediately before every send and key press.

Accepted only from TELEGRAM_BRIDGE_OPERATOR_ID, in his private chat, not
forwarded, not edited, and only when that chat id is also one of
`telegram_notify.own_targets()`: everything this sends goes to the operator's
own sink (`.claude/rules/lethal-trifecta.md`, first exception). If his
Telegram account is taken over, the user-id check, the kill switch, `/pause`,
the input journal and the token runbook (`TELEGRAM-AND-ALERTS.md`, registry
name `spec-docs`) are the only guards left.

THE GATE JOURNAL. Under both Herdr versions, immediately before each send, an
entry `{"id", "at", "target", "sha256", "length", "session"}` is appended to
`.claude/.bridge-journal.jsonl` in HELM's main clone, and after it
`{"id", "outcome": "sent" | "unknown"}`. The release gate
(`_bridged_hc_text` in `.claude/hooks/_dispatch.py`) accepts an enveloped
herdr-core text only against a `sent` entry for that terminal, and one entry
authorises one transcript record. The entry holds no text; the text is in the
input journal under `runtime:telegram-bridge/logs`, never pruned.

It starts only on the operator's word (M004, report v3 decision 6), in HELM,
by the unit `scripts/templates/systemd/telegram-bridge.service`; it refuses
to start while `getMe` reports `has_topics_enabled: false`, with a webhook
set, or without its registry rows.

Exit codes: 0 ok; 2 refused (a yard, not the main clone, bad arguments);
3 another instance holds the start lock; 4 not configured (token, operator id,
own sink, topics, registry row).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import secrets
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE))

from scripts.utils import herdr_agent as HA  # noqa: E402
from scripts.utils import telegram_bridge as TB  # noqa: E402

logger = logging.getLogger("telegram-bridge")

EXIT_REFUSED, EXIT_RUNNING, EXIT_UNCONFIGURED = 2, 3, 4
POLL_TIMEOUT_S = 5
CONFLICT_RETRY_S = 300
BUSY_STATES = ("idle", "done")
WAIT_STATES = ("idle", "done", "blocked")
JOURNAL_REL = Path(".claude") / ".bridge-journal.jsonl"
KILL_SWITCH = "off"
#: Appended to a removed yard's topic name just before the topic is deleted (HOS-416).
TOPIC_REMOVED_SUFFIX = " · removed"
PID_FILE, START_LOCK = "daemon.pid", "daemon.lock"


# ============================================================
# Paths
# ============================================================

def _runtime_dir(env_var: str, name: str, *, create: bool) -> Path:
    """The override directory when `env_var` is set, else the registry name."""
    override = os.environ.get(env_var, "").strip()
    if override:
        path = Path(override)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path
    from scripts.utils.data_path import data_path, data_write_dir
    if name == "runtime:telegram-bridge/state":
        return (data_write_dir if create else data_path)("runtime:telegram-bridge/state")
    return (data_write_dir if create else data_path)("runtime:telegram-bridge/logs")


def state_dir(*, create: bool) -> Path:
    return _runtime_dir("TELEGRAM_BRIDGE_STATE_DIR", "runtime:telegram-bridge/state", create=create)


def log_dir(*, create: bool) -> Path:
    return _runtime_dir("TELEGRAM_BRIDGE_LOG_DIR", "runtime:telegram-bridge/logs", create=create)


def gate_journal_path() -> Path:
    """HELM's bridge journal; TELEGRAM_BRIDGE_JOURNAL relocates it for a fixture run only."""
    override = os.environ.get("TELEGRAM_BRIDGE_JOURNAL", "").strip()
    if override:
        return Path(override)
    from scripts.utils.clone_guard import main_clone_path
    return main_clone_path(WORKSPACE) / JOURNAL_REL


def kill_switch_on() -> bool:
    """True when the switch file exists, or when the state directory cannot be read: fail closed."""
    try:
        return (state_dir(create=False) / KILL_SWITCH).exists()
    except Exception as exc:  # noqa: BLE001 - logged, and means "do not type"
        logger.warning("telegram-bridge: kill switch unreadable (%s); treated as on", exc)
        return True


def _append_line(path: Path, record: dict) -> None:
    """One JSON line by one `os.write` on an O_APPEND file; OSError on a failed or short write."""
    blob = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        written = os.write(fd, blob)
    finally:
        os.close(fd)
    if written != len(blob):
        raise OSError(f"{path.name} wrote {written} of {len(blob)} bytes")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _thread_missing(exc: Exception) -> bool:
    """Whether a Bot API error says the topic does not exist.

    # ASSUMED 2026-11-22: Telegram answers a send into a deleted topic with
    # "Bad Request: message thread not found" (or TOPIC_ID_INVALID); neither
    # string is in the Bot API documentation, and no live call was made.
    """
    text = f"{getattr(exc, 'telegram_description', '') or ''} {exc}".lower()
    return "thread not found" in text or "topic_id_invalid" in text


#: Set with every topic a session row is given: the deletion state of a topic
#: this key held before never carries over to the new one (HOS-415 review r1).
_FRESH_TOPIC = {"topic_deleted_at": None, "delete_tries": 0, "delete_next_at": None}

#: A queued notice that fails to post is tried again on later cycles, at most
#: this many times in all (HOS-415 review r1).
QUEUED_NOTICE_TRIES = 3


def _checkout_gone(path: str) -> bool:
    """True only when the checkout directory is known to be absent; unreadable means still there."""
    try:
        return not Path(path).exists()
    except OSError as exc:
        logger.info("telegram-bridge: checkout %s unreadable (%s); treated as present", path, exc)
        return False


# ============================================================
# The Herdr side, behind one object so a fixture run can replace it
# ============================================================

class HerdrSeam:
    """The calls the bridge makes into Herdr, all through `scripts/utils/herdr_agent.py`."""

    sessions = staticmethod(HA.all_sessions)
    status = staticmethod(HA.pane_status)
    screen = staticmethod(HA.screen_text)
    input_line = staticmethod(HA.read_input_line)
    send = staticmethod(HA.send_text)
    keys = staticmethod(HA.press_keys)
    lock = staticmethod(HA.input_lock)

    @staticmethod
    def checkout(record: dict) -> Path | None:
        pane = record.get("pane_id") or ""
        return HA.pane_checkout(pane) if pane else None


def _transcript_for(record: dict, checkout: Path, is_helm: bool) -> tuple[Path | None, str | None]:
    """(transcript, incarnation) for one session; (None, None) when it cannot be told.

    HELM is read the way a yard is.
    TUI yard: the `agent_session` uuid herdr reports. herdr-core yard: the
    newest transcript of the checkout (hc reports no session id,
    `herdr_agent.py:63-64`; one session per checkout, as the relay accepts).
    """
    from scripts.utils.checkpoint_paths import transcript_dir
    directory = transcript_dir(checkout)
    if directory is None or not directory.is_dir():
        return None, None
    session = record.get("agent_session")
    if isinstance(session, dict) and session.get("kind") == "id" and session.get("value"):
        path = directory / f"{session['value']}.jsonl"
        return (path, session["value"]) if path.is_file() else (None, None)
    newest = max(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, default=None)
    return (newest, newest.stem) if newest else (None, None)


# ============================================================
# The bridge
# ============================================================

class Bridge:
    """One store, one bot, one Herdr seam; `cycle()` is the whole loop body."""

    def __init__(self, store: TB.Store, bot, herdr, *, operator_id: int, helm: Path,
                 idle_minutes: int = TB.DEFAULT_IDLE_MINUTES, clock=time.time,
                 transcript_for=_transcript_for):
        self.store, self.bot, self.herdr = store, bot, herdr
        self.chat = operator_id
        self.helm = helm.resolve()
        self.idle_s = idle_minutes * 60
        self.clock = clock
        self.transcript_for = transcript_for
        self.journal = log_dir(create=True) / "inputs.jsonl"
        self._last_transcript: dict[str, Path | None] = {}
        self._typed: set[str] = set()

    # ---------- journals ----------
    def log(self, event: str, **fields) -> None:
        """One line in the input journal, the full text included (report v3: everything typed in)."""
        try:
            _append_line(self.journal, {"at": _now_iso(), "event": event, **fields})
        except OSError as exc:
            logger.error("telegram-bridge: input journal not written: %s", exc)

    # ---------- telegram ----------
    def say(self, text: str, thread: int | None = None, markup: dict | None = None) -> int | None:
        try:
            msg = self.bot.send_message(self.chat, text[:TB.PART_LIMIT], parse_mode=None,
                                        message_thread_id=thread, reply_markup=markup)
        except Exception as exc:  # noqa: BLE001 - logged; a failed post is not a failed bridge
            logger.warning("telegram-bridge: send to the operator failed: %s", exc)
            if thread is not None and _thread_missing(exc):
                self.topic_lost(thread)
            return None
        return msg.get("message_id") if isinstance(msg, dict) else None

    def topic_lost(self, thread: int) -> None:
        """The topic of an open session is gone (deleted by hand): the next cycle creates a new one."""
        row = self.store.session_by_thread(thread)
        if row is None:
            return
        self.store.upsert_session(row["key"], thread_id=None)
        self.log("topic lost", session=row["key"], thread_id=thread)

    def edit(self, message_id: int, text: str) -> None:
        try:
            self.bot.edit_message_text(self.chat, message_id, text[:TB.PART_LIMIT], parse_mode=None,
                                       reply_markup={"inline_keyboard": []})
        except Exception as exc:  # noqa: BLE001 - logged
            logger.info("telegram-bridge: edit of %s failed: %s", message_id, exc)

    # ---------- updates ----------
    def in_conflict(self) -> bool:
        """Whether another reader polled this bot (409) within CONFLICT_RETRY_S."""
        until = self.store.get("conflict_until")
        return bool(until) and float(until) > self.clock()

    def take_updates(self, timeout: int) -> None:
        """getUpdates once; store every accepted update BEFORE the offset moves past it."""
        if self.in_conflict():
            return
        offset = int(self.store.get("offset", "0"))
        try:
            updates = self.bot.get_updates(offset=offset, timeout=timeout,
                                           allowed_updates=["message", "callback_query"])
        except Exception as exc:  # noqa: BLE001 - 409 handled, the rest logged and retried
            if getattr(exc, "status_code", None) == 409:
                logger.error("telegram-bridge: another reader polls this bot (409); typing stops")
                self.store.put("conflict_until", str(self.clock() + CONFLICT_RETRY_S))
                self.log("conflict")
            else:
                logger.warning("telegram-bridge: getUpdates failed: %s", exc)
            return
        self.store.put("conflict_until", "")
        top = offset
        for update in updates or []:
            item, why = TB.accept_update(update, self.chat)
            uid = update.get("update_id") if isinstance(update, dict) else None
            if item is None:
                self.log("dropped", update_id=uid, reason=why)
            elif self.store.add_inbound(item, self.clock()):
                self.log("received", update_id=item.update_id, kind=item.kind,
                         thread_id=item.thread_id, text=item.text, data=item.data)
            if isinstance(uid, int):
                top = max(top, uid + 1)
        self.store.put("offset", str(top))

    # ---------- sessions ----------
    def refresh_sessions(self) -> dict[str, dict]:
        """Discover sessions, give each its checkout's topic, close gone ones, delete removed yards' topics."""
        try:
            records, unreadable = self.herdr.sessions()
        except Exception as exc:  # noqa: BLE001 - neither version answered: change nothing
            logger.warning("telegram-bridge: no session list: %s", exc)
            return {}
        live: dict[str, dict] = {}
        now = self.clock()
        for rec in records:
            if rec.get("agent") != "claude" or not rec.get("pane_id"):
                continue
            try:
                checkout = self.herdr.checkout(rec)
            except Exception as exc:  # noqa: BLE001 - this session is skipped this cycle
                logger.info("telegram-bridge: no checkout for %s: %s", rec.get("pane_id"), exc)
                continue
            if checkout is None:
                continue
            checkout = Path(checkout).resolve()
            is_helm = checkout == self.helm
            key = f"{rec['backend']}:{rec['pane_id']}:{checkout}"
            transcript, incarnation = self.transcript_for(rec, checkout, is_helm)
            live[key] = {**rec, "key": key, "checkout": checkout, "is_helm": is_helm,
                         "transcript": transcript, "incarnation": incarnation}
        # Gone sessions close FIRST, so a session that moved to another pane in
        # the same cycle finds its checkout's topic free to take (HOS-415).
        for row in self.store.sessions():
            if row["key"] in live or row["backend"] in unreadable:
                continue
            # No closeForumTopic: the Bot API names forum supergroups only for it
            # (DOCUMENTED 2026-11-22), so in this private chat it could only fail.
            # The topic stays until its checkout is gone (`delete_topics`).
            self.store.kill_buttons(row["key"], now)
            self.store.upsert_session(row["key"], closed_at=now)
            self.log("closed", session=row["key"])
        for key, entry in live.items():
            rec, checkout, is_helm = entry, entry["checkout"], entry["is_helm"]
            incarnation = entry["incarnation"]
            row = self.store.session(key)
            name = "HELM" if is_helm else checkout.name
            if row is not None and row["closed_at"] is None and row["thread_id"] is None:
                # An open session whose topic was lost (`topic_lost`): a new one, same session.
                thread = self._new_topic(name)
                if thread is not None:
                    self.store.upsert_session(key, thread_id=thread, **_FRESH_TOPIC)
                    self.log("topic", session=key, thread_id=thread, reason="recreated")
            if row is None or row["closed_at"] is not None:
                # A session that came back (its agent restarted, in this pane or
                # another) keeps the topic of its checkout; only a new checkout
                # gets a new topic (HOS-415).
                reuse = self.store.reusable_topic(str(checkout))
                thread = reuse["thread_id"] if reuse is not None else self._new_topic(name)
                if thread is None:
                    continue   # not created: retried next cycle
                if reuse is not None and reuse["key"] != key:
                    self.store.upsert_session(reuse["key"], thread_id=None)
                self.store.upsert_session(key, name=name, backend=rec["backend"],
                                          pane=rec["pane_id"], checkout=str(checkout),
                                          incarnation=incarnation, thread_id=thread,
                                          is_helm=int(is_helm), seen_at=now, closed_at=None,
                                          waiting_since=None, posted_turn=None,
                                          post_message_id=None, post_kind=None, **_FRESH_TOPIC)
                self.store.put(f"inc_since:{key}", str(now))
                self.log("topic", session=key, thread_id=thread,
                         reason="reused" if reuse is not None else "created")
                continue
            if incarnation and row["incarnation"] and incarnation != row["incarnation"]:
                # A new Claude session in the same pane: nothing of the old one carries over.
                self.store.kill_buttons(key, now)
                if row["post_message_id"]:
                    self.edit(row["post_message_id"], "Session restarted; this question is closed.")
                self.store.upsert_session(key, incarnation=incarnation, waiting_since=None,
                                          posted_turn=None, post_message_id=None, post_kind=None)
                self.store.put(f"inc_since:{key}", str(now))
                self.log("incarnation", session=key)
            elif incarnation and not row["incarnation"]:
                self.store.upsert_session(key, incarnation=incarnation)
            self.store.upsert_session(key, seen_at=now)
        self.delete_topics(now)
        return live

    def _new_topic(self, name: str) -> int | None:
        try:
            topic = self.bot.create_forum_topic(self.chat, name)
        except Exception as exc:  # noqa: BLE001 - retried next cycle
            logger.warning("telegram-bridge: topic for %s not created: %s", name, exc)
            return None
        return topic.get("message_thread_id") if isinstance(topic, dict) else None

    def delete_topics(self, now: float) -> None:
        """Delete the topic of every closed yard session whose checkout is gone (the yard was removed).

        A closed session whose checkout still exists only stopped its agent and
        keeps its topic for when it comes back. HELM's topic is never deleted:
        the store query skips `is_helm`, and the checkout test below skips HELM's
        clone whatever the row says. A delete refused because the topic is
        already gone (`_thread_missing`, e.g. deleted by hand) counts as
        deleted, journalled with reason "already gone". Any other failed delete
        keeps the row, is logged, and is retried after DELETE_RETRY_MIN_S
        doubling to DELETE_RETRY_MAX_S.

        Just before the first delete attempt the topic is renamed to the
        session's name plus TOPIC_REMOVED_SUFFIX (HOS-416), so a client that
        keeps a deleted topic in its list (Telegram Android, until restarted)
        shows it as removed. A failed rename is logged and never stops the
        delete; a retried delete (delete_tries > 0) does not rename again.
        """
        for row in self.store.topics_to_delete(now):
            if row["is_helm"] or Path(row["checkout"]).resolve() == self.helm:
                continue
            if not _checkout_gone(row["checkout"]):
                continue
            if not row["delete_tries"]:
                name = (row["name"] or "")[:128 - len(TOPIC_REMOVED_SUFFIX)] + TOPIC_REMOVED_SUFFIX
                try:
                    self.bot.edit_forum_topic(self.chat, row["thread_id"], name, _timeout=10)
                except Exception as exc:  # noqa: BLE001 - logged; the delete goes ahead
                    logger.info("telegram-bridge: topic %s not renamed before delete: %s",
                                row["thread_id"], exc)
            try:
                self.bot.delete_forum_topic(self.chat, row["thread_id"])
            except Exception as exc:  # noqa: BLE001 - logged, kept, retried later
                if _thread_missing(exc):
                    self.store.upsert_session(row["key"], topic_deleted_at=now)
                    self.log("topic deleted", session=row["key"], thread_id=row["thread_id"],
                             reason="already gone")
                    continue
                tries = (row["delete_tries"] or 0) + 1
                wait = min(TB.DELETE_RETRY_MIN_S * 2 ** (tries - 1), TB.DELETE_RETRY_MAX_S)
                self.store.upsert_session(row["key"], delete_tries=tries, delete_next_at=now + wait)
                logger.warning("telegram-bridge: topic %s not deleted (try %d, next in %d s): %s",
                               row["thread_id"], tries, wait, exc)
                self.log("topic not deleted", session=row["key"], thread_id=row["thread_id"],
                         tries=tries, detail=str(exc)[:200])
                continue
            self.store.upsert_session(row["key"], topic_deleted_at=now)
            self.log("topic deleted", session=row["key"], thread_id=row["thread_id"])

    # ---------- inbound ----------
    def _ready(self, pane: str) -> str | None:
        """None when `pane` may receive text, else why not (the relay's `not_ready`)."""
        try:
            status = self.herdr.status(pane)
        except Exception as exc:  # noqa: BLE001 - unreadable means not ready
            return f"status unreadable: {exc}"
        if status not in BUSY_STATES:
            return f"session is {status}"
        try:
            typed = self.herdr.input_line(pane)
        except Exception as exc:  # noqa: BLE001 - unreadable means not known empty
            return f"input line unreadable: {exc}"
        return "text typed but not sent in the input line" if typed else None

    def handle_inbound(self, live: dict[str, dict]) -> None:
        self._typed: set[str] = set()   # sessions typed into this cycle
        for row in self.store.pending():
            if row["kind"] == "callback":
                self.handle_callback(row, live)
            else:
                self.handle_message(row, live)

    def _refuse(self, row, why: str, thread=None, tell: str | None = None) -> None:
        self.store.set_state(row["update_id"], "refused", why)
        self.log("refused", update_id=row["update_id"], reason=why)
        if tell:
            self.say(tell, thread)

    def session_list(self) -> str:
        rows = self.store.sessions()
        if not rows:
            return "No sessions."
        return "Sessions (write in the topic of the one you mean):\n" + "\n".join(
            f"- {r['name']} ({'herdr-core' if r['backend'] == 'core' else 'Herdr TUI'})" for r in rows)

    def handle_message(self, row, live: dict[str, dict]) -> None:
        thread = row["thread_id"]
        cmd = TB.command_of(row["text"])
        session = self.store.session_by_thread(thread)
        if cmd is not None:
            self.store.set_state(row["update_id"], "refused", f"command {cmd[0]}")
            self.command(cmd[0], session, thread)
            return
        if session is None:
            if self.store.session_by_thread(thread, open_only=False) is not None:
                self._refuse(row, "session closed", thread,
                             "Not sent: this session has ended.\n" + self.session_list())
                return
            self._refuse(row, "no session topic", thread,
                         "Not sent: the message is not in a session topic.\n" + self.session_list())
            return
        # Old when FIRST seen, never old from waiting here: a fresh message for a
        # busy session stays queued (HOS-415; on 2026-11-22 the check read `now`
        # and refused a message that had waited 15 minutes for HELM).
        if row["received_at"] - row["msg_date"] > TB.STALE_INBOUND_S:
            self._refuse(row, "older than 15 minutes when first seen", thread,
                         "Not sent: the message is older than 15 minutes. Send it again if still needed.")
            return
        since = float(self.store.get(f"inc_since:{session['key']}", "0") or 0)
        if row["msg_date"] < since:
            self._refuse(row, "session restarted after the message", thread,
                         "Not sent: the session restarted after your message. Send it again.")
            return
        bad = TB.refuse_text(row["text"])
        if bad:
            self._refuse(row, bad, thread, f"Not sent: {bad}.")
            return
        if kill_switch_on() or self.store.get("paused") == "1":
            self._refuse(row, "paused or kill switch", thread,
                         "Not sent: the bridge is paused (/resume, or the switch on the machine).")
            return
        target = live.get(session["key"])
        if target is None or session["key"] in self._typed:
            # Not listed this cycle, or already typed into this cycle: one message
            # per turn, never a second one typed before the status moves off idle.
            self._queued(row, thread)
            return
        pane = target["pane_id"]
        if self._ready(pane) is not None:
            self._queued(row, thread)
            return
        with self.herdr.lock(pane) as held:
            if not held:
                self._queued(row, thread)
                return   # another typist is at this pane: next cycle
            why = self._ready(pane)
            if why is not None or kill_switch_on() or self.store.get("paused") == "1":
                return
            self._typed.add(session["key"])
            self.deliver(row, session, target)

    def _queued(self, row, thread) -> None:
        """Tell the operator ONCE that his message waits for the session; it stays `received`.

        Marked told only after the post went out (say() returns its id); a
        failed post is tried again next cycle, QUEUED_NOTICE_TRIES times in all.
        """
        if row["told_queued"] or row["queued_tries"] >= QUEUED_NOTICE_TRIES:
            return
        posted = self.say("Queued: the session is busy. It will be typed when the session is free.",
                          thread)
        tries = row["queued_tries"] + 1
        self.store.mark_queued_notice(row["update_id"], told=posted is not None, tries=tries)
        self.log("queued", update_id=row["update_id"], notice="posted" if posted is not None
                 else "not posted" if tries < QUEUED_NOTICE_TRIES else "given up")

    def deliver(self, row, session, target) -> None:
        """Mark sending, journal, send, then mark sent or unknown. Never retried."""
        text, pane = row["text"], target["pane_id"]
        entry_id = uuid.uuid4().hex
        self.store.set_state(row["update_id"], "sending", session_key=session["key"],
                             journal_id=entry_id)
        try:
            _append_line(gate_journal_path(), {
                "id": entry_id, "at": _now_iso(), "target": pane,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "length": len(text), "session": session["key"]})
        except OSError as exc:
            self.store.set_state(row["update_id"], "refused", f"gate journal not written: {exc}")
            self.log("refused", update_id=row["update_id"], reason=str(exc))
            self.say("Not sent: the bridge journal could not be written.", session["thread_id"])
            return
        try:
            self.herdr.send(pane, text)
        except Exception as exc:  # noqa: BLE001 - a send that may have landed is never resent
            outcome = "unknown"
            detail = f"{type(exc).__name__}: {exc}"
        else:
            outcome, detail = "sent", ""
        try:
            _append_line(gate_journal_path(), {"id": entry_id, "outcome": outcome})
        except OSError as exc:
            logger.error("telegram-bridge: gate journal outcome not written: %s", exc)
        self.store.set_state(row["update_id"], outcome, detail)
        self.log(outcome, update_id=row["update_id"], session=session["key"], pane=pane,
                 journal_id=entry_id, text=text, detail=detail)
        if outcome == "sent":
            self.supersede(session, from_telegram=True)
            self.say("Delivered.", session["thread_id"])
        else:
            self.say("Not sure it arrived, please check.", session["thread_id"])

    def handle_callback(self, row, live: dict[str, dict]) -> None:
        now = self.clock()

        def answer(text: str) -> None:
            try:
                self.bot.answer_callback_query(row["callback_id"], text=text)
            except Exception as exc:  # noqa: BLE001 - logged
                logger.info("telegram-bridge: callback answer failed: %s", exc)

        button = self.store.claim_button(row["data"], row["message_id"], now)
        if button is None:
            self._refuse(row, "stale or used button")
            answer("Stale.")
            return
        session = self.store.session(button["session_key"])
        target = live.get(button["session_key"]) if session else None
        if target is None or kill_switch_on() or self.store.get("paused") == "1":
            self._refuse(row, "session gone or paused")
            answer("Not pressed: no such session, or the bridge is paused.")
            return
        if target.get("incarnation") != button["incarnation"]:
            self._refuse(row, "session restarted")
            answer("Stale: the session restarted.")
            return
        pane = target["pane_id"]
        with self.herdr.lock(pane) as held:
            if not held:
                self._refuse(row, "input lock held")
                answer("Not pressed: another process is typing into the session. Press again later.")
                self.store.db.execute("UPDATE buttons SET used_at=NULL WHERE token=?",
                                      (button["token"],))
                return
            try:
                menu = TB.parse_screen(self.herdr.screen(pane))
            except Exception as exc:  # noqa: BLE001 - unreadable screen: nothing pressed
                menu, why = None, str(exc)
            else:
                why = "the menu changed"
            if menu is None or menu.digest != button["menu_sha"] or kill_switch_on():
                self._refuse(row, f"stale: {why}")
                answer("Stale.")
                return
            self.store.set_state(row["update_id"], "sending", session_key=session["key"])
            try:
                self.herdr.keys(pane, [button["key"]])
            except Exception as exc:  # noqa: BLE001 - a key that may have landed is never re-pressed
                self.store.set_state(row["update_id"], "unknown", str(exc))
                self.log("unknown", update_id=row["update_id"], session=session["key"],
                         key=button["key"], detail=str(exc))
                answer("Not sure it was pressed, please check.")
                return
        self.store.set_state(row["update_id"], "sent")
        self.log("pressed", update_id=row["update_id"], session=session["key"], key=button["key"])
        answer("Pressed.")
        self.supersede(session, from_telegram=True)

    # ---------- commands ----------
    def command(self, name: str, session, thread) -> None:
        if name == "/status":
            self.say(self.status_text(), thread)
        elif name == "/pause":
            self.store.put("paused", "1")
            self.say("Paused: nothing is typed into sessions. /resume lifts it.", thread)
        elif name == "/resume":
            if kill_switch_on():
                self.say("The switch on the machine is on; /resume does not lift it.", thread)
            self.store.put("paused", "0")
            self.say("Pause lifted.", thread)
        elif name in ("/mute", "/unmute") and session is not None:
            self.store.upsert_session(session["key"], muted=int(name == "/mute"))
            self.say("Topic muted." if name == "/mute" else "Notifications on.", thread)
        elif name == "/full" and session is not None:
            self.full(session, thread)
        else:
            self.say("Commands: /status, /pause, /resume, /mute, /unmute, /full (in a session topic).",
                     thread)

    def full(self, session, thread) -> None:
        """The whole answer of the turn that was POSTED, not of whatever the transcript holds now."""
        turn = (session["posted_turn"] or "").split(":", 1)[0]
        path = self._last_transcript.get(session["key"])
        if not turn or path is None:
            self.say("Nothing to show: this topic has no open answer.", thread)
            return
        text = TB.turn_answer(Path(path), turn)
        for part in TB.split_parts(text or "(empty)"):
            self.say(part, thread)

    def status_text(self) -> str:
        now = self.clock()
        lines = ["Bridge: " + ("SWITCH on the machine is on" if kill_switch_on()
                             else "paused" if self.store.get("paused") == "1" else "running")]
        for r in self.store.sessions():
            waiting = (f", waiting {int((now - r['waiting_since']) // 60)} min"
                       if r["waiting_since"] else "")
            lines.append(f"- {r['name']} ({'herdr-core' if r['backend'] == 'core' else 'TUI'})"
                         f"{waiting}{', muted' if r['muted'] else ''}"
                         f"{', answer open' if r['post_message_id'] else ''}")
        return "\n".join(lines)

    # ---------- waits and posts ----------
    def supersede(self, session, *, from_telegram: bool) -> None:
        """Close the open post of a session that went on: buttons die, the post says how."""
        row = self.store.session(session["key"])
        if row is None or not row["post_message_id"]:
            return
        self.store.kill_buttons(row["key"], self.clock())
        self.edit(row["post_message_id"],
                  "Answered from Telegram." if from_telegram
                  else "The session went on, not from Telegram (at the terminal or from HELM).")
        self.store.upsert_session(row["key"], post_message_id=None, post_kind=None)

    def watch(self, live: dict[str, dict]) -> None:
        now = self.clock()
        self._last_transcript = {k: v["transcript"] for k, v in live.items()}
        for key, target in live.items():
            row = self.store.session(key)
            if row is None or row["closed_at"] is not None:
                continue
            try:
                status = self.herdr.status(target["pane_id"])
            except Exception as exc:  # noqa: BLE001 - unknown state: nothing posted, nothing closed
                logger.info("telegram-bridge: status of %s unreadable: %s", key, exc)
                continue
            transcript = target.get("transcript")
            turn, answer = (TB.last_turn(transcript) if transcript and transcript.is_file()
                            else (None, ""))
            # A menu post records `<turn>:<menu digest>`, an answer post the bare turn.
            posted = row["posted_turn"] or ""
            new_turn = bool(turn) and posted != turn and not posted.startswith(f"{turn}:")
            if row["post_message_id"] and (status not in WAIT_STATES or new_turn):
                self.supersede(row, from_telegram=self.store.sent_since(key, row["posted_at"] or 0))
                row = self.store.session(key)
            if status not in WAIT_STATES:
                self.store.upsert_session(key, waiting_since=None)
                continue
            if row["waiting_since"] is None:
                self.store.upsert_session(key, waiting_since=now)
                continue
            if now - row["waiting_since"] < self.idle_s or row["muted"]:
                continue
            menu = None
            try:
                menu = TB.parse_screen(self.herdr.screen(target["pane_id"]))
            except Exception as exc:  # noqa: BLE001 - no menu read: no buttons
                logger.info("telegram-bridge: screen of %s unreadable: %s", key, exc)
            if not row["is_helm"] and menu is None:
                continue   # a yard posts only on a question or choice on screen
            if target.get("incarnation") is None or (turn is None and menu is None):
                continue   # transcript or session unknown: no content, no buttons (fail closed)
            post_id = turn if menu is None else f"{turn}:{menu.digest}"
            if row["posted_turn"] == post_id:
                continue
            self.post(row, target, answer, menu, status, post_id)

    def post(self, row, target, answer: str, menu, status: str, post_id: str) -> None:
        now = self.clock()
        minutes = int((now - row["waiting_since"]) // 60)
        short, cut = TB.shorten_answer(answer or "(the session transcript has no answer text)")
        parts = [f"{row['name']} has waited for you {minutes} min.", TB.ANSWER_HEADER, short]
        if cut:
            parts.append("(/full: show it all)")
        markup = None
        tokens: list[tuple[str, str, str]] = []
        if menu is not None:
            # The screen text is capped too, so the post stays under Telegram's 4096
            # characters: POST_LIMIT + MENU_LIMIT + the short labels (HOS-408 review).
            screen = ((menu.detail or menu.question) if menu.kind == "permission" else menu.question) or ""
            if len(screen) > TB.MENU_LIMIT:
                screen = screen[:TB.MENU_LIMIT - 3] + "..."
            if menu.kind == "permission":
                parts += ["Permission request:", screen]
                allow = TB.allow_key(menu)
                if allow:
                    tokens.append((secrets.token_hex(8), "Allow", allow))
                tokens.append((secrets.token_hex(8), "Deny", "esc"))
            else:
                parts += ["Choice on screen:", screen]
                tokens += [(secrets.token_hex(8), f"{d}. {label}"[:60], d) for d, label in menu.options]
            markup = {"inline_keyboard": [[{"text": t, "callback_data": tok}] for tok, t, _ in tokens]}
        message_id = self.say("\n\n".join(parts), row["thread_id"], markup)
        if message_id is None:
            return
        self.store.kill_buttons(row["key"], now)
        for tok, _text, key in tokens:
            self.store.add_button(tok, row["key"], message_id, target["incarnation"],
                                  menu.kind, key, menu.digest, now)
        self.store.upsert_session(row["key"], posted_turn=post_id, post_message_id=message_id,
                                  post_kind=menu.kind if menu else "answer", posted_at=now)
        self.log("posted", session=row["key"], message_id=message_id, kind=status,
                 buttons=len(tokens))

    # ---------- the loop ----------
    def recover(self) -> None:
        """At start: a message left in `sending` by a crash is `unknown`, never retried."""
        for row in self.store.orphaned_sends():
            self.store.set_state(row["update_id"], "unknown", "bridge stopped mid-send")
            self.log("unknown", update_id=row["update_id"], reason="bridge stopped mid-send")
            session = self.store.session(row["session_key"]) if row["session_key"] else None
            self.say("Not sure it arrived, please check.", session["thread_id"] if session else None)

    def cycle(self, poll_timeout: int = POLL_TIMEOUT_S) -> None:
        self.take_updates(poll_timeout)
        if self.in_conflict():
            return   # another reader polls this bot: nothing typed, pressed or posted (HOS-408 review)
        live = self.refresh_sessions()
        self.handle_inbound(live)
        self.watch(live)


# ============================================================
# Configuration and the CLI
# ============================================================

def _refuse_in_yard(what: str) -> None:
    if os.environ.get("HEADING_OS_YARD"):
        print(f"telegram-bridge: REFUSED. {what} runs in HELM only; this session carries "
              f"HEADING_OS_YARD. Nothing started.", file=sys.stderr)
        sys.exit(EXIT_REFUSED)
    from scripts.utils.clone_guard import require_main_clone
    require_main_clone(__file__)


def _config() -> tuple[str, int, int]:
    """(token, operator id, idle minutes) from the environment; exit 4 when unusable."""
    from scripts.utils.paths import load_env
    from scripts.utils.telegram_notify import own_targets
    load_env()
    token = os.environ.get("TELEGRAM_NOTIFY_BOT_TOKEN", "").strip()
    raw = os.environ.get("TELEGRAM_BRIDGE_OPERATOR_ID", "").strip()
    if not token or not raw.isdigit():
        print("telegram-bridge: TELEGRAM_NOTIFY_BOT_TOKEN and TELEGRAM_BRIDGE_OPERATOR_ID "
              "(his numeric user id) must be set in .env.", file=sys.stderr)
        sys.exit(EXIT_UNCONFIGURED)
    if raw not in own_targets():
        print("telegram-bridge: TELEGRAM_BRIDGE_OPERATOR_ID is not one of the operator's own "
              "sinks (HEADING_OS_SELF_TELEGRAM_TARGET or a *_TELEGRAM_TARGET); nothing will "
              "be sent to it.", file=sys.stderr)
        sys.exit(EXIT_UNCONFIGURED)
    try:
        idle = int(os.environ.get("TELEGRAM_BRIDGE_IDLE_MINUTES", TB.DEFAULT_IDLE_MINUTES))
    except ValueError:
        logger.warning("telegram-bridge: TELEGRAM_BRIDGE_IDLE_MINUTES is not a number; using %d",
                       TB.DEFAULT_IDLE_MINUTES)
        idle = TB.DEFAULT_IDLE_MINUTES
    return token, int(raw), max(1, min(idle, 120))


def _bot(token: str):
    from scripts.utils.telegram_bot import TelegramBot
    return TelegramBot(token, on_error=lambda m: logger.warning("telegram-bridge: %s", m))


def _preflight(bot) -> None:
    """Topics on, no webhook; exit 4 otherwise. getMe returns BotFullInfo (DOCUMENTED, getme.md)."""
    me = bot.get_me()
    if not (isinstance(me, dict) and me.get("has_topics_enabled")):
        print("telegram-bridge: topic mode is off for this bot (getMe has_topics_enabled is "
              "not true). Run `telegram-bridge.py enable-topics` in HELM first.", file=sys.stderr)
        sys.exit(EXIT_UNCONFIGURED)
    hook = bot.get_webhook_info()
    if isinstance(hook, dict) and hook.get("url"):
        print("telegram-bridge: a webhook is set on this bot, so getUpdates cannot be used. "
              "Nothing started.", file=sys.stderr)
        sys.exit(EXIT_UNCONFIGURED)


def _open_store(create: bool) -> TB.Store:
    try:
        directory = state_dir(create=create)
    except Exception as exc:  # noqa: BLE001 - the registry row is missing: say so
        print(f"telegram-bridge: no state directory ({exc}). The data registry needs the "
              f"`telegram-bridge` rows first.", file=sys.stderr)
        sys.exit(EXIT_UNCONFIGURED)
    return TB.Store(directory / "bridge.db")


def _setup_logging() -> None:
    from scripts.utils import tracing
    from scripts.utils.trace_filter import attach
    tracing.mint()
    logger.addHandler(logging.StreamHandler(sys.stderr))   # journald, through the unit
    logger.propagate = False
    attach(logger)
    logger.setLevel(logging.INFO)


def _start_lock(sdir: Path):
    """The start lock, held for the life of the process, or None when another instance holds it.

    The daemon and `run-once` both take it, so a manual cycle never runs beside
    the daemon: its `recover()` would mark the daemon's active send unknown and
    a pending row could be delivered twice (HOS-408 review round 1).
    """
    handle = (sdir / START_LOCK).open("w", encoding="utf-8")  # noqa: SIM115 - held for the life
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        print("telegram-bridge: another instance holds the start lock. Nothing done.", file=sys.stderr)
        return None
    return handle


def cmd_daemon(args) -> int:
    _refuse_in_yard("The bridge daemon")
    _setup_logging()
    token, operator, idle = _config()
    bot = _bot(token)
    _preflight(bot)
    sdir = state_dir(create=True)
    handle = _start_lock(sdir)
    if handle is None:
        return EXIT_RUNNING
    (sdir / PID_FILE).write_text(str(os.getpid()), encoding="utf-8")
    from scripts.utils.clone_guard import main_clone_path
    from scripts.utils.systemd_notify import sd_notify
    store = _open_store(create=True)
    bridge = Bridge(store, bot, HerdrSeam, operator_id=operator, helm=main_clone_path(WORKSPACE),
                    idle_minutes=idle)
    bridge.recover()
    sd_notify("READY=1", logger)
    while True:
        try:
            bridge.cycle(25 if args.long_poll else POLL_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - one bad cycle never ends the daemon; logged in full
            logger.exception("telegram-bridge: cycle failed")
            time.sleep(10)
        sd_notify("WATCHDOG=1", logger)


def cmd_run_once(args) -> int:
    _refuse_in_yard("A bridge cycle")
    _setup_logging()
    token, operator, idle = _config()
    bot = _bot(token)
    _preflight(bot)
    handle = _start_lock(state_dir(create=True))
    if handle is None:
        return EXIT_RUNNING
    from scripts.utils.clone_guard import main_clone_path
    bridge = Bridge(_open_store(create=True), bot, HerdrSeam, operator_id=operator,
                    helm=main_clone_path(WORKSPACE), idle_minutes=idle)
    bridge.recover()
    bridge.cycle(0)
    return 0


def cmd_status(args) -> int:
    print(f"kill switch: {'ON (nothing is typed)' if kill_switch_on() else 'off'}")
    try:
        directory = state_dir(create=False)
    except Exception as exc:  # noqa: BLE001 - reported
        print(f"state: unavailable ({exc})")
        return 0
    db = directory / "bridge.db"
    if not db.is_file():
        print(f"state: no store yet at {db}")
    else:
        store = TB.Store(db)
        print(f"paused: {store.get('paused') == '1'}; offset: {store.get('offset', '0')}")
        for r in store.sessions():
            print(f"  {r['name']:<32} {r['backend']:<4} {r['pane']:<8} thread {r['thread_id']} "
                  f"{'muted ' if r['muted'] else ''}{'post open' if r['post_message_id'] else ''}")
        counts = store.db.execute("SELECT state, COUNT(*) n FROM inbound GROUP BY state").fetchall()
        print("inbound: " + (", ".join(f"{c['state']} {c['n']}" for c in counts) or "none"))
        store.close()
    if args.live:
        _refuse_in_yard("`status --live`")
        token, _operator, _idle = _config()
        bot = _bot(token)
        me, hook = bot.get_me(), bot.get_webhook_info()
        print(f"getMe: @{me.get('username')} has_topics_enabled={me.get('has_topics_enabled')}")
        print(f"webhook: {'SET' if hook.get('url') else 'none'}; pending {hook.get('pending_update_count')}")
    return 0


def cmd_off(args) -> int:
    _refuse_in_yard("The kill switch")   # it changes what a live daemon does (M029.1)
    path = state_dir(create=True) / KILL_SWITCH
    path.write_text(f"set {_now_iso()} by telegram-bridge.py off\n", encoding="utf-8")
    print(f"kill switch ON: {path}. Nothing is typed into any session until `on` in HELM.")
    return 0


def cmd_on(args) -> int:
    _refuse_in_yard("Removing the kill switch")
    path = state_dir(create=False) / KILL_SWITCH
    if path.exists():
        path.unlink()
    print("kill switch off.")
    return 0


def cmd_journal(args) -> int:
    path = log_dir(create=False) / "inputs.jsonl"
    if not path.is_file():
        print("no input journal yet")
        return 0
    for line in path.read_text(encoding="utf-8").splitlines()[-args.n:]:
        print(line)
    return 0


def cmd_enable_topics(args) -> int:
    _refuse_in_yard("enable-topics")
    token, _operator, _idle = _config()
    bot = _bot(token)
    bot.set_my_settings(has_topics_enabled=True, allows_users_to_create_topics=False)
    me = bot.get_me()
    print(f"has_topics_enabled={me.get('has_topics_enabled')} "
          f"allows_users_to_create_topics={me.get('allows_users_to_create_topics')}")
    return 0 if me.get("has_topics_enabled") else EXIT_UNCONFIGURED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="telegram-bridge.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("daemon", help="the loop (HELM only; the unit runs it)")
    d.add_argument("--long-poll", action="store_true", help="getUpdates timeout 25 s instead of 5")
    sub.add_parser("run-once", help="one cycle (HELM only)")
    s = sub.add_parser("status", help="state from the store; --live adds getMe (HELM only)")
    s.add_argument("--live", action="store_true")
    sub.add_parser("off", help="kill switch on: nothing is typed")
    sub.add_parser("on", help="kill switch off (HELM only)")
    j = sub.add_parser("journal", help="the last lines of the input journal")
    j.add_argument("-n", type=int, default=20)
    sub.add_parser("enable-topics", help="setMySettings(has_topics_enabled=True) (HELM only)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return {"daemon": cmd_daemon, "run-once": cmd_run_once, "status": cmd_status, "off": cmd_off,
            "on": cmd_on, "journal": cmd_journal, "enable-topics": cmd_enable_topics}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
