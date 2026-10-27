#!/usr/bin/env python3
"""crm_next.py -- Daily top-3 follow-up queue with drafts ready for approval.

Reads crm-health.py --json output, ranks RED contacts by pipeline stage tier
then days overdue, generates a checking-in email draft per candidate, saves
the queue as next-YYYY-MM-DD.md under data_path('crm-operations') for approval.
Drafts are reviewed and sent by hand, one at a time, through send-email.py.

Usage:
  python3 scripts/crm_next.py             # generate today's queue
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.data_path import data_path
from scripts.utils.workspace import get_workspace_root, get_default_tz
from scripts.utils.colors import GREEN, RED, RESET
from scripts.utils.atomic import atomic_write_text
from scripts.utils.crm import contacts_home, is_radar_frozen
from scripts.utils.venv_guard import venv_python


def out_dir() -> Path:
    """Where the daily `next-<date>.md` is written: the registry name
    `crm-operations`, resolved at call time. A tree whose registry lacks the
    name raises `UnknownDataPath` rather than writing to a guessed address."""
    return data_path('crm-operations')


_ENTRY_RE = re.compile(r"^(?:###\s+|-\s+)(\d{4}-\d{2}-\d{2}\b.*)$", re.MULTILINE)


STAGE_TIER = {
    "Negotiation": 1,
    "Proposal": 2,
    "Demo": 3,
    "Demo/POC": 3,    # accept both spellings
    "Qualified": 4,
    "Lead": 5,
    "": 6,
}


def _overdue_days(value) -> int:
    """`days_overdue` as a sortable int, whatever the health output carried.

    `int(c.get("days_overdue", 0))` defaults only when the KEY IS ABSENT, so an
    explicit `None`, `""` or `"unknown"` raised inside the sort key and no
    queue was generated at all. A missing number is not a reason to produce no
    follow-ups; it sorts last.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _days_since(contact: dict) -> int | None:
    """Days since the last exchange, READ from the record, not rebuilt from it.

    `crm-health.py --json` already carries `days_since`. The draft used to
    reconstruct it as `days_overdue + (cadence or 14)`, and the `or 14` is where
    a number appeared out of nowhere: a contact with no cadence has
    `days_overdue: 0`, so a record saying 40 days of silence produced a draft
    saying "it's been 14 days since our last exchange" - a figure no data
    supported, in text addressed to a real person.

    None means the record does not say. The caller drops the clause rather than
    guess, per `.claude/rules/voss.md` on precise numbers and
    `.claude/rules/scope-claims.md` on stating only what the method established.
    """
    direct = _overdue_days_or_none(contact.get("days_since"))
    if direct is not None and direct >= 0:
        return direct
    # Fall back to the reconstruction, but only when cadence is a real positive
    # number - which is the one case where `days_overdue + cadence` is exactly
    # what `crm-health` computed `days_since` to be.
    cadence = _overdue_days_or_none(contact.get("cadence"))
    overdue = _overdue_days_or_none(contact.get("days_overdue"))
    if cadence is None or overdue is None or cadence <= 0 or overdue < 0:
        return None
    return cadence + overdue


def _overdue_days_or_none(value) -> int | None:
    """`value` as an int, or None when it is not a number at all.

    Distinct from `_overdue_days`, which answers 0 for an unusable value because
    a sort key needs SOME number. A sentence stating a figure needs the opposite:
    the absence has to survive so the sentence can be dropped.
    """
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def rank_candidates(contacts: list, top_n: int = 3, today=None) -> list:
    """Rank RED contacts by (stage_tier, -days_overdue). Filters frozen contacts and non-REDs."""
    if today is None:
        today_date = datetime.now(get_default_tz()).date()
    else:
        today_date = date.fromisoformat(today)
    filtered = []
    for c in contacts:
        if c.get("health") != "red":
            continue
        # `is_radar_frozen`, not a local parse. This was the third private copy
        # of one suppression control, and like the other two it swallowed a
        # parse failure into `pass` — so an unparseable `radar_freeze_until`
        # left a contact the operator had explicitly frozen sitting in the
        # outreach queue. The shared helper fails closed and says so.
        if is_radar_frozen(c.get("radar_freeze_until"), today_date):
            continue
        filtered.append(c)
    filtered.sort(key=lambda c: (
        STAGE_TIER.get(c.get("stage", ""), 6),
        -_overdue_days(c.get("days_overdue", 0)),
    ))
    return filtered[:top_n]


def _fenced(body: str) -> list[str]:
    """`body` inside a code fence long enough that the body cannot end it.

    Both fenced blocks in the queue carry text this script does not control: an
    interaction-log excerpt read from a relationship record, and a draft
    containing the contact's own name. A triple backtick anywhere in either one
    closed the fence early, and everything after it was rendered as queue
    markdown — arbitrary headings and links injected into the file the operator
    reads to approve outreach. CommonMark allows any run of three or more, so
    one longer than the longest run inside cannot be closed by the content.
    """
    longest = 0
    run = 0
    for ch in body:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return [fence, body, fence]


def _contact_path(contact: dict) -> Path | None:
    """The contact's record, or None when the name does not resolve inside it.

    `contacts_home() / c["file"]` would trust the health output twice over.
    A `file` value that is ABSOLUTE discards the base entirely under pathlib
    (`Path("/a") / "/etc/passwd"` is `/etc/passwd`), and `../../x` walks out of
    the tree — after which `last_interaction_excerpt` reads it and copies what
    it finds into the queue the operator reviews. A missing `file` key was a
    bare KeyError. Neither is exploitable from outside this machine, but an
    arbitrary-read primitive pointed at a review artifact is worth closing.

    The base is `crm.contacts_home()`, the one address that holds the cards:
    `base` is what the `relative_to` below allows reads INSIDE, so a wrong base
    would refuse every real contact file as a traversal attempt.
    """
    name = contact.get("file")
    if not name:
        print(f"crm-next: contact {contact.get('name', '?')!r} has no `file`; "
              f"no interaction excerpt.", file=sys.stderr)
        return None
    base = contacts_home().resolve()
    candidate = (base / str(name)).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        print(f"crm-next: refusing contact file {name!r} — it resolves outside "
              f"{base}.", file=sys.stderr)
        return None
    return candidate


def last_interaction_excerpt(contact_file_path: Path) -> str:
    """Read the most recent Interaction Log entry from a relationship record.

    Supports both heading-style (`### YYYY-MM-DD | ...`) and bullet-style
    (`- YYYY-MM-DD | ...`) entry formats. Returns the matched entry plus up
    to 3 following lines, capped at 4 lines total. Falls back to
    "(no prior interaction)" when no entry is found.
    """
    if not contact_file_path.exists():
        return "(no prior interaction)"
    try:
        text = contact_file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # A record this cannot read or decode has no readable entry; one
        # corrupt card must cost one excerpt, not the whole queue. Named on
        # stderr so the operator can tell WHICH record produced it.
        print(f"crm_next: could not read {contact_file_path}: {exc}",
              file=sys.stderr)
        return "(no prior interaction)"
    if "## Interaction Log" not in text:
        return "(no prior interaction)"
    log = text.split("## Interaction Log", 1)[1]
    m = _ENTRY_RE.search(log)
    if not m:
        return "(no prior interaction)"
    start = m.start()
    rest = log[m.end():]
    next_m = _ENTRY_RE.search(rest)
    end = m.end() + (next_m.start() if next_m else len(rest))
    entry = log[start:end]
    lines = entry.strip().split("\n")[:4]
    return "\n".join(lines).strip()


def render_draft(contact: dict, last_excerpt: str) -> str:
    """Render a checking-in email draft using the /follow-up template shape.

    No manual sign-off in the body - the branded auto-signature (loaded by
    send-email.py from the registry name `email-signature-html`) carries the
    sender's name and title. A sign-off here would double with that block.
    """
    name = contact.get("name", "there")
    days_since = _days_since(contact)
    subject = "Quick check-in"

    # `name.split()[0] if name else ...` treats "   " as truthy, and
    # `"   ".split()` is `[]`, so a whitespace-only name raised IndexError. The
    # None and empty cases were handled; this one was not.
    parts = str(name or "").split()
    first_name = parts[0] if parts else "there"

    opening = ("Wanted to check back in."
               if days_since is None else
               f"Wanted to check back in - it's been {days_since} days "
               "since our last exchange.")
    body_lines = [
        f"Hey {first_name},",
        "",
        opening,
        "",
    ]
    # Only include the "most recent thread" block if we have real context
    has_prior = last_excerpt and not last_excerpt.startswith("(no prior")
    if has_prior:
        first_line = last_excerpt.split("\n")[0]
        body_lines.extend([
            "Most recent thread on my end:",
            f"> {first_line}",
            "",
        ])
    body_lines.append(
        "What's the right next step from here? Happy to push the conversation forward whenever the timing works."
    )
    # No sign-off in the body: the branded auto-signature carries the name +
    # title. Adding "Best, <Name>" here would double with the signature.
    return f"Subject: {subject}\n\n" + "\n".join(body_lines)


def generate_queue(today=None) -> Path:
    """Run crm-health.py --json, rank, generate drafts, save queue file."""
    ws = get_workspace_root()
    health_json = subprocess.run(
        [str(venv_python()), "scripts/crm-health.py", "--json"],
        capture_output=True, text=True, cwd=str(ws),
    )
    if health_json.returncode != 0:
        print(f"{RED}crm-health.py --json failed:{RESET}\n{health_json.stderr}", file=sys.stderr)
        sys.exit(1)
    # A zero exit does not promise parseable JSON, and only the exit code was
    # checked: malformed stdout raised JSONDecodeError and the daily job died
    # on a traceback. A dict instead of a list was worse — `rank_candidates`
    # iterated its KEYS and failed later, further from the cause.
    try:
        contacts = json.loads(health_json.stdout)
    except ValueError as exc:
        print(f"{RED}crm-health.py --json exited 0 but its output is not JSON "
              f"({exc}).{RESET}\nFirst 200 bytes: {health_json.stdout[:200]!r}",
              file=sys.stderr)
        sys.exit(1)
    if not isinstance(contacts, list):
        print(f"{RED}crm-health.py --json returned a "
              f"{type(contacts).__name__}, not a list of contacts.{RESET}",
              file=sys.stderr)
        sys.exit(1)

    candidates = rank_candidates(contacts, top_n=3, today=today)

    today_str = today or datetime.now(get_default_tz()).date().isoformat()
    target = out_dir()
    target.mkdir(parents=True, exist_ok=True)
    out_file = target / f"next-{today_str}.md"

    lines = [
        f"# CRM Next - {today_str}",
        "",
        f"Top {len(candidates)} priority follow-ups, drafts ready for manual review.",
        "",
        "**Note:** this file is regenerated on every `/crm next` invocation. If you edit it, the edits will be lost on the next run. Copy any edited drafts to a separate location before re-running.",
        "",
        "**Workflow:** read the drafts below, copy the body of any you want to send, and run:",
        "",
        "```bash",
        "python3 scripts/send-email.py --to <recipient> --subject \"<subject>\" --body-stdin <<'BODY'",
        "<paste the draft body here>",
        "BODY",
        "```",
        "",
        "`--body-stdin`, not `--body`: an argv element is visible to every local "
        "account through `ps` for the life of the send, and outreach text is not "
        "something to leave in shell history either.",
        "",
        "Auto-log fires on the send (Phase 1), so last_touch + interaction log update without further action.",
        "",
    ]

    for i, c in enumerate(candidates, start=1):
        contact_file = _contact_path(c)
        last_excerpt = (last_interaction_excerpt(contact_file)
                        if contact_file else "(no prior interaction)")
        draft = render_draft(c, last_excerpt)
        lines.append(f"## {i}. {c.get('name')} - {c.get('company', '')}")
        lines.append("")
        lines.append(f"- Stage: **{c.get('stage', '(no pipeline link)')}**")
        lines.append(f"- Days overdue: {c.get('days_overdue', '?')}")
        lines.append(f"- Last touch: {c.get('last_touch', '?')}")
        lines.append(f"- Email: `{c.get('email', '(missing)')}`")
        lines.append("")
        lines.append("### Most recent interaction")
        lines.extend(_fenced(last_excerpt))
        lines.append("")
        lines.append("### Draft")
        lines.extend(_fenced(draft))
        lines.append("")

    # Atomic. The path is deterministic (`next-<today>.md`), so two runs on the
    # same day, or one run while the operator has the file open, raced on a
    # plain write_text: a reader could see a truncated approval queue, and two
    # writers could interleave.
    atomic_write_text(out_file, "\n".join(lines) + "\n")
    return out_file


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.parse_args()

    out_file = generate_queue()
    print(f"{GREEN}Queue written: {out_file}{RESET}")
    print(f"Review the file and reply with approve/revise/skip commands.")


if __name__ == "__main__":
    main()
