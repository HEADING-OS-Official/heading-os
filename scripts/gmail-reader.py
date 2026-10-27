#!/usr/bin/env python3
"""
gmail-reader.py -- Gmail Reader for Claude Code

Read unread emails from Gmail via the Gmail API.

Prerequisites:
    pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib

Setup:
    Uses the same Google OAuth credentials as google-contacts.py:
      .sessions/google/credentials.json
    Separate token stored at:
      .sessions/google/gmail_token.json
    First run opens browser for OAuth consent.

Usage:
    python scripts/gmail-reader.py unread [--count 5]
    python scripts/gmail-reader.py latest [--count 5]
    python scripts/gmail-reader.py read <message_id>
    python scripts/gmail-reader.py mark-read <message_id> [<message_id> ...]
    python scripts/gmail-reader.py mark-all-read    # marks ALL unread as read

Tests: none since the 2026-09-21 split; the paging and body guards below are unchecked.
"""

import argparse
import base64
import html
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.utils.venv_guard import ensure_venv  # noqa: E402

ensure_venv()
from scripts.utils.workspace import load_env  # noqa: E402


def get_service():
    """Authorized Gmail service.

    The OAuth handling lives in `scripts/utils/gmail_auth`, shared with
    `scripts/gmail-send.py`, so one token definition serves both. The import
    stays inside the function to keep module import pure (F-2.1).
    """
    from scripts.utils.gmail_auth import get_service as _get_service

    try:
        return _get_service()
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)


def get_header(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


NO_TEXT_BODY = "(no text body)"


def _mime_of(part):
    """The bare MIME type of a part, lowercased, without its parameters."""
    return str(part.get("mimeType") or "").split(";")[0].strip().lower()


def _raw_text(part):
    """The part's base64url body decoded to text."""
    return base64.urlsafe_b64decode(
        part["body"]["data"]).decode("utf-8", errors="replace")


def _html_to_text(raw):
    """Tags stripped, entities resolved, whitespace collapsed."""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def decode_body(payload):
    """Plain text for a message payload, or NO_TEXT_BODY when there is none.

    The MIME type is read on the single-part path too, so a top-level text/html
    body is stripped rather than printed as markup; a non-text part is
    NO_TEXT_BODY, and a part with no mimeType is treated as text.
    """
    if payload.get("body", {}).get("data"):
        raw = _raw_text(payload)
        mime = _mime_of(payload)
        if mime == "text/html":
            return _html_to_text(raw)
        if not mime or mime.startswith("text/"):
            return raw
        return NO_TEXT_BODY

    return _decode_parts(payload.get("parts", [])) or NO_TEXT_BODY


def _find_by_type(parts, mime):
    """The first `mime` body anywhere under `parts`, in document order, or "".

    Depth-first over the WHOLE tree for one type at a time. That is what makes
    the caller's promise below literally true.
    """
    for part in parts:
        if _mime_of(part) == mime and part.get("body", {}).get("data"):
            return _raw_text(part)
        if part.get("parts"):
            found = _find_by_type(part["parts"], mime)
            if found:
                return found
    return ""


def _decode_parts(parts):
    """Best available text in a part list, or "" when there is none.

    Two whole-tree passes, plain then html, so plain text wins across sibling
    branches too, and "" (never a non-empty placeholder) means nothing found.
    """
    plain = _find_by_type(parts, "text/plain")
    if plain:
        return plain
    raw = _find_by_type(parts, "text/html")
    return _html_to_text(raw) if raw else ""


def list_messages(service, query, count):
    results = service.users().messages().list(
        userId="me", q=query, maxResults=count
    ).execute()
    return results.get("messages", [])


PAGE_SIZE = 500
MAX_LIST_PAGES = 200        # 100,000 messages, then stop and say so


def list_all_messages(service, query, max_pages=MAX_LIST_PAGES):
    """Messages matching `query`, following nextPageToken. Returns (rows, complete).

    Every page, so `mark-all-read` means ALL; `complete` is False when either
    bound below stopped the walk, and the caller reports that.
    """
    # Bounded twice (a page cap, and no token followed twice): a server that
    # repeats a page forever once grew this loop until the OOM killer (2026-08-24).
    out = []
    token = None
    seen_tokens = set()
    seen_ids = set()
    for _ in range(max_pages):
        kwargs = {"userId": "me", "q": query, "maxResults": PAGE_SIZE}
        if token:
            kwargs["pageToken"] = token
        results = service.users().messages().list(**kwargs).execute()
        # De-duplicated by id: the token guard below fires one page too late,
        # so a repeated page would otherwise be counted twice.
        for m in results.get("messages", []):
            mid = m.get("id")
            if mid is not None:
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
            out.append(m)
        token = results.get("nextPageToken")
        if not token:
            return out, True
        if token in seen_tokens:
            print(f"warning: the server repeated page token {token!r}; "
                  f"stopping after {len(out)} message(s)", file=sys.stderr)
            return out, False
        seen_tokens.add(token)
    print(f"warning: stopped at the {max_pages}-page cap with {len(out)} "
          f"message(s); more remain", file=sys.stderr)
    return out, False


def get_message_summary(service, msg_id):
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "To", "Subject", "Date", "Cc"]
    ).execute()
    headers = msg.get("payload", {}).get("headers", [])
    labels = msg.get("labelIds", [])
    return {
        "id": msg_id,
        "from": get_header(headers, "From"),
        "to": get_header(headers, "To"),
        "cc": get_header(headers, "Cc"),
        "subject": get_header(headers, "Subject"),
        "date": get_header(headers, "Date"),
        "unread": "UNREAD" in labels,
        "snippet": msg.get("snippet", ""),
    }


def get_message_full(service, msg_id):
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    headers = msg.get("payload", {}).get("headers", [])
    labels = msg.get("labelIds", [])
    body = decode_body(msg.get("payload", {}))
    # Truncate very long bodies
    if len(body) > 3000:
        body = body[:3000] + "\n\n[...truncated]"
    return {
        "id": msg_id,
        "from": get_header(headers, "From"),
        "to": get_header(headers, "To"),
        "cc": get_header(headers, "Cc"),
        "subject": get_header(headers, "Subject"),
        "date": get_header(headers, "Date"),
        "unread": "UNREAD" in labels,
        "body": body,
    }


def cmd_unread(args):
    service = get_service()
    messages = list_messages(service, "is:unread", args.count)
    if not messages:
        print("No unread emails.")
        return
    print(f"Found {len(messages)} unread email(s):\n")
    for i, m in enumerate(messages, 1):
        s = get_message_summary(service, m["id"])
        print(f"--- {i}. {s['subject']} ---")
        print(f"  From:    {s['from']}")
        print(f"  Date:    {s['date']}")
        print(f"  To:      {s['to']}")
        if s['cc']:
            print(f"  CC:      {s['cc']}")
        print(f"  Preview: {s['snippet'][:200]}")
        print(f"  ID:      {s['id']}")
        print()


def cmd_latest(args):
    service = get_service()
    messages = list_messages(service, "", args.count)
    if not messages:
        print("No emails found.")
        return
    print(f"Latest {len(messages)} email(s):\n")
    for i, m in enumerate(messages, 1):
        s = get_message_summary(service, m["id"])
        status = "[UNREAD]" if s["unread"] else "[read]"
        print(f"--- {i}. {status} {s['subject']} ---")
        print(f"  From:    {s['from']}")
        print(f"  Date:    {s['date']}")
        print(f"  Preview: {s['snippet'][:200]}")
        print(f"  ID:      {s['id']}")
        print()


def cmd_read(args):
    service = get_service()
    msg = get_message_full(service, args.message_id)
    print(f"Subject: {msg['subject']}")
    print(f"From:    {msg['from']}")
    print(f"To:      {msg['to']}")
    if msg['cc']:
        print(f"CC:      {msg['cc']}")
    print(f"Date:    {msg['date']}")
    print(f"Status:  {'UNREAD' if msg['unread'] else 'Read'}")
    print(f"\n{'='*60}\n")
    print(msg['body'])


def cmd_mark_read(args):
    service = get_service()
    for mid in args.message_ids:
        service.users().messages().modify(
            userId="me", id=mid,
            body={"removeLabelIds": ["UNREAD"]}
        ).execute()
        print(f"Marked as read: {mid}")


def cmd_mark_all_read(args):
    service = get_service()
    messages, complete = list_all_messages(service, "is:unread")
    if not messages:
        print("No unread emails.")
        return
    for m in messages:
        s = get_message_summary(service, m["id"])
        service.users().messages().modify(
            userId="me", id=m["id"],
            body={"removeLabelIds": ["UNREAD"]}
        ).execute()
        print(f"  [x] {s['subject']}")
    if complete:
        print(f"\nMarked {len(messages)} emails as read.")
    else:
        # The whole point of this command's fix: never report completion over
        # a list that was truncated. `list_all_messages` has already said on
        # stderr why it stopped.
        print(f"\nMarked {len(messages)} emails as read. NOT all of them: the "
              f"listing was cut short (see the warning above). Run the command "
              f"again to continue.")


def main():
    parser = argparse.ArgumentParser(description="Gmail Reader for Claude Code")
    sub = parser.add_subparsers(dest="command")

    p_unread = sub.add_parser("unread", help="List unread emails")
    p_unread.add_argument("--count", type=int, default=5)

    p_latest = sub.add_parser("latest", help="List latest emails")
    p_latest.add_argument("--count", type=int, default=5)

    p_read = sub.add_parser("read", help="Read full email by ID")
    p_read.add_argument("message_id")

    p_mark = sub.add_parser("mark-read", help="Mark emails as read")
    p_mark.add_argument("message_ids", nargs="+")

    sub.add_parser("mark-all-read", help="Mark all unread as read")

    args = parser.parse_args()

    # The `.env` is read HERE, never at import, so importing this module does
    # not write the whole file into `os.environ` for the process.
    load_env(PROJECT_ROOT)

    if args.command == "unread":
        cmd_unread(args)
    elif args.command == "latest":
        cmd_latest(args)
    elif args.command == "read":
        cmd_read(args)
    elif args.command == "mark-read":
        cmd_mark_read(args)
    elif args.command == "mark-all-read":
        cmd_mark_all_read(args)
    else:
        # Non-zero, so a missing subcommand never reads as success to a wrapper.
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
