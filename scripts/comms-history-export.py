#!/usr/bin/env python3
"""
comms-history-export.py - read-only export of the operator's message history.

Writes JSONL snapshots of past mail and Telegram traffic for offline study, such
as fitting the Sentinel urgency rule to who the operator actually answers. It
reads and never writes back: no item is marked read, saved, moved, sent or
deleted, and no Telegram message is acknowledged, sent, joined or left.

The output is private (ceo-only) and belongs in the data repository. `--out` is
required and is never defaulted, so the caller resolves it by logical name
(`python scripts/data-path.py scratch`, or a registry name) rather than this
script guessing an address, and a directory inside the engine checkout is
refused before any source is read.

Usage:
  python scripts/comms-history-export.py exchange --out DIR --since 2026-06-25 [--calendar-ahead-days 14]
  python scripts/comms-history-export.py telegram --out DIR --since 2026-06-25 [--chat-names FILE] [--no-active-dms]

exchange writes inbox.jsonl (Inbox and its mail subfolders), deleted.jsonl
(Deleted Items, received in the period), sent.jsonl and calendar.jsonl. telegram writes telegram.jsonl (one line per message) and
telegram-chats.jsonl (one line per chat). Chats are the names listed in
--chat-names (one per line, `#` comments allowed) plus, unless --no-active-dms,
every non-bot direct dialog active since --since. Counts per month and the
number of empty values per field go to stdout as JSON, so a gap is said rather
than hidden.

Exit codes: 0 written; 1 a source could not be read; 2 bad arguments.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from scripts.utils.atomic import atomic_write_text  # noqa: E402
from scripts.utils.paths import DataRootError, require_outside_engine_clone  # noqa: E402
from scripts.utils.workspace import get_default_tz  # noqa: E402

REPLY_RE = re.compile(r"^\s*(re|ответ|отв)\s*:", re.I)
PREFIX_RE = re.compile(r"^\s*((re|fw|fwd|ответ|отв|пересл)\s*(\[\d+\])?\s*:\s*)+", re.I)
BODY_HEAD = 1500
TEXT_HEAD = 500


def _load(name: str, rel: str):
    """A sibling script by path: its file name carries a hyphen, so it cannot be imported."""
    spec = importlib.util.spec_from_file_location(name, ENGINE / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_jsonl(path: Path, rows: list[dict]) -> None:
    atomic_write_text(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), mode=0o600)


def tally(rows: list[dict], when: str) -> dict:
    """Total, per month and empty values per field: the coverage a reader needs."""
    per_month = Counter((r.get(when) or "")[:7] for r in rows)
    nulls = Counter(k for r in rows for k, v in r.items() if v is None)
    return {"total": len(rows), "per_month": dict(sorted(per_month.items())), "null_fields": dict(nulls)}


# ============================================================
# Exchange
# ============================================================

def _months(start, end, tz):
    from exchangelib import EWSDateTime
    cur = start
    while cur < end:
        nxt = EWSDateTime(cur.year + (cur.month == 12), cur.month % 12 + 1, 1, tzinfo=tz)
        yield cur, min(nxt, end)
        cur = nxt


def _emails(recips) -> list[str]:
    return [str(r.email_address).lower() for r in (recips or []) if r is not None and r.email_address]


def export_exchange(out: Path, since: date, ahead_days: int) -> dict:
    from exchangelib import EWSDateTime, EWSTimeZone

    ei = _load("email_intelligence", "scripts/email-intelligence.py")
    from scripts.utils.html_text import email_body_text

    tz = EWSTimeZone(str(get_default_tz()))
    start = EWSDateTime(since.year, since.month, since.day, tzinfo=tz)
    now = EWSDateTime.now(tz=tz)

    def iso(dt):
        return dt.astimezone(tz).isoformat() if dt else None

    # Read-only by construction: `.only()` field lists, iteration, and nothing
    # that assigns `is_read` or calls save/send/move/delete on an item.
    acc = ei.connect_exchange()
    owner = str(acc.primary_smtp_address).lower()
    stats: dict = {}

    fields = ("id", "conversation_id", "datetime_received", "sender", "to_recipients", "cc_recipients",
              "has_attachments", "attachments", "subject", "in_reply_to", "headers", "importance",
              "is_read", "text_body", "body")

    def received_mail(folders) -> tuple[list[dict], int]:
        rows: list[dict] = []
        body_failures = 0
        for folder in folders:
            for a, b in _months(start, now, tz):
                qs = folder.filter(datetime_received__gte=a, datetime_received__lt=b).only(*fields)
                qs.page_size = 50
                for it in qs.order_by("datetime_received"):
                    # Deleted Items also holds calendar items and tasks, which are not mail.
                    if not hasattr(it, "datetime_received") or not hasattr(it, "to_recipients"):
                        continue
                    to, cc = _emails(it.to_recipients), _emails(it.cc_recipients)
                    subject = str(it.subject or "")
                    headers = getattr(it, "headers", None)
                    atts = getattr(it, "attachments", None)
                    try:
                        body = email_body_text(it)
                    except (ValueError, TypeError, AttributeError) as exc:
                        body_failures += 1
                        print(f"exchange: body unreadable for one item ({type(exc).__name__})", file=sys.stderr)
                        body = None
                    rows.append({
                        "id": it.id,
                        "conversation_id": it.conversation_id.id if it.conversation_id else None,
                        "received": iso(it.datetime_received),
                        "from_name": str(it.sender.name) if it.sender and it.sender.name else None,
                        "from_email": (str(it.sender.email_address).lower()
                                       if it.sender and it.sender.email_address else None),
                        "to": to, "cc": cc, "n_recipients": len(to) + len(cc),
                        "operator_in_to": owner in to,
                        "has_attachments": it.has_attachments,
                        "attachment_count": (len(atts) if atts is not None
                                             else (0 if it.has_attachments is False else None)),
                        "subject": subject,
                        "is_reply": bool(REPLY_RE.match(subject) or it.in_reply_to),
                        "has_list_unsubscribe": (None if headers is None else
                                                 any(str(getattr(h, "name", "")).lower() == "list-unsubscribe"
                                                     for h in headers)),
                        "importance": str(it.importance) if it.importance else None,
                        "is_read": it.is_read,
                        "body_head": body[:BODY_HEAD] if body is not None else None,
                        "folder": str(folder.name),
                    })
        return rows, body_failures

    folders = [acc.inbox] + [f for f in acc.inbox.walk()
                             if getattr(f, "folder_class", None) in (None, "IPF.Note")]
    rows, failures = received_mail(folders)
    write_jsonl(out / "inbox.jsonl", rows)
    stats["inbox"] = {**tally(rows, "received"), "folders": [str(f.name) for f in folders],
                      "body_failures": failures}

    # Deleted Items: mail the operator threw away, which a study reads as his own
    # "not important" verdict. Only what the server still keeps is here; a purged
    # message is gone, and its absence says nothing.
    rows, failures = received_mail([acc.trash])
    write_jsonl(out / "deleted.jsonl", rows)
    stats["deleted"] = {**tally(rows, "received"), "body_failures": failures}

    rows = []
    for a, b in _months(start, now, tz):
        qs = acc.sent.filter(datetime_sent__gte=a, datetime_sent__lt=b).only(
            "id", "conversation_id", "datetime_sent", "to_recipients", "cc_recipients", "subject")
        qs.page_size = 100
        for it in qs.order_by("datetime_sent"):
            if not hasattr(it, "datetime_sent"):
                continue
            subject = str(it.subject or "")
            rows.append({"id": it.id,
                         "conversation_id": it.conversation_id.id if it.conversation_id else None,
                         "sent": iso(it.datetime_sent), "to": _emails(it.to_recipients),
                         "cc": _emails(it.cc_recipients), "subject": subject,
                         "subject_normalised": PREFIX_RE.sub("", subject).strip().lower()})
    write_jsonl(out / "sent.jsonl", rows)
    stats["sent"] = tally(rows, "sent")

    rows, seen = [], set()
    for a, b in _months(start, now + timedelta(days=ahead_days), tz):
        # `view` expands recurring series into their occurrences.
        for it in acc.calendar.view(start=a, end=b).only(
                "id", "start", "end", "subject", "organizer", "required_attendees",
                "optional_attendees", "is_cancelled"):
            key = (it.id, str(it.start))
            if key in seen:  # an event across a window boundary comes back twice
                continue
            seen.add(key)
            attendees = [str(x.mailbox.email_address).lower()
                         for x in (it.required_attendees or []) + (it.optional_attendees or [])
                         if x and x.mailbox and x.mailbox.email_address]
            rows.append({
                "start": it.start.astimezone(tz).isoformat() if hasattr(it.start, "astimezone") else str(it.start),
                "end": it.end.astimezone(tz).isoformat() if hasattr(it.end, "astimezone") else str(it.end),
                "subject": str(it.subject or ""),
                "organizer_email": (str(it.organizer.email_address).lower()
                                    if it.organizer and it.organizer.email_address else None),
                "attendee_emails": attendees, "is_cancelled": it.is_cancelled})
    write_jsonl(out / "calendar.jsonl", rows)
    stats["calendar"] = tally(rows, "start")
    return stats


# ============================================================
# Telegram
# ============================================================

def _chat_type(entity) -> str | None:
    from telethon import types
    if isinstance(entity, types.User):
        return "user"
    if isinstance(entity, types.Chat):
        return "group"
    if isinstance(entity, types.Channel):
        return "group" if entity.megagroup else "channel"
    return None


async def _export_telegram(out: Path, since: date, names: set[str], active_dms: bool) -> dict:
    from telethon import errors, types

    tc = _load("telegram_client", ".claude/skills/telegram/scripts/telegram_client.py")
    tz = get_default_tz()
    start = datetime(since.year, since.month, since.day, tzinfo=tz)

    client = tc.create_client()
    client.flood_sleep_threshold = 24 * 3600  # sleep through any flood wait Telegram asks for
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telegram session not authorised; run the telegram skill's setup first")
    messages: list[dict] = []
    chats: list[dict] = []
    try:
        selected, hits, bots = [], set(), 0
        async for d in client.iter_dialogs():
            name = tc.get_entity_name(d.entity)
            if name in names:
                hits.add(name)
                selected.append((d, True))
            elif active_dms and isinstance(d.entity, types.User):
                if d.entity.bot:
                    bots += 1
                elif d.date and d.date >= start:
                    selected.append((d, False))
        for d, listed in selected:
            ent, n, mine = d.entity, 0, 0
            while True:
                try:
                    batch = []
                    # iter_messages reads; it never sends a read acknowledgement.
                    async for m in client.iter_messages(ent, wait_time=1):
                        if m.date < start:
                            break
                        batch.append(m)
                    break
                except errors.FloodWaitError as exc:
                    print(f"telegram: flood wait {exc.seconds}s", file=sys.stderr)
                    await asyncio.sleep(exc.seconds + 1)
            for m in batch:
                text = m.message if isinstance(m, types.Message) else None
                messages.append({
                    "chat_id": d.id, "chat_name": tc.get_entity_name(ent), "chat_type": _chat_type(ent),
                    "msg_id": m.id, "date": m.date.astimezone(tz).isoformat(), "from_me": bool(m.out),
                    "sender_name": tc.get_entity_name(m.sender) if m.sender is not None else None,
                    "reply_to_msg_id": getattr(m.reply_to, "reply_to_msg_id", None) if m.reply_to else None,
                    "text_head": text[:TEXT_HEAD] if text else None,
                    "has_media": m.media is not None,
                    "is_service": isinstance(m, types.MessageService)})
                n += 1
                mine += int(bool(m.out))
            chats.append({"chat_id": d.id, "chat_name": tc.get_entity_name(ent), "chat_type": _chat_type(ent),
                          "n_messages": n, "n_from_me": mine, "listed": listed})
    finally:
        await client.disconnect()

    messages.sort(key=lambda r: (r["date"], r["chat_id"], r["msg_id"]))
    write_jsonl(out / "telegram.jsonl", messages)
    write_jsonl(out / "telegram-chats.jsonl", chats)
    return {"telegram": tally(messages, "date"), "chats": len(chats), "bot_dms_skipped": bots,
            "listed_names_not_found": sorted(names - hits)}


# ============================================================
# CLI
# ============================================================

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("exchange", "telegram"):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True, type=Path, help="output directory (resolve it by logical name)")
        p.add_argument("--since", required=True, type=date.fromisoformat, help="first day, YYYY-MM-DD")
        if name == "exchange":
            p.add_argument("--calendar-ahead-days", type=int, default=14)
        else:
            p.add_argument("--chat-names", type=Path, help="file of chat names, one per line")
            p.add_argument("--no-active-dms", action="store_true", help="only the listed chats")
    args = ap.parse_args()
    if not args.out.is_dir():
        print(f"--out {args.out} is not a directory", file=sys.stderr)
        return 2
    # The export is the operator's private mail and messages: never into the
    # public engine checkout, whatever --out says (engine audit F-3.p4.18).
    try:
        require_outside_engine_clone(args.out, "--out")
    except DataRootError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "exchange":
            stats = export_exchange(args.out, args.since, args.calendar_ahead_days)
        else:
            names: set[str] = set()
            if args.chat_names:
                names = {ln.strip() for ln in args.chat_names.read_text(encoding="utf-8").splitlines()
                         if ln.strip() and not ln.lstrip().startswith("#")}
            stats = asyncio.run(_export_telegram(args.out, args.since, names, not args.no_active_dms))
    except (RuntimeError, OSError, ConnectionError) as exc:
        print(f"{args.cmd}: could not read the source: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(stats, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
