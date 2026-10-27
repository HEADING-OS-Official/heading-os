#!/usr/bin/env python3
"""
Sentinel -- Unified Comms Monitor for HEADING OS

Continuously monitors corporate email (Exchange EWS) and Telegram,
analyzes incoming messages for urgency, and sends critical items to a
dedicated Telegram channel.

Two switches in sentinel_config.yaml, both defaulting to the original
behaviour:
    llm.provider   anthropic (Claude) | qwen (a private model through the
                   local proxy, with the model-free rule of
                   scripts/sentinel_rule.py while it is down)
    general.mode   legacy (every source alerts, two digests) | guardian
                   (email alerts in real time, Telegram to the digest only,
                   a digest at each time in digest.times, with "matters"
                   and "awaits your reply" read against Sent Items)

Optional: llm.thresholds_by_model maps the upstream model name the proxy
reports (the response's "model" field) to the alert threshold for scores that
model gave; a model absent from it uses general.urgency_threshold.

Prerequisites:
    uv sync (exchangelib, telethon, anthropic and pyyaml are engine dependencies)

Usage:
    python scripts/sentinel.py              # run daemon (foreground)
    python scripts/sentinel.py --test       # single cycle, dry-run
    python scripts/sentinel.py --check      # one live cycle, then exit
    python scripts/sentinel.py --status     # check if running
    python scripts/sentinel.py --stop       # stop running daemon
    python scripts/sentinel.py --daemon     # detached background process
    python scripts/sentinel.py --config F   # another config file
"""

import argparse
import asyncio
import contextlib
import fnmatch
import hashlib
import io
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from scripts import sentinel_rule  # noqa: E402
from scripts.utils import claude_models
from scripts.utils.user_agent import user_agent
from scripts.utils import daemon_heartbeat  # noqa: E402
from scripts.utils import telegram_notify  # noqa: E402
from scripts.utils import tracing  # noqa: E402
from scripts.utils.healthchecks import ping as hc_ping  # noqa: E402
from scripts.utils.html_text import email_body_text  # noqa: E402
from scripts.utils.llm_fallback import ClaudeStopped, call_anthropic_with_fallback  # noqa: E402
from scripts.utils.observability import observe  # noqa: E402
from scripts.utils.operator_identity import get_operator  # noqa: E402
from scripts.utils.pid_liveness import pid_runs_program  # noqa: E402
from scripts.utils.trace_filter import install_log_factory  # noqa: E402
from scripts.utils.paths import DataRootError, get_data_root  # noqa: E402
from scripts.utils.untrusted_input import (  # noqa: E402
    UNTRUSTED_SYSTEM_NOTE, sanitize_untrusted, wrap_untrusted)
from scripts.utils.workspace import get_default_tz, get_default_tz_name, get_workspace_root, load_env, resolve_config_with_example  # noqa: E402
from scripts.utils.data_path import (  # noqa: E402
    DataPathError, config_names, data_write_dir, deprecated_config_path)
from scripts.utils.clone_guard import require_main_clone


def _configure_session_wal(client, busy_timeout_ms=30000):
    """Set WAL journal mode and busy_timeout on the session's sqlite3 connection.

    WAL allows concurrent reads while writes proceed, preventing 'database is
    locked' errors between Sentinel, the telegram skill client, and the assistant-inbox reader.
    Monkey-patches _cursor() so pragmas survive connection recycling.
    """
    session = client.session
    original_cursor = session._cursor

    def _patched_cursor():
        was_none = session._conn is None
        cursor = original_cursor()
        if was_none and session._conn is not None:
            session._conn.execute(f'PRAGMA busy_timeout = {int(busy_timeout_ms)}')
            session._conn.execute('PRAGMA journal_mode = WAL')
        return cursor

    session._cursor = _patched_cursor

    # Apply immediately if connection already exists
    conn = getattr(session, '_conn', None)
    if conn is not None:
        conn.execute(f'PRAGMA busy_timeout = {int(busy_timeout_ms)}')
        conn.execute('PRAGMA journal_mode = WAL')

# --- Paths ---
WORKSPACE_ROOT = get_workspace_root()
ENV_FILE = WORKSPACE_ROOT / ".env"
# The state, log and pid, bound by `_bind_runtime_files()` in `main()` once the
# `.env` naming the data root has been read: `runtime:sentinel/state`, `/logs`
# and `/tmp` in the data repository, never the engine checkout (HOS-5 section 7).
STATE_FILE: Path
LOG_FILE: Path
PID_FILE: Path


def _bind_runtime_files() -> None:
    """Resolve and create the three files' directories, or refuse: never the engine checkout."""
    global STATE_FILE, LOG_FILE, PID_FILE
    try:
        state = data_write_dir("runtime:sentinel/state")
        logs = data_write_dir("runtime:sentinel/logs")
        tmp = data_write_dir("runtime:sentinel/tmp")
    except DataPathError as exc:
        print(f"Sentinel: no data repository to keep its state, log and pid in "
              f"({exc}); refusing", file=sys.stderr)
        sys.exit(2)
    STATE_FILE = state / "state.json"
    LOG_FILE = logs / "sentinel.log"
    PID_FILE = tmp / "sentinel.pid"
TELEGRAM_SESSION_DIR = WORKSPACE_ROOT / ".sessions" / "telegram"
TELEGRAM_SESSION_PATH = TELEGRAM_SESSION_DIR / "telegram"


# Config-DATA: the real config lives in the data overlay (config/sentinel_config.yaml,
# resolved via get_data_config_dir()); the engine ships sentinel_config.example.yaml
# as the fallback so a data-less clone runs out of the box.
def config_file() -> Path:
    """Resolved at call time, never at import.

    `resolve_config_with_example()` reads `HEADING_OS_DATA` on every call, so it
    follows the environment for a caller that asks after the environment moved.
    As a module-level constant it asked once, during this module's own import,
    and stored the answer, so a test that imported this module and then
    repointed the root still resolved the operator's real overlay. Every caller
    resolves the config once, at construction, so a long-running cycle cannot
    see it change mid-run.
    """
    return resolve_config_with_example(
        "sentinel_config.yaml", WORKSPACE_ROOT / "scripts" / "sentinel_config.example.yaml"
    )


# --- Fix Windows console encoding ---
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# The `.env` is loaded in `main()`, never at import: readers import this module
# for one function and must not have their environment rewritten.


# Body extraction and HTML stripping: see scripts/utils/html_text.py.
# `strip_html` is no longer imported here - this file called it only to
# build an email body, and that extraction moved to `email_body_text`,
# which redacts credential spans before the body can be persisted.


# ============================================================
# Configuration
# ============================================================

class SentinelConfig:
    """Load and validate sentinel_config.yaml."""

    def __init__(self, config_path: Path | None = None):
        config_path = config_file() if config_path is None else config_path
        if not config_path.exists():
            raise FileNotFoundError(f"Config not found: {config_path}")
        with open(config_path, "r", encoding="utf-8") as f:
            self._raw = yaml.safe_load(f)

        g = self._raw.get("general", {})
        self.check_interval = g.get("check_interval_minutes", 15) * 60  # seconds
        self.urgency_threshold = g.get("urgency_threshold", 7)
        self.timezone = ZoneInfo(g.get("timezone", get_default_tz_name()))
        self.log_level = g.get("log_level", "INFO")
        # `legacy` (the default) keeps the behaviour this daemon has always had:
        # every source alerts in real time and two fixed digests. `guardian` is
        # the redesign: real-time alerts for EMAIL only, Telegram in the digest
        # only, and a digest at each time in `digest.times`. A merge of the
        # redesign changes nothing until a deployment sets it.
        self.mode = str(g.get("mode") or "legacy").strip().lower()
        if self.mode not in ("legacy", "guardian"):
            raise ValueError(f"general.mode must be legacy or guardian, not {self.mode!r}")
        # The wake threshold while the model-free rule scores (the model is down).
        # Lower than `urgency_threshold` on purpose: the rule misses more, and
        # every item it scores is re-scored by the model when it returns.
        self.rule_wake_threshold = g.get("rule_wake_threshold", 6)

        self.email = self._raw.get("email", {})
        self.telegram = self._raw.get("telegram", {})
        self.digest = self._raw.get("digest", {})
        self.notification = self._raw.get("notification", {})
        self.llm = self._raw.get("llm", {})
        self.calendar = self._raw.get("calendar", {})
        # Two models behind one proxy alias score on different scales, so the
        # alert bar follows the model that actually answered.
        by_model = self.llm.get("thresholds_by_model") or {}
        if not isinstance(by_model, dict):
            raise ValueError("llm.thresholds_by_model must be a mapping of model name to 1-10")
        for name, bar in by_model.items():
            if isinstance(bar, bool) or not isinstance(bar, int) or not 1 <= bar <= 10:
                raise ValueError(f"llm.thresholds_by_model[{name!r}] must be an integer 1-10, not {bar!r}")
        self.thresholds_by_model = {str(k): v for k, v in by_model.items()}

    def threshold_for(self, model) -> int:
        """The alert threshold for a score `model` gave; urgency_threshold when unmapped."""
        return self.thresholds_by_model.get(model or "", self.urgency_threshold)


# How many medium-priority rows the evening digest prints. A constant so the
# heading and the slice cannot say different numbers: the heading names it.
MEDIUM_DIGEST_ROWS = 5


def local_stamp(dt) -> str:
    """A timestamp an operator can read: configured zone, and its label.

    Every item Sentinel raises used to carry a bare UTC wall clock with the
    offset deleted. The email path did it by slicing (`str(dt)[:19]` cuts
    `+00:00` off the end) and the two Telegram paths did it explicitly
    (`datetime.now(timezone.utc).isoformat()[:19]`). That string is then printed
    straight into the Telegram alert card and fed to the urgency model as
    `DATE:`, with nothing anywhere naming a zone.

    On this operator's clock that is four hours early, and before 04:00 local it
    is also the WRONG DAY: a VIP message that arrived at 01:34 was announced as
    yesterday 21:34. Recency is how a person judges urgency, so the one field an
    urgency monitor must get right was the field it silently shifted. The daemon
    that sends the alert already schedules on the configured zone
    (`datetime.now(self.config.timezone)`), so the alert body was on a different
    clock from the daemon that sent it.

    The same class was corrected elsewhere in this workspace with the same
    reasoning: `scripts/utils/checkpoint_paths.py::local_now` records that every
    artifact written between midnight and 04:00 local landed on yesterday's
    date, "which is exactly when this operator works".

    A naive datetime is READ AS UTC rather than guessed: every producer here is
    UTC-aware, so a naive one means something upstream dropped the zone, and
    treating it as local would silently invent a four-hour correction.
    """
    if dt is None:
        return ""
    if not hasattr(dt, "astimezone"):
        # Not a datetime. Every real producer here hands over an aware one, so
        # this is a shape nobody expects - but a fetch loop that raises on one
        # odd field drops the whole cycle, and a monitor that goes quiet is the
        # worst failure it has. Render what arrived and move on. The old
        # `str(dt)[:19]` swallowed this by accident; it is deliberate now.
        return str(dt)
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_default_tz()).strftime("%Y-%m-%d %H:%M:%S %Z")


# ============================================================
# State Manager
# ============================================================

class StateManager:
    """Persistent state tracking to avoid duplicate processing.

    `read_only` makes `save()` a no-op. A dry run still marks items in memory --
    that is what stops one cycle from processing the same message twice -- but
    it must not PERSIST those marks: doing so meant `--test` permanently
    consumed real state, and the production daemon was then blind to every
    message and invite the test had seen, with nothing said about it.
    """

    def __init__(self, state_path: Path | None = None, read_only: bool = False):
        self.path = STATE_FILE if state_path is None else state_path
        self.read_only = read_only
        self.data = self._load()

    def _load(self) -> dict:
        """The state file, with every required section guaranteed present.

        A file that parses is not a file that fits. A hand-edited, truncated or
        older-schema state.json was returned verbatim until 2026-08-25, and the
        first cycle then died on a bare subscript: `data["email"]["processed_ids"]`
        at is_email_processed, `data["digest"]` at record_digest_item,
        `data["email"]["last_check"]` in run_cycle. `save()` has no schema guard
        either, so once a short dict was in memory the daemon persisted it and
        cemented the shape.

        The `version` field made this look handled and did not: it is written
        here and read nowhere in the file, so it can neither gate a migration
        nor detect one. The merge below is what actually holds the contract, so
        a missing section is filled rather than discovered at the call site.
        """
        loaded = None
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
            except (json.JSONDecodeError, OSError):
                loaded = None
        skeleton = self._default_state()
        if not isinstance(loaded, dict):
            return skeleton
        for key, default in skeleton.items():
            if key not in loaded:
                loaded[key] = default
            elif isinstance(default, dict) and isinstance(loaded[key], dict):
                # One level down is enough: no required key sits deeper.
                for sub_key, sub_default in default.items():
                    loaded[key].setdefault(sub_key, sub_default)
            elif isinstance(default, dict) and not isinstance(loaded[key], dict):
                # A section of the wrong TYPE would subscript as badly as a
                # missing one, so it is replaced rather than merged.
                loaded[key] = default
        return loaded

    @staticmethod
    def _default_state() -> dict:
        return {
            "version": 2,
            "last_run": None,
            "email": {"processed_ids": [], "last_check": None},
            "telegram": {"per_chat": {}, "last_check": None},
            "notified_hashes": {},
            "digest": {
                "today": None,
                "emails_checked": 0,
                "tg_messages_checked": 0,
                "urgent_sent": 0,
                "items_by_urgency": [],
            },
            "calendar": {
                "processed_invite_ids": [],
                "decisions_today": [],
            },
            # The redesign's memory. `items` is a rolling record of everything
            # scored, across midnight, for the digest. `rule_items` are the full
            # items the fallback rule scored, kept for the model to re-score.
            # `unscored` counts attempts per content hash for an answer the
            # model gave but nobody could use (cut off by max_tokens, not JSON).
            "guardian": {
                "items": [],
                "rule_items": [],
                "unscored": {},
                "gave_up": [],
                "sent_slots": [],
                "last_digest_at": None,
                "tg_last_out": {},
                "sender_seen": {},
                "waiting_for_model": 0,
            },
        }

    def save(self):
        if self.read_only:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, default=str)
        os.replace(tmp, self.path)

    def is_email_processed(self, message_id: str) -> bool:
        return message_id in self.data["email"]["processed_ids"]

    def mark_email_processed(self, message_id: str):
        ids = self.data["email"]["processed_ids"]
        if message_id not in ids:
            ids.append(message_id)
        # Keep only last 500 to prevent unbounded growth
        if len(ids) > 500:
            self.data["email"]["processed_ids"] = ids[-500:]

    def rotate_old_state(self, max_age_days: int = 30):
        """Purge stale entries from processed_ids and invite_ids based on age.

        Since message IDs don't carry timestamps, we trim by count more
        aggressively when the list is large, and purge notified_hashes by age.
        """
        # Trim email processed_ids to last 300 if over 400
        ids = self.data["email"].get("processed_ids", [])
        if len(ids) > 400:
            self.data["email"]["processed_ids"] = ids[-300:]

        # Trim calendar invite_ids
        cal = self.data.get("calendar", {})
        invite_ids = cal.get("processed_invite_ids", [])
        if len(invite_ids) > 150:
            cal["processed_invite_ids"] = invite_ids[-100:]

        # Purge old notified_hashes (already has cleanup_old_hashes, but
        # this provides a broader sweep for any hashes older than max_age_days)
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        to_remove = []
        for h, ts in self.data.get("notified_hashes", {}).items():
            try:
                dt = datetime.fromisoformat(ts)
                if dt < cutoff:
                    to_remove.append(h)
            except (ValueError, TypeError):
                to_remove.append(h)
        for h in to_remove:
            del self.data["notified_hashes"][h]

        # Purge old digest items
        digest = self.data.get("digest", {})
        digest_items = digest.get("items_by_urgency", [])
        if len(digest_items) > 150:
            digest["items_by_urgency"] = digest_items[-100:]

    def get_telegram_last_id(self, chat_id: str) -> int:
        chat_data = self.data["telegram"]["per_chat"].get(str(chat_id), {})
        return chat_data.get("last_message_id", 0)

    def set_telegram_last_id(self, chat_id: str, name: str, msg_id: int):
        self.data["telegram"]["per_chat"][str(chat_id)] = {
            "name": name,
            "last_message_id": msg_id,
            "last_check": datetime.now(timezone.utc).isoformat(),
        }

    def is_already_notified(self, content_hash: str) -> bool:
        return content_hash in self.data["notified_hashes"]

    def mark_notified(self, content_hash: str):
        self.data["notified_hashes"][content_hash] = datetime.now(timezone.utc).isoformat()

    def cleanup_old_hashes(self, max_age_minutes: int):
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        to_remove = []
        for h, ts in self.data["notified_hashes"].items():
            try:
                dt = datetime.fromisoformat(ts)
                if dt < cutoff:
                    to_remove.append(h)
            except (ValueError, TypeError):
                to_remove.append(h)
        for h in to_remove:
            del self.data["notified_hashes"][h]

    def reset_daily_digest(self, today_str: str):
        if self.data["digest"].get("today") != today_str:
            self.data["digest"] = {
                "today": today_str,
                "emails_checked": 0,
                "tg_messages_checked": 0,
                "urgent_sent": 0,
                "items_by_urgency": [],
            }

    def record_digest_item(self, item: dict, urgency_score: int):
        self.data["digest"]["items_by_urgency"].append({
            "source": item.get("source", "unknown"),
            "sender": item.get("sender", "unknown"),
            "subject": item.get("subject", ""),
            "urgency": urgency_score,
            "time": datetime.now(timezone.utc).isoformat(),
        })
        # Cap at 200 items per day
        if len(self.data["digest"]["items_by_urgency"]) > 200:
            self.data["digest"]["items_by_urgency"] = self.data["digest"]["items_by_urgency"][-200:]

    # --- Guardian / Digest memory ---

    GUARDIAN_ITEM_CAP = 400
    RULE_ITEM_CAP = 200

    def record_guardian_item(self, item: dict, score, analysis: dict | None,
                             scored_by: str, notified: bool, threshold=None):
        """One scored (or given-up) item, for the digest. Metadata only: no body."""
        g = self.data["guardian"]
        g["items"].append({
            "key": item.get("_hash"),
            "source": item.get("source", "unknown"),
            "sender": item.get("sender", "unknown"),
            "sender_email": item.get("sender_email", ""),
            "subject": item.get("subject", ""),
            "conversation_id": (item.get("rule_meta") or {}).get("conversation_id"),
            "chat_id": item.get("chat_id"),
            "seen_at": item.get("_seen_at") or datetime.now(timezone.utc).isoformat(),
            "urgency": score,
            "action": (analysis or {}).get("recommended_action", ""),
            "scored_by": scored_by,
            "scored_model": (analysis or {}).get("scored_model"),
            "threshold": threshold,
            "notified": bool(notified),
        })
        if len(g["items"]) > self.GUARDIAN_ITEM_CAP:
            g["items"] = g["items"][-self.GUARDIAN_ITEM_CAP:]

    def update_guardian_item(self, key: str, **fields):
        for rec in reversed(self.data["guardian"]["items"]):
            if rec.get("key") == key:
                rec.update(fields)
                return

    def prune_guardian(self, keep_days: int):
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        g = self.data["guardian"]

        def _recent(iso):
            try:
                dt = datetime.fromisoformat(iso)
            except (TypeError, ValueError):
                return False
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt >= cutoff

        g["items"] = [r for r in g["items"] if _recent(r.get("seen_at"))]
        g["gave_up"] = [r for r in g["gave_up"] if _recent(r.get("seen_at"))]
        week = datetime.now(timezone.utc) - timedelta(days=8)
        for sender, times in list(g["sender_seen"].items()):
            kept = [t for t in times if _recent(t) and datetime.fromisoformat(t) >= week]
            if kept:
                g["sender_seen"][sender] = kept[-60:]
            else:
                del g["sender_seen"][sender]
        g["sent_slots"] = g["sent_slots"][-20:]

    def note_telegram_out(self, chat_id: str, when) -> None:
        """The newest moment the operator wrote in a Telegram chat."""
        if when is None or not chat_id:
            return
        if getattr(when, "tzinfo", None) is None:
            when = when.replace(tzinfo=timezone.utc)
        g = self.data["guardian"]["tg_last_out"]
        prev = g.get(str(chat_id))
        if prev is None or datetime.fromisoformat(prev) < when:
            g[str(chat_id)] = when.isoformat()

    # --- Calendar invite tracking ---

    def is_invite_processed(self, invite_id: str) -> bool:
        cal = self.data.get("calendar", {})
        return invite_id in cal.get("processed_invite_ids", [])

    def mark_invite_processed(self, invite_id: str):
        cal = self.data.setdefault("calendar", {"processed_invite_ids": [], "decisions_today": []})
        ids = cal.setdefault("processed_invite_ids", [])
        if invite_id not in ids:
            ids.append(invite_id)
        if len(ids) > 200:
            cal["processed_invite_ids"] = ids[-200:]

    def record_invite_decision(self, invite_id: str, subject: str, decision: str, reasons: list):
        cal = self.data.setdefault("calendar", {"processed_invite_ids": [], "decisions_today": []})
        decisions = cal.setdefault("decisions_today", [])
        decisions.append({
            "invite_id": invite_id,
            "subject": subject,
            "decision": decision,
            "reasons": reasons,
            "time": datetime.now(timezone.utc).isoformat(),
        })

    def reset_calendar_daily(self, today_str: str):
        cal = self.data.setdefault("calendar", {"processed_invite_ids": [], "decisions_today": []})
        if cal.get("today") != today_str:
            cal["decisions_today"] = []
            cal["today"] = today_str


# ============================================================
# Email Source
# ============================================================

# How far a cycle will page into the unread backlog before giving up and
# SAYING so. Bounds the cost of a mailbox with thousands of unread items;
# the warning is what keeps a partial scan from reading as a full one.
BACKLOG_SCAN_CAP = 500


class EmailSource:
    """Fetch new emails from Exchange EWS."""

    def __init__(self, config: dict, state: StateManager, logger: logging.Logger):
        self.config = config
        self.state = state
        self.logger = logger
        self.account = None

    def connect(self):
        from exchangelib import Account, Configuration, Credentials, DELEGATE

        email = os.getenv("EXCHANGE_EMAIL")
        password = os.getenv("EXCHANGE_PASSWORD")
        server = os.getenv("EXCHANGE_SERVER")
        username = os.getenv("EXCHANGE_USERNAME", email)

        if not all([email, password, server]):
            raise ValueError("Missing Exchange credentials in .env")

        credentials = Credentials(username=username, password=password)
        # exchangelib sends its own agent unless told otherwise; the workspace one
        # lives in config/user-agent.txt (scripts/utils/user_agent.py).
        from exchangelib.protocol import BaseProtocol
        BaseProtocol.USERAGENT = user_agent()
        exchange_config = Configuration(server=server, credentials=credentials)
        self.account = Account(
            primary_smtp_address=email,
            config=exchange_config,
            autodiscover=False,
            access_type=DELEGATE,
        )
        self.logger.info(f"Exchange connected as {email}")

    def check_new(self) -> list:
        if not self.account:
            self.connect()

        folder_name = self.config.get("folder", "Inbox")
        if folder_name.lower() == "inbox":
            folder = self.account.inbox
        elif folder_name.lower() == "sent":
            folder = self.account.sent
        else:
            folder = self.account.inbox / folder_name

        max_count = self.config.get("max_per_check", 50)
        unread = folder.filter(is_read=False)

        # Read from BOTH ends of the backlog, not just the newest end.
        #
        # Sentinel never marks mail read on the server, so the `is_read=False`
        # set only grows. A newest-first slice of `max_count` therefore pins a
        # window over the newest N: once those N are all in `processed_ids`,
        # every later cycle re-fetches the same N, skips every one of them, and
        # logs "0 new unread messages" while older unread mail sits permanently
        # outside the window. Nothing ages back INTO a newest-first slice.
        #
        # Newest-first stays first because this is an urgency daemon and fresh
        # mail must not queue behind a backlog. The oldest window is added only
        # when a backlog actually exists, so the ordinary case costs one count
        # query and nothing else.
        try:
            unread_total = unread.count()
        except Exception as exc:  # noqa: BLE001 - a count failure must not stop the cycle
            self.logger.warning(f"Email: could not count unread ({exc}); "
                                f"reading the newest {max_count} only")
            unread_total = None

        items = list(unread.order_by("-datetime_received")[:max_count])
        if unread_total is not None and unread_total > max_count:
            # Walk from the OLDEST end, skipping what this daemon has already
            # handled, until `max_count` genuinely-unseen items are in hand.
            #
            # A second FIXED window does not close the hole: with 120 unread and
            # max_count 50, the newest 50 and the oldest 50 leave 20 in the
            # middle that no cycle ever reaches, because a processed item stays
            # unread on the server and never leaves either slice. Skipping the
            # processed ones as we page is what makes the window ADVANCE.
            # Measured 2026-08-26 over a 120-item stand-in: two fixed windows
            # reached 100 of 120 and stopped; this reaches all 120.
            seen_ids = {it.id for it in items}
            backlog, scanned = [], 0
            for older in unread.order_by("datetime_received"):
                scanned += 1
                if scanned > BACKLOG_SCAN_CAP:
                    self.logger.warning(
                        f"Email: stopped scanning the backlog after "
                        f"{BACKLOG_SCAN_CAP} items; older unread mail was NOT "
                        f"examined this cycle")
                    break
                if older.id in seen_ids:
                    continue
                if self.state.is_email_processed(str(older.message_id or older.id)):
                    continue
                backlog.append(older)
                if len(backlog) >= max_count:
                    break
            items.extend(backlog)
            self.logger.info(
                f"Email: {unread_total} unread on the server; reading the "
                f"newest {max_count} and {len(backlog)} unseen from the backlog")

        new_items = []
        for email_item in items:
            msg_id = str(email_item.message_id or email_item.id)
            if self.state.is_email_processed(msg_id):
                continue

            sender_addr = ""
            sender_name = ""
            if email_item.sender:
                sender_addr = str(email_item.sender.email_address or "")
                sender_name = str(email_item.sender.name or sender_addr)

            if self._is_ignored(sender_addr):
                self.state.mark_email_processed(msg_id)
                continue

            # Extract body. Shared, and redacted before it can be persisted:
            # this dict is written to the sentinel state file and into queue
            # cards, both of which live in the DATA overlay and are pushed.
            body = email_body_text(email_item)

            if len(body) > 2000:
                body = body[:2000] + "\n[...truncated]"

            # Extract attachments
            attachments = []
            if email_item.has_attachments and email_item.attachments:
                attachments = [a.name for a in email_item.attachments if hasattr(a, "name") and a.name]

            date_str = local_stamp(email_item.datetime_received)

            # Header facts the fallback rule scores on (scripts/sentinel_rule.py)
            # and the digest matches replies by. Read from the item already in
            # hand; no extra request, nothing written back to the server.
            received = email_item.datetime_received
            subj = str(email_item.subject or "")
            headers = getattr(email_item, "headers", None)
            rule_meta = {
                "received": _utc(received).isoformat() if received else None,
                "conversation_id": (email_item.conversation_id.id
                                    if getattr(email_item, "conversation_id", None) else None),
                "to": _addresses(getattr(email_item, "to_recipients", None)),
                "cc": _addresses(getattr(email_item, "cc_recipients", None)),
                "has_attachments": bool(email_item.has_attachments),
                "is_reply": bool(getattr(email_item, "in_reply_to", None)),
                "list_unsubscribe": (None if headers is None else any(
                    str(getattr(h, "name", "")).lower() == "list-unsubscribe" for h in headers)),
                "importance": str(email_item.importance) if getattr(email_item, "importance", None) else None,
                "subject_core": subject_core(subj),
            }

            new_items.append({
                "source": "email",
                "message_id": msg_id,
                "sender": sender_name,
                "sender_email": sender_addr,
                "subject": email_item.subject or "(No subject)",
                "body": body,
                "date": date_str,
                "attachments": attachments,
                "is_vip": self._is_vip(sender_addr),
                "rule_meta": rule_meta,
            })

        # Say what was NOT examined. "0 new unread messages" over a server
        # holding hundreds is the sentence that hid the defect above.
        examined = f" (examined {len(items)}"
        if unread_total is not None:
            examined += f" of {unread_total} unread"
        examined += ")"
        self.logger.info(f"Email: {len(new_items)} new unread messages{examined}")
        return new_items

    # The email section of sentinel_config.yaml has its own `vip_senders` and
    # its own `ignore_patterns`, read by these two methods. They are the third
    # and fourth copies of the reader hardened in `CalendarPolicyEngine`, and
    # they were the two that still took a hand-edited list on trust: an empty
    # dash makes a None ENTRY and `.lower()` raises on it. Both are called at
    # try-depth 0 inside `check_new`'s `for email_item in items` loop, so the
    # raise ends the whole email cycle - the same shape as the invite livelock
    # documented on `_configured_seq`, on the other source.
    def _is_ignored(self, sender: str) -> bool:
        sender_lower = (sender or "").lower()
        for pattern in _configured_seq(self.config.get("ignore_patterns"), str,
                                       "ignore_patterns", self.logger):
            if fnmatch.fnmatch(sender_lower, pattern.strip().lower()):
                return True
        return False

    def _is_vip(self, sender: str) -> bool:
        sender_lower = (sender or "").lower()
        for vip in _configured_seq(self.config.get("vip_senders"), str,
                                   "vip_senders", self.logger):
            if sender_lower == vip.strip().lower():
                return True
        return False


def _utc(dt) -> datetime:
    """Any aware datetime, exchangelib's EWSDateTime included, as stdlib UTC.

    `EWSDateTime.astimezone(timezone.utc)` raises: exchangelib accepts only its
    own EWSTimeZone there. The timestamp is zone-free, so this goes through it.
    """
    return datetime.fromtimestamp(dt.timestamp(), tz=timezone.utc)


def _storable(item: dict) -> dict:
    """A copy of an item safe to keep in state.json for a later re-score."""
    return json.loads(json.dumps(item, default=str))


def _addresses(recipients) -> list:
    return [str(r.email_address or "").lower() for r in (recipients or [])
            if r is not None and getattr(r, "email_address", None)]


_SUBJECT_PREFIX_RE = re.compile(r"^\s*((re|fw|fwd|ответ|отв|пересл)\s*(\[\d+\])?\s*:\s*)+", re.I)


def subject_core(subject: str) -> str:
    """A subject without its reply/forward prefixes, case-folded, for matching."""
    return re.sub(r"\s+", " ", _SUBJECT_PREFIX_RE.sub("", subject or "")).strip().lower()


class SentFolderReader:
    """The operator's Exchange Sent Items, READ ONLY: what has he already answered.

    One query per refresh, `.only()` the three fields it needs, newest
    `lookback_days`. It never marks, moves, saves, sends or deletes anything:
    the queryset is iterated and dropped. Used by the digest's "awaits your
    reply" section and by the fallback rule's "a thread you wrote in" feature.
    """

    def __init__(self, logger: logging.Logger, lookback_days: int = 14):
        self.logger = logger
        self.lookback_days = lookback_days
        self.sent: list = []  # (conversation_id, sent_at utc, subject_core)
        self.refreshed_at = None

    def refresh(self, account) -> bool:
        if account is None:
            return False
        from exchangelib import UTC, EWSDateTime
        since = EWSDateTime.now(tz=UTC) - timedelta(days=self.lookback_days)
        try:
            qs = account.sent.filter(
                datetime_sent__gte=since
            ).only("conversation_id", "datetime_sent", "subject")
            rows = []
            for it in qs:
                sent_at = getattr(it, "datetime_sent", None)
                if sent_at is None:
                    continue
                conv = it.conversation_id.id if getattr(it, "conversation_id", None) else None
                rows.append((conv, _utc(sent_at), subject_core(str(it.subject or ""))))
        except Exception as e:  # noqa: BLE001 - a failed read must not stop the cycle
            self.logger.warning(f"Sent folder read failed ({e}); replies are unknown this cycle")
            return False
        self.sent = rows
        self.refreshed_at = datetime.now(timezone.utc)
        self.logger.info(f"Sent folder: {len(rows)} message(s) in the last {self.lookback_days} days")
        return True

    def wrote_before(self, conversation_id, before) -> bool:
        return bool(conversation_id) and any(
            c == conversation_id and t < before for c, t, _s in self.sent)

    def replied_after(self, conversation_id, subj_core: str, after) -> bool | None:
        """True/False once the folder has been read; None when it never was."""
        if self.refreshed_at is None:
            return None
        for c, t, s in self.sent:
            if t <= after:
                continue
            if (conversation_id and c == conversation_id) or (subj_core and s == subj_core):
                return True
        return False


# ============================================================
# Meeting Invite Source
# ============================================================

class MeetingInviteSource:
    """Detect and process meeting invites from Exchange inbox."""

    def __init__(self, config: dict, state: StateManager, logger: logging.Logger):
        self.config = config
        self.state = state
        self.logger = logger
        self.account = None  # Shared from EmailSource

    def check_new_invites(self) -> list:
        """Fetch unprocessed MeetingRequest items from inbox."""
        if not self.account:
            return []

        try:
            # Query for meeting request items in inbox
            invites = self.account.inbox.filter(
                item_class="IPM.Schedule.Meeting.Request"
            ).order_by("-datetime_received")[:20]
        except Exception as e:
            self.logger.error(f"Failed to query meeting invites: {e}")
            return []

        from exchangelib import UTC
        now = datetime.now(tz=UTC)

        new_invites = []
        for invite in invites:
            invite_id = str(invite.message_id or invite.id)
            if self.state.is_invite_processed(invite_id):
                continue

            # Skip past meetings - only process future invites
            if invite.start and invite.start < now:
                self.logger.debug(f"Skipping past invite: {invite.subject} ({invite.start})")
                self.state.mark_invite_processed(invite_id)
                continue

            sender_email = ""
            sender_name = ""
            if invite.sender:
                sender_email = str(invite.sender.email_address or "")
                sender_name = str(invite.sender.name or sender_email)

            # Count attendees
            attendee_count = 0
            if hasattr(invite, "required_attendees") and invite.required_attendees:
                attendee_count += len(invite.required_attendees)
            if hasattr(invite, "optional_attendees") and invite.optional_attendees:
                attendee_count += len(invite.optional_attendees)

            # Duration
            duration_minutes = 0
            if invite.start and invite.end:
                try:
                    duration_minutes = int((invite.end - invite.start).total_seconds() / 60)
                except (TypeError, AttributeError, ValueError) as e:
                    self.logger.debug(f"meeting duration calc fallback: {e}")

            # Body snippet. A meeting invite carries a join URL and a join URL
            # carries a token, so this is redacted for the same reason the mail
            # path above is. Sliced AFTER extraction, as before.
            body = email_body_text(invite)[:500]

            # Check if recurring
            is_recurring = False
            if hasattr(invite, "type") and invite.type == "RecurringMaster":
                is_recurring = True

            new_invites.append({
                "invite_id": invite_id,
                "item": invite,
                "sender": sender_name,
                "sender_email": sender_email,
                "subject": invite.subject or "(No subject)",
                "start": invite.start,
                "end": invite.end,
                "duration_minutes": duration_minutes,
                "location": str(invite.location) if invite.location else "",
                "body": body,
                "attendee_count": attendee_count,
                "is_recurring": is_recurring,
            })

        self.logger.info(f"Meeting invites: {len(new_invites)} new")
        return new_invites

    def get_existing_events(self, start_date, end_date) -> list:
        """Fetch calendar events for conflict checking."""
        if not self.account:
            return []

        from exchangelib import EWSDateTime, EWSTimeZone

        tz = EWSTimeZone(get_default_tz_name())
        ews_start = EWSDateTime(
            start_date.year, start_date.month, start_date.day,
            0, 0, 0, tzinfo=tz
        )
        ews_end = EWSDateTime(
            end_date.year, end_date.month, end_date.day,
            23, 59, 59, tzinfo=tz
        )

        events = []
        try:
            for item in self.account.calendar.view(start=ews_start, end=ews_end):
                if not hasattr(item.start, "hour"):
                    continue  # Skip all-day events
                events.append({
                    "start": item.start,
                    "end": item.end,
                    "subject": item.subject or "",
                })
        except Exception as e:
            self.logger.error(f"Failed to fetch calendar events: {e}")

        return events

    def accept_invite(self, item):
        """Accept a meeting request."""
        item.accept()
        self.logger.info(f"Accepted invite: {item.subject}")

    def decline_invite(self, item, message: str):
        """Decline a meeting request with message."""
        from exchangelib import Body
        item.decline(body=Body(message))
        self.logger.info(f"Declined invite: {item.subject}")


# ============================================================
# Calendar invite helpers (pure)
# ============================================================

_ZERO_WIDTH = dict.fromkeys([0x200b, 0x200c, 0x200d, 0x2060, 0xfeff], None)

_DEFAULT_RUNE_TOKEN = "[RUNE]"  # noqa: S105 — a subject tag, not a credential
_DEFAULT_DECLINE_MESSAGE = (
    "Due to some conflicts, I'd like to propose a new day and time for our meeting."
)


def _configured_text(value, default: str) -> str:
    """A hand-edited YAML string, or `default` for any value that is not one.

    `sentinel_config.yaml` is edited by hand, and a key written with nothing
    after it (`rune_token:`) parses as None rather than as an absent key, so a
    `.get(key, default)` default never fires. MEASURED 2026-09-01: a blank
    `rune_token:` made the decline message ask for a `None` tag the detector
    never matched, and a blank `decline_message:` raised past `state.save()`, a
    livelock that reprocessed the same invites every cycle.

    The auto-accept / auto-decline POLICY is the operator's design and frozen
    since 2026-08-23; this changes only the text of a reply already being sent.
    """
    return value if isinstance(value, str) and value.strip() else default


def _bad_config(logger, key: str, detail: str) -> None:
    """Say which key was wrong. A dropped value must never be a silent one."""
    if logger is not None and key:
        logger.warning(f"sentinel_config.yaml: calendar.{key} {detail}; "
                       f"ignoring it for this cycle")


def _configured_seq(value, kind: type, key: str = "", logger=None) -> list:
    """Entries of type `kind` from a hand-edited YAML sequence; the rest dropped.

    The LIST, MAPPING and NUMBER sibling of `_configured_text`, for the same
    hand-edited YAML: a blank key parses as None, an empty dash makes a None
    entry, a trailing colon makes a mapping. MEASURED 2026-09-01, fourteen such
    shapes (`vip_senders:`, `tribe_domains: [null]`, `protected_blocks:
    ["am"]`, ...) each raised out of `CalendarPolicyEngine.evaluate` into the
    same livelock `_configured_text` documents.

    A lone scalar (`vip_senders: ceo@example.org`) is read as a one-entry list;
    before, it iterated characters and a configured VIP was never matched. That
    can only move a decision from decline toward escalate, so the frozen
    auto-accept / auto-decline design (2026-08-23) is untouched.
    """
    if isinstance(value, (list, tuple)):
        items = list(value)
    elif isinstance(value, kind):
        items = [value]          # a lone scalar written without a leading dash
    elif value is None:
        items = []
    else:
        _bad_config(logger, key, f"is a {type(value).__name__}, not a list")
        return []
    kept = [v for v in items
            if isinstance(v, kind) and (not isinstance(v, str) or v.strip())]
    if len(kept) != len(items):
        _bad_config(logger, key,
                    f"has {len(items) - len(kept)} of {len(items)} entries that "
                    f"are not a usable {kind.__name__}")
    return kept


def _configured_number(value, default, key: str = "", logger=None):
    """A hand-edited YAML number, or `default`. See `_configured_seq`.

    `True` is an `int` in Python and a threshold of 1 is not what
    `max_attendees: yes` meant, so bool is refused rather than counted.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if value is not None:
            _bad_config(logger, key,
                        f"is {value!r}, not a number; using {default}")
        return default
    return value


def _configured_bound(block: dict, key: str, logger=None) -> str:
    """One hand-typed `"HH:MM"` edge of a protected block, or "" if unusable.

    `after: 9:00` written without quotes is not a string. YAML 1.1 reads an
    unprefixed `9:00` as sexagesimal and hands over the INTEGER 540, so
    `start_time < block["after"]` raised `'<' not supported between instances
    of 'str' and 'int'` - out of `evaluate`, into the livelock described on
    `_configured_seq`. `09:00` with the leading zero is not a valid sexagesimal
    and stays a string, which is why the shape survives review: the same edit
    is harmless on one line and fatal on the next.

    A bound that is not a usable string is treated as ABSENT and said out loud,
    never as a bound of "". Absent is what the operator gets today for a key
    they never wrote, and it is the only reading that cannot invent a protected
    window nobody configured.
    """
    if key not in block:
        return ""
    text = _configured_text(block.get(key), "")
    if not text:
        _bad_config(logger, f"protected_blocks[].{key}",
                    f'is {block.get(key)!r}, not an "HH:MM" string')
    return text


def _configured_mapping(value, key: str = "", logger=None) -> dict:
    """A hand-edited YAML mapping, or {}. See `_configured_seq`."""
    if isinstance(value, dict):
        return value
    if value is not None:
        _bad_config(logger, key,
                    f"is a {type(value).__name__}, not a mapping")
    return {}


def _normalize_subject(s: str) -> str:
    """Strip zero-width chars, collapse whitespace, for stable tag matching."""
    s = (s or "").translate(_ZERO_WIDTH)
    return re.sub(r"\s+", " ", s).strip()


def subject_has_rune(subject: str, rune_token: str = _DEFAULT_RUNE_TOKEN) -> bool:  # noqa: S107 — rune_token is a subject tag, not a credential
    """True when the bracketed override tag is present (case-insensitive)."""
    token = _normalize_subject(
        _configured_text(rune_token, _DEFAULT_RUNE_TOKEN)).lower()
    return token in _normalize_subject(subject).lower()


def build_tribe_decline_message(subject: str, alternative,
                                rune_token: str = _DEFAULT_RUNE_TOKEN,  # noqa: S107 — rune_token is a subject tag, not a credential
                                template: str = None) -> str:
    """Warm, team-specific decline body with a ready-to-copy override example."""
    subject = subject or "your meeting"
    # The same guard `subject_has_rune` has carried all along. Without it the
    # detector and the message disagree about what tag to look for, and the
    # message is the half a person acts on. See `_configured_text`.
    rune_token = _configured_text(rune_token, _DEFAULT_RUNE_TOKEN)
    # A template that is not a string (a list or a mapping in the YAML) reached
    # `.format` and raised AttributeError, which the tuple below does not catch
    # and which no caller of this function catches either.
    template = _configured_text(template, "")
    if template:
        try:
            return template.format(subject=subject,
                                   alternative=alternative or "another time",
                                   rune_token=rune_token)
        except (KeyError, IndexError, ValueError) as exc:
            # Malformed operator template: fall through to the built-in default body.
            logging.getLogger("sentinel").warning(
                "tribe decline template is malformed (%s: %s); using the built-in body",
                type(exc).__name__, exc)
    parts = [
        "Thanks for the invite. This is an internal team request, so it matters to me.",
        "It clashes with time I keep protected, so I can't take this exact slot.",
    ]
    if alternative:
        parts.append(f"Could we do {alternative} instead?")
    parts.append(
        f"If it's genuinely time-critical and needs me regardless, resend the same "
        f"invite with the tag {rune_token} added to the front of the subject, square "
        f"brackets included, and it comes straight to me to decide personally."
    )
    parts.append(f"Your subject would look exactly like this: {rune_token} {subject}")
    return " ".join(parts)


def select_decline_message(is_tribe: bool, subject: str, alternative,
                           calendar_config: dict) -> str:
    """Choose the decline body: team-specific vs the generic default."""
    if is_tribe:
        return build_tribe_decline_message(
            subject, alternative,
            calendar_config.get("rune_token", _DEFAULT_RUNE_TOKEN),
            calendar_config.get("tribe_decline_message"),
        )
    msg = _configured_text(calendar_config.get("decline_message"),
                           _DEFAULT_DECLINE_MESSAGE)
    if alternative:
        msg += f" How about {alternative}?"
    return msg


# ============================================================
# Calendar Policy Engine
# ============================================================

# How far ahead `find_alternative_slot` searches, and therefore how far ahead
# the caller must fetch events for the conflict check to mean anything.
ALTERNATIVE_SEARCH_DAYS = 5
# The extra week the search adds so a run of weekends cannot exhaust it.
_WEEKEND_BUFFER_DAYS = 7


def conflict_window_days(search_days: int = ALTERNATIVE_SEARCH_DAYS) -> int:
    """Days of calendar a caller must fetch for `find_alternative_slot`.

    The search starts at reference + 1 and runs `search_days + 7` offsets, so
    it can propose a time up to `1 + search_days + 7` days out. The caller
    fetched SEVEN days. For every candidate beyond day 7, `_has_conflict`
    filtered an event list that held nothing for that date and answered "no
    conflict" - so the daemon could propose, inside a decline sent to a real
    organizer, a time the operator was already booked for. Measured 2026-08-26:
    search reach 12 days, fetch window 7.

    Deriving it here is the point. Two numbers that must agree, written in two
    files, drifted; one function that computes both cannot.
    """
    return 1 + search_days + _WEEKEND_BUFFER_DAYS


class CalendarPolicyEngine:
    """Evaluate meeting invites against CEO Calendar Policy."""

    THEME_KEYWORDS = {
        "Strategy & Leadership": [
            "strategy", "leadership", "co-founder", "planning", "all-hands",
            "standup", "tribe", "vision", "board", "quarterly",
        ],
        "Technical & Product": [
            "product", "technical", "engineering", "architecture", "sprint",
            "demo", "review", "research", "lab", "development", "design",
            "testing", "qa", "release", "deployment",
        ],
        "External & Business": [
            "investor", "partner", "sales", "customer", "prospect", "deal",
            "pipeline", "channel", "legal", "contract", "nda", "mou",
        ],
        "People & Operations": [
            "1:1", "one-on-one", "hr", "interview", "hiring", "onboarding",
            "performance", "operations", "operational", "culture",
        ],
        "Review & Think": [
            "review", "weekly", "catch-up", "catchup", "marketing", "pr",
            "content", "linkedin",
        ],
    }

    def __init__(self, config: dict, tz: ZoneInfo, logger: logging.Logger,
                 analyzer=None):
        self.config = config
        self.tz = tz
        self.logger = logger
        self.analyzer = analyzer  # UrgencyAnalyzer for LLM theme classification

    def evaluate(self, invite: dict, existing_events: list) -> dict:
        """Evaluate invite against policy. Returns decision + reasons."""
        violations = []
        start = invite.get("start")
        end = invite.get("end")
        duration = invite.get("duration_minutes", 0)
        attendee_count = invite.get("attendee_count", 0)
        subject = invite.get("subject", "")
        sender_email = invite.get("sender_email", "")

        is_tribe = self._is_tribe(sender_email)

        # RUNE override: top precedence, before any policy evaluation.
        if self.config.get("rune_override_enabled", True) and subject_has_rune(
            subject, self.config.get("rune_token", _DEFAULT_RUNE_TOKEN)
        ):
            return {
                "decision": "escalate",
                "reasons": ["RUNE override -- held for operator"],
                "proposed_alternative": None,
                "violations": [],
                "is_vip": self._is_vip_or_external(sender_email),
                "is_tribe": is_tribe,
            }

        if start and end:
            # Check protected time
            prot = self._check_protected_time(start, end)
            if prot:
                violations.append({"type": "protected_time", "detail": prot})

            # Check back-to-back
            btb = self._check_back_to_back(start, end, existing_events)
            if btb:
                violations.append({"type": "back_to_back", "detail": btb})

            # Check theme alignment
            if start.weekday() < 5:  # Mon-Fri only
                theme_issue = self._check_theme_alignment(subject, invite.get("body", ""), start.weekday())
                if theme_issue:
                    violations.append({"type": "theme_mismatch", "detail": theme_issue})

        # Check duration
        max_dur = _configured_number(self.config.get("max_duration_minutes"), 80,
                                     "max_duration_minutes", self.logger)
        if duration > max_dur:
            violations.append({"type": "duration", "detail": f"Duration {duration}m exceeds {max_dur}m limit"})

        # Check attendees
        max_att = _configured_number(self.config.get("max_attendees"), 6,
                                     "max_attendees", self.logger)
        if attendee_count > max_att:
            violations.append({"type": "attendees", "detail": f"{attendee_count} attendees exceeds {max_att} limit"})

        # Determine VIP/external status
        is_vip = self._is_vip_or_external(sender_email)

        # Make decision
        decision = self._make_decision(violations, is_vip)
        reasons = [v["detail"] for v in violations] if violations else []

        # Find alternative if declining
        proposed_alternative = None
        if decision == "decline" and start:
            proposed_alternative = self.find_alternative_slot(
                duration or 25, subject, existing_events, start
            )

        return {
            "decision": decision,
            "reasons": reasons,
            "proposed_alternative": proposed_alternative,
            "violations": [v["type"] for v in violations],
            "is_vip": is_vip,
            "is_tribe": is_tribe,
        }

    def _check_protected_time(self, start, end) -> str:
        """Check if invite falls within a protected time block."""
        blocks = _configured_seq(self.config.get("protected_blocks"), dict,
                                 "protected_blocks", self.logger)
        # Convert to local time
        try:
            local_start = start.astimezone(self.tz)
            local_end = end.astimezone(self.tz)
        except Exception as exc:
            self.logger.warning("protected-time check skipped: cannot convert "
                                "invite times to local time (%s: %s)",
                                type(exc).__name__, exc)
            return ""

        day = local_start.weekday()
        start_time = local_start.strftime("%H:%M")
        end_time = local_end.strftime("%H:%M")

        for block in blocks:
            block_days = _configured_seq(block.get("days"), int,
                                         "protected_blocks[].days", self.logger)
            if day not in block_days:
                continue

            # Each bound is a hand-typed "HH:MM" compared with `<`, so a key
            # written with nothing after it made `start_time < None` raise. The
            # bound is read once, and a bound that is not a string is treated as
            # absent - the same outcome as never writing the key.
            before = _configured_bound(block, "before", self.logger)
            after = _configured_bound(block, "after", self.logger)
            b_start = _configured_bound(block, "start", self.logger)
            b_end = _configured_bound(block, "end", self.logger)

            # All-day block (no time constraints). Still asked of the KEYS, not
            # of the values read above: a block whose `before:` is blank is a
            # malformed bound, not a declaration that the whole day is blocked,
            # and reading it as the latter would turn an invite that is accepted
            # today into an auto-decline sent to a real organizer.
            if "before" not in block and "after" not in block and "start" not in block:
                day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
                return f"Protected time: {day_names[day]} is blocked"

            # Before X
            if before and start_time < before:
                return f"Protected time: before {before}"

            # After X
            if after and start_time >= after:
                return f"Protected time: after {after}"

            # Start-End range
            if b_start and b_end:
                if start_time < b_end and end_time > b_start:
                    return f"Protected time: {b_start}-{b_end} block"

        return ""

    def _check_back_to_back(self, start, end, existing_events: list) -> str:
        """Check for back-to-back violations.

        KNOWN DEFECT, DELIBERATELY NOT FIXED (2026-08-25): `0 < gap < min_gap`
        judges a zero gap (back-to-back) compliant; the fix is `0 <= gap` in
        both branches. Not applied because a violation routes to `decline`,
        which messages real organizers, and the auto-accept / auto-decline
        policy is the operator's design, frozen since 2026-08-23. Raise it with
        him; no test pins this, only this docstring.
        """
        min_gap = _configured_number(self.config.get("min_gap_minutes"), 15,
                                     "min_gap_minutes", self.logger)
        max_consecutive = _configured_number(self.config.get("max_consecutive"), 3,
                                             "max_consecutive", self.logger)

        try:
            local_start = start.astimezone(self.tz)
            local_end = end.astimezone(self.tz)
        except Exception as exc:
            self.logger.warning("back-to-back check skipped: cannot convert "
                                "invite times to local time (%s: %s)",
                                type(exc).__name__, exc)
            return ""

        # Check gap to nearest events
        for evt in existing_events:
            try:
                evt_start = evt["start"].astimezone(self.tz)
                evt_end = evt["end"].astimezone(self.tz)
            except Exception as e:
                self.logger.debug(f"skipping malformed event in gap check: {e}")
                continue

            # Same day check
            if evt_start.date() != local_start.date():
                continue

            # Gap before: event ends, then our invite starts
            if evt_end <= local_start:
                gap = (local_start - evt_end).total_seconds() / 60
                if 0 < gap < min_gap:
                    return f"Only {int(gap)}m gap (need {min_gap}m) after '{evt['subject'][:30]}'"

            # Gap after: our invite ends, then event starts
            if local_end <= evt_start:
                gap = (evt_start - local_end).total_seconds() / 60
                if 0 < gap < min_gap:
                    return f"Only {int(gap)}m gap (need {min_gap}m) before '{evt['subject'][:30]}'"

        # Check consecutive meetings
        same_day_events = []
        for evt in existing_events:
            try:
                evt_start = evt["start"].astimezone(self.tz)
                if evt_start.date() == local_start.date():
                    same_day_events.append(evt)
            except Exception as e:
                self.logger.debug(f"skipping malformed event in same-day scan: {e}")
                continue

        # Sort by start time and count consecutive
        all_events = same_day_events + [{"start": start, "end": end, "subject": "new invite"}]
        all_events.sort(key=lambda e: e["start"])

        consecutive = 1
        for i in range(1, len(all_events)):
            try:
                prev_end = all_events[i - 1]["end"].astimezone(self.tz)
                curr_start = all_events[i]["start"].astimezone(self.tz)
                gap = (curr_start - prev_end).total_seconds() / 60
                if gap < 30:  # Less than 30 min break = consecutive
                    consecutive += 1
                else:
                    consecutive = 1
            except Exception as exc:
                self.logger.debug("consecutive-meeting count reset: cannot compare "
                                  "event times (%s: %s)", type(exc).__name__, exc)
                consecutive = 1

            if consecutive > max_consecutive:
                return f"Would create {consecutive} consecutive meetings (max {max_consecutive})"

        return ""

    def _check_theme_alignment(self, subject: str, body: str, weekday: int) -> str:
        """Check if meeting topic aligns with the day's theme."""
        themes = _configured_mapping(self.config.get("day_themes"),
                                     "day_themes", self.logger)
        day_theme = _configured_text(themes.get(weekday), "")
        if not day_theme:
            return ""

        text = (subject + " " + body).lower()

        # Try LLM classification first
        if self.config.get("use_llm_for_theme", False) and self.analyzer:
            try:
                classified = self._classify_theme_llm(subject, body[:200])
                if classified and classified != day_theme:
                    return f"Topic appears to be '{classified}' but {['Mon','Tue','Wed','Thu','Fri'][weekday]} theme is '{day_theme}'"
            except Exception as e:
                # LLM call can raise anthropic API errors, network errors, or parse errors.
                # We intentionally fall through to keyword matching on any failure.
                self.logger.debug(f"LLM theme classification fallback to keywords: {e}")

        # Keyword fallback
        best_theme = None
        best_score = 0
        for theme, keywords in self.THEME_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in text)
            if score > best_score:
                best_score = score
                best_theme = theme

        if best_theme and best_theme != day_theme and best_score >= 2:
            return f"Topic appears to be '{best_theme}' but {['Mon','Tue','Wed','Thu','Fri'][weekday]} theme is '{day_theme}'"

        return ""

    @observe()
    def _classify_theme_llm(self, subject: str, body_snippet: str) -> str:
        """Use LLM to classify meeting into a day theme.

        Note: prompt is small (~150 chars, no system prompt) and varies per call -
        below any prompt-cache threshold. No cache_control applied here by design.
        """
        if not self.analyzer:
            return ""

        client = self.analyzer._get_client()
        themes = list(self.THEME_KEYWORDS.keys())
        prompt = (
            f"Classify this meeting into exactly one theme.\n"
            f"Themes: {', '.join(themes)}\n"
            f"Subject: {subject}\nBody: {body_snippet}\n"
            f"Reply with ONLY the theme name, nothing else."
        )
        # On the Anthropic path the answer is constrained to the theme list by
        # an enum; the fallback vendors get the sentence above and nothing else,
        # so the substring match below stays for them.
        theme_schema = {
            "type": "object",
            "properties": {"theme": {"type": "string", "enum": themes}},
            "required": ["theme"],
            "additionalProperties": False,
        }

        try:
            r = call_anthropic_with_fallback(
                client=client,
                model=self.analyzer.model,
                max_tokens=50,
                system="",
                messages=[{"role": "user", "content": prompt}],
                effort="low",
                output_schema=theme_schema,
                skill_name="sentinel.classify_theme_llm",
            )
            result = r.text
            try:
                chosen = json.loads(result).get("theme") if r.vendor == "anthropic" else None
            except (json.JSONDecodeError, AttributeError):
                chosen = None
            if chosen in themes:
                return chosen
            # Match against known themes
            for theme in themes:
                if theme.lower() in result.lower():
                    return theme
            return result
        except Exception as e:
            self.logger.warning(f"LLM theme classification failed across all vendors: {e}")
            return ""

    def _is_vip_or_external(self, sender_email: str) -> bool:
        """Check if sender is VIP or from an external domain."""
        # `(sender_email or "")` was on `_is_tribe` and not here, though both
        # read the same `invite.get("sender_email", "")` and `.get` with a
        # default is not a type check: a present-but-null key walks through it.
        # The rune-override branch of `evaluate` calls THIS one, so the escape
        # hatch the operator relies on was the path that raised.
        sender_lower = (sender_email or "").lower()

        for vip in _configured_seq(self.config.get("vip_senders"), str,
                                   "vip_senders", self.logger):
            if sender_lower == vip.strip().lower():
                return True

        for domain in _configured_seq(self.config.get("external_domains"), str,
                                      "external_domains", self.logger):
            if sender_lower.endswith(f"@{domain.strip().lower()}"):
                return True

        return False

    def _tribe_domains(self) -> list:
        """Configured team domains, defaulting to the operator's own domain."""
        doms = [d.strip().lower()
                for d in _configured_seq(self.config.get("tribe_domains"), str,
                                         "tribe_domains", self.logger)]
        if not doms:
            email = (get_operator().get("email") or "").strip().lower()
            if "@" in email:
                doms = [email.rsplit("@", 1)[-1]]
        return doms

    def _is_tribe(self, sender_email: str) -> bool:
        """True when the sender's domain is a team (internal) domain."""
        email = (sender_email or "").strip().lower()
        if "@" not in email:
            return False
        return email.rsplit("@", 1)[-1] in self._tribe_domains()

    def _make_decision(self, violations: list, is_vip: bool) -> str:
        """Decide: accept, decline, or escalate."""
        if not violations:
            return "accept"

        hard_types = {"protected_time"}
        soft_types = {"theme_mismatch", "duration", "attendees"}

        hard = [v for v in violations if v["type"] in hard_types]
        soft = [v for v in violations if v["type"] in soft_types]
        btb = [v for v in violations if v["type"] == "back_to_back"]

        # VIP/external: always escalate, never auto-decline
        if is_vip:
            return "escalate"

        if hard:
            return "decline"

        if btb:
            return "decline"

        if soft:
            return "escalate"

        return "decline"

    def find_alternative_slot(self, duration_minutes: int, subject: str,
                              existing_events: list, reference_date=None,
                              search_days: int = ALTERNATIVE_SEARCH_DAYS) -> str:
        """Find a policy-compliant alternative slot.

        The caller MUST supply `existing_events` covering
        `conflict_window_days(search_days)` from the reference date, or the
        conflict check below is judging some candidate days against an empty
        list. See that helper for what went wrong when the two disagreed.
        """
        if reference_date is None:
            reference_date = datetime.now(self.tz)
        else:
            try:
                reference_date = reference_date.astimezone(self.tz)
            except Exception as exc:
                self.logger.warning("alternative-slot search: cannot convert the "
                                    "reference date (%s: %s); searching from now",
                                    type(exc).__name__, exc)
                reference_date = datetime.now(self.tz)

        # Start searching from next business day
        search_start = reference_date + timedelta(days=1)

        for day_offset in range(search_days + _WEEKEND_BUFFER_DAYS):
            candidate_date = search_start + timedelta(days=day_offset)
            weekday = candidate_date.weekday()

            # Skip weekends
            if weekday >= 5:
                continue

            # Generate 30-min increment slots from 09:30 to 17:30 (the last
            # START, not the last end). The comment said 18:00 until
            # 2026-08-25; `range(9, 18)` yields 9..17, so an 18:00 start is
            # never generated and the end guard below never got the chance to
            # judge one. Only the sentence is corrected here. Extending the
            # range would change which alternative time is offered inside a
            # decline message sent to a real organizer, and the calendar
            # auto-reply is the operator's design and is frozen - see the note
            # on `_check_back_to_back`.
            for hour in range(9, 18):
                for minute in [0, 30]:
                    if hour == 9 and minute == 0:
                        continue  # Before 09:30
                    slot_start = candidate_date.replace(
                        hour=hour, minute=minute, second=0, microsecond=0
                    )
                    slot_end = slot_start + timedelta(minutes=duration_minutes)

                    # Don't go past 19:00
                    if slot_end.hour >= 19 or (slot_end.hour == 19 and slot_end.minute > 0):
                        continue

                    # Check protected time
                    if self._check_protected_time_simple(slot_start, slot_end, weekday):
                        continue

                    # Check conflicts with existing events
                    if self._has_conflict(slot_start, slot_end, existing_events):
                        continue

                    # Format the alternative
                    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
                    return f"{day_names[weekday]}, {slot_start.strftime('%B %d')} at {slot_start.strftime('%H:%M')} local time"

        return None

    def _check_protected_time_simple(self, start, end, weekday: int) -> bool:
        """Quick protected time check for slot finding."""
        start_time = start.strftime("%H:%M")
        end_time = end.strftime("%H:%M")

        # The second copy of the `protected_blocks` reader. It runs while
        # composing the alternative slot a decline message offers, so the same
        # malformed block that crashed `_check_protected_time` crashed here too,
        # one call later. Same treatment, same reasons - see `_configured_seq`.
        for block in _configured_seq(self.config.get("protected_blocks"), dict,
                                     "protected_blocks", self.logger):
            if weekday not in _configured_seq(block.get("days"), int,
                                              "protected_blocks[].days",
                                              self.logger):
                continue
            before = _configured_bound(block, "before", self.logger)
            after = _configured_bound(block, "after", self.logger)
            b_start = _configured_bound(block, "start", self.logger)
            b_end = _configured_bound(block, "end", self.logger)
            if "before" not in block and "after" not in block and "start" not in block:
                return True  # Full day block
            if before and start_time < before:
                return True
            if after and start_time >= after:
                return True
            if b_start and b_end:
                if start_time < b_end and end_time > b_start:
                    return True
        return False

    def _has_conflict(self, slot_start, slot_end, existing_events: list) -> bool:
        """Check if slot conflicts with existing events (with gap buffer)."""
        min_gap = _configured_number(self.config.get("min_gap_minutes"), 15,
                                     "min_gap_minutes", self.logger)
        buffered_start = slot_start - timedelta(minutes=min_gap)
        buffered_end = slot_end + timedelta(minutes=min_gap)

        for evt in existing_events:
            try:
                evt_start = evt["start"].astimezone(self.tz)
                evt_end = evt["end"].astimezone(self.tz)
            except Exception as e:
                self.logger.debug(f"skipping malformed event in conflict check: {e}")
                continue

            if evt_start.date() != slot_start.date():
                continue

            # Check overlap including gap buffer
            if buffered_start < evt_end and buffered_end > evt_start:
                return True

        return False


# ============================================================
# Telegram Source
# ============================================================

def ignore_keys(entries) -> set[str]:
    """The `ignore_chats` entries, normalised for `is_ignored`.

    YAML reads a bare id as an int, so every entry goes through `str()`.
    """
    return {str(e).strip().lower() for e in entries or [] if str(e).strip()}


def is_ignored(entity, display_name: str, keys: set[str]) -> bool:
    """Does an `ignore_chats` entry name this account?

    An entry matches the display name, the `@username`, or the numeric user
    id, case-insensitive for the names. The display name alone is not enough:
    a contact who renames themselves would come back silently.
    """
    names = {display_name.strip().lower(), str(getattr(entity, "id", "")).strip()}
    handles = [getattr(entity, "username", None)]
    handles += [getattr(u, "username", None) for u in getattr(entity, "usernames", None) or []]
    names.update("@" + h.lower() for h in handles if h)
    names.discard("")
    return not keys.isdisjoint(names)


class TelegramSource:
    """Fetch new Telegram messages via Telethon."""

    def __init__(self, config: dict, state: StateManager, logger: logging.Logger):
        self.config = config
        self.state = state
        self.logger = logger
        self.client = None

    async def connect(self):
        from telethon import TelegramClient

        # Reuse existing client — just reconnect the network layer.
        # Creating a new TelegramClient each cycle leaks SQLite handles on
        # Windows, causing "database is locked" on the next reconnect.
        if self.client is not None:
            if not self.client.is_connected():
                await self.client.connect()
            me = await self.client.get_me()
            self.logger.info(f"Telegram reconnected as {me.first_name} (@{me.username})")
            return

        # First-time setup
        api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
        api_hash = os.getenv("TELEGRAM_API_HASH", "")

        if not api_id or not api_hash:
            raise ValueError("Missing Telegram credentials in .env")

        os.makedirs(TELEGRAM_SESSION_DIR, exist_ok=True)

        # WAL checkpoint: clear any stale locks from previous crashes
        session_file = str(TELEGRAM_SESSION_PATH) + ".session"
        if os.path.exists(session_file):
            try:
                import sqlite3 as _sqlite3
                _tmp = _sqlite3.connect(session_file, timeout=5)
                _tmp.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                _tmp.close()
            except sqlite3.Error as e:
                # WAL checkpoint is best-effort cleanup before opening the real session.
                # A locked/corrupt session will surface via the subsequent TelegramClient call.
                self.logger.debug(f"telegram session WAL checkpoint fallback: {e}")

        self.client = TelegramClient(str(TELEGRAM_SESSION_PATH), api_id, api_hash)
        _configure_session_wal(self.client)
        await self.client.connect()

        if not await self.client.is_user_authorized():
            raise RuntimeError("Telegram session not authorized. Run .claude/skills/telegram/scripts/telegram_client.py setup first.")

        me = await self.client.get_me()
        self.logger.info(f"Telegram connected as {me.first_name} (@{me.username})")

    async def check_new(self) -> list:
        if not self.client or not self.client.is_connected():
            await self.connect()

        items = []
        dm_items = []

        # Check personal DMs (with timeout)
        if self.config.get("check_personal_dms", True):
            try:
                dm_items = await asyncio.wait_for(
                    self._check_personal_dms(), timeout=120
                )
                items.extend(dm_items)
            except asyncio.TimeoutError:
                self.logger.error("Telegram personal DM check timed out (120s)")
            except Exception as e:
                self.logger.error(f"Telegram personal DM check failed: {e}")

        # Check monitored chats (with timeout)
        monitored = self.config.get("monitored_chats", [])
        if monitored:
            try:
                mc_items = await asyncio.wait_for(
                    self._check_monitored_chats(monitored, dm_items), timeout=120
                )
                items.extend(mc_items)
            except asyncio.TimeoutError:
                self.logger.error("Telegram monitored chats check timed out (120s)")
            except Exception as e:
                self.logger.error(f"Telegram monitored chats check failed: {e}")

        self.logger.info(f"Telegram: {len(items)} new messages")
        return items

    # How many pages of `limit` messages one dialog may drag out of Telegram in
    # a single cycle. A bound is needed - a dialog that has been quiet for a
    # month should not stall the whole cycle - but the bound must be REPORTED
    # when it bites, or the loss is silent again.
    MAX_FETCH_PAGES = 5

    # SEC-014 bounds how long ONE chat may stall the cycle. The single fetch
    # this paging loop replaced carried `timeout=15`, and a per-page timeout of
    # 15 would have quietly raised that ceiling to 75 - five times the budget
    # the control exists to enforce. The whole loop shares one deadline instead,
    # so the bound is unchanged no matter how many pages it takes.
    FETCH_TIMEOUT_SECONDS = 15

    async def _fetch_since(self, entity, last_known_id, limit, chat_name,
                           max_pages=None):
        """Every message above `last_known_id`, newest first, in pages.

        A single `get_messages(limit=N, min_id=cursor)` returns the N NEWEST
        messages above the cursor and nothing older. Both callers then advanced
        the cursor to `max(m.id)`, so whenever more than N messages had arrived
        since the last cycle, every message between the cursor and the oldest
        one fetched was skipped PERMANENTLY, with no log line and no counter. A
        30-message burst in a group chat between two cycles is routine, and the
        default `max_messages_per_chat` is 30.

        Paging backwards with `max_id` closes the gap. `MAX_FETCH_PAGES` bounds
        it, and when the bound is hit the number of messages left behind is
        logged rather than swallowed.

        `max_pages` overrides that bound, and the FIRST-SIGHT caller sets it to
        1. With `last_known_id == 0` there is no lower bound at all, so the
        paging loop does not stop at the unread window: it kept going while
        pages came back full and pulled `MAX_FETCH_PAGES * limit` messages -
        150 at the shipped `max_messages_per_chat: 30` - of which 120 were
        already-read history, concatenated into the alert body and sent to the
        model. That is the backfill the caller's own comment says it is
        avoiding. Measured 2026-08-26 against a 1000-message stand-in dialog.
        """
        collected = []
        next_max_id = 0  # 0 means "no upper bound" in Telethon
        page_budget = self.MAX_FETCH_PAGES if max_pages is None else max_pages

        async def _all_pages():
            nonlocal next_max_id
            for _ in range(page_budget):
                page = await self.client.get_messages(
                    entity, limit=limit, min_id=last_known_id, max_id=next_max_id)
                if not page:
                    return True
                collected.extend(page)
                if len(page) < limit:
                    return True
                next_max_id = min(m.id for m in page)
            return False

        # One deadline for the whole loop (SEC-014), not one per page.
        drained = await asyncio.wait_for(
            _all_pages(), timeout=self.FETCH_TIMEOUT_SECONDS)
        if not drained and last_known_id == 0:
            # No lower bound, so `oldest - last_known_id - 1` is "every message
            # id below the oldest one fetched" - the whole chat history, a
            # number this function never measured against anything unread. It
            # was logged as an alarm about hundreds of missed messages. Say
            # what is true instead.
            self.logger.info(
                f"{chat_name}: first sight, read the newest {len(collected)} "
                f"message(s); older history is not backfilled by design")
        elif not drained:
            # The page cap bit. Say how much was left behind: a silent bound is
            # the original defect in a smaller shape.
            oldest = min((m.id for m in collected), default=last_known_id + 1)
            skipped = oldest - last_known_id - 1
            if skipped > 0:
                self.logger.warning(
                    f"{chat_name}: stopped after {page_budget} pages; "
                    f"{skipped} message(s) between id {last_known_id} and {oldest} "
                    f"were NOT read and will not be reported")
        return collected

    async def _check_personal_dms(self) -> list:
        from telethon import types

        ignore_chats = ignore_keys(self.config.get("ignore_chats", []))
        max_msgs = self.config.get("max_messages_per_chat", 30)

        # Newness is the dialog's top message id against our cursor, NOT the
        # unread badge. Reading a message on the phone clears the badge, and a
        # dialog where the operator wrote last never had one -- both used to
        # make this reader permanently blind to those messages. iter_dialogs
        # already carries the top message, so this test costs no API call.
        pending_dialogs = []
        async for dialog in self.client.iter_dialogs(limit=100):
            if not isinstance(dialog.entity, types.User):
                continue
            if dialog.entity.bot:
                continue
            chat_name = self._entity_name(dialog.entity)
            if is_ignored(dialog.entity, chat_name, ignore_chats):
                continue

            chat_id = str(dialog.entity.id)
            last_known_id = self.state.get_telegram_last_id(chat_id)
            top_id = dialog.message.id if dialog.message else 0

            if top_id and top_id <= last_known_id:
                continue

            if last_known_id == 0:
                # First sight. Seed the cursor instead of backfilling, or the
                # first run after this change drags max_msgs out of every
                # dialog at once. Anything genuinely unread is still reported,
                # so a brand new counterpart is not silent for a cycle.
                if dialog.unread_count == 0:
                    if top_id:
                        self.state.set_telegram_last_id(chat_id, chat_name, top_id)
                    continue
                limit = min(dialog.unread_count, max_msgs)
            else:
                limit = max_msgs

            pending_dialogs.append((dialog, chat_name, last_known_id, limit))

        if not pending_dialogs:
            return []

        async def _fetch_dm(dialog, chat_name, last_known_id, limit):
            chat_id = str(dialog.entity.id)
            try:
                messages = await self._fetch_since(
                    dialog.entity, last_known_id, limit, chat_name,
                    # First sight has no lower bound, so paging would walk the
                    # whole history. One page is the unread window.
                    max_pages=1 if last_known_id == 0 else None)
            except asyncio.TimeoutError:
                self.logger.warning(f"Timeout fetching DMs from {chat_name}")
                return None

            if not messages:
                return None

            # The cursor covers everything fetched, including the operator's
            # own messages. Skipping those would rescan the dialog every cycle
            # for as long as he happened to write last.
            max_id = max(m.id for m in messages)

            combined_text = []
            for msg in reversed(messages):
                if getattr(msg, "out", False):
                    # Not an alert about himself, but it IS his reply: the
                    # digest's "awaits your reply" reads it.
                    self.state.note_telegram_out(chat_id, getattr(msg, "date", None))
                    continue
                if msg.text:
                    combined_text.append(msg.text)
                elif msg.media:
                    combined_text.append("[media attachment]")

            if not combined_text:
                # Nothing to analyse, so nothing downstream will ever mark this
                # window done. Advance here or the same messages are re-fetched
                # every cycle forever.
                self.state.set_telegram_last_id(chat_id, chat_name, max_id)
                return None

            full_text = "\n---\n".join(combined_text)
            if len(full_text) > 2000:
                full_text = full_text[:2000] + "\n[...truncated]"

            # `cursor_id` rides along instead of being written here. The cursor
            # is this source's only memory of what it has read, and advancing
            # it at FETCH time consumed the message before anything looked at
            # it: an Anthropic outage, or a cycle that raised anywhere between
            # here and the digest, lost the DM permanently - never retried,
            # never digested, never notified. `_mark_item_processed` advances
            # it once the item reaches a terminal outcome, which is exactly the
            # change the email path already carries (see the note in
            # `_analyze_and_notify`).
            return {
                "source": "telegram",
                "chat_id": chat_id,
                "chat_name": chat_name,
                "cursor_id": max_id,
                "sender": chat_name,
                "subject": f"Telegram DM from {chat_name}",
                "body": full_text,
                "date": local_stamp(datetime.now(timezone.utc)),
                "message_count": len(combined_text),
                "is_vip": False,
            }

        results = await asyncio.gather(
            *[_fetch_dm(d, name, cursor, limit) for d, name, cursor, limit in pending_dialogs],
            return_exceptions=True,
        )

        items = []
        for result in results:
            if isinstance(result, Exception):
                self.logger.error(f"DM fetch error: {result}")
            elif result is not None:
                items.append(result)

        return items

    async def _check_monitored_chats(self, monitored: list, dm_items: list = ()) -> list:
        """One item per monitored chat with new messages.

        A monitored chat can be a person, and the DM reader has then already
        fetched it this cycle. Both read one per-chat cursor, so a second item
        for the same messages is not a harmless duplicate: whichever is scored
        first advances the cursor past the other, and an unusable answer on the
        other is never retried, staying "not scored yet" in every digest.
        MEASURED 2026-10-26 by a reproduction run: one DM fetched as "Telegram
        DM from" and "Telegram Group:", the group item scored, the cursor moved
        to its message, the DM item's record stuck at attempt 1 of 3 through
        five quiet cycles. Such a chat now only lends its priority to the DM
        item.
        """
        items = []
        max_msgs = self.config.get("max_messages_per_chat", 30)
        dm_by_chat = {it.get("chat_id"): it for it in dm_items}
        from telethon import types

        for chat_cfg in monitored:
            chat_name_or_id = chat_cfg.get("name", "")
            priority = chat_cfg.get("priority", "medium")

            try:
                entity = await self._resolve_chat(chat_name_or_id)
            except Exception as e:
                self.logger.warning(f"Could not resolve chat '{chat_name_or_id}': {e}")
                continue

            chat_id = str(entity.id)
            if chat_id in dm_by_chat:
                if priority == "high":
                    dm_by_chat[chat_id]["is_vip"] = True
                continue
            last_known_id = self.state.get_telegram_last_id(chat_id)

            try:
                messages = await self._fetch_since(
                    entity, last_known_id, max_msgs, str(chat_name_or_id))
            except asyncio.TimeoutError:
                self.logger.warning(f"Timeout checking chat '{chat_name_or_id}', skipping")
                continue

            if not messages:
                continue

            max_id = max(m.id for m in messages)
            chat_display = self._entity_name(entity)

            combined_text = []
            for msg in reversed(messages):
                if getattr(msg, "out", False):
                    self.state.note_telegram_out(chat_id, getattr(msg, "date", None))
                sender_name = await self._get_sender_name(msg)
                text = msg.text or "[media]"
                combined_text.append(f"[{sender_name}]: {text}")

            full_text = "\n".join(combined_text)
            if len(full_text) > 2000:
                full_text = full_text[:2000] + "\n[...truncated]"

            # Deferred to the terminal outcome, for the reason spelled out in
            # `_fetch_dm`. Every fetched message becomes a line above, so this
            # loop has no "nothing to analyse" branch to advance early.
            items.append({
                "source": "telegram",
                "chat_id": chat_id,
                "chat_name": chat_display,
                "cursor_id": max_id,
                "sender": chat_display,
                # A person is a DM whichever reader fetched it, never a "Group".
                "subject": (f"Telegram DM from {chat_display}" if isinstance(entity, types.User)
                            else f"Telegram Group: {chat_display}"),
                "body": full_text,
                "date": local_stamp(datetime.now(timezone.utc)),
                "message_count": len(messages),
                "is_vip": priority == "high",
            })

        return items

    async def _resolve_chat(self, identifier: str):
        """Resolve chat identifier (mirrors .claude/skills/telegram/scripts/telegram_client.py)."""
        from telethon import errors

        ident = identifier.strip()

        # Try numeric ID
        try:
            num_id = int(ident)
            return await self.client.get_entity(num_id)
        except (ValueError, errors.RPCError):
            pass

        # Try @username
        if ident.startswith("@"):
            return await self.client.get_entity(ident)

        # Try as username without @
        try:
            return await self.client.get_entity(ident)
        except (ValueError, errors.RPCError):
            pass

        # Fuzzy match against dialog names
        ident_lower = ident.lower()
        best_match = None
        best_score = 0
        async for dialog in self.client.iter_dialogs(limit=200):
            name = dialog.name or ""
            name_lower = name.lower()
            if ident_lower == name_lower:
                return dialog.entity
            if ident_lower in name_lower:
                score = len(ident_lower) / len(name_lower) if name_lower else 0
                if score > best_score:
                    best_score = score
                    best_match = dialog.entity

        if best_match and best_score > 0.3:
            return best_match

        raise ValueError(f"Could not resolve chat: '{identifier}'")

    def _entity_name(self, entity) -> str:
        from telethon import types

        if isinstance(entity, types.User):
            parts = [entity.first_name or "", entity.last_name or ""]
            name = " ".join(p for p in parts if p)
            return name or entity.username or str(entity.id)
        if isinstance(entity, (types.Chat, types.Channel)):
            return entity.title or str(entity.id)
        return str(entity)

    async def _get_sender_name(self, msg) -> str:
        if msg.sender:
            return self._entity_name(msg.sender)
        try:
            sender = await msg.get_sender()
            return self._entity_name(sender)
        except Exception as exc:
            self.logger.debug("sender lookup failed for %s (%s: %s); using the id",
                              msg.sender_id, type(exc).__name__, exc)
            return f"User#{msg.sender_id}"

    async def disconnect(self):
        if self.client:
            if self.client.is_connected():
                await self.client.disconnect()
            # Explicitly close the SQLite session handle to release the file
            # lock on Windows. Without this, the GC may not collect the handle
            # before the next reconnect, causing "database is locked".
            session = getattr(self.client, 'session', None)
            if session:
                conn = getattr(session, '_conn', None)
                if conn:
                    try:
                        conn.close()
                        session._conn = None
                    except (sqlite3.Error, AttributeError) as e:
                        # Session handle may already be invalid; safe to ignore.
                        self.logger.debug(f"session _conn close fallback: {e}")


# ============================================================
# Urgency Analyzer
# ============================================================

# An answer the model DID give that nobody can use: cut off by its token
# budget, not JSON, missing from a batch. Until 2026-10-12 the parse cases were
# scored 5 ("LLM response could not be parsed"), a made-up middling score
# standing in for a real one, and the max_tokens case was logged as "message
# not scored" and counted nowhere. The journal showed one on 2026-10-11 13:31.
# Now such an item is retried next cycle at a larger budget, counted, and named
# in the digest; after `llm.max_score_attempts` it is listed there as "could
# not be scored" rather than dropped. Distinct from None, which means the model
# did not answer at all (an outage).
UNSCORED_KEY = "_unscored"


def unscored(reason: str) -> dict:
    return {UNSCORED_KEY: reason}


def is_unscored(analysis) -> bool:
    return isinstance(analysis, dict) and UNSCORED_KEY in analysis


def _budget_scale(items) -> int:
    """2x the token budget per earlier unusable answer, capped at 4x."""
    attempt = max((int(i.get("_attempt") or 0) for i in items), default=0)
    return min(2 ** attempt, 4)


class UrgencyAnalyzer:
    """Classify message urgency using Claude API."""

    provider = "anthropic"
    down = False

    def begin_cycle(self):
        """Per-cycle reset; the Anthropic path keeps no cycle state."""

    # `{operator}` is filled from `get_operator()`, which resolves from the DATA
    # overlay and yields "Operator" on a fresh clone. It used to be the
    # operator's name, employer and product hardcoded here, so a stranger who
    # cloned the public engine got a triage system that believed it worked for
    # somebody else. What the business actually IS belongs in
    # `{business_context}`, loaded from the overlay's `context_files` -- which
    # is the mechanism that already exists for exactly this.
    SYSTEM_PROMPT = """You are Sentinel, an urgency triage system for {operator}.

Your job: analyze incoming messages and score their urgency on a 1-10 scale.
{operator} is extremely busy; treat their attention as the scarce resource.

{untrusted_note}

URGENCY SCORING GUIDE:
- 9-10: CRITICAL - Requires immediate action. Deal at risk, security incident, investor/partner emergency, legal deadline, production system down.
- 7-8: HIGH - Needs attention today. Important partner/client communication, time-sensitive opportunity, meeting confirmation needed, financial matter.
- 5-6: MEDIUM - Should see within 24 hours. Business update, non-urgent partner message, internal team matter, follow-up request.
- 3-4: LOW - Can wait. Informational, routine approvals, general updates.
- 1-2: NOISE - Ignore. Spam, automated notifications, marketing, bulk newsletters.

{business_context}

RECOMMENDED ACTIONS - be specific and CEO-appropriate. Use one of these categories:
- "Reply needed: [draft key points to address]" - when a response from the CEO is expected
- "Forward to [person/role] for handling" - when delegation is appropriate
- "Schedule follow-up: [topic] by [timeframe]" - for items needing future action
- "Review attachment: [what to look for]" - for documents needing CEO eyes
- "Approve/Decide: [what decision is needed]" - for items awaiting CEO decision
- "FYI only - no action needed" - for informational items
- "Escalate: [why and to whom]" - for items requiring immediate escalation

Consider the priorities named in the business context above. Flag anything affecting deal velocity, partner relationships, or product delivery.

Respond ONLY in this JSON format (no markdown, no code fences):
{{"urgency_score": <1-10>, "reason": "<1 sentence>", "summary": "<2-3 sentences>", "recommended_action": "<specific action using categories above>"}}"""

    # The same four fields SYSTEM_PROMPT asks for in words, as a schema, so the
    # Anthropic path returns them by construction. The fallback vendors never
    # see this and are still parsed out of text by `_extract_json`. No numeric
    # range on the score: structured outputs do not accept one, and
    # `_clamp_score` already holds 1-10.
    ANALYSIS_SCHEMA = {
        "type": "object",
        "properties": {
            "urgency_score": {"type": "integer"},
            "reason": {"type": "string"},
            "summary": {"type": "string"},
            "recommended_action": {"type": "string"},
        },
        "required": ["urgency_score", "reason", "summary", "recommended_action"],
        "additionalProperties": False,
    }
    BATCH_SCHEMA = {
        "type": "object",
        "properties": {"results": {"type": "array", "items": ANALYSIS_SCHEMA}},
        "required": ["results"],
        "additionalProperties": False,
    }

    def __init__(self, config: dict, logger: logging.Logger):
        self.model = config.get("model") or claude_models.latest("haiku")
        self.max_tokens = config.get("max_tokens", 500)
        self.logger = logger
        self.client = None
        self.business_context = ""
        self.operator_name = get_operator()["name"]

        # Load business context from files
        context_files = config.get("context_files", [])
        self._load_business_context(context_files)

    def _resolve_context_file(self, rel_path: str) -> Path | None:
        """First existing match for `rel_path` across the DATA and engine roots.

        The `context_files` shipped then were the `strategy`, `pipeline` and
        `people` context files and `reference/ceo-calendar-policy.md`, every one of them a DATA-overlay
        path. This resolved them against the ENGINE root alone, where
        `context/` does not exist at all, so every file missed, `parts` stayed
        empty, and `{business_context}` was substituted as the empty string on
        every run since the seam landed. The prompt above says the business
        context is the mechanism that replaced the hardcoded operator details;
        it was loading nothing.

        DATA is tried first because that is where the configured files live. An
        absolute path needs no branch of its own: on POSIX `root / "/a/b.md"` is
        `/a/b.md`, so the first root already returns the path as given. A guard
        clause for that case was written here and removed, because no mutation
        of it could change an answer.
        """
        roots = []
        # A public clone with no overlay: the engine root is all there is.
        with contextlib.suppress(DataRootError):
            roots.append(get_data_root())
        roots.append(WORKSPACE_ROOT)
        for root in roots:
            full_path = root / rel_path
            if full_path.exists():
                return full_path
        return None

    def _context_entries(self, context_files: list) -> list[tuple[str, Path | None]]:
        """(label, file or None) for each `context_files` entry, in order.

        HOS-234 part 5: an entry is a registry name or `group:<group>`, resolved
        through `config_names`, and labelled by its data-relative path so the
        scoring prompt reads the same as it did under the path form. A path is
        still resolved by `_resolve_context_file` and said on stderr as
        deprecated. A name the registry does not place is logged as REFUSED at
        ERROR and left out; it is not read as a path, and it does not take the
        always-on daemon down, for the reason the read handler below gives.
        """
        out: list[tuple[str, Path | None]] = []
        for entry in context_files:
            field = f"sentinel_config.yaml context_files entry {entry!r}"
            try:
                named = config_names(entry, field=field)
            except DataPathError as exc:
                self.logger.error("business context REFUSED: %s", exc)
                continue
            if named is None:
                deprecated_config_path(field, entry)
                out.append((entry, self._resolve_context_file(entry)))
                continue
            for _name, path in named:
                label = str(path)
                with contextlib.suppress(DataRootError, ValueError):
                    label = path.relative_to(get_data_root()).as_posix()
                out.append((label, path if path.exists() else None))
        return out

    def _load_business_context(self, context_files: list):
        parts = []
        missing = []
        for rel_path, full_path in self._context_entries(context_files):
            if full_path is None:
                missing.append(rel_path)
                continue
            try:
                content = full_path.read_text(encoding="utf-8")
                # Truncate each context file to keep prompt manageable
                if len(content) > 3000:
                    content = content[:3000] + "\n[...truncated]"
                parts.append(f"--- {rel_path} ---\n{content}")
            except (OSError, UnicodeDecodeError) as e:
                # `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so
                # one context file carrying a byte that is not UTF-8 raised
                # out of here, out of `UrgencyAnalyzer.__init__`, and took the
                # always-on daemon down at startup. The configured files are
                # hand-maintained markdown in the DATA overlay, which is
                # exactly where a stray encoding arrives, and the handler
                # beside this one already treats an unreadable context file as
                # a warning-and-skip.
                self.logger.warning(f"Could not read context file {rel_path}: {e}")

        if missing:
            # Say it. A configured file that resolves nowhere used to be
            # skipped by a bare `exists()` test with no log line, so a scoring
            # prompt running on no business context at all looked exactly like
            # one running on all of it.
            self.logger.warning(
                "business context: %d of %d configured file(s) found under no "
                "known root, so they are NOT in the scoring prompt: %s",
                len(missing), len(context_files), ", ".join(missing))

        if parts:
            self.business_context = "BUSINESS CONTEXT:\n" + "\n\n".join(parts)

    def _get_client(self):
        if self.client is None:
            import anthropic
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                raise ValueError(
                    "ANTHROPIC_API_KEY not found in .env. "
                    "Get one at console.anthropic.com and add to .env"
                )
            self.client = anthropic.Anthropic(api_key=api_key, default_headers={"User-Agent": user_agent()})
        return self.client

    def _format_item_prompt(self, item: dict, index: int = None) -> str:
        """Format a single item for the LLM prompt.

        Every field an outsider authored is sanitised, and the whole item is
        wrapped in the labelled untrusted-data frame from
        `scripts/utils/untrusted_input.py`. This used to interpolate the sender,
        the subject, the attachment filenames and the body straight into a bare
        `---` fence, and a bare fence is not a boundary the model can tell from
        the prompt's own structure: a body containing its own `SUBJECT:` and
        `---` lines rendered as if this function had emitted them. The prompt
        that surrounds this text carries the whole business context and asks for
        a routing decision, so the message being scored could argue about how it
        should be scored.

        `source` and `date` are ours - a literal set at fetch time, and a
        timestamp the server stamped and `local_stamp` formatted - so they stay
        outside the frame and identify the item even if the frame is stripped.
        """
        parts = [
            f"FROM: {sanitize_untrusted(str(item.get('sender', 'unknown')))} "
            f"({sanitize_untrusted(str(item.get('sender_email') or item.get('chat_name') or ''))})",
            f"SUBJECT: {sanitize_untrusted(str(item.get('subject', '')))}",
            f"BODY:\n{sanitize_untrusted(str(item.get('body') or '(empty)'))}",
        ]
        if item.get("attachments"):
            # Filenames are chosen by the sender, so they are content, not
            # metadata: "invoice.pdf" and a filename holding a paragraph of
            # instructions arrive by the same route.
            names = ", ".join(sanitize_untrusted(str(a)) for a in item["attachments"])
            parts.append(f"ATTACHMENTS: {names}")

        prefix = f"MESSAGE {index}:\n" if index is not None else ""
        return (
            f"{prefix}SOURCE: {item.get('source', 'unknown')}\n"
            f"DATE: {item.get('date', '')}\n"
            + wrap_untrusted("message-content", "\n".join(parts))
        )

    def _extract_json(self, text: str) -> dict:
        """Extract first valid JSON object from text."""
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```\s*$", "", text)

        # `raw_decode` and not a brace counter. The counter that used to sit
        # here read every `{` and `}` in the text, including the ones inside
        # JSON string values, and failed in three different ways on responses
        # that were valid JSON:
        #
        #   {"summary": "a } b", ...}      the count hit zero mid-string, the
        #                                  object was cut in half -> Unterminated
        #                                  string
        #   {"summary": "use { braces"}    the count never returned to zero, so
        #     ...trailing prose            NO truncation happened at all and the
        #                                  model's closing sentence reached
        #                                  json.loads -> Extra data
        #   {"s": "he said \\"} \\" ok"}     an escaped quote, same as the first
        #
        # Each landed in the caller's JSONDecodeError branch, which returns
        # urgency 5 and "LLM response could not be parsed" - a made-up middling
        # score standing in for a real one the model actually sent.
        #
        # `json.JSONDecoder().raw_decode` parses ONE value from an offset and
        # reports where it ended, so it is string-literal aware by construction
        # and cutting the object out of surrounding prose is what it is for.
        start = text.find("{")
        if start == -1:
            # No object at all. Let json.loads produce the error, so a scalar
            # or an empty response still reaches the caller's existing branch.
            return json.loads(text)
        obj, _end = json.JSONDecoder().raw_decode(text, start)
        return obj

    @observe()
    def analyze(self, item: dict) -> dict:
        """Analyze a single item for urgency. Returns dict with score and details."""
        client = self._get_client()

        system_prompt = self.SYSTEM_PROMPT.format(
            business_context=self.business_context, operator=self.operator_name,
            untrusted_note=UNTRUSTED_SYSTEM_NOTE)

        user_prompt = f"Analyze this incoming message:\n\n{self._format_item_prompt(item)}"

        try:
            r = call_anthropic_with_fallback(
                client=client,
                model=self.model,
                max_tokens=self.max_tokens * _budget_scale([item]),
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_prompt}],
                effort="low",
                output_schema=self.ANALYSIS_SCHEMA,
                skill_name="sentinel.analyze",
            )
            return self._extract_json(r.text)

        except ClaudeStopped as e:
            # A refusal or a spent cap is not an unparseable answer, and
            # scoring it 5 as one hid which of the two had happened.
            self.logger.error(f"LLM analysis stopped: stop_reason={e.stop_reason}"
                              f"{f' category={e.category}' if e.category else ''}; "
                              f"message not scored, will retry")
            return unscored(f"stopped: {e.stop_reason}")
        except json.JSONDecodeError as e:
            self.logger.error(f"LLM returned invalid JSON: {e}; message not scored, will retry")
            return unscored("answer was not JSON")
        except Exception as e:
            self.logger.error(f"LLM analysis failed across all vendors: {e}")
            return None

    @observe()
    @staticmethod
    def _clamp_score(value) -> int:
        """A usable 1-10 urgency, whatever the model returned.

        `urgency_score` was trusted to be an int. A well-formed
        `{"urgency_score": "8"}` parses fine and then raises TypeError at the
        first `score >= threshold`, or later in the digest's sort -- and
        `record_digest_item` had already poisoned `items_by_urgency`, so every
        subsequent evening digest crashed too.
        """
        try:
            score = int(float(value))
        except (TypeError, ValueError):
            return 5
        return max(1, min(10, score))

    def analyze_batch(self, items: list) -> list:
        """Analyze multiple items in a single LLM call. Returns list of dicts."""
        if not items:
            return []
        if len(items) == 1:
            result = self.analyze(items[0])
            return [result]

        client = self._get_client()
        system_prompt = self.SYSTEM_PROMPT.format(
            business_context=self.business_context, operator=self.operator_name,
            untrusted_note=UNTRUSTED_SYSTEM_NOTE)

        # Build combined prompt
        parts = [f"Analyze these {len(items)} incoming messages. For EACH message, provide a separate JSON object.\n"
                 f'Respond with one JSON object {{"results": [...]}} whose array holds one object per message, in the same order.\n']
        for i, item in enumerate(items, 1):
            parts.append(self._format_item_prompt(item, index=i))
        user_prompt = "\n\n".join(parts)

        try:
            r = call_anthropic_with_fallback(
                client=client,
                model=self.model,
                max_tokens=self.max_tokens * min(len(items), 8) * _budget_scale(items),
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_prompt}],
                effort="low",
                output_schema=self.BATCH_SCHEMA,
                skill_name="sentinel.analyze_batch",
            )

            result_text = r.text
            # Strip markdown code fences
            if result_text.startswith("```"):
                result_text = re.sub(r"^```(?:json)?\s*", "", result_text)
                result_text = re.sub(r"\s*```\s*$", "", result_text)

            parsed = json.loads(result_text)
            # The array arrives wrapped as {"results": [...]}, because a
            # structured-output schema is an object at the top. A bare array
            # from a fallback vendor that ignored the wrapper still works.
            if isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
                parsed = parsed["results"]
            if isinstance(parsed, list):
                # ELEMENT type, not just the container. The guard below covers
                # "the reply is not a list"; a reply that IS a list of scalars
                # (`["urgent", "ignore"]`, `[1, 2]`) walked straight past it,
                # got padded to length, and every consumer then called
                # `analysis.get(...)` on a string. AttributeError, out of a
                # daemon whose `run_cycle` and `start` catch neither - the exact
                # shape the comment twenty lines below records for the outer
                # type, one level in. A model that answered the wrong SHAPE is
                # no more trustworthy per-element than per-response, so the
                # whole batch falls back rather than one item.
                if any(not isinstance(entry, dict) for entry in parsed):
                    kinds = sorted({type(e).__name__ for e in parsed
                                    if not isinstance(e, dict)})
                    self.logger.warning(
                        f"Batch LLM analysis returned a list containing "
                        f"{', '.join(kinds)} where objects were required; "
                        f"falling back to individual calls")
                    return [self.analyze(item) for item in items]
                # Pad or truncate to match input length. A missing answer is an
                # unscored item to retry, never a made-up 5.
                while len(parsed) < len(items):
                    parsed.append(unscored("missing from batch response"))
                return parsed[:len(items)]
            elif isinstance(parsed, dict):
                # Single object returned despite batch request — use for first item
                return [parsed] + [self.analyze(item) for item in items[1:]]
            else:
                # Valid JSON that is neither list nor dict -- a bare string or
                # number. There was no else here, so the function fell off the
                # end and returned None; `all_analyses.extend(None)` then raised
                # TypeError, and neither run_cycle nor start() catches it, so one
                # type-deviant response killed the daemon. Same fallback as a
                # parse error, for the same reason.
                self.logger.warning(
                    f"Batch LLM analysis returned {type(parsed).__name__}, not a "
                    f"list or object; falling back to individual calls")
                return [self.analyze(item) for item in items]

        except json.JSONDecodeError as e:
            self.logger.warning(f"Batch LLM analysis JSON parse error ({e}), falling back to individual calls")
            return [self.analyze(item) for item in items]
        except Exception as e:
            self.logger.warning(f"Batch LLM analysis unexpected error ({e}), falling back to individual calls")
            return [self.analyze(item) for item in items]


class QwenUrgencyAnalyzer(UrgencyAnalyzer):
    """The same triage, scored by a private model through the local proxy.

    `llm.provider: qwen`. The prompt is UrgencyAnalyzer's own SYSTEM_PROMPT and
    item format, joined into one user message exactly as the 2026-10-11 study
    sent it (`proxy_transport.call_model` carries no system block), at
    temperature 0. The answer is parsed by the same `_extract_json`, so the
    output schema is unchanged. The proxy alias is `llm.qwen_model` in the
    private config, required: it names a machine of the operator's, so it has
    no default here.

    Items are scored one per call by default (`llm.qwen_batch_size: 1`): the
    study that accepted this model measured it one item at a time, and a batch
    is a different input it was never measured on. Raise it only with a
    measurement.

    Every result is one of three things, and the caller treats them apart:
      * the analysis dict: scored;
      * `unscored(reason)`: the model answered and the answer is unusable (cut
        off by its budget, empty, not JSON). Retried next cycle, counted;
      * None: the model did not answer (no key, connection refused, timeout,
        an HTTP error). An outage: the fallback rule takes over. Once one call
        in a cycle is an outage the rest of the cycle does not wait on it.
    """

    provider = "qwen"

    def __init__(self, config: dict, logger: logging.Logger):
        super().__init__(config, logger)
        self.model = str(config.get("qwen_model") or "").strip()
        if not self.model:
            raise ValueError("llm.provider is qwen but llm.qwen_model (the proxy's model alias) is not set")
        self.max_tokens = int(config.get("qwen_max_tokens", 600))
        self.timeout = float(config.get("qwen_timeout_seconds", 120))
        self.batch_size = max(1, int(config.get("qwen_batch_size", 1)))
        self.down = False
        self.last_error = ""
        # The upstream model the proxy reported for the last answer: the alias
        # in `self.model` can be served by more than one, and each scores on
        # its own scale. Stamped on every result as `scored_model`.
        self.last_model = None

    def begin_cycle(self):
        self.down = False

    def _outage(self, why: str):
        if not self.down:
            self.logger.error(f"Qwen unavailable ({why}); the fallback rule scores email until it returns")
        self.down = True
        self.last_error = why
        return None

    def _call(self, prompt: str, budget: int):
        """The answer text, or None after marking an outage."""
        from scripts.utils.api import load_api_key
        from scripts.utils.proxy_transport import call_model
        if not load_api_key("CLIPROXY_API_KEY", required=False):
            return self._outage("CLIPROXY_API_KEY is not set")
        self.last_model = None
        try:
            text, self.last_model = call_model(self.model, prompt, temperature=0.0,
                                               max_tokens=budget, timeout=self.timeout,
                                               with_model=True)
            return text
        except ImportError as e:
            # `openai` ships in the `ai-extra` group; a venv synced without it
            # would otherwise raise out of the whole cycle.
            return self._outage(f"the openai package is not installed ({e})")
        except RuntimeError as e:
            # `call_model` raises RuntimeError for everything. The ones raised
            # FROM an exception (connection, timeout, HTTP status, a retried
            # 503) mean the model never answered; the ones raised from nothing
            # are its own verdicts on an answer that arrived (cut off by the
            # budget, empty, blocked).
            if e.__cause__ is not None:
                return self._outage(str(e)[:200])
            raise

    def _system(self) -> str:
        return self.SYSTEM_PROMPT.format(
            business_context=self.business_context, operator=self.operator_name,
            untrusted_note=UNTRUSTED_SYSTEM_NOTE)

    @observe()
    def analyze(self, item: dict) -> dict:
        if self.down:
            return None
        prompt = (self._system() + "\n\nAnalyze this incoming message:\n\n"
                  + self._format_item_prompt(item))
        try:
            text = self._call(prompt, self.max_tokens * _budget_scale([item]))
        except RuntimeError as e:
            self.logger.error(f"Qwen answer unusable ({str(e)[:160]}); message not scored, will retry")
            return unscored(str(e)[:120])
        if text is None:
            return None
        try:
            result = self._extract_json(text)
        except json.JSONDecodeError as e:
            self.logger.error(f"Qwen returned invalid JSON: {e}; message not scored, will retry")
            return unscored("answer was not JSON")
        if not isinstance(result, dict):
            return unscored(f"answer was a {type(result).__name__}, not an object")
        # Always overwritten: a "scored_model" the model itself wrote (an email
        # can ask it to) must never choose the threshold.
        result["scored_model"] = self.last_model
        return result

    def analyze_batch(self, items: list) -> list:
        if self.batch_size == 1 or len(items) == 1:
            return [self.analyze(item) for item in items]
        if self.down:
            return [None] * len(items)
        parts = [f"Analyze these {len(items)} incoming messages. For EACH message, provide a separate JSON object.\n"
                 f'Respond with one JSON object {{"results": [...]}} whose array holds one object per message, in the same order.\n']
        parts += [self._format_item_prompt(item, index=i) for i, item in enumerate(items, 1)]
        try:
            text = self._call(self._system() + "\n\n" + "\n\n".join(parts),
                              self.max_tokens * len(items) * _budget_scale(items))
        except RuntimeError as e:
            self.logger.warning(f"Qwen batch answer unusable ({str(e)[:160]}); scoring one by one")
            return [self.analyze(item) for item in items]
        if text is None:
            return [None] * len(items)
        try:
            parsed = self._extract_json(text)
        except json.JSONDecodeError:
            return [self.analyze(item) for item in items]
        results = parsed.get("results") if isinstance(parsed, dict) else None
        if not isinstance(results, list) or any(not isinstance(r, dict) for r in results):
            return [self.analyze(item) for item in items]
        results = results[:len(items)]
        for r in results:
            r["scored_model"] = self.last_model
        while len(results) < len(items):
            results.append(unscored("missing from batch response"))
        return results


# ============================================================
# Telegram Notifier
# ============================================================

def resolve_notify_target() -> str:
    """Bot-resolvable notification target, read from .env only.

    SENTINEL_TELEGRAM_TARGET -> ODIN_CADENCE_TELEGRAM_TARGET -> "" (no send),
    mirroring scripts/reminders-notify.py so every HEADING OS notification
    lands in one place.

    The sentinel_config.yaml `notification.target_chat` name is deliberately
    NOT consulted: it names a channel for the userbot, and the bot transport
    cannot resolve a human-readable channel name.

    Each candidate is stripped BEFORE the chain, never once at the end. Stripped
    afterwards, a whitespace-only `SENTINEL_TELEGRAM_TARGET` is truthy, so it
    wins the `or` chain over a perfectly good `ODIN_CADENCE_TELEGRAM_TARGET` and
    only then collapses to "". Measured 2026-08-07: with the two set to `"   "`
    and `"100200300"`, this returned `""`, `Sentinel.start` logged "alerts will
    NOT be delivered", and the daemon ran on watching everything and telling
    nobody. A trailing space on an edited `.env` line is the ordinary way a
    value becomes whitespace-only, and the fallback exists precisely so that a
    missing specific target is survivable.
    """
    load_env(WORKSPACE_ROOT)
    return (
        (os.environ.get("SENTINEL_TELEGRAM_TARGET") or "").strip()
        or (os.environ.get("ODIN_CADENCE_TELEGRAM_TARGET") or "").strip()
    )


class TelegramNotifier:
    """Send urgent notifications through the HEADING OS notifications bot.

    Delivery is the shared bot transport (scripts/utils/telegram_notify), not
    the userbot client: a Bot API sendMessage always push-notifies, whereas a
    message the userbot posts into a channel it already owns does not
    reliably. Because the bot needs no Telethon session, this notifier is
    independent of whether Telegram *reading* is connected.
    """

    def __init__(self, target: str, logger: logging.Logger, dry_run: bool = False):
        self.target = target
        self.logger = logger
        self.dry_run = dry_run

    async def _send(self, message: str) -> bool:
        """Hand one message to the bot transport. Never raises."""
        if self.dry_run:
            self.logger.info(f"Dry-run: notification NOT sent:\n{message}")
            return True
        # notify() is blocking urllib; keep the daemon's event loop free.
        ok = await asyncio.to_thread(telegram_notify.notify, self.target, message)
        if not ok:
            self.logger.error(
                "Notification send failed (see telegram_notify log). Check "
                "TELEGRAM_NOTIFY_BOT_TOKEN and SENTINEL_TELEGRAM_TARGET in .env."
            )
        return ok

    async def send_notification(self, item: dict, analysis: dict):
        if await self._send(self._format_message(item, analysis)):
            # `_clamp_score(analysis.get(...))`, not `analysis['urgency_score']`:
            # the alert is already on the wire here, and a missing key used to
            # log "Notification failed" for a delivered alert, skip the
            # `urgent_sent` count and re-alert a byte-identical repeat.
            score = UrgencyAnalyzer._clamp_score(analysis.get("urgency_score"))
            self.logger.info(
                f"Notification sent: [{score}/10] {item.get('subject', '')}")

    async def send_digest(self, message: str):
        if await self._send(message):
            self.logger.info("Digest sent")

    @staticmethod
    def _scored_line(analysis: dict) -> str:
        """Which model scored it and the threshold it was held to; empty when unknown."""
        model, bar = analysis.get("scored_model"), analysis.get("threshold")
        if not model and bar is None:
            return ""
        return f"\n\U0001f9e0 Scored by {model or 'unreported model'}, threshold {bar}"

    def _format_message(self, item: dict, analysis: dict) -> str:
        score = UrgencyAnalyzer._clamp_score(analysis.get("urgency_score"))

        if score >= 9:
            icon = "\U0001f534"  # red circle
            label = "CRITICAL"
        elif score >= 7:
            icon = "\U0001f7e1"  # yellow circle
            label = "HIGH"
        else:
            icon = "\U0001f7e0"  # orange circle
            label = "ALERT"

        source = item.get("source", "unknown").upper()
        source_icon = "\U0001f4e9" if source == "EMAIL" else "\U0001f4ac"

        sender_line = item.get("sender", "Unknown")
        if item.get("sender_email"):
            sender_line += f" <{item['sender_email']}>"

        subject_line = ""
        if item.get("subject") and item["source"] == "email":
            subject_line = f"\n\U0001f4cb Subject: {item['subject']}"

        # Snippet of original
        body = item.get("body", "")
        snippet = body[:300].strip()
        if len(body) > 300:
            snippet += "..."

        msg = f"""{icon} {label} [{score}/10] -- {source}

{source_icon} From: {sender_line}
\U0001f4c5 {item.get('date', 'unknown')}{subject_line}

Summary: {analysis.get('summary', 'N/A')}

\u26a1 Why urgent: {analysis.get('reason', 'N/A')}

\u2705 Action: {analysis.get('recommended_action', 'Review manually')}{self._scored_line(analysis)}

--- Original snippet ---
\"{snippet}\""""

        return msg


# ============================================================
# Sentinel Daemon
# ============================================================

class Sentinel:
    """Main orchestrator."""

    def __init__(self, config_path: Path | None = None, dry_run: bool = False,
                 once: bool = False):
        config_path = config_file() if config_path is None else config_path
        self.config = SentinelConfig(config_path)
        self.dry_run = dry_run
        # `once` is a LIVE single cycle (`--check`), not `dry_run`: it is what
        # the 15-minute task `install_sentinel_schedule` in
        # `scripts/utils/schedule.py` runs, and `--test` cannot stand in for it
        # because a dry run neither sends nor writes state back.
        self.once = once
        self.logger = self._setup_logging()
        # A dry run reads the real state but never writes it back.
        self.state = StateManager(read_only=dry_run)
        self.email_source = EmailSource(self.config.email, self.state, self.logger)
        self.telegram_source = TelegramSource(self.config.telegram, self.state, self.logger)
        # `llm.provider` picks who scores email and Telegram. `anthropic`, the
        # default, is the path this daemon has always run. `qwen` scores through
        # the local proxy and brings the model-free rule in as its outage
        # fallback. The calendar invite analyzer keeps the Anthropic path either
        # way (operator decision, 2026-10-11).
        provider = str(self.config.llm.get("provider") or "anthropic").strip().lower()
        if provider not in ("anthropic", "qwen"):
            raise ValueError(f"llm.provider must be anthropic or qwen, not {provider!r}")
        calendar_analyzer = UrgencyAnalyzer(self.config.llm, self.logger)
        self.analyzer = (QwenUrgencyAnalyzer(self.config.llm, self.logger)
                         if provider == "qwen" else calendar_analyzer)
        self.rule = None
        self._crm_index = None
        self._crm_loaded_at = None
        if provider == "qwen":
            try:
                self.rule = sentinel_rule.load_rule(self.config.llm.get("rule_weights_file"))
                self.logger.info(
                    f"Fallback rule loaded from {self.rule.source}; "
                    f"{len(self.rule.missing)} feature(s) imputed at their training mean "
                    f"because this runtime does not compute them: {', '.join(self.rule.missing) or 'none'}")
            except sentinel_rule.RuleUnavailable as e:
                self.logger.error(f"Fallback rule UNAVAILABLE ({e}): while Qwen is down, "
                                  f"email waits for it and only VIP senders alert")
        self.sent_reader = SentFolderReader(
            self.logger, int(self.config.email.get("sent_lookback_days", 14)))
        self.notifier = None  # Initialized after Telegram connects
        self._running = True
        self._stop_event = asyncio.Event()
        self._consecutive_email_failures = 0
        self._consecutive_tg_failures = 0
        self._heartbeat_task = None

        # Calendar invite monitoring
        self.invite_source = MeetingInviteSource(self.config.calendar, self.state, self.logger)
        self.policy_engine = CalendarPolicyEngine(
            self.config.calendar, self.config.timezone, self.logger,
            analyzer=calendar_analyzer,
        )

    def _setup_logging(self) -> logging.Logger:
        # R12: mint trace ID + install record factory before any handler so
        # every line carries [trace_id].
        tracing.mint()
        install_log_factory()
        logger = logging.getLogger("sentinel")
        logger.setLevel(getattr(logging, self.config.log_level, logging.INFO))

        # File handler (rotating, 5MB x 3 files)
        fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [%(trace_id)s] %(message)s"))
        logger.addHandler(fh)

        # Console handler
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [%(trace_id)s] %(message)s"))
        logger.addHandler(ch)

        return logger

    async def _heartbeat_loop(self):
        """R14: dedicated 60s liveness beat, decoupled from the up-to-15-min
        work cycle. A beat tied to the work loop would advance the heartbeat
        at most every check_interval, forcing the watchdog grace above the
        cycle length and delaying crash detection. One file:
        `daemon_heartbeat.heartbeats_dir()/sentinel.json`, the data repository's
        `runtime:daemon-heartbeat/tmp`. Beats once immediately, then
        every 60s until cancelled at shutdown."""
        try:
            while True:
                daemon_heartbeat.beat("sentinel")
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            return

    def install_signal_handlers(self) -> str:
        """Wire SIGINT/SIGTERM into `_stop_event` THROUGH THE RUNNING LOOP.

        `main()` registers the same intent with `signal.signal`, and that alone
        does not work. A `signal.signal` handler runs between bytecodes; the
        loop is blocked in `select()` with no pending callback, so nothing wakes
        it and `Event.set()` sits unnoticed until the current
        `asyncio.wait_for` timeout expires on its own. Measured 2026-08-27
        against the exact shape of the wait in `start()`: SIGTERM delivered at
        2 s, `wait_for(..., timeout=30)` returned at **29.54 s**. With a real
        check_interval that is up to fifteen minutes of "shutting down".

        `loop.add_signal_handler` writes to the loop's self-pipe, which IS the
        thing `select()` is watching, so the wait returns immediately: the same
        probe returned at 1.84 s, i.e. the moment the signal arrived.

        Returns the mechanism actually installed, so a caller can say which one
        it got. Windows' event loops raise NotImplementedError here and keep the
        `signal.signal` path `main()` already set up; that is the platform's
        limit, not a choice.
        """
        loop = asyncio.get_running_loop()

        def _request_stop(name: str) -> None:
            self.logger.info(f"Shutdown signal received ({name}); stopping.")
            self._running = False
            self._stop_event.set()

        installed = "loop"
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _request_stop, sig.name)
            except (NotImplementedError, AttributeError, ValueError, RuntimeError):
                # NotImplementedError: Windows. ValueError/RuntimeError: not the
                # main thread. Either way the process keeps the handler main()
                # installed, which stops the loop late rather than not at all.
                installed = "signal.signal"
        return installed

    async def start(self):
        self.install_signal_handlers()
        self.logger.info("=" * 50)
        self.logger.info("Sentinel starting...")
        self.logger.info(f"Check interval: {self.config.check_interval // 60} minutes")
        self.logger.info(f"Urgency threshold: {self.config.urgency_threshold}")
        self.logger.info("Per-model thresholds: " + (
            ", ".join(f"{m}={t}" for m, t in self.config.thresholds_by_model.items())
            or "none (urgency_threshold for every model)"))
        self.logger.info(f"Dry run: {self.dry_run}")
        self.logger.info("=" * 50)

        # Write PID file with file lock (SEC-016). Its directory is created by
        # `_bind_runtime_files`, under the runtime store's 0700 root.
        my_pid = os.getpid()
        # "a+" and NOT "w": "w" truncates at open(2), before the flock can
        # fail, so a losing second instance emptied the live daemon's PID file
        # and broke `--status` / `--stop`. "a+" leaves it as found.
        self._pid_file_handle = open(PID_FILE, "a+")
        try:
            if sys.platform != "win32":
                import fcntl
                fcntl.flock(self._pid_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # WINDOWS HAS NO LOCK HERE: two daemons can run at once there. This
            # daemon runs on Linux, where the flock above is real; closing the
            # gap needs an msvcrt byte-range lock held for the process lifetime.
        except (IOError, OSError):
            self._pid_file_handle.close()
            self.logger.error("Another Sentinel instance is already running (PID file locked)")
            sys.exit(1)
        # The lock is held from here on, so this is the first moment at which
        # emptying the file is safe. "a+" left any previous content in place.
        self._pid_file_handle.seek(0)
        self._pid_file_handle.truncate()
        self._pid_file_handle.write(str(my_pid))
        self._pid_file_handle.flush()
        if sys.platform == "win32":
            # Close immediately on Windows - keeping it open locks it from readers
            self._pid_file_handle.close()
            self._pid_file_handle = None
        self.logger.info(f"PID file written: {PID_FILE} (PID: {my_pid})")

        # R14: start the dedicated 60s liveness heartbeat before the (possibly
        # slow) Telegram/Exchange connects so the watchdog sees the daemon
        # alive within the first second of boot.
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # Notifications go out over the bot transport, which needs no Telethon
        # session -- so the notifier is built before (and independently of) the
        # Telegram *reading* connect below.
        target = resolve_notify_target()
        self.notifier = TelegramNotifier(target, self.logger, dry_run=self.dry_run)
        if self.dry_run:
            self.logger.info("Dry-run: notifications will be logged, not sent")
        elif target:
            self.logger.info(f"Notifications route to bot target {target}")
        else:
            self.logger.error(
                "No notification target set (SENTINEL_TELEGRAM_TARGET / "
                "ODIN_CADENCE_TELEGRAM_TARGET in .env) -- alerts will NOT be delivered"
            )

        # Connect Telegram (needed for reading Telegram sources)
        # `enabled OR not dry_run` is always true in live mode, so the flag was
        # dead at the one place it matters: an operator who set
        # `telegram.enabled: false` still had connect() called, and connect()
        # raises ValueError on absent credentials with no handler here -- so
        # email-only monitoring could not boot at all.
        if self.config.telegram.get("enabled", True):
            await self.telegram_source.connect()

        # Connect Exchange
        if self.config.email.get("enabled", True):
            try:
                self.email_source.connect()
                # Share Exchange account with invite source
                if self.config.calendar.get("enabled", False):
                    self.invite_source.account = self.email_source.account
                    self.logger.info("Calendar invite monitoring enabled")
            except Exception as e:
                self.logger.error(f"Exchange connection failed: {e}")
                self.logger.info("Will retry on next cycle")

        # Run loop
        try:
            while self._running:
                # Reconnect Telegram at cycle start (was disconnected after last cycle)
                if self.config.telegram.get("enabled", True):
                    try:
                        if not self.telegram_source.client or not self.telegram_source.client.is_connected():
                            await self.telegram_source.connect()
                    except Exception as e:
                        self.logger.error(f"Telegram reconnect at cycle start failed: {e}")

                await self.run_cycle()
                if self.dry_run or self.once:
                    break
                # Deadman: a completed work cycle pings the Healthchecks.io
                # check so a silently-stuck sentinel (hung Telegram/Exchange,
                # crashed loop) trips an external alert. Best-effort, never
                # raises (see scripts/utils/healthchecks.ping).
                hc_ping("STEWARD_HC_SENTINEL")

                # Disconnect Telegram during sleep to release SQLite session lock
                if self.config.telegram.get("enabled", True):
                    try:
                        await self.telegram_source.disconnect()
                        self.logger.debug("Telegram disconnected (releasing session lock for sleep)")
                    except Exception as e:
                        # Disconnect can fail on network errors, already-closed sockets, or telethon internals.
                        # Lock release will happen via GC; logging the specific cause aids debugging.
                        self.logger.debug(f"Telegram disconnect-for-sleep fallback: {e}")

                self.logger.info(f"Next check in {self.config.check_interval // 60} minutes")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.config.check_interval)
                except asyncio.TimeoutError:
                    pass  # Normal: interval elapsed, continue loop
                if self._stop_event.is_set():
                    break
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def run_cycle(self):
        cycle_start = datetime.now(timezone.utc)
        self.logger.info("-" * 40)
        self.logger.info(f"Cycle starting at {cycle_start.isoformat()[:19]}")
        try:
            await self._run_cycle_body(cycle_start)
        finally:
            self.state.save()

    async def _run_cycle_body(self, cycle_start):

        # Reset daily digest counters if new day
        now_local = datetime.now(self.config.timezone)
        today_str = now_local.strftime("%Y-%m-%d")
        self.state.reset_daily_digest(today_str)
        self.state.reset_calendar_daily(today_str)

        # Cleanup old notification hashes and rotate stale state
        cooldown = self.config.notification.get("dedup_cooldown_minutes", 60)
        self.state.cleanup_old_hashes(cooldown)
        self.state.rotate_old_state(max_age_days=30)
        self.state.prune_guardian(keep_days=max(int(self.config.digest.get("reply_window_days", 3)), 2) + 1)
        self.analyzer.begin_cycle()
        self.state.data["guardian"]["waiting_for_model"] = 0

        items = []

        # --- Email check ---
        if self.config.email.get("enabled", True):
            try:
                email_items = self.email_source.check_new()
                items.extend(email_items)
                self.state.data["digest"]["emails_checked"] += len(email_items)
                self._consecutive_email_failures = 0
                # Keep invite source in sync with email account
                if self.config.calendar.get("enabled", False) and not self.invite_source.account:
                    self.invite_source.account = self.email_source.account
            except Exception as e:
                self.logger.error(f"Email check failed: {e}")
                self._consecutive_email_failures += 1
                # Force reconnect on connection errors (socket reset, timeout, etc.)
                if "Connection" in str(e) or "connection" in str(e) or "timeout" in str(type(e).__name__).lower():
                    self.logger.warning("Connection error -- forcing reconnect next cycle")
                    self.email_source.account = None
                    self.invite_source.account = None
                elif self._consecutive_email_failures >= 3:
                    self.logger.warning("3 consecutive email failures -- reconnecting next cycle")
                    self.email_source.account = None
                    self.invite_source.account = None

        # --- Meeting invite check ---
        if self.config.calendar.get("enabled", False) and self.invite_source.account:
            try:
                await self._process_meeting_invites()
            except Exception as e:
                self.logger.error(f"Meeting invite check failed: {e}")

        # --- Telegram check (with sqlite3 DB lock retry) ---
        if self.config.telegram.get("enabled", True):
            tg_success = False
            for tg_attempt in range(1, 4):  # up to 3 attempts
                try:
                    tg_items = await self.telegram_source.check_new()
                    items.extend(tg_items)
                    self.state.data["digest"]["tg_messages_checked"] += len(tg_items)
                    self._consecutive_tg_failures = 0
                    tg_success = True
                    break
                except (sqlite3.OperationalError, OSError) as e:
                    if 'locked' in str(e).lower() and tg_attempt < 3:
                        delay = 2 * tg_attempt
                        self.logger.warning(
                            f"Session DB locked (attempt {tg_attempt}/3), retrying in {delay}s..."
                        )
                        try:
                            await self.telegram_source.disconnect()
                        except Exception as disc_err:
                            # Retry-recovery disconnect; any error here is non-fatal
                            # since the next connect() attempts a fresh session.
                            self.logger.debug(f"Telegram retry-disconnect fallback: {disc_err}")
                        await asyncio.sleep(delay)
                        continue
                    self.logger.error(f"Telegram check failed: {e}")
                    self._consecutive_tg_failures += 1
                    break
                except Exception as e:
                    self.logger.error(f"Telegram check failed: {e}")
                    self._consecutive_tg_failures += 1
                    break

            if not tg_success and self._consecutive_tg_failures >= 3:
                self.logger.warning("3 consecutive Telegram failures -- reconnecting")
                try:
                    await self.telegram_source.disconnect()
                    await self.telegram_source.connect()
                except Exception as re_err:
                    self.logger.error(f"Telegram reconnect failed: {re_err}")

        # --- Sent folder: what the operator already answered (read only) ---
        # Read when something uses it: the digest's "awaits your reply" in
        # guardian mode, and the fallback rule's "thread you wrote in".
        if (self.config.mode == "guardian" or self.rule is not None) and self.email_source.account:
            self.sent_reader.refresh(self.email_source.account)

        # --- The model is back: re-score what the fallback rule handled ---
        await self._rescore_rule_items()

        # --- Analyze and notify ---
        fed = await self._analyze_and_notify(items) if items else set()

        # --- Unscored records no cycle will retry ---
        self._settle_unscored(fed)

        # --- Check if digest is due ---
        await self._check_digest_schedule(now_local)

        # Update state
        self.state.data["last_run"] = cycle_start.isoformat()
        self.state.data["email"]["last_check"] = cycle_start.isoformat()
        self.state.data["telegram"]["last_check"] = cycle_start.isoformat()
        self.state.save()

        elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        self.logger.info(f"Cycle complete: {len(items)} items in {elapsed:.1f}s")

    def _mark_item_processed(self, item: dict) -> None:
        """Record that an item reached a terminal outcome this cycle.

        Each source has its own memory of what it has read: an id set for
        email, a per-chat high-water mark for Telegram. Both are advanced HERE
        and nowhere else, so "fetched" and "accounted for" stay separate states
        and a failure between them costs a re-read rather than the message.
        """
        if item.get("source") == "email" and item.get("message_id"):
            self.state.mark_email_processed(item["message_id"])
        elif (item.get("source") == "telegram" and item.get("cursor_id")
              and item["cursor_id"] > self.state.get_telegram_last_id(item.get("chat_id", ""))):
            # Forward only: an item settled late (a retried one) must not
            # rewind a cursor a newer item of the same chat already moved.
            self.state.set_telegram_last_id(
                item.get("chat_id", ""), item.get("chat_name", ""),
                item["cursor_id"])

    async def _analyze_and_notify(self, items: list) -> set:
        """Score and notify; returns the content hashes of the items it analysed."""
        max_notifs = self.config.notification.get("max_notifications_per_cycle", 10)
        sent_count = 0

        # Pre-process: dedup and collect items needing LLM analysis.
        #
        # NOTHING is marked processed here. Marking before analysis meant an
        # Anthropic outage, or a cycle that hit max_notifications_per_cycle,
        # permanently consumed the message: never retried, never in the digest,
        # never notified. Silence is the worst failure an urgency monitor has.
        # A message is now marked only once it reaches a terminal outcome
        # below. Email was fixed here first and Telegram was left behind,
        # advancing its per-chat cursor inside the fetch itself; both sources
        # go through `_mark_item_processed` now.
        items_to_analyze = []  # (item, content_hash) pairs
        for item in items:
            # The subject is part of the identity. Without it, two different
            # messages from one sender whose first 500 body characters match
            # hashed the same and the second was dropped as a duplicate,
            # notifying nobody. That is routine, not exotic: a templated alert
            # ("Please review the attached.") or a reply quoting the same
            # thread carries its whole meaning in the subject line. The field
            # separator matters too - without one, sender "ab" + body "c" and
            # sender "a" + body "bc" are the same string.
            #
            # Widening the key invalidates the hashes already in state, so the
            # first cycle after this lands may repeat a notification. That is
            # the safe direction for a monitor whose worst failure is silence.
            content_hash = hashlib.md5(
                "\x1f".join([
                    str(item.get("source") or ""),
                    str(item.get("sender") or ""),
                    str(item.get("subject") or ""),
                    str(item.get("body") or "")[:500],
                ]).encode(),
                usedforsecurity=False,
            ).hexdigest()

            if self.state.is_already_notified(content_hash):
                # Terminal: this exact content already reached the operator, so
                # there is nothing left to do with it. It must still be marked.
                # An unmarked duplicate is re-fetched and re-hashed every cycle
                # until the hash ages out, and for Telegram - whose memory is a
                # per-chat cursor rather than a per-message id - the cursor
                # never advances past it at all, so the dialog is re-read
                # forever and every message behind the duplicate is re-analysed
                # with it.
                self.logger.debug(f"Skipping duplicate: {item.get('subject', '')}")
                self._mark_item_processed(item)
                continue

            items_to_analyze.append((item, content_hash))

        if not items_to_analyze:
            return set()

        guardian = self.config.mode == "guardian"
        g = self.state.data["guardian"]
        now_iso = datetime.now(timezone.utc).isoformat()
        for item, content_hash in items_to_analyze:
            item["_hash"] = content_hash
            item.setdefault("_seen_at", (item.get("rule_meta") or {}).get("received") or now_iso)
            item["_attempt"] = (g["unscored"].get(content_hash) or {}).get("attempts", 0)

        # Batch LLM analysis (up to 8 items per call to stay within token limits)
        BATCH_SIZE = 8
        all_analyses = []
        for batch_start in range(0, len(items_to_analyze), BATCH_SIZE):
            batch = items_to_analyze[batch_start:batch_start + BATCH_SIZE]
            batch_items = [pair[0] for pair in batch]
            batch_results = self.analyzer.analyze_batch(batch_items)
            all_analyses.extend(batch_results)

        # Process results
        waiting = 0
        for (item, content_hash), analysis in zip(items_to_analyze, all_analyses):
            analysis, scored_by = self._resolve_analysis(item, content_hash, analysis)
            if analysis is None:
                if scored_by == "retry":
                    waiting += 1
                    continue
                if scored_by == "gave-up":
                    continue
                # The model did not answer and nothing else could score it. NOT
                # marked processed: the next cycle retries it. The VIP fallback
                # below is a best-effort notify on top of that, not a substitute
                # for the retry. Guardian mode never alerts on Telegram.
                waiting += 1
                realtime = not guardian or item.get("source") == "email"
                if item.get("is_vip") and self.notifier and realtime:
                    fallback_analysis = {
                        "urgency_score": 7,
                        "reason": "VIP sender (LLM unavailable)",
                        "summary": item.get("subject", "Message from VIP contact"),
                        "recommended_action": "Review this message manually",
                    }
                    try:
                        await self.notifier.send_notification(item, fallback_analysis)
                        self.state.mark_notified(content_hash)
                        sent_count += 1
                    except Exception as e:
                        self.logger.error(f"Fallback notification failed: {e}")
                continue

            score = UrgencyAnalyzer._clamp_score(analysis.get("urgency_score"))
            self.state.record_digest_item(item, score)
            # Terminal: it has been scored and it is in the digest, so it is
            # accounted for whether or not a notification goes out below.
            self._mark_item_processed(item)
            g["unscored"].pop(content_hash, None)

            # Guardian mode wakes the operator for email only; Telegram waits for
            # the digest. Under the fallback rule the bar is lower, because the
            # rule misses more and the model re-scores its items on return. A
            # model's score is held to that model's own bar.
            realtime = not guardian or item.get("source") == "email"
            if scored_by == "rule":
                bar, how = self.config.rule_wake_threshold, " rule"
            else:
                bar = self.config.threshold_for(analysis.get("scored_model"))
                analysis["threshold"] = bar
                how = f" {analysis.get('scored_model') or scored_by} bar {bar}"
            self.logger.info(
                f"  [{score}/10{how}] {item.get('source')}: "
                f"{item.get('sender')} - {item.get('subject', '')[:60]}"
            )
            notified = False
            if realtime and score >= bar and self.notifier and sent_count < max_notifs:
                try:
                    await self.notifier.send_notification(item, analysis)
                    self.state.mark_notified(content_hash)
                    self.state.data["digest"]["urgent_sent"] += 1
                    sent_count += 1
                    notified = True
                except Exception as e:
                    self.logger.error(f"Notification failed: {e}")

            self.state.record_guardian_item(item, score, analysis, scored_by, notified, bar)
            if scored_by == "rule":
                g["rule_items"].append({"item": _storable(item), "rule_score": score,
                                        "notified": notified, "attempts": 0})
                if len(g["rule_items"]) > StateManager.RULE_ITEM_CAP:
                    dropped = len(g["rule_items"]) - StateManager.RULE_ITEM_CAP
                    self.logger.warning(f"{dropped} rule-scored item(s) dropped from the re-score queue "
                                        f"(cap {StateManager.RULE_ITEM_CAP}); they keep the rule's score")
                    g["rule_items"] = g["rule_items"][-StateManager.RULE_ITEM_CAP:]
            if item.get("source") == "email" and item.get("sender_email"):
                g["sender_seen"].setdefault(item["sender_email"].lower(), []).append(item["_seen_at"])

        g["waiting_for_model"] = waiting
        return {content_hash for _, content_hash in items_to_analyze}

    def _max_score_attempts(self) -> int:
        return max(1, int(self.config.llm.get("max_score_attempts", 3)))

    # A retry comes from the source feeding the same item again on the next
    # cycle. An item no source feeds any more (an email read on the phone, so
    # no longer unread) gets no further attempt, and after this long without
    # one its record is listed under gave_up instead of "retrying" forever.
    UNSCORED_STALE = timedelta(hours=6)

    def _settle_unscored(self, fed: set) -> None:
        """Resolve the unscored records that can never reach another attempt.

        Two kinds. A Telegram record whose chat cursor has passed its messages
        is superseded: a later item of that chat, which carried the same
        messages, was scored, so the record is dropped. Any record with no
        attempt for `UNSCORED_STALE` is listed under gave_up and dropped,
        unless a source fed its item this cycle (`fed`, the content hashes
        `_analyze_and_notify` analysed): that item is still being retried, and
        a gap is only the daemon having been stopped or the model down.
        MEASURED 2026-10-26 by a reproduction run, before `fed`: a record with
        one unusable attempt, aged past the window and fed again, went to
        gave_up and its next unusable answer restarted at attempt 1.
        MEASURED 2026-10-26 by a reproduction run, before this: a record of
        each kind stayed at attempt 1 of 3 through every following cycle, and
        the digest said "retrying" for each, which no cycle ever did.
        """
        g = self.state.data["guardian"]
        now = datetime.now(timezone.utc)
        for content_hash, rec in list(g["unscored"].items()):
            if content_hash in fed:
                continue
            cursor_id = rec.get("cursor_id")
            if (rec.get("source") == "telegram" and cursor_id
                    and self.state.get_telegram_last_id(rec.get("chat_id") or "") >= cursor_id):
                self.logger.info(f"Unscored item superseded by a later scored item of the same chat: "
                                 f"{str(rec.get('subject', ''))[:60]}")
                g["unscored"].pop(content_hash)
                continue
            try:
                last = datetime.fromisoformat(rec.get("last_at") or rec.get("first_at") or "")
            except (TypeError, ValueError):
                last = None
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last is not None and now - last < self.UNSCORED_STALE:
                continue
            reason = f"no longer fetched, not retried; {rec.get('reason', '')}"
            self.logger.error(f"Could not be scored after {rec.get('attempts', 0)} attempt(s) ({reason}); "
                              f"listed in the digest: {str(rec.get('subject', ''))[:60]}")
            g["gave_up"].append({"source": rec.get("source"), "sender": rec.get("sender"),
                                 "subject": rec.get("subject", ""), "reason": reason,
                                 "seen_at": rec.get("seen_at") or rec.get("first_at") or now.isoformat()})
            g["unscored"].pop(content_hash)

    def _resolve_analysis(self, item: dict, content_hash: str, analysis):
        """(analysis, scored_by) for one model result, or (None, why).

        `why` is "retry" for an unusable answer that gets another cycle,
        "gave-up" for one that has had its attempts and is now listed in the
        digest, and None for an outage nothing could cover.
        """
        g = self.state.data["guardian"]
        if is_unscored(analysis):
            rec = g["unscored"].setdefault(content_hash, {
                "attempts": 0, "first_at": datetime.now(timezone.utc).isoformat(),
                "source": item.get("source"), "sender": item.get("sender"),
                "subject": str(item.get("subject", ""))[:120]})
            rec["attempts"] += 1
            rec["reason"] = analysis[UNSCORED_KEY]
            # What `_settle_unscored` needs to tell a record still being
            # retried from one nothing will feed again.
            rec["last_at"] = datetime.now(timezone.utc).isoformat()
            rec["seen_at"] = item.get("_seen_at")
            if item.get("source") == "telegram":
                rec["chat_id"], rec["cursor_id"] = item.get("chat_id"), item.get("cursor_id")
            if rec["attempts"] < self._max_score_attempts():
                self.logger.warning(
                    f"Not scored ({rec['reason']}), attempt {rec['attempts']} of "
                    f"{self._max_score_attempts()}; retrying next cycle: {item.get('subject', '')[:60]}")
                return None, "retry"
            g["unscored"].pop(content_hash, None)
            ruled = self._rule_analysis(item)
            if ruled is not None:
                self.logger.warning(f"Not scored after {rec['attempts']} attempts; the fallback rule "
                                    f"scores it: {item.get('subject', '')[:60]}")
                return ruled, "rule"
            self.logger.error(f"Could not be scored after {rec['attempts']} attempts ({rec['reason']}); "
                              f"listed in the digest: {item.get('subject', '')[:60]}")
            g["gave_up"].append({"source": item.get("source"), "sender": item.get("sender"),
                                 "subject": str(item.get("subject", ""))[:120], "reason": rec["reason"],
                                 "seen_at": item.get("_seen_at")})
            self._mark_item_processed(item)
            return None, "gave-up"
        if analysis is None:
            if self.analyzer.provider == "qwen":
                ruled = self._rule_analysis(item)
                if ruled is not None:
                    return ruled, "rule"
            return None, None
        if self.analyzer.provider != "qwen":
            # Only the proxy path stamps the serving model; a "scored_model"
            # arriving any other way is the model's own text and picks no bar.
            analysis["scored_model"] = None
        return analysis, self.analyzer.provider

    def _crm(self) -> dict | None:
        """The CRM address index, reloaded hourly; None, and said, when unreadable."""
        now = datetime.now(timezone.utc)
        if self._crm_loaded_at is None or now - self._crm_loaded_at > timedelta(hours=1):
            try:
                from scripts.utils.crm import contact_index_by_email
                self._crm_index = contact_index_by_email()
            except Exception as e:  # noqa: BLE001 - the rule degrades, the cycle goes on
                self.logger.warning(f"CRM index unreadable ({e}); the rule scores without it")
                self._crm_index = None
            self._crm_loaded_at = now
        return self._crm_index

    def _rule_analysis(self, item: dict) -> dict | None:
        """The fallback rule's verdict on an email, shaped like a model's."""
        if self.rule is None or item.get("source") != "email":
            return None
        operator = get_operator()
        account = self.email_source.account
        own_email = str(getattr(account, "primary_smtp_address", "") or os.getenv("EXCHANGE_EMAIL", ""))
        first_name = (operator.get("name") or "").split(" ")[0].upper()
        own_domains = self.rule.own_domains or ([own_email.rsplit("@", 1)[-1]] if "@" in own_email else [])
        times = []
        for t in self.state.data["guardian"]["sender_seen"].get((item.get("sender_email") or "").lower(), []):
            with contextlib.suppress(TypeError, ValueError):
                dt = datetime.fromisoformat(t)
                times.append(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
        ctx = sentinel_rule.RuleContext(
            operator_email=own_email, own_domains=own_domains,
            name_caps=self.rule.name_caps or ([first_name] if first_name else []),
            weekend_days=self.rule.weekend_days or [5, 6], tz=self.config.timezone,
            crm_index=self._crm(), vip=item.get("is_vip"),
            wrote_in_conversation=(self.sent_reader.wrote_before
                                   if self.sent_reader.refreshed_at else None),
            sender_times=times)
        try:
            score, why = self.rule.score(sentinel_rule.email_features(item, ctx))
        except Exception as e:  # noqa: BLE001 - a rule defect must not cost the item
            self.logger.error(f"Fallback rule failed on an item ({e}); it waits for the model")
            return None
        return {
            "urgency_score": score,
            "reason": f"Scored by the fallback rule while the model is unavailable ({why})",
            "summary": item.get("subject", ""),
            "recommended_action": "Review it yourself; the model re-scores it when it returns",
        }

    async def _rescore_rule_items(self):
        """When the model answers again, re-score what the rule scored.

        Anything the model now rates at the urgency threshold that the rule had
        held back is sent at once. The model's score replaces the rule's in the
        digest. Up to 16 per cycle; an outage stops the pass and keeps the rest.
        """
        g = self.state.data["guardian"]
        pending = g["rule_items"]
        if not pending or self.analyzer.provider != "qwen" or self.analyzer.down:
            return
        batch, rest = pending[:16], pending[16:]
        results = [self.analyzer.analyze(rec["item"]) for rec in batch]
        keep = []
        for rec, analysis in zip(batch, results, strict=True):
            item = rec["item"]
            if analysis is None:
                keep.append(rec)
                continue
            if is_unscored(analysis):
                rec["attempts"] = rec.get("attempts", 0) + 1
                if rec["attempts"] < self._max_score_attempts():
                    keep.append(rec)
                else:
                    self.logger.warning(f"Re-score gave up after {rec['attempts']} unusable answers; "
                                        f"the rule's score stands: {item.get('subject', '')[:60]}")
                continue
            score = UrgencyAnalyzer._clamp_score(analysis.get("urgency_score"))
            threshold = self.config.threshold_for(analysis.get("scored_model"))
            self.logger.info(f"  re-scored [{rec['rule_score']} rule -> {score}/10 "
                             f"{analysis.get('scored_model') or 'model'} bar {threshold}] "
                             f"{item.get('sender')} - {item.get('subject', '')[:60]}")
            fields = {"urgency": score, "scored_by": "qwen (re-scored)",
                      "scored_model": analysis.get("scored_model"), "threshold": threshold,
                      "action": analysis.get("recommended_action", "")}
            if score >= threshold and not rec.get("notified") and self.notifier:
                analysis = dict(analysis, threshold=threshold)
                analysis["reason"] = ("Held back by the fallback rule during the model outage; "
                                      + str(analysis.get("reason", "")))
                try:
                    await self.notifier.send_notification(item, analysis)
                    self.state.mark_notified(item.get("_hash", ""))
                    self.state.data["digest"]["urgent_sent"] += 1
                    fields["notified"] = True
                except Exception as e:
                    self.logger.error(f"Re-score notification failed: {e}")
                    keep.append(rec)
                    continue
            self.state.update_guardian_item(item.get("_hash"), **fields)
        g["rule_items"] = keep + rest

    async def _check_digest_schedule(self, now_local: datetime):
        if not self.config.digest.get("enabled", False) or not self.notifier:
            return
        if self.config.mode == "guardian":
            await self._check_guardian_digest(now_local)
            return

        current_time = now_local.strftime("%H:%M")
        morning = self.config.digest.get("morning_time", "08:00")
        evening = self.config.digest.get("evening_time", "22:00")

        # The window is COMPUTED from the configured interval, never assumed to
        # match it. The comment here used to assert the match while the window
        # was the literal 15 in `_time_in_window` and the interval was
        # `check_interval_minutes`, so at any other interval the digest could
        # never fire. It also missed at the SHIPPED default: the run loop sleeps
        # check_interval AFTER each cycle finishes, so the true period is the
        # interval plus the cycle's own duration and the start time drifts
        # forward. A day's cycles straddling 07:58 -> 08:16 skipped 08:00-08:15
        # with no config change at all.
        morning_due = self._time_in_window(current_time, morning)
        evening_due = self._time_in_window(current_time, evening)

        # Prevent double-sending with state key
        digest_state = self.state.data.get("digest", {})
        today_str = now_local.strftime("%Y-%m-%d")

        if morning_due and digest_state.get("morning_sent") != today_str:
            msg = self._build_morning_digest(now_local)
            try:
                await self.notifier.send_digest(msg)
                self.state.data.setdefault("digest", {})["morning_sent"] = today_str
            except Exception as e:
                self.logger.error(f"Morning digest failed: {e}")

        if evening_due and digest_state.get("evening_sent") != today_str:
            msg = self._build_evening_digest(now_local)
            try:
                await self.notifier.send_digest(msg)
                self.state.data.setdefault("digest", {})["evening_sent"] = today_str
            except Exception as e:
                self.logger.error(f"Evening digest failed: {e}")

    def _digest_window_minutes(self) -> int:
        """How long after its target time a digest may still fire.

        Twice the configured check interval, floored at 30 minutes. Twice,
        because the real period is the interval plus however long a cycle takes
        (Exchange fetch, Telethon connect, batched LLM calls), and a window
        merely EQUAL to the interval is missed the moment a cycle runs long. The
        floor keeps a very short interval from producing a window so tight that
        one slow cycle skips the day's digest.

        Firing late cannot double-send: `_check_digest_schedule` only sends when
        the `morning_sent` / `evening_sent` date key is not today's.
        """
        return max(2 * (self.config.check_interval // 60), 30)

    def _time_in_window(self, current: str, target: str) -> bool:
        """True when `current` is at or after `target`, within the digest window."""
        try:
            c_h, c_m = map(int, current.split(":"))
            t_h, t_m = map(int, target.split(":"))
            c_mins = c_h * 60 + c_m
            t_mins = t_h * 60 + t_m
            return 0 <= (c_mins - t_mins) < self._digest_window_minutes()
        except (ValueError, IndexError):
            return False

    def _build_morning_digest(self, now_local: datetime) -> str:
        d = self.state.data.get("digest", {})
        items = d.get("items_by_urgency", [])
        urgent_count = d.get("urgent_sent", 0)

        # Top 3 items by urgency
        sorted_items = sorted(items, key=lambda x: x.get("urgency", 0), reverse=True)[:3]
        top_items_str = ""
        for i, it in enumerate(sorted_items, 1):
            top_items_str += f"\n  {i}. [{it.get('urgency', '?')}/10] {it.get('source', '?').upper()}: {it.get('sender', '?')} - {it.get('subject', '')[:50]}"

        if not top_items_str:
            top_items_str = "\n  No items processed overnight"

        return f"""\U0001f4ca Morning Brief -- {now_local.strftime('%Y-%m-%d %H:%M')}

Overnight summary:
  \U0001f4e7 Emails checked: {d.get('emails_checked', 0)}
  \U0001f4ac Telegram messages: {d.get('tg_messages_checked', 0)}
  \U0001f6a8 Urgent alerts sent: {urgent_count}

Top items by urgency:{top_items_str}{self._scoring_health()}"""

    def _build_evening_digest(self, now_local: datetime) -> str:
        d = self.state.data.get("digest", {})
        items = d.get("items_by_urgency", [])
        urgent_count = d.get("urgent_sent", 0)

        # Medium items (5-6 score) that didn't trigger alerts
        medium_items = [
            it for it in items
            if 5 <= it.get("urgency", 0) <= 6
        ]
        # Sorted, and the drop is named. This list was the first five in ARRIVAL
        # order under a heading making no top-N claim, so on a busy day the CEO
        # read five routine morning items as the whole medium band while a 6/10
        # from a partner at 17:40 was never mentioned and no number hinted it
        # existed. The morning digest one function up already sorts by urgency
        # and says "Top 3"; this one did neither.
        medium_items.sort(key=lambda it: (it.get("urgency", 0), it.get("time", "")),
                          reverse=True)
        shown = medium_items[:MEDIUM_DIGEST_ROWS]
        medium_str = ""
        for it in shown:
            medium_str += f"\n  - [{it.get('urgency')}/10] {it.get('source', '?').upper()}: {it.get('sender', '?')} - {it.get('subject', '')[:50]}"

        dropped = len(medium_items) - len(shown)
        if dropped:
            medium_str += f"\n  ... and {dropped} more at 5-6/10, not shown"

        if not medium_str:
            medium_str = "\n  None"

        # Top senders by volume
        sender_counts = {}
        for it in items:
            s = it.get("sender", "unknown")
            sender_counts[s] = sender_counts.get(s, 0) + 1
        top_senders = sorted(sender_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        senders_str = ""
        for name, count in top_senders:
            senders_str += f"\n  - {name}: {count} messages"

        if not senders_str:
            senders_str = "\n  No activity"

        return f"""\U0001f4ca Evening Summary -- {now_local.strftime('%Y-%m-%d %H:%M')}

Today's stats:
  \U0001f4e7 Emails checked: {d.get('emails_checked', 0)}
  \U0001f4ac Telegram messages: {d.get('tg_messages_checked', 0)}
  \U0001f6a8 Urgent alerts sent: {urgent_count}
  \U0001f4cb Total items analyzed: {len(items)}

Medium-priority ({len(medium_items)} at 5-6/10, top {MEDIUM_DIGEST_ROWS} shown):{medium_str}

Top senders:{senders_str}{self._scoring_health()}"""

    # --- Guardian mode: the digest at each configured time ---

    DEFAULT_DIGEST_TIMES = ("09:00", "13:00", "18:00", "22:00")

    def _digest_times(self) -> list:
        times = _configured_seq(self.config.digest.get("times"), str, "digest.times", self.logger)
        good = []
        for t in times or list(self.DEFAULT_DIGEST_TIMES):
            try:
                h, m = map(int, t.strip().split(":"))
                good.append(f"{h:02d}:{m:02d}")
            except ValueError:
                self.logger.warning(f"sentinel_config.yaml: digest.times entry {t!r} is not HH:MM; ignored")
        return sorted(set(good))

    async def _check_guardian_digest(self, now_local: datetime):
        current = now_local.strftime("%H:%M")
        today = now_local.strftime("%Y-%m-%d")
        g = self.state.data["guardian"]
        for t in self._digest_times():
            slot = f"{today} {t}"
            if slot in g["sent_slots"] or not self._time_in_window(current, t):
                continue
            msg = self._build_guardian_digest(now_local)
            try:
                await self.notifier.send_digest(msg)
            except Exception as e:
                self.logger.error(f"Digest {t} failed: {e}")
                continue
            g["sent_slots"].append(slot)
            g["last_digest_at"] = datetime.now(timezone.utc).isoformat()

    def _awaits_reply(self, rec: dict) -> bool | None:
        """Did the operator answer this item? None when that cannot be known."""
        try:
            seen = datetime.fromisoformat(rec.get("seen_at"))
        except (TypeError, ValueError):
            return None
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if rec.get("source") == "email":
            replied = self.sent_reader.replied_after(
                rec.get("conversation_id"), subject_core(rec.get("subject", "")), seen)
            return None if replied is None else not replied
        if rec.get("source") == "telegram":
            out = self.state.data["guardian"]["tg_last_out"].get(str(rec.get("chat_id")))
            return not (out and datetime.fromisoformat(out) > seen)
        return None

    def _build_guardian_digest(self, now_local: datetime) -> str:
        g = self.state.data["guardian"]
        cfg = self.config.digest
        rows = int(cfg.get("max_rows", 12))
        matters_min = int(cfg.get("matters_min_score", 5))
        reply_min = int(cfg.get("reply_min_score", 6))
        window = timedelta(days=int(cfg.get("reply_window_days", 3)))
        now = datetime.now(timezone.utc)
        try:
            since = datetime.fromisoformat(g["last_digest_at"])
        except (TypeError, ValueError):
            since = now - timedelta(hours=24)

        def _at(rec):
            try:
                dt = datetime.fromisoformat(rec.get("seen_at"))
            except (TypeError, ValueError):
                return None
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        def _age(dt):
            hours = int((now - dt).total_seconds() // 3600)
            return f"{hours // 24}d" if hours >= 48 else f"{hours}h"

        def _line(rec, extra=""):
            if rec.get("scored_by") == "rule":
                tag = " rule"
            elif rec.get("scored_model") or rec.get("threshold") is not None:
                tag = f" {rec.get('scored_model') or 'model'} bar {rec.get('threshold')}"
            else:
                tag = ""
            sent = " (alerted)" if rec.get("notified") else ""
            return (f"\n  [{rec.get('urgency')}/10{tag}] {str(rec.get('source', '?')).upper()}: "
                    f"{rec.get('sender', '?')} - {str(rec.get('subject', ''))[:60]}{sent}{extra}")

        scored = [r for r in g["items"] if isinstance(r.get("urgency"), int) and _at(r)]
        recent = [r for r in scored if _at(r) > since]
        matters = sorted((r for r in recent if r["urgency"] >= matters_min),
                         key=lambda r: (r["urgency"], r.get("seen_at", "")), reverse=True)
        matters_str = "".join(_line(r) for r in matters[:rows]) or "\n  Nothing at or above " f"{matters_min}/10"
        if len(matters) > rows:
            matters_str += f"\n  ... and {len(matters) - rows} more at {matters_min}/10 or above, not shown"

        waiting, unknown = [], 0
        for r in scored:
            if now - _at(r) > window:
                continue
            asked = str(r.get("action", "")).lower().startswith(("reply needed", "approve/decide"))
            if r["urgency"] < reply_min and not asked:
                continue
            verdict = self._awaits_reply(r)
            if verdict is None:
                unknown += 1
            elif verdict:
                waiting.append(r)
        waiting.sort(key=lambda r: (r["urgency"], r.get("seen_at", "")), reverse=True)
        reply_str = "".join(_line(r, f", {_age(_at(r))} ago") for r in waiting[:rows]) or "\n  Nothing waiting"
        if len(waiting) > rows:
            reply_str += f"\n  ... and {len(waiting) - rows} more, not shown"
        if unknown:
            reply_str += (f"\n  ({unknown} email item(s) not checked: the Sent folder could not be read)")

        by_source = {"email": 0, "telegram": 0}
        for r in recent:
            by_source[r.get("source")] = by_source.get(r.get("source"), 0) + 1
        alerts = sum(1 for r in recent if r.get("notified"))
        return f"""\U0001f4ca Digest -- {now_local.strftime('%Y-%m-%d %H:%M')}

Matters (since the last digest):{matters_str}

Awaits your reply (last {window.days} days):{reply_str}

Since the last digest: {by_source.get('email', 0)} email, {by_source.get('telegram', 0)} Telegram scored; {alerts} real-time alert(s).{self._scoring_health()}"""

    def _scoring_health(self) -> str:
        """Lines naming what was NOT scored normally. Empty when all was."""
        g = self.state.data["guardian"]
        lines = []
        rule_pending = len(g["rule_items"])
        if rule_pending:
            lines.append(f"\u26a0 {rule_pending} email(s) scored by the fallback rule while the model was "
                         f"unavailable; they are re-scored when it answers.")
        if g.get("waiting_for_model"):
            lines.append(f"\u26a0 {g['waiting_for_model']} item(s) waiting for the model (it did not answer "
                         f"last cycle).")
        if g["unscored"]:
            lines.append(f"\u26a0 {len(g['unscored'])} item(s) not scored yet: the model's answer was unusable "
                         f"(cut off or not JSON); retrying.")
        if g["gave_up"]:
            lines.append(f"\u26a0 Could not be scored after {self._max_score_attempts()} attempts:"
                         + "".join(f"\n  - {str(r.get('source', '?')).upper()}: {r.get('sender', '?')} - "
                                   f"{str(r.get('subject', ''))[:60]} ({r.get('reason', '')[:60]})"
                                   for r in g["gave_up"][-10:]))
        return ("\n\n" + "\n".join(lines)) if lines else ""

    # --- Meeting invite processing ---

    def _unprocessed_after_failed_escalation(self, invite_id, what):
        """Log why an invite is being left for the next cycle.

        The `else` branch below has done this since it was written; the other
        four escalation call sites discarded `_escalate_invite`'s bool and fell
        through to `mark_invite_processed` regardless. An invite consumed after
        a failed notification is an invite NOBODY was told about, and it never
        comes back - which is the exact outcome the escalation path exists to
        prevent. `_escalate_invite` returns True when there is no notifier at
        all, so a permanently unconfigured workspace is not retried forever.
        """
        self.logger.warning(
            f"Invite {invite_id} left unprocessed: {what} escalation was not "
            f"delivered; it will be retried next cycle")

    async def _process_meeting_invites(self):
        """Check and process new meeting invites per CEO Calendar Policy."""
        invites = self.invite_source.check_new_invites()
        if not invites:
            return

        # Fetch the calendar for the WHOLE window the alternative search can
        # reach, not seven days of it. Derived from the same constant the
        # search uses, so the two cannot drift apart again.
        now = datetime.now(self.config.timezone)
        existing_events = self.invite_source.get_existing_events(
            now, now + timedelta(days=conflict_window_days())
        )

        for invite in invites:
            invite_id = invite["invite_id"]

            # Recurring invites: always escalate
            if invite.get("is_recurring"):
                escalated = await self._escalate_invite(
                    invite, ["Recurring meeting change -- requires CEO review"]
                )
                if not escalated:
                    self._unprocessed_after_failed_escalation(
                        invite_id, "the recurring-change")
                    continue
                self.state.mark_invite_processed(invite_id)
                self.state.record_invite_decision(
                    invite_id, invite["subject"], "escalate",
                    ["Recurring meeting change"]
                )
                continue

            # Evaluate against policy
            result = self.policy_engine.evaluate(invite, existing_events)
            decision = result["decision"]
            reasons = result["reasons"]

            # Execute decision.
            #
            # `--test` is documented as a dry run, and until now it only muted
            # Telegram: accept_invite and decline_invite had no dry_run guard,
            # so a first "safe" test run against a real Exchange account really
            # accepted invites and really sent decline replies to real people.
            # Nothing about the auto-accept/auto-decline POLICY changes here --
            # that design is the operator's and is untouched. What changes is
            # that the dry run stops lying.
            if self.dry_run and decision in ("accept", "decline"):
                self.logger.info(
                    f"[dry-run] would {decision} invite {invite.get('subject')!r} "
                    f"({'; '.join(reasons) or 'no reason recorded'}); no calendar "
                    f"action taken and no reply sent")
                if not await self._escalate_invite(invite, reasons):
                    self._unprocessed_after_failed_escalation(
                        invite_id, "the dry-run")
                    continue

            elif decision == "accept" and self.config.calendar.get("auto_accept", True):
                try:
                    self.invite_source.accept_invite(invite["item"])
                    await self._notify_invite_decision(invite, "ACCEPTED", reasons)
                except Exception as e:
                    self.logger.error(f"Failed to accept invite: {e}")
                    if not await self._escalate_invite(
                            invite, [f"Auto-accept failed: {e}"]):
                        self._unprocessed_after_failed_escalation(
                            invite_id, "the failed-accept")
                        continue

            elif decision == "decline" and self.config.calendar.get("auto_decline", True):
                alternative = result.get("proposed_alternative")
                decline_msg = select_decline_message(
                    result.get("is_tribe", False),
                    invite["subject"],
                    alternative,
                    self.config.calendar,
                )

                try:
                    self.invite_source.decline_invite(invite["item"], decline_msg)
                    await self._notify_invite_decision(
                        invite, "DECLINED", reasons, alternative
                    )
                except Exception as e:
                    self.logger.error(f"Failed to decline invite: {e}")
                    if not await self._escalate_invite(
                            invite, [f"Auto-decline failed: {e}"]):
                        self._unprocessed_after_failed_escalation(
                            invite_id, "the failed-decline")
                        continue

            else:
                # Escalate (VIP, external, soft violations, or auto-action disabled)
                escalated = await self._escalate_invite(invite, reasons)
                if not escalated:
                    # Leave it UNPROCESSED so the next cycle re-escalates. The
                    # escalation path exists precisely for the invites a human
                    # must see -- VIP, external, RUNE overrides -- and marking
                    # them processed after a failed notify meant the operator
                    # never learned they existed.
                    self._unprocessed_after_failed_escalation(
                        invite_id, "the policy")
                    continue

            self.state.mark_invite_processed(invite_id)
            self.state.record_invite_decision(
                invite_id, invite["subject"], decision, reasons
            )

        self.state.save()

    async def _notify_invite_decision(self, invite: dict, decision_label: str,
                                       reasons: list, alternative: str = None):
        """Notify the operator about an auto-handled invite via Telegram."""
        if not self.notifier:
            return

        icon = "\u2705" if decision_label == "ACCEPTED" else "\u274c"
        reasons_str = "\n".join(f"  - {r}" for r in reasons) if reasons else "  Policy compliant"
        alt_line = f"\n\U0001f4c5 Proposed alternative: {alternative}" if alternative else ""

        start_str = str(invite.get("start", ""))[:16]
        end_str = str(invite.get("end", ""))[:16]

        msg = f"""{icon} Meeting {decision_label}

\U0001f4e8 From: {invite['sender']} <{invite['sender_email']}>
\U0001f4cb Subject: {invite['subject']}
\U0001f552 When: {start_str} - {end_str}
\u23f1 Duration: {invite['duration_minutes']}m

Policy check:
{reasons_str}{alt_line}"""

        try:
            await self.notifier.send_digest(msg)
        except Exception as e:
            self.logger.error(f"Invite notification failed: {e}")

    async def _escalate_invite(self, invite: dict, reasons: list) -> bool:
        """Send urgent notification requiring CEO decision on an invite.

        Returns True when the escalation was delivered (or when there is no
        notifier at all), False when a configured notifier failed. The caller
        retries on False, so the two cases must not be conflated: a missing
        notifier is a permanent configuration state, and returning False for it
        would leave every escalated invite unprocessed forever, retried on every
        cycle. That is a startup-level gap, logged here and never silent.
        """
        if not self.notifier:
            self.logger.warning(
                f"No notifier configured; invite {invite.get('subject')!r} needs a "
                f"decision and NOBODY WAS TOLD. Configure Telegram, or handle it "
                f"in Outlook.")
            return True

        reasons_str = "\n".join(f"  - {r}" for r in reasons)
        start_str = str(invite.get("start", ""))[:16]
        end_str = str(invite.get("end", ""))[:16]
        location = invite.get("location", "-") or "-"

        msg = f"""\u26a0\ufe0f MEETING NEEDS YOUR DECISION

\U0001f4e8 From: {invite['sender']} <{invite['sender_email']}>
\U0001f4cb Subject: {invite['subject']}
\U0001f552 When: {start_str} - {end_str}
\u23f1 Duration: {invite['duration_minutes']}m
\U0001f4cd Location: {location}

Issues found:
{reasons_str}

Reply with your decision or handle in Outlook."""

        try:
            await self.notifier.send_digest(msg)
        except Exception as e:
            self.logger.error(f"Invite escalation notification failed: {e}")
            return False
        return True

    async def shutdown(self):
        self.logger.info("Sentinel shutting down...")
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        self.state.save()
        await self.telegram_source.disconnect()
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError as exc:
            self.logger.warning("could not remove the PID file at shutdown: %s", exc)
        self.logger.info("Sentinel stopped.")


# ============================================================
# CLI
# ============================================================

def _is_pid_alive(pid: int) -> bool:
    """True when `pid` is alive AND is this daemon. One implementation, in
    `pid_liveness`.

    Identity, not liveness (2026-10-12): a reused PID read an unrelated process
    as Sentinel. `_pid_is_sentinel` below stays the stricter check before any
    signal. A private copy here once caught `PermissionError` as "dead" about a
    live daemon under a service account, and `--status` then deleted its PID
    file (MEASURED 2026-08-29).
    """
    return pid_runs_program(pid, "sentinel.py")


def _read_pid_file() -> int | None:
    """The PID in PID_FILE, or None when it is absent, empty or corrupt.

    All three CLI paths (--status, --stop, and the already-running check in
    main) did a bare `int(PID_FILE.read_text().strip())`, so a truncated file --
    the normal residue of a crash mid-write -- raised ValueError out of main as
    a traceback instead of a diagnosable state.
    """
    try:
        raw = PID_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        # UnicodeError too. A PID file is the residue of a crash mid-write, so
        # a partial byte sequence in it is the ordinary case, and
        # `read_text(encoding="utf-8")` answers that with UnicodeDecodeError --
        # a ValueError, not an OSError. It escaped a reader documented to return
        # None on a file it cannot use, into all three CLI paths, which is the
        # traceback-instead-of-a-diagnosable-state this function was written to
        # remove.
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _pid_is_sentinel(pid: int) -> bool:
    """True only when `pid` is THIS program, not merely a live process.

    `--stop` used to verify the PID existed and then SIGKILL / `taskkill /F` it.
    After a crash the PID file outlives the process, the number gets reused, and
    the operator's next `--stop` destroys whatever unrelated program inherited
    it. Liveness is not identity.

    Unknown (unreadable /proc, tasklist unavailable) returns False: refusing to
    kill a process we cannot identify is the safe direction, and the stale PID
    file is cleaned either way.
    """
    if sys.platform == "win32":
        import subprocess
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return False
        # tasklist gives the image name only; python.exe/pythonw.exe running
        # this daemon is as tight as it gets without WMI.
        return "python" in (result.stdout or "").lower()
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(
            "utf-8", "replace")
    except OSError:
        return False
    return "sentinel.py" in cmdline


def check_status():
    """Check if Sentinel is running."""
    if not PID_FILE.exists():
        print("Sentinel is NOT running (no PID file)")
        return

    pid = _read_pid_file()
    if pid is None:
        print(f"Sentinel status UNKNOWN: the PID file at {PID_FILE} is empty or corrupt")
        return
    # Liveness is not identity, and this line is the one the operator acts on.
    # `os.kill(pid, 0)` establishes only that SOME process holds that number.
    # After a crash the PID file outlives the daemon and the number gets reused,
    # so until 2026-08-25 `--status` announced "Sentinel is RUNNING" over an
    # unrelated program - a sentence far wider than its method, and the same
    # defect class `.claude/rules/scope-claims.md` was written for. `--stop`
    # already checked identity; the other two callers did not.
    if _is_pid_alive(pid):
        if _pid_is_sentinel(pid):
            print(f"Sentinel is RUNNING (PID: {pid})")
        else:
            print(f"Sentinel is NOT running: PID {pid} is alive but is not this "
                  f"daemon (the PID was reused after a crash). Removing the "
                  f"stale PID file.")
            PID_FILE.unlink(missing_ok=True)
            return
    else:
        print(f"Sentinel is NOT running (stale PID file, PID {pid})")
        PID_FILE.unlink(missing_ok=True)
        return

    # Show last run info
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            last_run = state.get("last_run", "never")
            digest = state.get("digest", {})
            print(f"  Last check: {last_run}")
            print(f"  Today: {digest.get('emails_checked', 0)} emails, "
                  f"{digest.get('tg_messages_checked', 0)} TG messages, "
                  f"{digest.get('urgent_sent', 0)} urgent alerts")
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
            # State file missing/corrupt/partial - status display is best-effort.
            print(f"  (state file unreadable: {e})", file=sys.stderr)


def stop_daemon():
    """Stop the running Sentinel daemon."""
    if not PID_FILE.exists():
        print("Sentinel is not running")
        return

    pid = _read_pid_file()
    if pid is None:
        print(f"PID file at {PID_FILE} is empty or corrupt; removing it.")
        PID_FILE.unlink(missing_ok=True)
        return

    if not _is_pid_alive(pid):
        print(f"Process {pid} not found (already stopped?)")
        PID_FILE.unlink(missing_ok=True)
        return

    if not _pid_is_sentinel(pid):
        print(f"REFUSING to kill PID {pid}: it is alive but is not this daemon "
              f"(the PID was reused after a crash). Removing the stale PID file.")
        PID_FILE.unlink(missing_ok=True)
        return

    if sys.platform == "win32":
        # Windows: use taskkill for reliable termination
        import subprocess
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True)
        print(f"Terminated Sentinel (PID: {pid})")
    else:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to Sentinel (PID: {pid})")
        time.sleep(2)
        # The escalation asks the same question as everywhere else, so it asks
        # it the same way. Inline, `except OSError` swallowed `PermissionError`
        # too, which is the answer "it is alive and not ours to signal" -- the
        # one case where escalating matters most, and the one where this skipped
        # the SIGKILL and reported the daemon stopped.
        if pid_runs_program(pid, "sentinel.py"):
            print("Process still alive, sending SIGKILL")
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)

    time.sleep(1)
    PID_FILE.unlink(missing_ok=True)
    print("Sentinel stopped.")


def launch_daemon(config_path):
    """Launch Sentinel as a fully detached background process.

    Windows: CREATE_NO_WINDOW + DETACHED_PROCESS for true background.
    POSIX: start_new_session=True puts the child in its own session so it
    survives parent shell exit. For Linux production deployments, prefer
    running the daemon under the systemd user unit
    scripts/templates/systemd/sentinel.service, which runs the script in the
    foreground without --daemon and handles the backgrounding itself.
    """
    import subprocess
    python = sys.executable
    script = str(Path(__file__).resolve())
    cmd = [python, script, "--config", str(config_path)]

    if sys.platform == "win32":
        CREATE_NO_WINDOW = 0x08000000
        DETACHED_PROCESS = 0x00000008
        proc = subprocess.Popen(
            cmd,
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    else:
        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    print(f"Sentinel launched as detached process (PID: {proc.pid})")
    print(f"Use --status to check, --stop to stop, logs at {LOG_FILE}")


def main():
    require_main_clone(__file__)
    # Before the parser, because `--config`'s default calls `config_file()`,
    # which resolves against `HEADING_OS_DATA` and therefore needs the file
    # read first.
    if ENV_FILE.exists():
        load_env(WORKSPACE_ROOT)
    _bind_runtime_files()
    parser = argparse.ArgumentParser(description="Sentinel -- Unified Comms Monitor")
    parser.add_argument("--test", action="store_true",
                        help="Run one cycle as a TRUE dry run: notifications are "
                             "logged not sent, calendar invites are neither accepted "
                             "nor declined, and state is read but never written back")
    parser.add_argument("--check", action="store_true",
                        help="Run ONE live cycle and exit: notifications are sent "
                             "and state is written. This is what the 15-minute "
                             "task install_sentinel_schedule (scripts/utils/schedule.py) "
                             "installs runs; --test is a dry run and cannot stand in for it")
    parser.add_argument("--status", action="store_true", help="Check if Sentinel is running")
    parser.add_argument("--stop", action="store_true", help="Stop running Sentinel daemon")
    parser.add_argument("--daemon", action="store_true", help="Launch as detached background process (cross-platform; on Linux, prefer systemd user unit)")
    parser.add_argument("--config", type=str, default=str(config_file()), help="Path to config file")
    args = parser.parse_args()

    if args.status:
        check_status()
        return

    if args.stop:
        stop_daemon()
        return

    if args.daemon:
        launch_daemon(args.config)
        return

    # Check if already running
    # Same identity check as --status, and it matters more here: this guard
    # REFUSES a legitimate start. On a reused PID it used to tell the operator
    # the daemon was already running and exit 1, when nothing was running at
    # all. It is advisory anyway - the real second-instance guard is the flock
    # in Sentinel.start - so failing toward "let it start and let the lock
    # decide" is the safe direction.
    if not args.test and PID_FILE.exists():
        pid = _read_pid_file()
        if pid is not None and _is_pid_alive(pid) and _pid_is_sentinel(pid):
            print(f"Sentinel is already running (PID: {pid}). Use --stop first.")
            sys.exit(1)
        else:
            PID_FILE.unlink(missing_ok=True)

    sentinel = Sentinel(config_path=Path(args.config), dry_run=args.test,
                        once=args.check)

    # Handle graceful shutdown (registered AFTER sentinel object created - SEC-011)
    def signal_handler(sig, frame):
        print("\nShutdown signal received...")
        sentinel._running = False
        if hasattr(sentinel, '_stop_event'):
            sentinel._stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    try:
        asyncio.run(sentinel.start())
    except Exception as e:
        # Catch-all so daemon mode crashes are logged, not lost
        sentinel.logger.critical(f"Sentinel crashed: {e}", exc_info=True)
        PID_FILE.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    main()
