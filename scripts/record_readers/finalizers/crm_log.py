"""CRM-log finalizer: log one email conversation on its linked CRM contact.

Appends an entry to that contact's `crm-contact` record (parameter
`person=<slug>`) under its Interaction Log and bumps last_touch, reusing the
helpers in scripts/utils/crm_autolog. Its caller is the `crm_log` outcome
eval in `scripts/eval-outcomes.py` (case files under the email-intel skill's
`evals/outcomes/`), which runs it against a sandbox data root.

The conversation -> contact link comes from email-intelligence.py's
crm_context.contact_slug in _latest-fetch.json. Conversations already logged
are recorded in _crm-logged.jsonl, so a second call must not write two entries.
"""
import json
import re
import threading
from datetime import date, datetime
from pathlib import Path

from scripts.utils.workspace import get_default_tz

from scripts.record_readers.sources.inbox import (
    crm_logged_path,
    latest_fetch_path,
    mark_crm_logged,
    read_crm_logged,
)
from scripts.utils.crm_autolog import (
    append_log_entry,
    atomic_write,
    bump_last_touch_in_text,
    data_is_readonly,
)
from scripts.utils.data_path import data_path
from scripts.utils.paths import get_data_root

# Contact slugs are kebab-case filenames under `crm-contacts`. The
# allowlist rejects path traversal and any shape that could escape the
# contacts directory.
_SLUG_RE = re.compile(r"^[a-z0-9-]{1,80}$")


_CONTACT_WRITE_LOCK = threading.Lock()

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _interaction_date(raw_dt: str) -> str:
    """The `YYYY-MM-DD` from an ISO timestamp, or today when it is not one.

    The comment promised a fallback when `latest_datetime` was "missing or
    malformed", but the code only measured LENGTH: any string of ten or more
    characters was sliced verbatim, so `not-a-date-xx` was written into a CRM
    contact file as a plausible-looking interaction date.
    """
    head = (raw_dt or "")[:10]
    # Shape first, then a real calendar check. `date.fromisoformat` alone is
    # too permissive on 3.11: it accepts the compact `20260824` form, and a
    # producer emitting that would write a date this file never formats.
    if not _ISO_DATE_RE.match(head):
        return datetime.now(get_default_tz()).strftime("%Y-%m-%d")
    try:
        date(int(head[0:4]), int(head[5:7]), int(head[8:10]))
    except ValueError:
        return datetime.now(get_default_tz()).strftime("%Y-%m-%d")
    return head


def log_to_crm(conv_id: str, data_root: "Path | None" = None) -> dict:
    """Append an interaction-log entry for `conv_id` to its CRM contact.

    There is no `workspace_root` parameter: everything this touches is DATA.

    Returns {ok: True, slug, date} on success, or {ok: False, error}
    when the conversation is missing, has no linked contact, was already
    logged, or the CRM write fails.

    HEADING OS engine/data split: the fetch file, the crm-logged dedupe log,
    and the `crm-contact` record are all DATA, so they resolve under
    ``data_root`` (falls back to the ``get_data_root()`` seam when not supplied, NOT to ``workspace_root``).
    """
    if data_root is None:
        data_root = get_data_root()
    if not isinstance(conv_id, str) or not conv_id.strip():
        return {"ok": False, "error": "conv_id is required"}
    # Trimmed: the guard above tests the STRIPPED value, so writing the raw one
    # puts a key in the dedupe log that no read can match.
    conv_id = conv_id.strip()
    if len(conv_id) > 500:
        return {"ok": False, "error": "conv_id too long"}

    # Idempotency, fast path. This read is an optimisation only; the check that
    # decides is the one under the lock further down. See the comment there.
    if conv_id in read_crm_logged(data_root):
        return {"ok": False, "error": "conversation already logged to CRM"}

    fetch_path = latest_fetch_path(data_root)
    if not fetch_path.exists():
        return {"ok": False, "error": "no latest fetch on disk (run /email-intel first)"}
    try:
        data = json.loads(fetch_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        # `UnicodeDecodeError` comes out of `read_text`, ahead of `json.loads`.
        return {"ok": False, "error": f"fetch unreadable: {e}"}
    conversations = data.get("conversations", [])
    if not isinstance(conversations, list):
        return {"ok": False, "error": "unexpected fetch schema"}
    conv = next(
        (c for c in conversations if isinstance(c, dict) and c.get("id") == conv_id),
        None,
    )
    if conv is None:
        return {"ok": False, "error": "conversation not in latest fetch"}

    crm = conv.get("crm_context") or {}
    slug = (crm.get("contact_slug") or "").strip()
    if not slug:
        return {"ok": False, "error": "no CRM contact linked to this conversation"}
    if not _SLUG_RE.match(slug):
        return {"ok": False, "error": f"invalid contact slug: {slug!r}"}

    # The registry name, rooted at `data_root` so the eval's sandbox is the
    # tree read and written. Nothing here calls `mkdir`, so an unreachable
    # contact fails closed on the `exists()` check below.
    contact_file = data_path("crm-contacts", data_root=data_root) / f"{slug}.md"
    if not contact_file.exists():
        return {"ok": False, "error": f"CRM contact file not found: {slug}"}

    topic = conv.get("topic") or "(no subject)"
    # latest_datetime is ISO; take the date portion as the interaction
    # date, falling back to today if it is missing or malformed.
    raw_dt = conv.get("latest_datetime") or ""
    log_date = _interaction_date(raw_dt)

    # This writes the contact card without going through `crm_autolog`'s own
    # read-only guard (added 2026-09-02), so it checks the flag itself, before
    # the lock.
    if data_is_readonly():
        return {"ok": False,
                "error": f"HEADING_OS_DATA_READONLY is set: this host mirrors "
                         f"the data repo and must not write to it, so {slug} "
                         f"was not logged"}

    # Locked read-modify-write on a shared contact file; the dedupe check and the
    # mark sit in the same critical section so two calls cannot both pass it
    # (since 2026-08-24).
    with _CONTACT_WRITE_LOCK:
        if conv_id in read_crm_logged(data_root):
            return {"ok": False, "error": "conversation already logged to CRM"}
        try:
            text = contact_file.read_text(encoding="utf-8")
            text = bump_last_touch_in_text(text, log_date)
            text = append_log_entry(
                text, log_date, "Email", topic,
                "Logged from email intelligence.",
            )
            atomic_write(contact_file, text)
        except (OSError, UnicodeDecodeError) as e:
            # A hand-edited contact record may not be UTF-8; answer, never raise.
            return {"ok": False, "error": f"CRM write failed: {e}"}
        ok, err = mark_crm_logged(data_root, conv_id, slug)
    if not ok:
        # The CRM entry IS written; only the dedupe record failed. Report
        # success but flag the gap so a retry could double-log.
        return {"ok": True, "slug": slug, "date": log_date,
                "warning": f"dedupe log not updated: {err}"}
    return {"ok": True, "slug": slug, "date": log_date}


# `crm_logged_path` is re-exported for callers that want the log path
# without reaching into the inbox module directly.
__all__ = ["log_to_crm", "crm_logged_path"]
