#!/usr/bin/env python3
"""
CRM Relationship Health Scorer for HEADING OS

Reads the `crm-contacts` records (resolve the directory with
`python scripts/data-path.py path crm-contacts`), calculates health scores based
on last_touch dates and expected cadence, and outputs a relationship radar.

Usage:
    python scripts/crm-health.py              # print health radar to terminal
    python scripts/crm-health.py --update     # also regenerate radar table in people.md
    python scripts/crm-health.py --json       # output as JSON (for programmatic use)
    python scripts/crm-health.py --roster     # print the full team roster instead
"""

import argparse
import datetime as _dt
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.atomic import atomic_write_text
from scripts.utils.colors import GREEN, YELLOW, RED, GRAY, BOLD, RESET
from scripts.utils.data_path import DataPathError, data_write_dir
from scripts.utils.workspace import (
    get_data_root,
    get_default_tz,
    make_run_clock,
)
from scripts.utils.tribe_roster import load_roster, roster_path
from scripts.utils.crm import (
    contacts_home,
    crm_config_path,
    people_path,
    parse_config,
    scan_contacts,
)

def contacts_dir() -> Path:
    """`crm-contacts`, resolved at call time (never at import, so a changed
    `HEADING_OS_DATA` is followed) through `scripts.utils.crm`, which answers
    for the whole CRM tree."""
    return contacts_home()


def config_file() -> Path:
    """`crm-config`, the cadence table `main()` reads first, so a wrong address
    here stops the whole health run (2026-09-16)."""
    return crm_config_path()


def people_file() -> Path:
    """`people`, the file `--update` rewrites the radar table into (a write target)."""
    return people_path()


# This run's single wall-clock reading, taken at first use rather than at
# import, and memoised: the date is compared and stamped in several places below, and one reading keeps a run that
# straddles midnight consistent. Why both: `make_run_clock` in
# scripts/utils/workspace.py.
_clock = make_run_clock()


def _today() -> _dt.date:
    """This run's local date. See `_clock`."""
    return _clock().date()


def format_terminal_report(contacts, tribe_warnings=None):
    """Format health report for terminal output."""
    red = [c for c in contacts if c["health"] == "red"]
    yellow = [c for c in contacts if c["health"] == "yellow"]
    green = [c for c in contacts if c["health"] == "green"]
    gray = [c for c in contacts if c["health"] == "gray"]

    lines = []
    lines.append(f"\n{BOLD}Relationship Radar{RESET}\n")

    # Team detection warnings (before health sections)
    if tribe_warnings:
        lines.append(f"{YELLOW}{BOLD}TEAM DETECTION WARNINGS{RESET}")
        for w in tribe_warnings:
            lines.append(
                f"  {YELLOW}{w['name']}{RESET} ({w['company']}) - type: {w['type']} - "
                f"has a corporate-domain email ({w['email']}) but is NOT typed as tribe/tribe-leadership"
            )
        lines.append("")

    if red:
        lines.append(f"{RED}{BOLD}RED - Overdue{RESET}")
        for c in red:
            days_str = f"{c['days_since']} days" if c["days_since"] is not None else "no recorded touch"
            lines.append(f"  {RED}{c['name']}{RESET} ({c['company']}) - {c['type']} - {days_str} (cadence: {c['cadence']})")
        lines.append("")

    if yellow:
        lines.append(f"{YELLOW}{BOLD}YELLOW - Approaching{RESET}")
        for c in yellow:
            lines.append(f"  {YELLOW}{c['name']}{RESET} ({c['company']}) - {c['type']} - {c['days_since']} days (cadence: {c['cadence']})")
        lines.append("")

    if green:
        lines.append(f"{GREEN}{BOLD}GREEN - On Track{RESET}")
        for c in green:
            lines.append(f"  {GREEN}{c['name']}{RESET} ({c['company']}) - {c['type']} - {c['days_since']} days (cadence: {c['cadence']})")
        lines.append("")

    if gray:
        lines.append(f"{GRAY}{BOLD}GRAY - No Cadence{RESET}")
        for c in gray:
            lines.append(f"  {GRAY}{c['name']}{RESET} ({c['company']}) - {c['type']}")
        lines.append("")

    # Upcoming commitments
    upcoming = []
    for c in contacts:
        for commit in c["commitments"]:
            if commit["due"] and commit["due"] <= _today() + timedelta(days=7):
                upcoming.append((c["name"], commit))

    if upcoming:
        lines.append(f"{BOLD}Active Commitments Due Soon:{RESET}")
        for name, commit in sorted(upcoming, key=lambda x: x[1]["due"] or _today()):
            due_str = commit["due"].strftime("%Y-%m-%d") if commit["due"] else "no date"
            overdue = " (OVERDUE)" if commit["due"] and commit["due"] < _today() else ""
            lines.append(f"  [ ] {name} - {commit['text']}{RED}{overdue}{RESET}")
        lines.append("")

    # Summary
    total = len(contacts)
    lines.append(f"{BOLD}Total:{RESET} {total} contacts tracked | {RED}{len(red)} red{RESET} | {YELLOW}{len(yellow)} yellow{RESET} | {GREEN}{len(green)} green{RESET}")
    lines.append("")

    return "\n".join(lines)


_FM_OPEN_RE = re.compile(r"\A---[ \t]*\r?\n")
# `\r?` on the close too. The OPEN fence spells out `\r?\n`, so this file
# already claims to read CRLF; the close did not, and on a CRLF file the
# fence line is `---\r`, which `[ \t]*$` cannot match (Python's `$` sits
# before `\n`, not before `\r`). frontmatter_end then returned -1 for every
# CRLF contact, and --demote-candidates reported "no frontmatter" about
# files whose frontmatter was fine. Two regexes, two line-ending policies.
_FM_CLOSE_RE = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)


def frontmatter_end(text: str) -> int:
    """Index where the CLOSING frontmatter fence begins, or -1 if there is none.

    Anchored, and requiring the document to OPEN with a fence. This was
    `text.find("---", 3)`, a plain substring search, and it failed twice over:

    * A frontmatter VALUE containing `---` (`notes: 2026-01-01---draft`) ended
      the slice early. If `status:` sat after that point the anchored guard
      found nothing, and the else-branch inserted `status: dormant` at
      `text.rfind("\\n", 0, fm_end)` — inside the frontmatter, splitting a value
      line and leaving malformed YAML.
    * A file with NO frontmatter but a `---` horizontal rule in the body sailed
      past the `fm_end == -1` guard. `text[:fm_end]` was then body text, and a
      `^status:` line in an interaction-log entry got rewritten — the exact
      body-rewrite the comment at the call site says the slicing prevents. The
      guard tested the wrong thing.
    """
    if not _FM_OPEN_RE.match(text):
        return -1
    match = _FM_CLOSE_RE.search(text, _FM_OPEN_RE.match(text).end())
    return -1 if match is None else match.start()


def _radar_insert_pos(content: str) -> int:
    """Where the radar table may be inserted without displacing line 1.

    Never 0. `context-freshness.py` reads ONLY the first line looking for
    `> Last verified:`, so anything written above it makes a stamped file
    report as unstamped, and the next `stamp` then adds a second marker.

    Order: after a closing frontmatter fence when the file opens with one;
    otherwise after the first line, whatever that line is.

    Uses `frontmatter_end`, whose docstring above explains why a substring
    search is wrong. This function kept the substring form and reproduced both
    halves of it: `startswith("---\n")` refused an opener of `--- ` or `---\r\n`,
    and `find("\n---", 3)` matched any line merely BEGINNING with `---` -- a
    `----` rule, or `--- draft` inside a block scalar. Both mistakes land in the
    same place. The fallback below is "after the first line", and on a file with
    frontmatter the first line IS the opening fence, so `--update` spliced the
    Contact Radar table INTO the YAML of the file it was writing.
    """
    fm_end = frontmatter_end(content)
    if fm_end != -1:
        line_end = content.find("\n", fm_end)
        return line_end + 1 if line_end != -1 else len(content)
    first_break = content.find("\n")
    return first_break + 1 if first_break != -1 else len(content)


def generate_radar_table(contacts):
    """Generate the Contact Radar markdown table for people.md."""
    lines = []
    lines.append("## Contact Radar")
    lines.append("")
    lines.append(f"> Auto-generated by crm-health.py on {_today().strftime('%Y-%m-%d')}")
    lines.append("")
    lines.append("| Name | Company | Type | Last Touch | Health | CRM File |")
    lines.append("|------|---------|------|-----------|--------|----------|")

    # Sort: red first, then yellow, then green, then gray
    order = {"red": 0, "yellow": 1, "green": 2, "gray": 3}
    sorted_contacts = sorted(contacts, key=lambda c: (order.get(c["health"], 4), c["name"]))
    # Relative from where the table is written to where the cards are, both by
    # registry name (HOS-234 part 8). A fixed `../crm/contacts/` was right only
    # in the pre-move layout, and the C3 move puts the people file beside the
    # cards' own directory.
    base = Path(os.path.relpath(contacts_dir(), people_file().parent)).as_posix()

    for c in sorted_contacts:
        health_icon = {"red": "RED", "yellow": "YELLOW", "green": "GREEN", "gray": "GRAY"}.get(c["health"], "?")
        last = c["last_touch"] if c["last_touch"] else "-"
        link = f"[crm]({base}/{c['file']})"
        lines.append(f"| {c['name']} | {c['company']} | {c['type']} | {last} | {health_icon} | {link} |")

    lines.append("")
    return "\n".join(lines)


def update_people_md(contacts):
    """Update the Contact Radar table in the `people` record."""
    if not people_file().exists():
        print(f"{RED}Error: {people_file()} not found{RESET}")
        return False

    content = people_file().read_text(encoding="utf-8")
    radar_table = generate_radar_table(contacts)

    # Remove the existing radar table. The old lookahead was
    # `(?=\n---|\n## [^C])`, which required the table to be followed by a rule
    # or by a `## ` heading whose first letter is NOT C — so it failed in two
    # ordinary cases: the table sitting at end of file, and the next section
    # being `## CRM Pipeline` or `## Contacts`. When the lookahead failed the
    # old table was simply left in place and a second `## Contact Radar` was
    # appended on every `--update`, stacking one more each run.
    #
    # Stop at the next `## ` heading of any letter, at a `---` rule, or at end
    # of input. `\Z` is what was missing.
    pattern = r"\n?## Contact Radar\n.*?(?=\n---|\n## |\Z)"
    content = re.sub(pattern, "", content, flags=re.DOTALL)

    # Insert after the frontmatter block. The comment here said "after the
    # header block (after first ---)" while the code found the SECOND `---`,
    # which for a file with frontmatter is the closing fence — right, but not
    # what the comment said. Worse was the else-branch: with fewer than two
    # `---` separators it put the table at byte 0, pushing the
    # `> Last verified:` marker off line 1. `context-freshness.py` reads only
    # the first line, so it then reported "No marker" for a file that has one,
    # and a later `stamp` inserted a SECOND marker above the table. Two scripts
    # in this workspace silently corrupted each other's invariant.
    insert_pos = _radar_insert_pos(content)
    content = content[:insert_pos] + "\n" + radar_table + "\n" + content[insert_pos:]

    atomic_write_text(people_file(), content)
    return True


def format_json(contacts):
    """Output contacts as JSON, including the fields /crm next ranker depends on."""
    output = []
    for c in contacts:
        # Compute days_overdue: how many days past the red threshold we are.
        # Negative or zero means within cadence; ranker filters to >0 for REDs.
        cadence = c.get("cadence", 0)
        days_since = c.get("days_since")
        if days_since is None or not cadence:
            days_overdue = 0
        else:
            days_overdue = max(0, int(days_since) - int(cadence))

        entry = {
            "name": c["name"],
            "company": c["company"],
            "email": c.get("email", ""),
            "type": c["type"],
            "stage": c.get("stage", ""),
            "last_touch": c["last_touch"],
            "cadence": cadence,
            "health": c["health"],
            "days_since": days_since,
            "days_overdue": days_overdue,
            "radar_freeze_until": c.get("radar_freeze_until", ""),
            "commitments": [
                {"text": cm["text"], "due": cm["due"].strftime("%Y-%m-%d") if cm["due"] else None}
                for cm in c["commitments"]
            ],
            "file": c["file"],
        }
        output.append(entry)
    return json.dumps(output, indent=2)


TRIBE_TYPES = {"tribe", "tribe-leadership"}


def _days_since_touch(last_touch, today):
    """Days between `last_touch` and `today`, or None when there is no date.

    Derived here rather than read from the contact's `days_since`, and the
    reason is a decision made elsewhere on purpose. `calculate_health` leaves
    `days_since` as None for every type in `NO_CADENCE_TYPES`, which includes
    both team types, because the operator's standing rule is that team
    members get no cadence alerts. That is correct for the RADAR and wrong for
    this listing: "no cadence is owed" is not "nobody knows when we last
    spoke". MEASURED 2026-09-20 before this helper existed, the roster printed
    `no CRM record` against all 19 people the CRM does track, every one of them
    carrying a real `last_touch`.
    """
    if not last_touch:
        return None
    try:
        # `date.fromisoformat`, not `datetime.strptime(...).date()`: a
        # `last_touch` is a calendar DAY with no time and no zone in it,
        # so building a naive datetime only to discard the time half
        # invents a midnight that nobody wrote down. Ruff DTZ007 flags
        # exactly that shape, and it is right to.
        touched = _dt.date.fromisoformat(str(last_touch).strip())
    except (ValueError, TypeError):
        return None
    return (today - touched).days


def merge_roster(contacts, roster, today=None):
    """Union the org roster with the CRM team contacts. Roster drives the list.

    Every roster person appears, carrying the org fields the workbook holds.
    A person who is ALSO a CRM team contact carries the CRM fields beside
    them; `days_since` is None for anyone the CRM does not know, which reads
    differently from zero and must keep reading differently.

    A CRM team contact absent from the workbook is appended rather than
    dropped: the workbook is a snapshot of an org chart and the CRM is what the
    operator actually maintains, so neither side is authoritative alone.

    Join on the lowercased email, falling back to the lowercased name, because
    the workbook carries an email for everyone and the CRM does not.
    """
    today = today or datetime.now(get_default_tz()).date()
    by_email, by_name = {}, {}
    for c in contacts:
        if str(c.get("type", "")).strip().lower() not in TRIBE_TYPES:
            continue
        email = str(c.get("email", "") or "").strip().lower()
        if email:
            by_email[email] = c
        by_name[str(c.get("name", "") or "").strip().lower()] = c

    merged, claimed = [], set()
    for person in roster:
        email = person.get("email", "")
        name_key = person.get("name", "").strip().lower()
        crm = by_email.get(email) or by_name.get(name_key)
        if crm is not None:
            claimed.add(id(crm))
        merged.append({
            "name": person.get("name", ""),
            "email": email,
            "title": person.get("title", ""),
            "department": person.get("department", ""),
            "reports_to": person.get("reports_to", ""),
            "telegram": person.get("telegram", ""),
            "in_roster": True,
            "in_crm": crm is not None,
            "type": (crm or {}).get("type", ""),
            "last_touch": (crm or {}).get("last_touch", ""),
            "days_since": _days_since_touch((crm or {}).get("last_touch"), today),
        })

    for c in contacts:
        if str(c.get("type", "")).strip().lower() not in TRIBE_TYPES:
            continue
        if id(c) in claimed:
            continue
        merged.append({
            "name": c.get("name", ""),
            "email": str(c.get("email", "") or "").strip().lower(),
            "title": "", "department": "", "reports_to": "", "telegram": "",
            "in_roster": False,
            "in_crm": True,
            "type": c.get("type", ""),
            "last_touch": c.get("last_touch", ""),
            "days_since": _days_since_touch(c.get("last_touch"), today),
        })

    # Longest-overlooked first, which is the order the team page uses. An
    # unknown `days_since` sorts LAST rather than first: "the CRM has never
    # heard of this person" is not the same claim as "nobody has spoken to
    # them in a very long time", and sorting None to the top would print the
    # second sentence while meaning the first.
    merged.sort(key=lambda m: (m["days_since"] is None,
                               -(m["days_since"] or 0),
                               m["name"].lower()))
    return merged


def format_roster_report(merged, roster_present):
    """Plain-text roster for the terminal. Returns the whole block as a string."""
    lines = [f"\n{BOLD}Team Roster{RESET}\n"]
    if not roster_present:
        lines.append(f"{YELLOW}Roster workbook not found; showing CRM team contacts only.{RESET}\n")
    in_crm = sum(1 for m in merged if m["in_crm"])
    lines.append(f"{len(merged)} people | {in_crm} tracked in CRM | "
                 f"{len(merged) - in_crm} in the roster only\n")
    for m in merged:
        if m["days_since"] is None:
            # Two different silences, told apart rather than merged. "The CRM
            # does not track this person" and "the CRM tracks them and holds no
            # touch date" are different facts about different fixes.
            touch = (f"{GRAY}no touch date{RESET}" if m["in_crm"]
                     else f"{GRAY}not in CRM{RESET}")
        elif m["days_since"] >= 90:
            touch = f"{RED}{m['days_since']}d{RESET}"
        elif m["days_since"] >= 30:
            touch = f"{YELLOW}{m['days_since']}d{RESET}"
        else:
            touch = f"{GREEN}{m['days_since']}d{RESET}"
        title = m["title"] or m["type"] or "-"
        dept = f" [{m['department']}]" if m["department"] else ""
        lines.append(f"  {m['name']:<34} {title}{dept} - {touch}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="CRM Relationship Health Scorer")
    parser.add_argument("--update", action="store_true", help="Regenerate radar table in people.md")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--roster", action="store_true",
                        help="Print the full team roster (org workbook merged with CRM) "
                             "instead of the health radar")
    parser.add_argument("--demote-candidates", action="store_true",
                        help="Surface dormancy candidates (90+ days silent prospects) for approval")
    parser.add_argument("--demote-threshold", type=int, default=90,
                        help="Days silent before a contact qualifies for demotion (default 90)")
    args = parser.parse_args()

    config = parse_config(config_file())
    contacts, tribe_warnings, dangling_refs, _stages, _aliases = scan_contacts(config)

    # Phase 2.4: log pipeline.md companies with no matching CRM contact.
    # Helps maintain `crm-aliases` - run crm-health.py to see stale entries.
    _contact_companies: set = set()
    for _c in contacts:
        _co = (_c.get("pipeline_company", "") or _c.get("company", "")).lower().strip()
        if _co:
            _contact_companies.add(_co)
            # If the contact's company normalises through an alias (e.g., contact has
            # `company: TT` and aliases.md maps tt -> exampletelco), we add the canonical
            # form. This ensures _contact_companies matches _stages.keys() which uses the
            # canonical pipeline.md company name.
            _contact_companies.add(_aliases.get(_co, _co))
    _unmatched = set(_stages.keys()) - _contact_companies
    if _unmatched:
        # In the data repository's `runtime:crm-health/logs`. It was the engine
        # checkout's `.sync/logs/` until 2026-09-25, when HOS-5 section 7 took
        # every artifact out of the checkout: company names are private data.
        try:
            _log_dir = data_write_dir("runtime:crm-health/logs")
        except DataPathError as _exc:
            # The SessionStart hook runs this; a tree with no data repository
            # keeps no log, and says so rather than failing the session.
            print(f"crm-health: unmatched-pipeline log not written ({_exc})",
                  file=sys.stderr)
        else:
            _log_path = _log_dir / f"crm-unmatched-pipeline-{_dt.datetime.now(get_default_tz()).strftime('%Y-%m-%d')}.log"
            _log_path.write_text(
                "Pipeline.md companies with no matching CRM contact:\n" +
                "\n".join(f"  - {c}" for c in sorted(_unmatched)) + "\n",
                encoding="utf-8",
            )

    if dangling_refs:
        # Route to stderr so `crm-health.py --json` produces a clean JSON
        # stream on stdout (crm_next.py and any other pipeline consumer
        # parses stdout). Terminal callers still see the warning - stderr
        # mixes into the same TTY by default.
        print(f"{YELLOW}WARN: {len(dangling_refs)} contact(s) reference missing address-book entities:{RESET}", file=sys.stderr)
        for dr in dangling_refs:
            print(f"  - {dr['file']} -> entity_ref {dr['entity_ref']}", file=sys.stderr)
        print("  Fix: create {slug}.md in `crm-address-book` (python scripts/data-path.py path crm-address-book) for each missing entity.", file=sys.stderr)

    if args.roster:
        # BEFORE the empty-contacts exit below, deliberately: the roster is a
        # separate source (scripts/utils/tribe_roster.py), so a workspace whose
        # CRM holds nothing can still answer "who is in the team". The radar
        # sees only contacts typed `tribe`, a subset of the roster (2026-09-20).
        data_root = get_data_root()
        roster = load_roster(data_root)
        merged = merge_roster(contacts, roster)
        if args.json:
            print(json.dumps(merged, indent=2, default=str))
        else:
            print(format_roster_report(merged, roster_path(data_root).exists()))
        # "The workbook is missing" and "the workbook is empty" are different
        # facts, and only the first is worth an exit code: a caller that gets 0
        # here has been handed the real list, however short.
        sys.exit(0 if roster or merged else 1)

    if not contacts:
        # An empty CRM is still an answer, and under --json the answer is `[]`,
        # never prose on stdout, which every JSON consumer would fail to parse.
        if args.json:
            print("[]")
        else:
            print(f"{YELLOW}No contact files found in {contacts_dir()}{RESET}")
        print("Add contacts with: /crm add [name] [company] [type]",
              file=sys.stderr)
        sys.exit(0)

    if args.json:
        print(format_json(contacts))
    else:
        print(format_terminal_report(contacts, tribe_warnings))

    if args.update:
        # stderr, for the same reason the dangling-ref warning above is on
        # stderr: `--json --update` printed these lines AFTER the JSON document
        # on the same stream, so `crm-health.py --json --update | jq .` failed
        # to parse. The comment twenty lines up already knew stdout must stay
        # clean for crm_next.py; the flags were simply never made exclusive.
        if update_people_md(contacts):
            print(f"{GREEN}Radar table updated in {people_file().name}{RESET}",
                  file=sys.stderr)
        else:
            print(f"{RED}Failed to update {people_file().name}{RESET}",
                  file=sys.stderr)

    if args.demote_candidates:
        from scripts.utils.crm import find_dormancy_candidates
        from scripts.utils.crm_autolog import atomic_write
        # The third branch that emits non-JSON on the shared stream, and the
        # last one still doing it. MEASURED 2026-08-29 against the live CRM:
        # `--json --demote-candidates` printed a valid 169-record JSON document
        # and then appended 5 038 bytes of candidate prose, the confirmation
        # question and the `> ` prompt after it, so `| jq .` died on "Extra
        # data" - the same failure the dangling-ref warning and the `--update`
        # status line were each moved off stdout to prevent, with the comments
        # twenty and forty lines up saying so. Prose that a JSON consumer must
        # not see goes to stderr, which a terminal caller still reads on the
        # same TTY; without --json stdout stays the primary stream.
        out = sys.stderr if args.json else sys.stdout
        candidates = find_dormancy_candidates(contacts, today=datetime.now(get_default_tz()).date(), threshold_days=args.demote_threshold)
        if not candidates:
            print(f"{GREEN}No dormancy candidates - all active contacts within {args.demote_threshold}d.{RESET}", file=out)
        else:
            print(f"\n{YELLOW}{BOLD}DORMANCY CANDIDATES ({len(candidates)} contacts {args.demote_threshold}+ days silent):{RESET}", file=out)
            for c in candidates:
                name = c.get("name", c.get("slug", c["file"]))
                ctype = c.get("type", "?")
                lt = c.get("last_touch", "?")
                days = c.get("days_silent", "?")
                print(f"  {c['file']}: {name} ({ctype}) - last touch {lt} ({days} days)", file=out)
            print(f"\nTo demote: set `status: dormant` in each contact file.", file=out)
            print(f"Confirm demote all {len(candidates)} contacts to dormant? [yes/no]", file=out)
            try:
                # The prompt string of `input()` goes to stdout unconditionally,
                # so it too has to be written by hand to the chosen stream.
                print("> ", end="", flush=True, file=out)
                resp = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.", file=out)
                return
            if resp == "yes":
                demoted = 0
                for c in candidates:
                    path = contacts_dir() / c["file"]
                    text = path.read_text(encoding="utf-8")
                    # Scope the status:-check to the frontmatter slice (text before
                    # the closing ---). Whole-file check would match body content
                    # (e.g., a `status:` literal in an interaction log entry).
                    fm_end = frontmatter_end(text)
                    if fm_end == -1:
                        # No frontmatter — skip silently. Should not happen for valid
                        # relationship records but defensive.
                        # `c.get("slug", ...)`, matching the defensive chain
                        # thirty lines up that already treats slug as optional.
                        # A hard index here crashed mid-demote, AFTER some
                        # contacts had been rewritten and with no rollback.
                        label = c.get("slug", c["file"])
                        print(f"  {YELLOW}[skipped]{RESET} {label}: no frontmatter", file=out)
                        continue
                    frontmatter = text[:fm_end]
                    # Anchored, like the replacement it guards. `"status:" in
                    # frontmatter` is a substring test, so frontmatter carrying
                    # `notes: status: pending` and no top-level `status:` passed
                    # the guard, the anchored re.sub replaced nothing, the file
                    # was rewritten byte-identical, and the script printed
                    # [demoted] over a no-op.
                    if re.search(r"^status:", frontmatter, re.MULTILINE):
                        new_frontmatter = re.sub(r"^status:.*$", "status: dormant", frontmatter, count=1, flags=re.MULTILINE)
                        text = new_frontmatter + text[fm_end:]
                    else:
                        # Insert status field before closing --- of frontmatter
                        insert_at = text.rfind("\n", 0, fm_end)
                        text = text[:insert_at] + "\nstatus: dormant" + text[insert_at:]
                    atomic_write(path, text)
                    demoted += 1
                    print(f"  {GREEN}[demoted]{RESET} {c['file']}", file=out)
                # What was WRITTEN, not what was offered. This printed
                # `len(candidates)`, and the loop above can `continue` past a
                # file with no frontmatter, so the confirmation line overstated
                # a human-approved bulk mutation by the number it had skipped.
                skipped = len(candidates) - demoted
                print(f"{GREEN}{demoted} contacts demoted to dormant.{RESET}", file=out)
                if skipped:
                    print(f"{YELLOW}{skipped} skipped (see above).{RESET}", file=out)
            else:
                print("No changes made.", file=out)


if __name__ == "__main__":
    main()
