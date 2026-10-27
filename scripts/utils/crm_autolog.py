"""crm_autolog.py -- Shared library for auto-logging Exchange interactions to CRM.

Resolves a recipient or sender email to a relationship record via strict match
against the address book (canonical_email + other_emails union). Appends a 1-line
interaction log entry on outbound and bumps `last_touch`. For inbound, only bumps
`last_touch` silently (log entry creation stays under /email-intel approval flow).

All writes are atomic through `scripts/utils/atomic.atomic_write_text` (per the global no-non-atomic-state-writes rule).

Cached email index: the (email -> [slug]) map is built lazily and invalidated on
address-book directory mtime change. Lookup is O(1) after first call.

Audit log: every log_outbound / bump_inbound / multi-match conflict appends a
JSONL entry to `crm-autolog-{date}.jsonl` under the runtime store
`runtime:sync-exchange/logs` for observability. That store is gitignored in the
data repository, so the trail is local to this machine and is NOT backed up.

Read-only hosts: `HEADING_OS_DATA_READONLY` stops both record writers. See
`data_is_readonly` for the measurement that produced it.
"""

import html as _html
import json as _json
import logging
import os
import re
import stat
import sys
from datetime import datetime, timezone

from scripts.utils.workspace import DataRootError, get_default_tz
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Ensure scripts.utils.markdown is importable when crm_autolog is invoked as a
# library from elsewhere in the workspace.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts.utils.atomic import atomic_write_text  # noqa: E402
from scripts.utils.markdown import parse_frontmatter, set_frontmatter_field


def _address_book_dir(workspace_root: Optional[Path] = None) -> Path:
    """Resolve the address book, through the data-root seam when no root is given.

    This used to resolve against `get_workspace_root()`, the ENGINE clone, while
    the CRM tree lives in the private DATA overlay, so every production call
    resolved to `<engine>/crm/address-book`, a directory that does not exist. `resolve_recipient`
    returns None for a missing directory exactly as it does for an unknown address,
    so 19 290 auto-log calls between 2026-06-14 and 2026-08-25 were recorded as
    `"matched": false` - "no such contact" - while the real reason was that nothing
    had been looked at. Not one of them matched.

    `scripts/utils/crm.py` already carries the correct resolution, so this delegates
    rather than restating it: a second copy of a seam is the copy that stops being
    fixed. An explicit `workspace_root` still means "this exact tree", which is what
    the test fixtures pass.
    """
    if workspace_root is None:
        from scripts.utils.crm import _address_book_dir as _seam
        return _seam()
    # The executive workspace's read-only `corporate/crm/address-book/` was a
    # second candidate until 2026-09-25, removed with the executive model.
    # By name against that tree, as `crm._address_book_dir` does (HOS-234 part 8).
    from scripts.utils.data_path import data_path
    return data_path("crm-address-book", data_root=Path(workspace_root))


def _contacts_dir(workspace_root: Optional[Path] = None) -> Path:
    """Resolve the contacts tree; see `_address_book_dir` for the seam it shares.

    It shared the seam for the address book and not for the contacts, which is
    the half-converted shape the sibling above was written to end. This called
    the legacy contacts helper (retired since), so after the HEADING OS 2 move it raised
    `LegacyHomeVacated` while `_address_book_dir` beside it answered
    `status/entity/crm/address-book` (MEASURED 2026-09-16 on the migrated  # leak-guard: ok (measurement: the address answered on 2026-09-16)
    rehearsal overlay). Both now delegate to `scripts/utils/crm.py`, which is
    the same rule the docstring above already states: a second copy of a seam
    is the copy that stops being fixed.
    """
    if workspace_root is None:
        from scripts.utils.crm import contacts_home
        return contacts_home()
    # The two-layer workspace's `personal/crm/contacts/` was a second candidate  # leak-guard: ok (history: a candidate removed 2026-09-25)
    # until 2026-09-25, removed with the executive model.
    from scripts.utils.data_path import data_path
    return data_path("crm-contacts", data_root=Path(workspace_root))


def _logs_dir(workspace_root: Optional[Path] = None) -> Path:
    """Where the audit trail is written: `runtime:sync-exchange/logs`.

    Every entry carries an e-mail address, so the trail is private data and
    lives in the data repository, never the engine clone. The store is
    gitignored there, so the trail stays on this machine and is not backed up.
    `require_writable_data_root()` refuses when there is no data repository
    rather than writing anywhere else, and `_audit_log` reports the refusal on
    stderr. An explicit `workspace_root` still means that exact tree.
    """
    if workspace_root is None:
        from scripts.utils.workspace import require_writable_data_root
        base = require_writable_data_root()
    else:
        base = Path(workspace_root)
    from scripts.utils.data_path import data_write_dir
    return data_write_dir("runtime:sync-exchange/logs", data_root=base)


def _audit_log(event: dict, workspace_root: Optional[Path] = None) -> None:
    """Append a JSONL audit entry. Best-effort; never raises into the caller."""
    from scripts.utils.data_path import DataPathError
    try:
        today = datetime.now(get_default_tz()).strftime("%Y-%m-%d")
        log_path = _logs_dir(workspace_root) / f"crm-autolog-{today}.jsonl"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(_json.dumps({**event, "ts": datetime.now(timezone.utc).isoformat()}) + "\n")
    except (DataRootError, DataPathError) as e:
        # No private overlay on this clone, so there is nowhere the trail may
        # live. The front door reports that, and a store it refuses, as
        # `DataPathError` (named since 2026-09-25, when `_logs_dir` moved onto it). Say so on stderr; the alternative is writing e-mail addresses
        # into the public engine tree.
        print(f"[crm_autolog] audit entry not written, no private data root: {e}",
              file=sys.stderr)
    except OSError as e:
        # Best-effort: never raise into the caller. Surface to stderr so
        # daemon logs capture the failure for operator review.
        print(f"[crm_autolog] audit log failed: {e}", file=sys.stderr)


DATA_READONLY_ENV = "HEADING_OS_DATA_READONLY"

# Spelled out in BOTH directions on purpose. A value that is neither reads as
# ON, because the only way this name is non-empty is that somebody set it, and a
# guard whose typo reads as "off" is the defect it exists to prevent. The
# workspace already has that scar: `resolve_mode` in
# `scripts/utils/overlay_write_guard.py` carries the same reasoning for
# `HEADING_OS_OVERLAY_GUARD=recrod`. Erring toward ON costs an announced,
# recoverable skip; erring toward OFF costs the frozen mirror below.
_READONLY_ON = frozenset({"1", "true", "yes", "on"})
_READONLY_OFF = frozenset({"", "0", "false", "no", "off"})


def data_is_readonly() -> bool:
    """Whether this host is forbidden from mutating the CRM records it holds.

    MEASURED 2026-08-30 on the operator's service host. That host executes scripts
    and runs three daemons, and it keeps a PULL-ONLY clone of the private DATA
    repo: a sync script runs `git pull --ff-only` hourly and nothing there ever
    commits or pushes. At 15:00:00 a team-bot daemon's
    backup-email job shelled out to `scripts/send-email.py`, whose
    `_autolog_to` calls `log_outbound` for every recipient unconditionally - no
    environment flag, no host check, no mode switch - and five contact cards
    were rewritten in the mirror's working tree between 15:00:03 and 15:00:21.
    `git pull --ff-only` aborted on every hour that followed, so the mirror sat
    five commits behind for about three and a half days. The sync script printed
    a warning and still exited 0, and systemd reported success the whole time.

    The operator's ruling: a host that executes scripts must never mutate the
    data repo. The control lives here rather than in each daemon's own
    configuration so it holds on every mirror host, and not by convention.

    Reads `os.environ` only, never the `.env` FILE. A caller that runs
    `load_env()` first therefore sees a `.env` value - `send-email.py` does,
    inside `load_config()`, before any send - while a caller that does not must
    have the name exported, which is the systemd `Environment=` case this was
    written for.
    """
    raw = os.environ.get(DATA_READONLY_ENV, "").strip().lower()
    if raw in _READONLY_OFF:
        return False
    if raw in _READONLY_ON:
        return True
    print(f"[crm_autolog] {DATA_READONLY_ENV}={raw!r} is not a value this "
          f"understands; treating the data root as read-only", file=sys.stderr)
    return True


# Process-level email-index cache. Keyed by (ab_dir, mtime-signature).
# Rebuilt on directory mtime change; O(1) lookup after first call.
_EMAIL_INDEX_CACHE: dict = {}


def _index_signature(ab_dir: Path) -> tuple:
    """Cheap mtime-based signature of the address-book directory contents."""
    try:
        return tuple(sorted(
            (p.name, p.stat().st_mtime) for p in ab_dir.glob("*.md")
        ))
    except OSError:
        return ()


def _build_email_index(ab_dir: Path) -> dict:
    """Return {email_lowercase: [slug, ...]} for every entity in the address book.

    Cached; rebuilt only when mtime signature changes.
    """
    sig = _index_signature(ab_dir)
    cached = _EMAIL_INDEX_CACHE.get(str(ab_dir))
    if cached and cached[0] == sig:
        return cached[1]

    idx: dict = {}
    for entity_file in ab_dir.glob("*.md"):
        try:
            text = entity_file.read_text(encoding="utf-8")
            fm, _body = parse_frontmatter(text)
        except (OSError, UnicodeDecodeError):
            # `UnicodeDecodeError` is a `ValueError`, NOT an `OSError`, so one
            # address-book entity that is not valid UTF-8 raised out of the
            # whole walk rather than skipping its own iteration, and the index
            # came back as an exception instead of a dict. MEASURED 2026-09-01
            # on two entity files, one clean and one carrying a lone 0xe9.
            #
            # The cache above makes the silence worse: a caller that swallowed
            # the raise saw no index at all, and nothing named the file. The
            # sibling defect in `crm.contact_index_by_email` was fixed the same
            # day, for the same reason.
            logger.warning("skipping unreadable address-book entity %s; its "
                           "addresses will be absent from the email index",
                           entity_file, exc_info=True)
            continue
        emails = set()
        ce = (fm.get("canonical_email") or "").strip().lower()
        if ce:
            emails.add(ce)
        other = fm.get("other_emails", [])
        if isinstance(other, list):
            for e in other:
                if e:
                    emails.add(str(e).strip().lower())
        elif isinstance(other, str) and other.strip():
            emails.add(other.strip().lower())
        for e in emails:
            idx.setdefault(e, []).append(entity_file.stem)

    _EMAIL_INDEX_CACHE[str(ab_dir)] = (sig, idx)
    return idx


def resolve_recipient(email: str, workspace_root: Optional[Path] = None) -> Optional[Path]:
    """Resolve an email to the matching relationship record file path.

    Strict match: lowercase exact comparison against the address book's
    canonical_email + other_emails union. Returns None on no match OR
    on ambiguous multi-match (two entities claim the same email).

    A multi-match is recorded as a `"kind": "conflict"` entry in the ordinary
    daily audit log, `runtime-junk/sync-exchange/logs/crm-autolog-{date}.jsonl`. This used to name a
    separate `crm-autolog-conflicts-{date}.jsonl`, which nothing writes and
    nothing reads: an operator following the docstring would have looked for a
    file that has never existed, found none, and concluded there were no
    conflicts. `_audit_log` has only ever had one destination.
    """
    if not email:
        return None
    target = email.strip().lower()
    if not target:
        return None

    ab_dir = _address_book_dir(workspace_root)
    if not ab_dir.exists():
        return None

    idx = _build_email_index(ab_dir)
    matched_slugs = idx.get(target, [])

    if len(matched_slugs) > 1:
        # Multi-match conflict: log + refuse to write. Surfaces in crm-health
        # radar so CEO can deduplicate the address book.
        _audit_log({
            "kind": "conflict",
            "email": target,
            "matched_slugs": matched_slugs,
        }, workspace_root=workspace_root)
        return None

    if len(matched_slugs) != 1:
        return None

    slug = matched_slugs[0]
    rel_path = _contacts_dir(workspace_root) / f"{slug}.md"
    return rel_path if rel_path.exists() else None


def atomic_write(path: Path, content: str) -> None:
    """Atomic write through `atomic_write_text`, keeping the target's mode.

    The temp name is unique per call, so two writers (`sync-exchange` calling
    `bump_inbound` while `send-email.py` calls `log_outbound`) never share one
    scratch file. The existing record's mode is passed through, so an auto-log
    never narrows a contact file to the temp file's 0600; a new file is 0644.
    """
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    atomic_write_text(path, content, mode=mode)


def plain_snippet(body: str, limit: int = 200) -> str:
    """Convert an HTML-or-plain email body to a single-line plain-text snippet.

    Strips tags, unescapes entities, and collapses whitespace so the CRM
    interaction log never shows raw markup. send-email.py passes the raw HTML
    body (one line, no newlines), so the previous `split("\\n")[0]` approach
    captured raw `<p>` tags.

    Both `<`-matching rules require a tag NAME after the bracket, because a
    less-than sign is ordinary prose and this snippet is what the operator's
    interaction log records as the thing that was discussed. They used to be
    `<[^>]+>` and `<[^>]*$`, which treat any `<` as the start of markup.
    MEASURED 2026-09-01, before the fix:

        'Budget is < 50k'      -> 'Budget is'
        'temp < 0 degrees'     -> 'temp'
        'Q3 revenue < target'  -> 'Q3 revenue'
        'x < y and y > z'      -> 'x z'

    The dangling rule was the worse half: it deletes every character from a
    trailing `<` to the end of the string, so the loss is unbounded and silent,
    and the docstring's promise that this is safe on plain text was false for
    the one character the function is built around. Real markup always opens
    with a letter, a `/`, or a `!` (comment, doctype), so requiring one keeps
    every HTML case and gives prose back. A body cut off mid-tag still loses the
    fragment, including a bare trailing `<`.
    """
    if not body:
        return ""
    # Block-level close tags and <br> become spaces so words don't run together.
    s = re.sub(r"(?is)<\s*br\s*/?\s*>", " ", body)
    s = re.sub(r"(?is)</\s*(p|div|li|tr|h[1-6]|ul|ol|table)\s*>", " ", s)
    s = re.sub(r"(?is)</?[A-Za-z!][^>]*>", "", s)  # strip all remaining tags
    # Drop a dangling unclosed tag (truncated body): "<", "<p", "</di".
    s = re.sub(r"<(?:/?[A-Za-z!][^>]*)?$", "", s)
    s = _html.unescape(s)
    s = " ".join(s.split())  # collapse whitespace runs
    return s[:limit]


def bump_last_touch_in_text(text: str, new_date: str) -> str:
    """Set `last_touch: YYYY-MM-DD` in YAML frontmatter, or leave the text alone.

    Both halves now run inside the block, through
    `scripts.utils.markdown.set_frontmatter_field`. The INSERT half was scoped
    correctly and the docstring said so; the REPLACE half was not, and the
    docstring's claim that the write "lands inside the frontmatter block or
    nowhere" was false for it.

    MEASURED 2026-08-29. A card with no `last_touch` in frontmatter and a body
    line beginning `last_touch:` -- a quoted record in an interaction-log entry
    is enough -- took the replace branch, rewrote the BODY line and returned. The
    frontmatter field the whole auto-log exists to maintain stayed absent, the
    audit entry recorded `matched: true`, and `calculate_health` read the contact
    as never touched, permanently. A document with no frontmatter at all had the
    key written into its prose, which is the exact outcome the old docstring
    claimed to have fixed.
    """
    return set_frontmatter_field(text, "last_touch", new_date)


def append_log_entry(text: str, date: str, kind: str, subject: str, body: str) -> str:
    """Insert a new log entry at the top of the Interaction Log section."""
    entry = f"\n### {date} | {kind} | {subject}\n{body.strip()}\n"
    marker = "## Interaction Log"
    if marker not in text:
        # Append the section if missing
        return text.rstrip() + f"\n\n{marker}\n{entry}"
    head, _, tail = text.partition(marker)
    return head + marker + entry + tail


def log_outbound(
    recipient_email: str,
    subject: str,
    body_excerpt: str,
    date: Optional[str] = None,
    workspace_root: Optional[Path] = None,
) -> bool:
    """Log an outbound email to the matching relationship record.

    Returns True if a log entry was written, False if no match (or conflict),
    and False on a read-only host (`data_is_readonly`). Writes a JSONL audit
    entry to runtime-junk/sync-exchange/logs/crm-autolog-{date}.jsonl on every invocation regardless
    of match outcome.
    """
    target = (recipient_email or "").strip().lower()
    rel_path = resolve_recipient(recipient_email, workspace_root=workspace_root)
    if rel_path is None:
        _audit_log({
            "kind": "outbound",
            "email": target,
            "matched": False,
        }, workspace_root=workspace_root)
        return False
    if data_is_readonly():
        # Checked HERE, after the resolve and before the read-modify-write: it
        # is the narrowest point that still names the record, and the no-match
        # branch above is untouched because it never wrote anything anyway.
        #
        # Announced, never silent. This line is what reaches the daemon journal
        # on the mirror host, and it is the operator's only signal that an
        # interaction went unfiled. A skip nobody can see is how five contact
        # cards got rewritten without anyone noticing for three days.
        print(f"crm-autolog: DATA_READONLY, not logging to {rel_path.stem} "
              f"(to={target}, subject={subject or '(no subject)'})",
              file=sys.stderr)
        # The audit entry still goes down. Its store, `runtime:sync-exchange/logs`,
        # is gitignored in the DATA repo, so neither the JSONL nor the `mkdir`
        # that creates its directory shows up in `git status`, and neither can
        # be what aborts the mirror's `git pull --ff-only`. Dropping it would
        # throw away the only durable record that the send happened at all.
        _audit_log({
            "kind": "outbound",
            "email": target,
            "matched": True,
            "slug": rel_path.stem,
            "subject": subject or "",
            "skipped": "data_readonly",
        }, workspace_root=workspace_root)
        return False
    date = date or datetime.now(get_default_tz()).strftime("%Y-%m-%d")
    text = rel_path.read_text(encoding="utf-8")
    text = bump_last_touch_in_text(text, date)
    snippet = plain_snippet(body_excerpt)
    text = append_log_entry(text, date, "Email", subject or "(no subject)", snippet)
    atomic_write(rel_path, text)
    _audit_log({
        "kind": "outbound",
        "email": target,
        "matched": True,
        "slug": rel_path.stem,
        "subject": subject or "",
    }, workspace_root=workspace_root)
    return True


def bump_inbound(
    sender_email: str,
    date: Optional[str] = None,
    workspace_root: Optional[Path] = None,
) -> bool:
    """Silently bump last_touch on the matching relationship record. No log entry.

    Returns True if the sender resolved to a known contact (regardless of
    whether the last_touch value actually changed — duplicate inbound on the
    same day is a no-op write but still a match). Returns False on no match
    or multi-match conflict, and False on a read-only host
    (`data_is_readonly`) even though the sender resolved, because the caller
    reads the return as "the touch is recorded" and on such a host it is not.
    Writes a JSONL audit entry on every invocation.
    """
    target = (sender_email or "").strip().lower()
    rel_path = resolve_recipient(sender_email, workspace_root=workspace_root)
    if rel_path is None:
        _audit_log({
            "kind": "inbound",
            "email": target,
            "matched": False,
        }, workspace_root=workspace_root)
        return False
    if data_is_readonly():
        # Same placement and the same reasoning as `log_outbound`; see there.
        print(f"crm-autolog: DATA_READONLY, not bumping last_touch on "
              f"{rel_path.stem} (from={target})", file=sys.stderr)
        _audit_log({
            "kind": "inbound",
            "email": target,
            "matched": True,
            "slug": rel_path.stem,
            "skipped": "data_readonly",
        }, workspace_root=workspace_root)
        return False
    date = date or datetime.now(get_default_tz()).strftime("%Y-%m-%d")
    text = rel_path.read_text(encoding="utf-8")
    new_text = bump_last_touch_in_text(text, date)
    if new_text != text:
        atomic_write(rel_path, new_text)
    _audit_log({
        "kind": "inbound",
        "email": target,
        "matched": True,
        "slug": rel_path.stem,
    }, workspace_root=workspace_root)
    return True
