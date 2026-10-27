#!/usr/bin/env python3
"""
Context Freshness Marker Manager for HEADING OS

Manages '> Last verified: YYYY-MM-DD' headers on context files.

Usage:
    python scripts/context-freshness.py check                       # show freshness of all context files
    python scripts/context-freshness.py stamp <context record>       # update timestamp to today
    python scripts/context-freshness.py touch <context record>       # alias of stamp
    python scripts/context-freshness.py stamp-all                    # stamp every record of the context-files group

A relative record path is resolved against the data root.

Coverage: none live. The 2026-09-21 test-suite split left this script with no
automatic check, so its behaviour rests on whoever edits it.
"""

import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.colors import GREEN, YELLOW, RED, BOLD, RESET
from scripts.utils.workspace import get_data_root, get_default_tz
from scripts.utils.data_homes import homes

def context_homes() -> tuple[Path, ...]:
    """Every directory the context records live in, from the `context-homes`
    registry group (HOS-240, 2026-10-13). `check` lists these directories and
    `stamp`/`touch` accept a file inside one of them."""
    return homes("context-homes")


def context_records() -> list[Path]:
    """The real context records: the `context-files` registry group, existing
    markdown files only. `stamp-all` stamps these and nothing else, because a
    context home also holds records that are not context (F-3.p3.2)."""
    return sorted(p for p in homes("context-files")
                  if p.suffix.lower() == ".md" and p.is_file())


def get_freshness(filepath):
    """Read the freshness marker from a file.

    Returns:
        (date_str, age_days)  a usable marker
        (date_str, None)      a marker shaped like a date that is not one
        (None, None)          no marker at all
    """
    # `errors="replace"`, for the same reason the `except ValueError` below
    # exists and one input class over. A bare decode raised UnicodeDecodeError
    # on a context file carrying one non-UTF-8 byte, which aborted the `check`
    # listing partway through and made every OTHER file's freshness
    # unreportable - the exact failure that handler was written to end.
    # MEASURED 2026-09-01 on a file whose marker was perfectly good and whose
    # BODY held a cp1252 byte: the function raised, so none of the three return
    # shapes this docstring promises could be delivered.
    #
    # Replacing is safe for what is being read: the marker is ASCII and is
    # anchored at position 0 by `re.match`, so a replaced byte can neither
    # invent a marker nor hide one that is there.
    content = filepath.read_text(encoding="utf-8", errors="replace")
    first_line = content.split("\n")[0] if content else ""
    match = re.match(r">\s*Last verified:\s*(\d{4}-\d{2}-\d{2})", first_line)
    if match:
        date_str = match.group(1)
        try:
            verified = date.fromisoformat(date_str)
        except ValueError:
            # The regex checks the SHAPE; only fromisoformat checks the value,
            # and `2026-13-40` matches the shape. These markers are hand-edited
            # headers, so a wrong month or day is an ordinary typo - and it used
            # to raise out of `get_freshness`, abort the `check` listing partway
            # through, and make every OTHER file's freshness unreportable. The
            # file is reported as carrying an unusable marker, which is true and
            # is what the operator needs to see.
            return date_str, None
        age_days = (datetime.now(get_default_tz()).date() - verified).days
        return date_str, age_days
    return None, None


def stamp_file(filepath, date_str=None):
    """Add or update the freshness marker on a file."""
    if date_str is None:
        date_str = datetime.now(get_default_tz()).strftime("%Y-%m-%d")

    content = filepath.read_text(encoding="utf-8")
    marker = f"> Last verified: {date_str}"

    # Check if first line is already a freshness marker
    lines = content.split("\n")
    if lines and re.match(r">\s*Last verified:", lines[0]):
        lines[0] = marker
    else:
        lines.insert(0, marker)

    filepath.write_text("\n".join(lines), encoding="utf-8")
    return date_str


def check_all():
    """Show freshness status of all context files."""
    homes = context_homes()
    context_files = sorted(f for h in homes for f in h.glob("*.md"))
    if not context_files:
        print(f"{RED}No context files found in "
              f"{', '.join(str(h) for h in homes)}{RESET}")
        return

    print(f"\n{BOLD}Context File Freshness{RESET}")
    # Every directory searched, named. One line saying "Directory: <one path>"
    # over a five-way fan-out would state coverage this listing does not have.
    for h in homes:
        print(f"Directory: {h}")
    print()

    for f in context_files:
        date_str, age_days = get_freshness(f)
        if date_str and age_days is None:
            # A marker that is shaped like a date and is not one. Named on its
            # own line rather than crashing the listing, and rather than being
            # folded into "No marker": there IS a marker, and the operator has
            # to go and fix it.
            print(f"  {RED}{'Bad':6s}{RESET}  {f.name:30s}  "
                  f"Unusable marker: {date_str}")
        elif date_str:
            if age_days <= 7:
                color = GREEN
                status = "Fresh"
            elif age_days <= 30:
                color = YELLOW
                status = "Aging"
            else:
                color = RED
                status = "Stale"
            print(f"  {color}{status:6s}{RESET}  {f.name:30s}  Verified: {date_str} ({age_days}d ago)")
        else:
            # Fall back to modification time
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=get_default_tz())
            age_days = (datetime.now(get_default_tz()) - mtime).days
            print(f"  {YELLOW}{'No marker':6s}{RESET}  {f.name:30s}  Modified: {mtime.strftime('%Y-%m-%d')} ({age_days}d ago)")


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python scripts/context-freshness.py check")
        print("  python scripts/context-freshness.py stamp <file>")
        print("  python scripts/context-freshness.py touch <file>   (alias of stamp)")
        print("  python scripts/context-freshness.py stamp-all")
        sys.exit(1)

    command = sys.argv[1]

    if command == "check":
        check_all()

    elif command in ("stamp", "touch"):
        if len(sys.argv) < 3:
            print(f"{RED}Error: specify a file path{RESET}")
            sys.exit(1)
        filepath = Path(sys.argv[2])
        if not filepath.is_absolute():
            filepath = get_data_root() / filepath
        # Resolved, then held to the context records: an unchecked path once
        # prepended a marker into any file it reached (2026-08-24).
        filepath = filepath.resolve()
        allowed = {r.resolve() for r in context_records()}
        in_home = any(filepath.parent == h.resolve() for h in context_homes())
        if filepath not in allowed and not in_home:
            print(f"{RED}Error: {filepath} is not a context record (the "
                  f"context-files group or a context-homes directory).{RESET}")
            sys.exit(1)
        if filepath.suffix.lower() != ".md":
            # `stamp_file` inserts a markdown blockquote at line 1. On anything
            # else that is not a stamp, it is corruption.
            print(f"{RED}Error: {filepath.name} is not a .md file; the marker "
                  f"is a markdown blockquote and would corrupt it.{RESET}")
            sys.exit(1)
        if not filepath.exists():
            print(f"{RED}Error: {filepath} not found{RESET}")
            sys.exit(1)
        date_str = stamp_file(filepath)
        print(f"{GREEN}Stamped{RESET} {filepath.name} with: > Last verified: {date_str}")

    elif command == "stamp-all":
        context_files = context_records()
        for f in context_files:
            date_str = stamp_file(f)
            print(f"{GREEN}Stamped{RESET} {f.name} with: > Last verified: {date_str}")
        print(f"\n{GREEN}All {len(context_files)} context files stamped.{RESET}")

    else:
        print(f"{RED}Unknown command: {command}{RESET}")
        print("Use: check, stamp, touch, or stamp-all")
        sys.exit(1)


if __name__ == "__main__":
    main()
