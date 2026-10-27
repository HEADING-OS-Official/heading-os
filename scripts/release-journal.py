#!/usr/bin/env python3
"""Read the release gate's journal as DECISIONS rather than as checks.

The gate in `.claude/hooks/_dispatch.py` appends one line per commit or push it
PERMITS, which is the audit record it is meant to be and is documented as such
beside `_record_release`. It is not a record of how many times the operator
decided something, and read line by line it says the opposite: MEASURED
2026-09-23 over the registered journal, 1682 rows carrying 115 distinct
authorising phrasings, one of which accounts for 1004 rows over three days.
That is what this reader is for. It collapses the repeats of one authorisation
and prints the check count BESIDE each decision rather than instead of it, so
neither number has to be inferred from the other.

WHAT ONE PRINTED DECISION IS, and it is a grouping rule over the file rather
than a fact the file carries: a maximal run of consecutive rows, within one
checkout, whose authorising words are identical once whitespace is collapsed,
uninterrupted in that checkout by a row carrying different words.

WHAT THAT CANNOT ESTABLISH, stated because the whole defect this answers was an
honest reading of a file that misleads. The journal carries no session id and no
turn id, so the same word typed twice in a row with nothing between the two
typings arrives as one decision here. The count is therefore a floor on the
number of decisions, never the number itself. Rows written before 2026-09-08
carry no `checkout` and no `branch` field; they are grouped under one
`unrecorded` checkout and printed as such. `HOOKS-REFERENCE.md` (in the data
repository under `spec-docs` since 2026-09-25) reads that absence as the main
clone, branch unknown; this reader does not assert it.

It READS. It never writes, never moves and never rewrites a row: the journal is
an audit record and a reader that edits one is not a reader.

Usage:

    .venv/bin/python scripts/release-journal.py                 # last 20 decisions
    .venv/bin/python scripts/release-journal.py --summary
    .venv/bin/python scripts/release-journal.py --since 2026-09-20 --checks
    .venv/bin/python scripts/release-journal.py --action push --all
    .venv/bin/python scripts/release-journal.py --grep hos81 --full
    .venv/bin/python scripts/release-journal.py --json
"""
import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.colors import BOLD, CYAN, GRAY, GREEN, RESET, YELLOW  # noqa: E402
from scripts.utils.data_path import data_read  # noqa: E402
from scripts.utils.paths import read_env_value  # noqa: E402

#: Spelled here for the same reason `_dispatch.py` spells it: this module
#: supplies the data root rather than letting it be read out of the ambient
#: environment alone, because a yard has no sibling overlay to fall back to.
_DATA_ROOT_PIN = "HEADING_OS_DATA"

WORKSPACE = Path(__file__).resolve().parent.parent

#: The `unrecorded` bucket, shown verbatim, for rows written before the
#: 2026-09-08 move added `checkout` and `branch` to the record.
UNRECORDED = "unrecorded (pre-2026-09-08 record)"

FOOTER = (
    "One decision = consecutive rows in one checkout with identical authorising "
    "words. The journal holds no session or turn id, so two identical words typed "
    "in a row read as one decision: the decision count is a floor."
)


# ============================================================
# Loading
# ============================================================

def journal_paths(explicit: str | None) -> list[Path]:
    """Every address a reader of this journal must look at, on THIS checkout.

    The root is derived the way `_release_log_default` in `.claude/hooks/
    _dispatch.py` derives it, and for its measured reason: `get_data_root()`
    answers from the environment and then from the SIBLING of the workspace
    root, and a yard two levels deeper has no sibling overlay. The checkout's
    own `.env` answers instead, and only where the environment is silent, so an
    operator's deliberate override and a test's pin both still win.
    """
    if explicit:
        return [Path(explicit).expanduser()]
    root = None
    if not os.environ.get(_DATA_ROOT_PIN):
        value = read_env_value(WORKSPACE / ".env", _DATA_ROOT_PIN)
        if value:
            candidate = Path(value).expanduser()
            if candidate.is_dir():
                root = candidate
    return list(data_read("release-journal", data_root=root))


def load(paths: list[Path]) -> tuple[list[dict], int, list[Path]]:
    """Every row, in time order, plus the count of lines that did not parse.

    A line that does not parse is COUNTED and reported, never dropped in
    silence: this is the file that answers "who authorised this", and a reader
    that quietly skips part of it is worse than one that refuses.

    Sorted by `at` with a STABLE sort, so rows sharing a timestamp to the second
    keep the order they were appended in. Several checkouts append here
    concurrently, so file order is the only tie-break there is.
    """
    rows: list[dict] = []
    unparseable = 0
    present: list[Path] = []
    for path in paths:
        if not path.is_file():
            continue
        present.append(path)
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    unparseable += 1
                    continue
                if isinstance(record, dict) and record.get("at"):
                    rows.append(record)
                else:
                    unparseable += 1
    rows.sort(key=lambda r: str(r.get("at")))
    return rows, unparseable, present


# ============================================================
# Grouping
# ============================================================

def flatten(text: str) -> str:
    """Whitespace collapsed, so two spellings of one prompt compare equal."""
    return " ".join((text or "").split())


def decisions(rows: list[dict]) -> list[dict]:
    """The rows grouped into authorisation runs, oldest first.

    The run BREAKS when the same checkout next carries different words. It does
    not break on time, because any gap threshold would be a number this reader
    invented rather than one the journal holds.
    """
    open_runs: dict[str, dict] = {}
    runs: list[dict] = []
    for row in rows:
        checkout = row.get("checkout") or UNRECORDED
        words = flatten(row.get("authorised_by"))
        run = open_runs.get(checkout)
        if run is None or run["words"] != words:
            run = {
                "checkout": checkout,
                "words": words,
                "words_verbatim": row.get("authorised_by") or "",
                "first_at": row.get("at"),
                "last_at": row.get("at"),
                "checks": 0,
                "actions": Counter(),
                "branches": [],
                "commands": [],
                "rows": [],
            }
            open_runs[checkout] = run
            runs.append(run)
        run["last_at"] = row.get("at")
        run["checks"] += 1
        run["actions"][row.get("action") or "?"] += 1
        branch = row.get("branch")
        if branch and branch not in run["branches"]:
            run["branches"].append(branch)
        command = row.get("command") or ""
        if command not in run["commands"]:
            run["commands"].append(command)
        run["rows"].append(row)
    return runs


def matches(run: dict, args: argparse.Namespace) -> bool:
    """Whether one decision survives the filters, judged on the WHOLE run.

    Filtering happens after grouping and never before it. A filter that cut rows
    first would join two runs the journal kept apart, by removing the row that
    separated them, and the reader would then report a decision that never
    happened.
    """
    if args.since and str(run["last_at"]) < args.since:
        return False
    if args.until and str(run["first_at"])[:10] > args.until:
        return False
    if args.action and not run["actions"].get(args.action):
        return False
    if args.checkout and args.checkout not in run["checkout"]:
        return False
    if args.branch and not any(args.branch in b for b in run["branches"]):
        return False
    if args.grep:
        needle = args.grep.lower()
        haystack = " ".join([run["words"]] + run["commands"]).lower()
        if needle not in haystack:
            return False
    return True


# ============================================================
# Output
# ============================================================

def short(text: str, width: int) -> str:
    """`text` capped at `width`, with the cut made visible."""
    return text if len(text) <= width else text[:width - 3] + "..."


def render(runs: list[dict], args: argparse.Namespace) -> None:
    for run in runs:
        counts = " ".join(f"{action} {n}"
                          for action, n in sorted(run["actions"].items()))
        span = str(run["first_at"])[:16].replace("T", " ")
        if str(run["last_at"])[:16] != str(run["first_at"])[:16]:
            span += " to " + str(run["last_at"])[:16].replace("T", " ")
        checks = f"{run['checks']} check" + ("s" if run["checks"] != 1 else "")
        print(f"{BOLD}{span}{RESET}  {GREEN}{counts}{RESET}  {GRAY}{checks}{RESET}")
        where = run["checkout"]
        if where != UNRECORDED:
            where = str(Path(where).name) + f"  ({where})"
        if run["branches"]:
            where += "  branch " + ", ".join(run["branches"])
        print(f"  {GRAY}where{RESET}  {where}")
        words = run["words_verbatim"] if args.full else short(run["words"], 120)
        print(f"  {CYAN}words{RESET}  {words}")
        commands = run["commands"] if args.full else run["commands"][:3]
        for command in commands:
            print(f"  {GRAY}cmd{RESET}    {short(flatten(command), 120)}")
        hidden = len(run["commands"]) - len(commands)
        if hidden > 0:
            print(f"  {GRAY}cmd    ... {hidden} more distinct command(s){RESET}")
        if args.checks:
            for row in run["rows"]:
                stamp = str(row.get("at"))[:19].replace("T", " ")
                print(f"    {GRAY}{stamp}  {row.get('action')}  "
                      f"{short(flatten(row.get('command') or ''), 90)}{RESET}")
        print()


def summarise(rows: list[dict], runs: list[dict], paths: list[Path],
              unparseable: int) -> None:
    actions = Counter(r.get("action") or "?" for r in rows)
    words = Counter(flatten(r.get("authorised_by")) for r in rows)
    checkouts = {r.get("checkout") or UNRECORDED for r in rows}
    print(f"{BOLD}Release journal{RESET}")
    for path in paths:
        print(f"  file        {path}")
    print(f"  rows        {len(rows)}"
          + (f"  ({unparseable} unparseable)" if unparseable else ""))
    print(f"  decisions   {len(runs)}")
    print(f"  span        {str(min(r['at'] for r in rows))[:16]} to "
          f"{str(max(r['at'] for r in rows))[:16]}")
    print("  actions     " + "  ".join(f"{a} {n}" for a, n in sorted(actions.items())))
    print(f"  phrasings   {len(words)} distinct authorising phrasings")
    print(f"  checkouts   {len(checkouts)}")
    top = words.most_common(1)[0]
    print(f"  widest      one phrasing covers {top[1]} rows "
          f"({top[1] * 100 // max(len(rows), 1)}% of the file)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read the release gate's journal as decisions, not checks.",
        epilog=FOOTER)
    parser.add_argument("--path", help="read this journal instead of the registered one")
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="decisions still open on or after this date")
    parser.add_argument("--until", metavar="YYYY-MM-DD", help="decisions that started on or before this date")
    parser.add_argument("--action", choices=("commit", "push"), help="decisions carrying this action")
    parser.add_argument("--checkout", help="substring of the checkout path")
    parser.add_argument("--branch", help="substring of a branch name")
    parser.add_argument("--grep", help="substring of the authorising words or a command")
    parser.add_argument("--checks", action="store_true", help="list every row inside each decision")
    parser.add_argument("--full", action="store_true", help="the authorising words and every command, uncut")
    parser.add_argument("--limit", type=int, default=20, help="how many decisions to print (default 20)")
    parser.add_argument("--all", action="store_true", help="no limit")
    parser.add_argument("--oldest", action="store_true", help="oldest first (default is newest first)")
    parser.add_argument("--summary", action="store_true", help="counts over the whole journal, no listing")
    parser.add_argument("--json", action="store_true", help="the selected decisions as JSON on stdout")
    args = parser.parse_args()

    paths = journal_paths(args.path)
    rows, unparseable, present = load(paths)
    if not rows:
        where = ", ".join(str(p) for p in paths) or "no address resolved"
        print(f"No release journal to read ({where}).", file=sys.stderr)
        return 1
    if unparseable:
        print(f"{YELLOW}{unparseable} line(s) did not parse and are not counted "
              f"below.{RESET}", file=sys.stderr)

    runs = decisions(rows)
    if args.summary:
        summarise(rows, runs, present, unparseable)
        print(f"\n{GRAY}{FOOTER}{RESET}")
        return 0

    selected = [run for run in runs if matches(run, args)]
    if not args.oldest:
        selected = list(reversed(selected))
    shown = selected if args.all else selected[:args.limit]

    if args.json:
        print(json.dumps([{k: (dict(v) if isinstance(v, Counter) else v)
                           for k, v in run.items() if k != "rows"}
                          for run in shown], ensure_ascii=False, indent=2))
        return 0

    render(shown, args)
    print(f"{GRAY}{len(shown)} of {len(selected)} matching decision(s), "
          f"over {sum(r['checks'] for r in selected)} journal row(s) "
          f"of {len(rows)}.{RESET}")
    print(f"{GRAY}{FOOTER}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
