#!/usr/bin/env python3
"""
Exchange Sync for HEADING OS

Pulls calendar events and emails from on-premises Microsoft Exchange
via EWS (Exchange Web Services) and saves them as readable markdown
files in the runtime caches `exchange-calendar-cache` and `exchange-email-cache`.

Prerequisites:
    uv sync --extra email   (exchangelib; `require("exchangelib", extra="email")`
    is what the code itself enforces, so a bare `pip install` diverges from the
    pinned set)

Setup:
    1. Copy .env.example to .env in the workspace root
    2. Fill in your Exchange credentials
    3. Run: python scripts/sync-exchange.py

Usage:
    python scripts/sync-exchange.py --help           # the authoritative flag list

    python scripts/sync-exchange.py                  # sync both calendar and emails
    python scripts/sync-exchange.py --calendar       # sync calendar only
    python scripts/sync-exchange.py --emails         # sync emails only
    python scripts/sync-exchange.py --days 7         # calendar: next N days (default: 7)
    python scripts/sync-exchange.py --email-count 50 # emails: last N messages (default: 30)
    python scripts/sync-exchange.py --unread         # emails: unread only
    python scripts/sync-exchange.py --folder Inbox   # emails: specific folder (default: Inbox)
    python scripts/sync-exchange.py --create-meeting "Subject" --time "14:30" \
        --duration 30 --location Room --attendees a@x.example  # HOLD, no invite
    python scripts/sync-exchange.py --create-meeting "Subject" --time "14:30" \
        --attendees a@x.example --send-invites       # actually emails the invite
    python scripts/sync-exchange.py --delete "subject text"       # asks first
    python scripts/sync-exchange.py --delete "subject text" --yes # no prompt
    python scripts/sync-exchange.py --export-metadata mail.jsonl --since 2026-04-16  # read only, no bodies

This docstring is hand-maintained and has drifted before; `--help` is generated
from argparse and cannot.
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


from scripts.utils.venv_guard import ensure_venv  # noqa: E402
from scripts.utils.exchange_conn import build_account  # noqa: E402
from scripts.utils.atomic import atomic_write_text  # noqa: E402

ensure_venv()
# exchangelib names are bound lazily (F-2.1: import stays pure). The daemon
# always calls connect() before any sync work, so binding there covers every
# downstream constructor.
Account = CalendarItem = EWSDateTime = EWSTimeZone = None


def _ensure_exchangelib():
    global Account, CalendarItem, EWSDateTime, EWSTimeZone
    if Account is not None:
        return
    from scripts.utils.optdeps import require
    require("exchangelib", extra="email")
    from exchangelib import Account, CalendarItem, EWSDateTime, EWSTimeZone


from scripts.utils.html_text import email_body_text, strip_html  # noqa: E402
from scripts.utils.secret_patterns import redact  # noqa: E402
from scripts.utils.data_path import data_write_dir  # noqa: E402
from scripts.utils.workspace import get_data_root, get_default_tz, get_default_tz_name, get_workspace_root, load_env  # noqa: E402

# ============================================================
# Configuration
# ============================================================

# --- Constants ---
WORKSPACE_ROOT = get_workspace_root()
ENV_FILE = WORKSPACE_ROOT / ".env"


def calendar_dir() -> Path:
    """The registry name `exchange-calendar-cache`, created.

    Resolved at call time, never at import, so a changed HEADING_OS_DATA is
    followed. A sync dump is rebuilt by the next sync, so it lives in the
    untracked runtime store.
    """
    return data_write_dir("exchange-calendar-cache")


def email_dir() -> Path:
    """The registry name `exchange-email-cache`, created; see `calendar_dir`."""
    return data_write_dir("exchange-email-cache")


def _display_path(path):
    """A short path for a log line, and never an exception.

    A cosmetic path in a success message must not be able to abort the caller,
    so a path under neither root degrades to its absolute form rather than
    raising ValueError out of a sync that succeeded.
    """
    for base in (get_data_root(), get_workspace_root()):
        try:
            return str(path.relative_to(base))
        except ValueError:
            continue
    return str(path)


def load_config():
    """Load Exchange credentials from .env file."""
    if not ENV_FILE.exists():
        print(f"[ERROR] .env file not found at: {ENV_FILE}")
        print(f"        Copy .env.example to .env and fill in your Exchange credentials.")
        sys.exit(1)

    load_env(WORKSPACE_ROOT)

    required = ["EXCHANGE_EMAIL", "EXCHANGE_PASSWORD", "EXCHANGE_SERVER"]
    config = {}
    for key in required:
        val = os.getenv(key)
        if not val:
            print(f"[ERROR] Missing {key} in .env file")
            sys.exit(1)
        config[key] = val

    # Optional settings
    config["EXCHANGE_USERNAME"] = os.getenv("EXCHANGE_USERNAME", config["EXCHANGE_EMAIL"])
    config["EXCHANGE_AUTH_TYPE"] = os.getenv("EXCHANGE_AUTH_TYPE", "NTLM")
    config["EXCHANGE_TIMEZONE"] = os.getenv("EXCHANGE_TIMEZONE", get_default_tz_name())

    return config


# ============================================================
# Exchange Connection
# ============================================================

def connect(config):
    """Connect to Exchange server via EWS."""
    _ensure_exchangelib()
    print(f"[INFO] Connecting to {config['EXCHANGE_SERVER']}...")

    account = build_account(config)

    # NOT "Connected". `Account(...)` with `autodiscover=False` does no network
    # I/O -- exchangelib is lazy and the first real request is what reaches the
    # server. The old wording claimed a connection the constructor never made,
    # so a dead server printed "[OK] Connected", then two [ERROR] lanes. Say
    # what the call established, per `.claude/rules/scope-claims.md`.
    print(f"[OK] Account configured for {config['EXCHANGE_EMAIL']} (not yet contacted)")
    return account


# ============================================================
# Calendar Sync
# ============================================================

_LOCALISE_WARNED = set()


def _warn_once(message):
    """Print a degradation warning the first time it happens, then stay quiet.

    A calendar range holds many items and a bad one is usually a whole class of
    bad ones, so warning per item would bury the sync output. Warning zero times
    is what the four blanket `except Exception` handlers below used to do.
    """
    if message in _LOCALISE_WARNED:
        return
    _LOCALISE_WARNED.add(message)
    print(f"[WARN] {message}")


def _to_local(value, local_tz):
    """Return `value` converted to local time, or None when it cannot be.

    All-day events arrive as an `EWSDate`, which has no `astimezone`, and a
    malformed item can arrive with `start=None`. Both used to hit a blanket
    `except Exception` that then guessed by slicing `str(value)`, so a real
    timezone fault was indistinguishable from an all-day event and reported
    nothing at all.
    """
    try:
        return value.astimezone(local_tz)
    except (AttributeError, TypeError, ValueError) as exc:
        _warn_once(f"could not convert {type(value).__name__} start to local time: {exc}")
        return None


UNDATED_KEY = "undated"
"""Grouping key for an event whose start is missing.

`str(None)[:10]` is the four characters "None", so a malformed item produced a
`## None` heading in upcoming.md and a per-day file literally named `None.md`.
`DAY_FILE_RE` never matches that name, so the prune pass could not remove it and
it stayed on disk forever. MEASURED 2026-08-29 with one `start=None` item in the
range.

The word, not a bracketed label: the day sections are written in
`sorted(by_date.keys())` order, and a letter sorts after every ISO date while a
punctuation mark sorts before all of them. The undated bucket belongs at the end
of the file, where the accidental "None" key used to put it.
"""


def _start_sort_key(value, local_tz):
    """A total ordering over starts that mix EWSDate, EWSDateTime and None.

    `sorted(events, key=lambda e: e.start)` raised
    `TypeError: can't compare EWSDateTime to EWSDate` as soon as one all-day
    event shared a range with one timed meeting, and the whole calendar lane
    died at that line: `_to_local`, the grouping, the duration handling and the
    stale-file prune below were all built for exactly these inputs and none of
    them ever ran. `sync_calendar` returned -1 and the process exited 1.

    MEASURED 2026-08-29 on exchangelib 5.6.0, and the crash even depended on
    arrival order. `date.__lt__` compares happily against a datetime, while
    `datetime.__lt__` refuses, so `[all-day, timed]` raised and `[timed,
    all-day]` silently sorted by year/month/day alone. A calendar view returns
    the all-day item first, which is the half that raised.

    Every component is an int, so no comparison here can raise whatever the
    server sends. Day first, then the clock, so an all-day event leads its own
    day; a start that is neither a date nor a datetime sorts last rather than
    taking the lane down.
    """
    if isinstance(value, datetime):
        local = _to_local(value, local_tz)
        if local is not None:
            return (0, local.year, local.month, local.day,
                    local.hour * 60 + local.minute)
        return (1, 0, 0, 0, 0)
    if isinstance(value, date):
        return (0, value.year, value.month, value.day, -1)
    return (1, 0, 0, 0, 0)


def _event_time_str(value, local_tz):
    """`HH:MM` in local time, or a best-effort label for a non-datetime start."""
    local = _to_local(value, local_tz)
    if local is not None:
        return local.strftime("%H:%M")
    text = str(value)
    return text[11:16] if len(text) > 10 else "All day"


def sync_calendar(account, days=7, timezone_str=None):
    """Pull calendar events and save as markdown.

    `timezone_str=None` resolves per call. A `get_default_tz_name()` DEFAULT is
    evaluated once, at function-definition time, so importing this module ran
    env resolution as a side effect and any later config change was ignored for
    the life of the process.
    """
    # The lazy names this function reads are module globals bound by
    # `_ensure_exchangelib`, and nothing here bound them. It worked only because
    # `main()` calls `connect()` first; a caller that reaches this function any
    # other way got `AttributeError: 'NoneType' object has no attribute
    # 'from_timezone'` instead of the clean "install the email extra" refusal
    # the lazy-import contract promises. Reproduced 2026-08-26, from a test that
    # passed or failed depending on which xdist worker ran it.
    _ensure_exchangelib()

    if timezone_str is None:
        timezone_str = get_default_tz_name()
    cal_dir = calendar_dir()
    cal_dir.mkdir(parents=True, exist_ok=True)

    local_tz = ZoneInfo(timezone_str)
    tz = EWSTimeZone.from_timezone(local_tz)

    # "Today" in the zone the window is expressed in. It used to be read from
    # `get_default_tz()` and then stamped with the Exchange zone, so whenever an
    # operator set EXCHANGE_TIMEZONE apart from HEADING_OS_TZ the window could
    # start on the wrong calendar day.
    now = datetime.now(local_tz)
    start = EWSDateTime(now.year, now.month, now.day, 0, 0, 0, tzinfo=tz)
    end = start + timedelta(days=days)

    print(f"[INFO] Fetching calendar events: {start.date()} to {end.date()}...")

    events = account.calendar.view(start=start, end=end)
    event_list = sorted(events, key=lambda e: _start_sort_key(e.start, local_tz))

    if not event_list:
        print("[INFO] No calendar events found in this range.")

    # Group by date (in local timezone)
    by_date = {}
    for event in event_list:
        local = _to_local(event.start, local_tz)
        if local is not None:
            date_key = local.date()
        elif event.start is None:
            date_key = UNDATED_KEY
        else:
            date_key = event.start.date() if hasattr(event.start, "date") else str(event.start)[:10]
        date_str = str(date_key)
        if date_str not in by_date:
            by_date[date_str] = []
        by_date[date_str].append(event)

    # Write combined file
    output_file = cal_dir / "upcoming.md"
    lines = []
    lines.append(f"# Calendar - Next {days} Days")
    lines.append(f"")
    # The clock comes from the zone the label names. It used to be read from
    # `get_default_tz()` and labelled `timezone_str`, so a workspace with the
    # two set apart wrote a timestamp that asserted the zone it was not in.
    lines.append(f"> Synced: {datetime.now(local_tz).strftime('%Y-%m-%d %H:%M')} ({timezone_str})")
    lines.append(f"> Range: {start.date()} to {end.date()}")
    lines.append("")

    total = 0
    for date_str in sorted(by_date.keys()):
        day_events = by_date[date_str]
        lines.append(f"## {date_str}")
        lines.append("")
        lines.append("| Time | Subject | Location | Duration |")
        lines.append("|------|---------|----------|----------|")

        for event in day_events:
            total += 1
            time_str = _event_time_str(event.start, local_tz)
            subject = (event.subject or "(No subject)").replace("|", "-")
            location = (event.location or "-").replace("|", "-") if event.location else "-"

            if event.start and event.end:
                try:
                    duration_mins = int((event.end - event.start).total_seconds() / 60)
                    if duration_mins >= 60:
                        duration = f"{duration_mins // 60}h{duration_mins % 60:02d}m"
                    else:
                        duration = f"{duration_mins}m"
                except (AttributeError, TypeError, ValueError) as exc:
                    _warn_once(f"could not measure event duration: {exc}")
                    duration = "-"
            else:
                duration = "-"

            lines.append(f"| {time_str} | {subject} | {location} | {duration} |")

        lines.append("")

        # Detail section for events with body/attendees
        for event in day_events:
            has_details = (event.body and str(event.body).strip()) or event.required_attendees or event.optional_attendees
            if has_details:
                # `_event_time_str`, like the table above. Slicing str(event.start)
                # gave the UTC wall clock, so a 09:00 UTC meeting in a zone east of UTC
                # was listed at its local hour in the table and titled 09:00 in the
                # detail section right below - the section carrying the agenda
                # and the attendees.
                detail_time = _event_time_str(event.start, local_tz)
                lines.append(f"### {detail_time} - {event.subject or '(No subject)'}")
                lines.append("")

                if event.required_attendees:
                    attendees = [a.mailbox.email_address for a in event.required_attendees if a.mailbox]
                    if attendees:
                        lines.append(f"**Attendees:** {', '.join(attendees)}")
                        lines.append("")

                if event.optional_attendees:
                    optional = [a.mailbox.email_address for a in event.optional_attendees if a.mailbox]
                    if optional:
                        lines.append(f"**Optional:** {', '.join(optional)}")
                        lines.append("")

                if event.body and str(event.body).strip():
                    # `redact` directly, not `email_body_text`: a calendar body
                    # is read from `body` alone here, and switching it to prefer
                    # `text_body` would change what this file writes for reasons
                    # unrelated to the defect. What must not differ between the
                    # two paths is that neither writes an unredacted body - an
                    # invite carries a join URL, and a join URL carries a token.
                    body_text = redact(strip_html(event.body))
                    # Truncate very long bodies
                    if len(body_text) > 1000:
                        body_text = body_text[:1000] + "\n\n[...truncated]"
                    lines.append(body_text)
                    lines.append("")

    atomic_write_text(output_file, "\n".join(lines))
    print(f"[OK] Calendar: {total} events saved to {_display_path(output_file)}")

    # Also write per-day files
    written_days = set()
    for date_str, day_events in by_date.items():
        # Only a real day gets a per-day file. `_prune_stale_day_files` can
        # remove nothing whose name `DAY_FILE_RE` does not match, so writing
        # `(undated).md` would create a file that outlives every later sync.
        # The undated bucket is still listed in upcoming.md above.
        if not DAY_FILE_RE.match(f"{date_str}.md"):
            continue
        day_file = cal_dir / f"{date_str}.md"
        written_days.add(day_file.name)
        day_lines = [f"# Calendar - {date_str}", "",
                     f"> Synced: {datetime.now(local_tz).strftime('%Y-%m-%d %H:%M')} ({timezone_str})", ""]
        day_lines.append("| Time | Subject | Location |")
        day_lines.append("|------|---------|----------|")
        for event in day_events:
            time_str = _event_time_str(event.start, local_tz)
            subject = (event.subject or "(No subject)").replace("|", "-")
            location = (event.location or "-").replace("|", "-") if event.location else "-"
            day_lines.append(f"| {time_str} | {subject} | {location} |")
        atomic_write_text(day_file, "\n".join(day_lines))

    # Prune per-day files that fell out of the window. The loop above only ever
    # CREATES, so a day that emptied (meeting cancelled) or slid out of range
    # kept its old file on disk forever, and any reader globbing this directory
    # served last month's meetings as current.
    _prune_stale_day_files(written_days, start.date(), end.date())

    return total


DAY_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


def _prune_stale_day_files(written_days, window_start, window_end):
    """Delete `YYYY-MM-DD.md` files inside the synced window that we did not write.

    Scoped to the window on purpose: a file dated outside the range was written
    by an earlier run with different `--days` and is not this run's to judge.
    `upcoming.md` and any hand-made note are untouched, because the name has to
    match the date pattern exactly.
    """
    for existing in calendar_dir().glob("*.md"):
        if not DAY_FILE_RE.match(existing.name) or existing.name in written_days:
            continue
        try:
            stamp = date.fromisoformat(existing.stem)
        except ValueError:
            continue
        if not (window_start <= stamp < window_end):
            continue
        try:
            existing.unlink()
            print(f"[INFO] Pruned stale calendar day file: {existing.name}")
        except OSError as exc:
            print(f"[WARN] Could not prune {existing.name}: {exc}")


# ============================================================
# Email Sync
# ============================================================

def _mail_folder(account, folder_name):
    """The named well-known folder (Inbox, Sent, Drafts), else a subfolder of Inbox."""
    name = folder_name.lower()
    if name == "inbox":
        return account.inbox
    if name == "sent":
        return account.sent
    if name == "drafts":
        return account.drafts
    return account.inbox / folder_name


def sync_emails(account, count=30, unread_only=False, folder_name="Inbox"):
    """Pull emails and save as markdown."""
    mail_dir = email_dir()
    mail_dir.mkdir(parents=True, exist_ok=True)

    print(f"[INFO] Fetching emails from {folder_name}" +
          (f" (unread only)" if unread_only else f" (last {count})") + "...")

    folder = _mail_folder(account, folder_name)

    if unread_only:
        items = folder.filter(is_read=False).order_by("-datetime_received")[:count]
    else:
        items = folder.all().order_by("-datetime_received")[:count]

    email_list = list(items)

    if not email_list:
        # No early return. This used to skip the write entirely, so the file
        # from the LAST successful run stayed on disk carrying its own older
        # `> Synced:` stamp: MEASURED 2026-08-29, a second sync over an emptied
        # mailbox left `inbox-latest.md` still saying "Count: 1 emails" and
        # still listing that message, and any reader globbing the email dir served
        # it as current. That is the same defect `_prune_stale_day_files` names
        # on the calendar side, where a zero-event range still writes its file.
        print(f"[INFO] No emails found in {folder_name}.")

    # Write combined file
    suffix = "unread" if unread_only else "latest"
    output_file = mail_dir / f"{folder_name.lower()}-{suffix}.md"

    lines = []
    lines.append(f"# {folder_name} - {'Unread' if unread_only else f'Last {count}'}")
    lines.append("")
    lines.append(f"> Synced: {datetime.now(get_default_tz()).strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"> Count: {len(email_list)} emails")
    lines.append("")

    # Summary table
    lines.append("| # | Date | From | Subject | Read |")
    lines.append("|---|------|------|---------|------|")

    for i, email in enumerate(email_list, 1):
        date_str = str(email.datetime_received)[:16] if email.datetime_received else "-"
        sender = str(email.sender.email_address) if email.sender else "-"
        subject = (email.subject or "(No subject)").replace("|", "-")
        read = "Yes" if email.is_read else "**No**"
        lines.append(f"| {i} | {date_str} | {sender} | {subject} | {read} |")

    lines.append("")
    lines.append("---")
    lines.append("")

    # Full email content
    for i, email in enumerate(email_list, 1):
        date_str = str(email.datetime_received)[:16] if email.datetime_received else "-"
        sender = str(email.sender.email_address) if email.sender else "-"
        sender_name = str(email.sender.name) if email.sender and email.sender.name else sender
        subject = email.subject or "(No subject)"

        lines.append(f"## {i}. {subject}")
        lines.append("")
        lines.append(f"**From:** {sender_name} <{sender}>")
        lines.append(f"**Date:** {date_str}")

        if email.to_recipients:
            to_list = [r.email_address for r in email.to_recipients if r.email_address]
            if to_list:
                lines.append(f"**To:** {', '.join(to_list)}")

        if email.cc_recipients:
            cc_list = [r.email_address for r in email.cc_recipients if r.email_address]
            if cc_list:
                lines.append(f"**CC:** {', '.join(cc_list)}")

        read_status = "Read" if email.is_read else "Unread"
        lines.append(f"**Status:** {read_status}")

        if email.has_attachments and email.attachments:
            att_names = [a.name for a in email.attachments if hasattr(a, 'name') and a.name]
            if att_names:
                lines.append(f"**Attachments:** {', '.join(att_names)}")

        lines.append("")

        # Email body - prefer plain text, fall back to HTML-stripped, and
        # remove any credential-shaped span before it is written to disk. See
        # `email_body_text`: this file used to carry its own copy of the
        # extraction, and the result of that copy is what refused a backup.
        body = email_body_text(email) or "(No body)"

        # Truncate very long emails
        if len(body) > 3000:
            body = body[:3000] + "\n\n[...truncated - full email too long]"

        lines.append(body)
        lines.append("")
        lines.append("---")
        lines.append("")

        # Auto-bump CRM last_touch on inbound: silently update the matched
        # relationship record. Log entry creation stays under /email-intel
        # approval flow. Strict email match. Silent no-op on no match.
        # Added 2026-05-15 (Phase 1 of CRM action engine).
        try:
            from scripts.utils.crm_autolog import bump_inbound
            sender_addr = (getattr(email.sender, "email_address", None) or "").strip().lower()
            # Self-bump guard: when --folder Sent is used, the sender is the
            # authenticated Exchange user (us). Bumping our own contact on
            # every outbound email is semantically wrong -- the outbound
            # auto-log path (send-email.py) handles those. Skip here.
            # EXCHANGE_EMAIL is loaded into os.environ by load_config() before
            # sync_emails() is ever called, so os.getenv() is safe here.
            self_email = (os.getenv("EXCHANGE_EMAIL", "") or "").strip().lower()
            if sender_addr and sender_addr != self_email:
                bump_inbound(sender_email=sender_addr)
        except Exception as _e:
            # Best-effort: never disrupt the email sync primary work.
            print(f"[WARN] crm_autolog.bump_inbound failed: {_e}", file=sys.stderr)

    atomic_write_text(output_file, "\n".join(lines))
    print(f"[OK] Emails: {len(email_list)} saved to {_display_path(output_file)}")

    return len(email_list)


# ============================================================
# Metadata Export (read only)
# ============================================================

EXPORT_FIELDS = ("subject", "sender", "to_recipients", "cc_recipients",
                 "datetime_received", "datetime_sent", "conversation_id")


def _addr(mailbox):
    return (getattr(mailbox, "email_address", None) or "").strip().lower() if mailbox else ""


def _export_folders(account):
    """(label, folder) for Inbox and its subfolders, Sent Items, and Archive with its subfolders.

    Archive is an ordinary folder under the message root, not a well-known one,
    so a mailbox without it is reported and skipped rather than failing the run.
    Only mail folders (`IPF.Note*`) are walked: a subfolder holding contacts or
    tasks has no `datetime_received` to filter on.
    """
    def mail_tree(root):
        yield root
        for sub in root.walk():
            if (getattr(sub, "folder_class", None) or "IPF.Note").startswith("IPF.Note"):
                yield sub

    folders = [("Inbox", f) for f in mail_tree(account.inbox)]
    folders.append(("Sent Items", account.sent))
    from exchangelib.errors import ErrorFolderNotFound
    try:
        archive = account.msg_folder_root / "Archive"
    except ErrorFolderNotFound:
        print("[WARN] no Archive folder in this mailbox; skipped")
    else:
        folders += [("Archive", f) for f in mail_tree(archive)]
    return folders


def export_metadata(account, since, until, out_path, self_email):
    """Write one JSON line per message received or sent in [since, until], metadata only.

    Keys: dir (in|out), date (ISO, in the configured timezone), subject, from,
    from_name, to, to_names, cc, conv, folder. No body is read. `since` and
    `until` are calendar days in the configured timezone, both inclusive, so a
    mail sent at 01:30 local is filed under its local day and never under the
    UTC one (the defect the removed crm-backfill-exchange.py carried until
    2026-09-25). Direction: Sent Items is always `out`; elsewhere a message
    whose sender is the mailbox owner is `out`, anything else `in`.
    """
    _ensure_exchangelib()
    local_tz = get_default_tz()
    tz = EWSTimeZone.from_timezone(local_tz)
    start = EWSDateTime(since.year, since.month, since.day, 0, 0, 0, tzinfo=tz)
    end_day = until + timedelta(days=1)
    end = EWSDateTime(end_day.year, end_day.month, end_day.day, 0, 0, 0, tzinfo=tz)
    me = (self_email or "").strip().lower()

    rows, seen = [], set()
    for label, folder in _export_folders(account):
        field = "datetime_sent" if folder is account.sent else "datetime_received"
        qs = folder.filter(**{f"{field}__gte": start, f"{field}__lt": end}).only(*EXPORT_FIELDS)
        n = 0
        for it in qs:
            if not hasattr(it, "subject"):
                continue  # an error placeholder from exchangelib, not a message
            when = getattr(it, field) or getattr(it, "datetime_received", None)
            if when is None:
                continue
            sender = _addr(it.sender)
            direction = "out" if (folder is account.sent or (me and sender == me)) else "in"
            to = it.to_recipients or []
            rec = {
                "dir": direction,
                "date": when.astimezone(local_tz).isoformat(),
                "subject": (it.subject or "")[:200],
                "from": sender,
                "from_name": (getattr(it.sender, "name", "") or "") if it.sender else "",
                "to": [_addr(r) for r in to],
                "to_names": {_addr(r): (r.name or "") for r in to},
                "cc": [_addr(r) for r in (it.cc_recipients or [])],
                "conv": getattr(getattr(it, "conversation_id", None), "id", None),
                "folder": label,
            }
            # The recipients are part of the key: two mails from one sender with
            # one subject in the same second to different people are two rows.
            key = (rec["dir"], rec["date"], rec["from"], rec["subject"],
                   tuple(sorted(rec["to"])), tuple(sorted(rec["cc"])))
            if key in seen:
                continue  # one message filed twice (a copy in Archive and in Sent)
            seen.add(key)
            rows.append(rec)
            n += 1
        print(f"[INFO] {label}/{getattr(folder, 'name', '?')}: {n}")

    rows.sort(key=lambda r: r["date"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out_path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"[OK] Metadata export: {len(rows)} messages, {since}..{until}, written to {out_path}")
    return len(rows)


# ============================================================
# Email Deletion
# ============================================================

DELETE_MATCH_CAP = 50
"""How many matches one `--delete` run will act on.

A bound is right -- an unbounded destructive loop over a broad query is worse --
but the bound used to be an anonymous `[:50]` slice, so a query matching 300
messages printed "Found 50 matching email(s)" and then "Deleted 50 email(s)".
Both lines were true and together they read as "the mailbox is now clean". The
cap is named, counted against the real total, and reported when it bites.
"""


def delete_emails(account, subject_query, folder_name="Inbox", confirm=True):
    """Delete emails matching a subject query.

    Returns the number deleted. Raises ValueError on a blank query, which would
    otherwise match every message in the folder.
    """
    if not subject_query or not subject_query.strip():
        raise ValueError("refusing a blank --delete query: it matches every message in the folder")
    folder = _mail_folder(account, folder_name)

    print(f"[INFO] Searching {folder_name} for emails matching: \"{subject_query}\"...")

    hits = folder.filter(subject__icontains=subject_query).order_by("-datetime_received")
    matches = list(hits[:DELETE_MATCH_CAP])

    if not matches:
        print(f"[INFO] No emails found matching \"{subject_query}\".")
        return 0

    truncated = len(matches) == DELETE_MATCH_CAP
    total = None
    if truncated:
        try:
            total = folder.filter(subject__icontains=subject_query).count()
        except Exception as exc:  # noqa: BLE001 - the count is a courtesy, the cap is not
            print(f"[WARN] Could not count the full match set: {exc}")
        if total is not None and total <= DELETE_MATCH_CAP:
            truncated = False

    print(f"[INFO] Found {len(matches)} matching email(s):\n")
    for i, email in enumerate(matches, 1):
        date_str = str(email.datetime_received)[:16] if email.datetime_received else "-"
        sender = str(email.sender.email_address) if email.sender else "-"
        print(f"  {i}. [{date_str}] From: {sender} — {email.subject}")

    if truncated:
        shown = f"{total} match" if total is not None else "more than this"
        print(
            f"\n[WARN] Capped at {DELETE_MATCH_CAP}; {shown}. "
            f"Only the {DELETE_MATCH_CAP} newest are listed above and only those "
            f"will be deleted. Re-run to take the next batch."
        )

    if confirm:
        print()
        answer = input(f"Delete {'this email' if len(matches) == 1 else f'all {len(matches)} emails'}? (y/N): ").strip().lower()
        if answer != "y":
            print("[INFO] Cancelled. No emails deleted.")
            return 0

    for email in matches:
        email.delete()

    if truncated:
        remaining = f"{total - len(matches)} " if total is not None else ""
        print(f"[OK] Deleted {len(matches)} email(s). {remaining}still match; re-run to continue.")
    else:
        print(f"[OK] Deleted {len(matches)} email(s).")
    return len(matches)


# ============================================================
# Meeting Creation
# ============================================================

def create_meeting(account, subject, start_time, duration_minutes=30, location=None, body=None, attendees=None, send_invites=False, timezone_str=None):
    """Create a calendar meeting.

    When send_invites is True and attendees are present, the meeting invitation
    is emailed to the attendees. Otherwise the item is saved as a private HOLD
    with no invitation sent.
    """
    # Same binding gap as `sync_calendar`: the two `from exchangelib import`
    # lines below are local names, and `CalendarItem`, `EWSDateTime` and
    # `EWSTimeZone` are module globals that only `_ensure_exchangelib` sets.
    _ensure_exchangelib()

    from exchangelib import Mailbox, Attendee
    from exchangelib.items import SEND_ONLY_TO_ALL, SEND_TO_NONE

    if timezone_str is None:
        timezone_str = get_default_tz_name()

    local_tz = ZoneInfo(timezone_str)
    tz = EWSTimeZone.from_timezone(local_tz)

    # Parse start_time: "HH:MM" (today) or "YYYY-MM-DD HH:MM"
    if len(start_time) <= 5:
        # "Today" in the zone the timestamp is stamped with. It was read from
        # `get_default_tz()` and then stamped with `tz`, the same mismatch
        # `sync_calendar` carries a comment about having fixed. MEASURED
        # 2026-08-29 with HEADING_OS_TZ=UTC and EXCHANGE_TIMEZONE four hours east of
        # UTC, at the instant 2026-09-01T20:30Z: that zone's wall clock reads 2026-09-02
        # 00:30, and `--time 02:00` booked 2026-09-01 02:00 +04:00, twenty two
        # hours in the past.
        now = datetime.now(local_tz)
        hour, minute = map(int, start_time.split(":"))
        start = EWSDateTime(now.year, now.month, now.day, hour, minute, 0, tzinfo=tz)
    else:
        parts = start_time.split(" ")
        date_parts = list(map(int, parts[0].split("-")))
        time_parts = list(map(int, parts[1].split(":")))
        start = EWSDateTime(date_parts[0], date_parts[1], date_parts[2],
                           time_parts[0], time_parts[1], 0, tzinfo=tz)

    end = start + timedelta(minutes=duration_minutes)

    item = CalendarItem(
        account=account,
        folder=account.calendar,
        subject=subject,
        start=start,
        end=end,
        location=location,
        body=body or "",
    )

    if attendees:
        item.required_attendees = [
            Attendee(mailbox=Mailbox(email_address=email.strip()))
            for email in attendees
        ]

    invite_mode = SEND_ONLY_TO_ALL if (send_invites and attendees) else SEND_TO_NONE
    item.save(send_meeting_invitations=invite_mode)
    print(f"[OK] Meeting created: '{subject}'")
    print(f"     Time: {start} - {end} ({duration_minutes}m)")
    if location:
        print(f"     Location: {location}")
    if attendees:
        sent = "invite sent" if invite_mode == SEND_ONLY_TO_ALL else "HOLD only, no invite sent"
        print(f"     Attendees: {', '.join(attendees)} ({sent})")


# ============================================================
# Main / CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Sync Exchange calendar and emails to workspace")
    parser.add_argument("--calendar", action="store_true", help="Sync calendar only")
    parser.add_argument("--emails", action="store_true", help="Sync emails only")
    parser.add_argument("--days", type=int, default=7, help="Calendar: days ahead (default: 7)")
    parser.add_argument("--email-count", type=int, default=30, help="Emails: how many to fetch (default: 30)")
    parser.add_argument("--unread", action="store_true", help="Emails: unread only")
    parser.add_argument("--folder", type=str, default="Inbox", help="Emails: folder name (default: Inbox)")

    # Meeting creation
    parser.add_argument("--create-meeting", type=str, metavar="SUBJECT", help="Create a calendar meeting")
    parser.add_argument("--time", type=str, help="Meeting start: 'HH:MM' (today) or 'YYYY-MM-DD HH:MM'")
    parser.add_argument("--duration", type=int, default=30, help="Meeting duration in minutes (default: 30)")
    parser.add_argument("--location", type=str, help="Meeting location")
    parser.add_argument("--body", type=str, help="Meeting description")
    parser.add_argument("--attendees", type=str, nargs="*", help="Attendee email addresses")
    parser.add_argument("--send-invites", action="store_true", help="Send the meeting invitation to --attendees (default: HOLD only, no invite sent)")

    # Email deletion
    parser.add_argument("--delete", type=str, metavar="SUBJECT", help=f"Delete emails matching subject (case-insensitive); acts on at most {DELETE_MATCH_CAP} per run, newest first")
    parser.add_argument("--yes", action="store_true", help="Skip the delete confirmation prompt; the per-run cap still applies")
    parser.add_argument("--export-metadata", type=Path, metavar="FILE",
                        help="Read only: write message metadata (no bodies) from Inbox with its subfolders, "
                             "Sent Items and Archive to FILE as JSON lines; needs --since")
    parser.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD",
                        help="--export-metadata: first local day, inclusive")
    parser.add_argument("--until", type=date.fromisoformat, metavar="YYYY-MM-DD",
                        help="--export-metadata: last local day, inclusive (default: today)")

    args = parser.parse_args()
    if args.export_metadata is not None and args.since is None:
        parser.error("--export-metadata needs --since YYYY-MM-DD")

    print("=" * 50)
    print("Exchange Sync")
    print("=" * 50)

    config = load_config()
    account = connect(config)

    if args.export_metadata is not None:
        until = args.until or datetime.now(get_default_tz()).date()
        try:
            export_metadata(account, args.since, until, args.export_metadata.resolve(),
                            self_email=config["EXCHANGE_EMAIL"])
        except Exception as e:
            print(f"[ERROR] Metadata export failed: {e}")
            return 1
        return 0

    # Handle email deletion
    #
    # `is not None`, not truthiness. `--delete ""` parses to the empty string,
    # which is falsy, so this branch was skipped and the run fell through to an
    # ordinary calendar-plus-email sync and exited 0. MEASURED 2026-08-29: the
    # operator asked for a destructive action, got an unrelated sync, and was
    # told nothing. `delete_emails` raises ValueError on a blank query for
    # exactly this reason, and the blank value never reached it.
    if args.delete is not None:
        try:
            delete_emails(
                account,
                subject_query=args.delete,
                folder_name=args.folder,
                confirm=not args.yes,
            )
        except Exception as e:
            print(f"[ERROR] Failed to delete emails: {e}")
            return 1
        return 0

    # Handle meeting creation. Same `is not None`, same reason as --delete
    # above: `--create-meeting ""` ran a full sync and exited 0. Unlike
    # `delete_emails` there is no downstream validator for a blank subject, so
    # the refusal lives here rather than saving an untitled item to the calendar.
    if args.create_meeting is not None:
        if not args.create_meeting.strip():
            print("[ERROR] --create-meeting needs a subject; refusing a blank one.")
            sys.exit(1)
        if not args.time:
            print("[ERROR] --time is required for --create-meeting (e.g., --time 14:30)")
            sys.exit(1)
        try:
            create_meeting(
                account,
                subject=args.create_meeting,
                start_time=args.time,
                duration_minutes=args.duration,
                location=args.location,
                body=args.body,
                attendees=args.attendees,
                send_invites=args.send_invites,
                timezone_str=config["EXCHANGE_TIMEZONE"],
            )
        except Exception as e:
            print(f"[ERROR] Failed to create meeting: {e}")
            return 1
        return 0

    # If neither specified, sync both
    sync_cal = args.calendar or (not args.calendar and not args.emails)
    sync_mail = args.emails or (not args.calendar and not args.emails)

    results = {}

    if sync_cal:
        try:
            results["calendar"] = sync_calendar(account, days=args.days, timezone_str=config["EXCHANGE_TIMEZONE"])
        except Exception as e:
            print(f"[ERROR] Calendar sync failed: {e}")
            results["calendar"] = -1

    if sync_mail:
        try:
            results["emails"] = sync_emails(account, count=args.email_count, unread_only=args.unread, folder_name=args.folder)
        except Exception as e:
            print(f"[ERROR] Email sync failed: {e}")
            results["emails"] = -1

    print("")
    print("=" * 50)
    failed = [k for k, v in results.items() if v < 0]
    print("Sync complete." if not failed else "Sync FAILED.")
    for k, v in results.items():
        status = f"{v} items" if v >= 0 else "FAILED"
        print(f"  {k}: {status}")
    print("=" * 50)
    # Non-zero when any lane failed. Every failure path here printed [ERROR] and
    # returned normally, so the process exited 0 and cron, systemd, wrappers and
    # the hook ecosystem could not tell "synced 0 items" from "sync failed"
    # without parsing stdout.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
