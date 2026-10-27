#!/usr/bin/env python3
"""
Knowledge Base Health Engine for HEADING OS

Scans the `/zk`-schema note directories under the knowledge root, validates
frontmatter schema, reports note counts by type and status, flags stale seeds,
orphan notes, and keyword frequency.

Scope, stated because it used to be over-claimed. This engine reads the
IMMEDIATE child directories of the knowledge root, minus the trees that carry a
different schema and have their own reader (`OTHER_SCHEMA_DIRS` below). It
reports every `.md` it did not read, so a directory it does not understand is
loud rather than silent.

The scan boundary is derived from disk, never a hand-written directory list:
a list that misspelled one directory hid its notes from every count
(MEASURED 2026-08-28).

Usage:
    python scripts/knowledge-health.py           # terminal output (color-coded)
    python scripts/knowledge-health.py --json    # JSON for programmatic use
"""

import argparse
import io
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

# Force UTF-8 stdout when the locale is not UTF-8 (a bare POSIX locale under a
# timer, say): note titles carry non-ASCII text and printing them must not crash.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.atomic import atomic_write_text
from scripts.utils.colors import GREEN, YELLOW, RED, CYAN, GRAY, BOLD, RESET
from scripts.utils.workspace import display_path, get_knowledge_dir, make_run_clock
from scripts.utils.markdown import frontmatter_date, frontmatter_list
from scripts.utils.markdown import parse_frontmatter as _parse_frontmatter
from scripts.utils.data_homes import claimed_children


def knowledge_root() -> Path:
    """Resolved at call time, never at import: `get_knowledge_dir()` reads
    HEADING_OS_DATA on every call, and a module-level constant asked once
    during its own import and stored the answer, so a test that imported this
    module and then repointed the root still scanned the operator's real
    overlay.

    Named `knowledge_root` and not `knowledge_dir` because three functions
    below already take a `knowledge_dir` parameter.
    """
    return get_knowledge_dir()


def index_file() -> Path:
    return knowledge_root() / "INDEX.md"


# This run's single wall-clock reading, taken at first use rather than at
# import, and memoised: the date is both compared against note ages and stamped into the report below, and one reading keeps a run that
# straddles midnight consistent. Why both: `make_run_clock` in
# scripts/utils/workspace.py.
_clock = make_run_clock()


def _today() -> date:
    """This run's local date. See `_clock`."""
    return _clock().date()

VALID_TYPES = {"fleeting", "signal", "decision", "meeting", "research", "strategy", "people", "technology"}
VALID_STATUSES = {"seed", "growing", "evergreen", "archived"}
VALID_CONFIDENCES = {"high", "medium", "low", "unverified"}
REQUIRED_FIELDS = {"id", "title", "type", "keywords", "status", "created"}
STALE_SEED_DAYS = 7

# Immediate children of the knowledge root that are NOT `/zk`-schema notes.
# Each has its own reader, so folding them into this scan would double-count or
# report false schema violations:
#   odin-brain/  a different required-field list per subdir; scripts/odin-brain-health.py
#   shared/      not `/zk`-schema notes; its reader left with HOS-130 (2026-09-25).
OTHER_SCHEMA_DIRS = {"odin-brain", "shared"}


def other_schema_dirs(knowledge_dir=None):
    """`OTHER_SCHEMA_DIRS` plus every child a registry name places.

    A child of the knowledge root that another registry name owns is that
    name's records, never `/zk` notes: the research surveys
    (`research-surveys`), the intelligence reports (`intelligence`) and the
    book library (`book-library`). Asked of the registry on every call, so a
    repoint needs no edit here.
    """
    root = knowledge_root() if knowledge_dir is None else knowledge_dir
    return OTHER_SCHEMA_DIRS | claimed_children(root, ignore=("knowledge-base",))

# Display ORDER ONLY, no longer the scan boundary. Directories found on disk that
# are absent from this list are scanned and sort after it, alphabetically.
#
# The list keeps its historical plural spellings on purpose. `VALID_TYPES` above
# is singular (`signal`, `decision`, `meeting`) and matches the note TYPE that
# `.claude/skills/zk/references/subcommands.md` defines, so the two never agreed;
# the live directory is `signal`. That disagreement is what hid a note while this
# list was the boundary. Renaming entries here would assert a directory
# vocabulary this engine does not own, and it no longer needs to: a name in
# neither the list nor the tree costs nothing, and a directory in the tree is
# read whatever it is called.
SUBDIR_ORDER = ["fleeting", "signals", "decisions", "meetings", "research", "strategy", "people", "technology"]


def parse_frontmatter(content):
    """Parse YAML frontmatter from a note file.

    Thin wrapper around :func:`scripts.utils.markdown.parse_frontmatter` that
    drops the body and preserves the historical return shape (dict only).
    Native YAML types are preserved when PyYAML is available.
    """
    fm, _body = _parse_frontmatter(content)
    return fm


def extract_links(content):
    """Extract [[wiki-links]] from note content."""
    return re.findall(r"\[\[(\d{14})(?:\|[^\]]+)?\]\]", content)


def note_subdirs(knowledge_dir=None):
    """The `/zk`-note directories to scan, DERIVED FROM DISK.

    Every immediate child directory of the knowledge root except
    `other_schema_dirs()` and dotted directories. Ordered by `SUBDIR_ORDER` first,
    then alphabetically, so the generated INDEX.md keeps a stable shape while a
    directory nobody predicted still gets read (never a hand-written list).
    """
    root = knowledge_root() if knowledge_dir is None else knowledge_dir
    if not root.exists():
        return []
    other = other_schema_dirs(root)
    found = sorted(
        p.name for p in root.iterdir()
        if p.is_dir() and p.name not in other and not p.name.startswith(".")
    )
    known = [n for n in SUBDIR_ORDER if n in found]
    return known + [n for n in found if n not in SUBDIR_ORDER]


def scanned_note_files(knowledge_dir=None):
    """Exactly the files `scan_notes` opens: one `*.md` glob per note subdir.

    `scan_notes` iterates THIS, and `unread_note_files` reconciles against it, so
    the read set has one definition. A second derivation of the same walk is a
    second thing to keep in step, and the pair would drift the way every other
    duplicated rule in this repo has.
    """
    root = knowledge_root() if knowledge_dir is None else knowledge_dir
    files = []
    for subdir in note_subdirs(root):
        dir_path = root / subdir
        if not dir_path.exists():
            continue
        files.extend((subdir, p) for p in sorted(dir_path.glob("*.md")))
    return files


def unread_note_files(knowledge_dir=None):
    """Every `.md` under the knowledge root that `scan_notes` did not read.

    The point is obligation 2 of `.claude/rules/scope-claims.md`: name what you
    left out, because silence about an exclusion reads as coverage. Deriving the
    scan set from disk fixes today's mismatch; this is what makes the NEXT one
    loud instead of silent.

    Three exclusions, each because another reader owns the file:
      * a `.md` at the root itself (INDEX.md and friends) is not a note;
      * `odin-brain/**` is scripts/odin-brain-health.py's corpus;
      * `shared/**` is the retired corporate tier (see OTHER_SCHEMA_DIRS);
      * a child another registry name owns (see `other_schema_dirs`).
    Anything else -- a note one directory deeper than this engine looks, a
    directory added after the reader was written -- is reported by name.
    """
    root = knowledge_root() if knowledge_dir is None else knowledge_dir
    if not root.exists():
        return []
    seen = {str(p) for _subdir, p in scanned_note_files(root)}
    other = other_schema_dirs(root)
    missed = []
    for path in sorted(root.rglob("*.md")):
        if str(path) in seen:
            continue
        rel = path.relative_to(root)
        if len(rel.parts) == 1:
            continue
        if rel.parts[0] in other:
            continue
        missed.append(path)
    return missed


def scan_notes():
    """Scan the `/zk`-schema note directories under the knowledge root.

    Iterates `scanned_note_files`, which `unread_note_files` reconciles against,
    so the read set has exactly one definition.
    """
    notes = []

    for subdir, file_path in scanned_note_files():
        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # Never drop a note silently, never end the scan on one bad byte;
            # `odin-brain-health.py` handles this read the same way.
            print(f"{YELLOW}warn:{RESET} {display_path(file_path)} could not be "
                  f"read ({type(exc).__name__}: {exc}); it is absent from every "
                  f"count in this report.", file=sys.stderr)
            continue
        fm = parse_frontmatter(content)
        links = extract_links(content)

        issues = []
        # Validate required fields
        missing = REQUIRED_FIELDS - set(fm.keys())
        if missing:
            issues.append(f"missing fields: {', '.join(sorted(missing))}")

        # Validate type
        note_type = fm.get("type", "")
        if note_type and note_type not in VALID_TYPES:
            issues.append(f"invalid type: {note_type}")

        # Validate status
        status = fm.get("status", "")
        if status and status not in VALID_STATUSES:
            issues.append(f"invalid status: {status}")

        # Validate confidence
        confidence = fm.get("confidence", "")
        if confidence and confidence not in VALID_CONFIDENCES:
            issues.append(f"invalid confidence: {confidence}")

        # Check stale seed
        is_stale = False
        if status == "seed" and fm.get("created"):
            try:
                # Through the shared coercion: `created` may be a YAML date or
                # datetime, and both engines must age them alike.
                created = frontmatter_date(fm["created"])
                days_old = (_today() - created).days
                if days_old > STALE_SEED_DAYS:
                    is_stale = True
                    issues.append(f"stale seed ({days_old} days old)")
            except ValueError as exc:
                # Reported, THEN skipped: an unaged seed must be said.
                print(f"{YELLOW}warn:{RESET} {display_path(file_path)} has an "
                      f"unreadable `created:` value ({fm['created']!r}: {exc}); "
                      f"not aged.", file=sys.stderr)

        keywords = frontmatter_list(fm.get("keywords"))

        notes.append({
            "file": file_path.name,
            "path": display_path(file_path),
            "subdir": subdir,
            "id": str(fm.get("id", "")),
            "title": fm.get("title", file_path.stem),
            "type": note_type,
            "status": status,
            "keywords": keywords,
            "confidence": confidence,
            "created": str(fm.get("created", "")),
            "updated": str(fm.get("updated", "")),
            "links_out": links,
            "issues": issues,
            "is_stale": is_stale,
        })

    return notes


def find_orphans(notes):
    """Find notes with no incoming or outgoing links."""
    all_ids = {n["id"] for n in notes if n["id"]}
    linked_to = set()
    has_outgoing = set()

    for n in notes:
        if n["links_out"]:
            has_outgoing.add(n["id"])
            for link_id in n["links_out"]:
                linked_to.add(link_id)

    orphans = []
    for n in notes:
        nid = n["id"]
        if nid and nid not in linked_to and nid not in has_outgoing:
            orphans.append(n)

    return orphans


def keyword_frequency(notes):
    """Count keyword frequency across all notes."""
    counter = Counter()
    for n in notes:
        for kw in n["keywords"]:
            counter[kw.lower()] += 1
    return counter


def format_terminal_report(notes):
    """Format health report for terminal output."""
    lines = []
    lines.append(f"\n{BOLD}Knowledge Base Health{RESET}\n")

    # Counts by status
    status_counts = Counter(n["status"] for n in notes)
    type_counts = Counter(n["type"] for n in notes)
    total = len(notes)

    lines.append(f"{BOLD}Notes:{RESET} {total} total | "
                 f"{GREEN}{status_counts.get('evergreen', 0)} evergreen{RESET} | "
                 f"{CYAN}{status_counts.get('growing', 0)} growing{RESET} | "
                 f"{YELLOW}{status_counts.get('seed', 0)} seeds{RESET} | "
                 f"{GRAY}{status_counts.get('archived', 0)} archived{RESET}")
    lines.append("")

    # By type
    if type_counts:
        lines.append(f"{BOLD}By Type:{RESET}")
        for t in sorted(VALID_TYPES):
            count = type_counts.get(t, 0)
            if count > 0:
                lines.append(f"  {t}: {count}")
        lines.append("")

    # Stale seeds
    stale = [n for n in notes if n["is_stale"]]
    if stale:
        lines.append(f"{RED}{BOLD}Stale Seeds (> {STALE_SEED_DAYS} days):{RESET}")
        for n in stale:
            lines.append(f"  {RED}{n['title']}{RESET} ({n['path']}) - created {n['created']}")
        lines.append("")

    # Orphans
    orphans = find_orphans(notes)
    if orphans:
        lines.append(f"{YELLOW}{BOLD}Orphan Notes (no links in or out):{RESET}")
        for n in orphans:
            lines.append(f"  {YELLOW}{n['title']}{RESET} ({n['path']})")
        lines.append("")

    # Schema issues
    issues_notes = [n for n in notes if n["issues"]]
    if issues_notes:
        lines.append(f"{RED}{BOLD}Schema Issues:{RESET}")
        # Filter per ISSUE, not per note: only the stale-seed line duplicates
        # the section above, and the terminal must match `--json`.
        for n in issues_notes:
            for issue in n["issues"]:
                if "stale seed" not in issue:
                    lines.append(f"  {RED}{n['title']}{RESET}: {issue}")
        lines.append("")

    # Tag cloud (top 15)
    kw_freq = keyword_frequency(notes)
    if kw_freq:
        top_kw = kw_freq.most_common(15)
        lines.append(f"{BOLD}Top Keywords:{RESET}")
        kw_strs = [f"{kw} ({count})" for kw, count in top_kw]
        lines.append(f"  {', '.join(kw_strs)}")
        lines.append("")

    if total == 0:
        lines.append(f"{GRAY}Knowledge base is empty. Use /zk add to create your first note.{RESET}")
        lines.append("")

    # What this run did NOT read, by name. Every number above is over the scanned
    # set; without this block a smaller set prints exactly like a complete one.
    unread = unread_note_files()
    if unread:
        lines.append(f"{YELLOW}{BOLD}Not read by this scan ({len(unread)}):{RESET}")
        for path in unread:
            lines.append(f"  {YELLOW}{display_path(path)}{RESET}")
        lines.append(f"  {GRAY}Counted nowhere above. This engine reads the immediate "
                     f"child directories of the knowledge root.{RESET}")
        lines.append("")

    odin_brain_dir = knowledge_root() / "odin-brain"
    if odin_brain_dir.exists():
        lines.append(f"{GRAY}Note: odin-brain uses a separate schema - run scripts/odin-brain-health.py for its report.{RESET}")
        lines.append("")
    owned = sorted(d for d in other_schema_dirs() - OTHER_SCHEMA_DIRS
                   if (knowledge_root() / d).is_dir())
    if owned:
        lines.append(f"{GRAY}Not scanned, owned by other registry names: {', '.join(owned)}.{RESET}")
        lines.append("")

    return "\n".join(lines)


def format_json(notes):
    """Output notes health as JSON."""
    status_counts = Counter(n["status"] for n in notes)
    type_counts = Counter(n["type"] for n in notes)
    orphans = find_orphans(notes)
    stale = [n for n in notes if n["is_stale"]]
    kw_freq = keyword_frequency(notes)

    output = {
        "total": len(notes),
        # The scope the totals cover, and what fell outside it. A consumer that
        # reads `total` as "every note under the knowledge root" was reading a
        # claim this tool never established; now it can check.
        "scanned_subdirs": note_subdirs(),
        "excluded_subdirs": sorted(other_schema_dirs()),
        "unread_files": [display_path(p) for p in unread_note_files()],
        "by_status": dict(status_counts),
        "by_type": dict(type_counts),
        "stale_seeds": [{"title": n["title"], "path": n["path"], "created": n["created"]} for n in stale],
        "orphans": [{"title": n["title"], "path": n["path"]} for n in orphans],
        "schema_issues": [
            {"title": n["title"], "path": n["path"], "issues": n["issues"]}
            for n in notes if n["issues"]
        ],
        "top_keywords": dict(kw_freq.most_common(20)),
        "notes": [
            {
                "id": n["id"],
                "title": n["title"],
                "type": n["type"],
                "status": n["status"],
                "keywords": n["keywords"],
                "path": n["path"],
                "created": n["created"],
                "links_out": n["links_out"],
            }
            for n in notes
        ],
    }
    return json.dumps(output, indent=2)


def regenerate_index(notes):
    """Regenerate the `knowledge-index` file with current stats."""
    status_counts = Counter(n["status"] for n in notes)
    type_counts = Counter(n["type"] for n in notes)
    orphans = find_orphans(notes)
    kw_freq = keyword_frequency(notes)
    total = len(notes)

    lines = []
    lines.append("# Knowledge Base Index")
    lines.append("")
    lines.append("> Auto-generated by `/zk stats` and `scripts/knowledge-health.py`")
    lines.append(f"> Last updated: {_today().strftime('%Y-%m-%d')}")
    lines.append("")

    # Stats table
    lines.append("## Stats")
    lines.append("")
    lines.append("| Metric | Count |")
    lines.append("|--------|-------|")
    lines.append(f"| Total notes | {total} |")
    lines.append(f"| Seeds | {status_counts.get('seed', 0)} |")
    lines.append(f"| Growing | {status_counts.get('growing', 0)} |")
    lines.append(f"| Evergreen | {status_counts.get('evergreen', 0)} |")
    lines.append(f"| Archived | {status_counts.get('archived', 0)} |")
    lines.append(f"| Orphans | {len(orphans)} |")
    lines.append("")

    # Recent notes (last 10 by created date)
    lines.append("## Recent Notes")
    lines.append("")
    sorted_notes = sorted(
        [n for n in notes if n["created"]],
        key=lambda n: n["created"],
        reverse=True
    )[:10]
    if sorted_notes:
        for n in sorted_notes:
            lines.append(f"- **{n['title']}** ({n['type']}, {n['status']}) - {n['created']} - `{n['path']}`")
    else:
        lines.append("*No notes yet. Use `/zk add` to create your first note.*")
    lines.append("")

    # By type
    lines.append("## Notes by Type")
    lines.append("")
    lines.append("| Type | Count |")
    lines.append("|------|-------|")
    for t in sorted(VALID_TYPES):
        lines.append(f"| {t} | {type_counts.get(t, 0)} |")
    lines.append("")

    # Tag cloud
    lines.append("## Tag Cloud")
    lines.append("")
    if kw_freq:
        top_kw = kw_freq.most_common(30)
        kw_strs = [f"**{kw}** ({count})" for kw, count in top_kw]
        lines.append(", ".join(kw_strs))
    else:
        lines.append("*No tags yet.*")
    lines.append("")

    # All notes by subdir
    lines.append("---")
    lines.append("")
    lines.append("## All Notes")
    lines.append("")
    if not notes:
        lines.append("*Empty knowledge base. Start with `/zk add` or `/zk distill`.*")
    else:
        # Group by the subdirs the notes CAME FROM, so this section always
        # agrees with the stats table above.
        present = [s for s in note_subdirs() if any(n["subdir"] == s for n in notes)]
        extra = sorted({n["subdir"] for n in notes} - set(present))
        for subdir in present + extra:
            subdir_notes = [n for n in notes if n["subdir"] == subdir]
            if subdir_notes:
                lines.append(f"### {subdir.capitalize()}")
                lines.append("")
                for n in sorted(subdir_notes, key=lambda x: x["created"], reverse=True):
                    status_icon = {"seed": "S", "growing": "G", "evergreen": "E", "archived": "A"}.get(n["status"], "?")
                    kw_str = ", ".join(n["keywords"][:5]) if n["keywords"] else ""
                    lines.append(f"- [{status_icon}] **{n['title']}** - {kw_str} - `{n['path']}`")
                lines.append("")

    content = "\n".join(lines) + "\n"
    atomic_write_text(index_file(), content)
    return True


def main():
    parser = argparse.ArgumentParser(description="Knowledge Base Health Engine")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--update-index", action="store_true", help="Regenerate INDEX.md")
    args = parser.parse_args()

    root = knowledge_root()
    if not root.exists():
        print(f"{RED}Knowledge directory not found: {root}{RESET}")
        sys.exit(1)

    notes = scan_notes()

    if args.json:
        print(format_json(notes))
    else:
        print(format_terminal_report(notes))

    if args.update_index:
        if regenerate_index(notes):
            print(f"{GREEN}INDEX.md regenerated.{RESET}")


if __name__ == "__main__":
    main()
