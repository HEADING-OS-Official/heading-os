#!/usr/bin/env python3
"""Every engine site that still addresses the pre-reform `outputs` directory, counted.

Usage:
    python scripts/check-legacy-outputs-sites.py            # human report
    python scripts/check-legacy-outputs-sites.py --check    # exit 1 on a rise or a stale row
    python scripts/check-legacy-outputs-sites.py --json     # machine-readable

WHY A SEPARATE SCANNER, AND WHICH EXISTING ONES WERE ASKED FIRST
================================================================
The HEADING OS 2 move drained `<data>/outputs/` into nine family roots. Until
HOS-147 what was left there was the move's own working directory and three
files whose writers had been repointed; HOS-147 moved the directory and two of
the files into the archive and the third back into its own series, so the
directory holds nothing at all. A site that still names that
address is a site whose artifact lands somewhere that reads alive and that
nobody reads. `get_outputs_dir()`, which recreated the directory on the way, was
deleted on 2026-09-25, but a literal join onto the data root recreates it just
the same, so nothing raises and nothing prints.

Three existing instruments answer neighbouring questions and not this one:
`scripts/check-data-path-references.py` (does a named data path resolve; it
classes most `outputs` sites as prose or docstring), `scripts/audit-skill-bash-paths.py`
(a bare cwd-relative data path handed to a script from a SKILL bash line)
and `scripts/utils/runtime_write_guard.py`
(write sites spelled the way runtime junk was spelled).

WHAT IS COUNTED
===============
A string constant whose first path component is `outputs`, in PYTHON only,
excluding the constant that is a module, class or function docstring, and
excluding a constant spanning lines. Both exclusions are mechanical rather than
a judgement about the text: a docstring and a multi-line constant are a
document, and no path this engine builds spans lines.

Markdown is deliberately NOT read. A bare legacy `outputs` path handed to Bash
from a skill is `audit-skill-bash-paths.py`'s subject and it gates on it;
everything else in a `.md` under `.claude/` is prose, most of it explaining the
very literal it contains.

A second anchor, the bare call to the seam helper `get_outputs_dir()`, stood
here until 2026-10-16. The helper was deleted on 2026-09-25 (HOS-5 section 6),
so a new call cannot import, and the anchor's last two hits were past-tense
prose in two SKILL.md files; the vacuity refusal rested on those two sentences.

WHY A COUNT RATCHET AND NOT A REASON PER SITE
==============================================
`check-data-path-references.py` carries both instruments and keeps them apart
for a stated reason: its `BASELINE` takes one path and one reason at a time and
was frozen EMPTY so that pre-loading it with the day's findings could not hide
the worklist. The same reasoning applies here: on 2026-09-22 this scanner found
over a hundred sites, and writing a reason per site to make a gate pass is the
move this workspace forbids outright.

So COUNTS is the ratchet and it moves in one direction only. A RISE is a finding:
a new site appeared and nobody judged it. A FALL is also reported, because a
number that no longer matches the tree is a number nobody re-derived; lowering it
is free and is what a conversion is supposed to do.

REVIEWED is the other instrument, and it is small on purpose. A row goes in when
a site has actually been judged, carrying its verdict and the reason itself, so
that the next reader finds the reason rather than deriving it again. Two verdicts:
`keep`, where the pre-reform address is correct and meant to stay, and `blocked`,
where it should be converted and cannot be until the operator's registry gains a
row. `--check` does not fail on `blocked`; those are the worklist, and the report
prints them as one.

A row whose file matches no anchor any more is reported as STALE, which is what
happens when a site is converted and the row is forgotten. That mechanism also
bounds the table, and it did so twice while this file was being written: a
`blocked` Markdown site whose address is a prose literal, and this scanner's own
anchor pattern, each matched nothing and could not be given a row. So a blocker
recorded ONLY in a Markdown file is invisible here, by the same decision that
keeps the anchor out of Markdown. Where two files share one blocked address, the
row goes on the one an anchor can see and names the other.

Neither instrument claims to be a guarantee. What it establishes is narrower and
real: a legacy address newly named in this repository, in a Python string,
cannot arrive unnoticed.

THE SCOPE, AND THE REFUSAL THAT KEEPS IT HONEST
===============================================
Corpus is the tracked files under the subtrees in `SUBTREES`. Exit 2, never 0 and
never 1, when the corpus is empty or when the anchor finds nothing at all: a
scanner that read nothing has established nothing, and an anchor that stops
matching is indistinguishable from a tree that was repaired.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The engine subtrees read. `docs/` and `reference/` are here because a
#: sentence naming a destination misroutes an artifact exactly as a call does.
SUBTREES = ("scripts/", "tests/", ".claude/", "config/", "docs/", "reference/")

#: A data-relative literal whose FIRST component is `outputs`. The trailing
#: character class is what stops `outputs` alone, and the leading one is what
#: stops `work/deliverable/outputs/...`, which is a CURRENT address.  # leak-guard: ok (scanner vocabulary)
LITERAL = re.compile(r"(?:^|[^A-Za-z0-9_./<>{-])outputs/[A-Za-z0-9_.{}<>*-]")  # leak-guard: ok (guard vocabulary: the legacy spelling this check exists to count)

#: The ratchet. It may FALL freely and a rise is a finding; each change is
#: dated in `git log -p` of this file with the measurement behind it.
COUNTS: dict[str, int] = {
    "literal_sites": 39,
}

#: Sites HOS-76 judged, one row per file, each carrying its verdict and the
#: reason itself rather than a pointer to one. A row matching no anchor is stale
#: and is reported, which is what happens when a site is converted and the row
#: is forgotten.
#:
#:   keep     the pre-reform address is correct here and is meant to stay.
#:   blocked  it should be converted and cannot be, because the operator's
#:            registry has no row for the address. The row needed is named at
#:            the site. `--check` does NOT fail on these; they are the worklist.
KEEP, BLOCKED = "keep", "blocked"

REVIEWED: dict[str, tuple[str, str]] = {
    "scripts/utils/runtime_junk_moves.py": (KEEP,
        "the one pre-move address the Action Queue reader still checks "
        "(LEGACY_STORE_PREFIXES). Naming the old address is its content."),
    "scripts/heading_os_2/families.py": (KEEP,
        "the migration's own classification rules, keyed on the pre-reform path "
        "prefixes. Naming the old address is the table's content."),
    # Rows for converted sites leave by this table's own stale rule; their
    # history is in `git log -p` of this file.
}


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

def tracked_files() -> list[str]:
    """Every tracked path under `SUBTREES`."""
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, check=True,
                         capture_output=True, text=True).stdout.split("\n")
    keep = []
    for rel in out:
        if not rel or not rel.startswith(SUBTREES):
            continue
        keep.append(rel)
    return keep


# ---------------------------------------------------------------------------
# The anchor: the literal, Python only
# ---------------------------------------------------------------------------

def _docstring_ids(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            out.add(id(first.value))
    return out


def scan_literal_python(rel: str, text: str) -> list[dict]:
    tree = ast.parse(text)
    skip = _docstring_ids(tree)
    rows = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in skip or "\n" in node.value:
            continue
        if not LITERAL.search(node.value):
            continue
        rows.append({"file": rel, "line": node.lineno, "anchor": "literal",
                     "outcome": "designates"})
    return rows


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def collect() -> dict:
    rows: list[dict] = []
    unparsed: list[str] = []
    read = 0
    for rel in tracked_files():
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        read += 1
        # A cheap pre-filter before the AST walk, compared against file text
        # and never joined to a root.
        if rel.endswith(".py") and "outputs/" in text:  # leak-guard: ok (substring pre-filter)
            try:
                rows += scan_literal_python(rel, text)
            except SyntaxError:
                unparsed.append(rel)

    literals = rows
    measured = {"literal_sites": len(literals)}

    findings: list[str] = []
    for key, ceiling in COUNTS.items():
        now = measured[key]
        if now > ceiling:
            findings.append(f"{key} rose: {ceiling} -> {now}. "
                            f"A new site names the pre-reform address and nobody judged it.")
        elif now < ceiling:
            findings.append(f"{key} fell: {ceiling} -> {now}. "
                            f"Lower COUNTS to the measured truth.")

    touched = {r["file"] for r in literals}
    stale = sorted(f for f in REVIEWED if f not in touched)
    for f in stale:
        findings.append(f"REVIEWED row for {f} matches no anchor any more; "
                        f"the site was converted and the row was forgotten.")

    return {
        "files_read": read,
        "python_unparsed": unparsed,
        "measured": measured,
        "ceilings": dict(COUNTS),
        "literals": sorted(literals, key=lambda r: (r["file"], r["line"])),
        "reviewed": {f: {"verdict": v, "reason": r}
                     for f, (v, r) in REVIEWED.items()},
        "blocked": sorted(f for f, (v, _) in REVIEWED.items() if v == BLOCKED),
        "stale_reviewed": stale,
        "findings": findings,
    }


def _note(rel: str) -> str:
    row = REVIEWED.get(rel)
    return f" [{row[0]}]" if row else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="exit 1 on a rise or a stale REVIEWED row")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    args = ap.parse_args()

    report = collect()

    # The vacuity refusal. A scanner that read nothing, or an anchor that stopped
    # matching, has established nothing, and a green here would be evidence.
    if report["files_read"] == 0:
        print("REFUSED: read 0 files. The corpus is empty or SUBTREES is wrong.",
              file=sys.stderr)
        return 2
    if report["measured"]["literal_sites"] == 0:
        print("REFUSED: the literal anchor matched nothing anywhere. Either the "
              "pattern is broken or the corpus is not this repository; a pass "
              "here would be vacuous.", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        m, c = report["measured"], report["ceilings"]
        print(f"files read: {report['files_read']}")
        print(f"python literal sites:  "
              f"{m['literal_sites']} (ceiling {c['literal_sites']})")
        if report["python_unparsed"]:
            print(f"python files that did not parse: {report['python_unparsed']}")
        print()
        for row in report["literals"]:
            print(f"  literal  {row['file']}:{row['line']}{_note(row['file'])}")
        print()
        if report["blocked"]:
            print(f"blocked on a missing registry row ({len(report['blocked'])}), "
                  f"the row needed is named at each site:")
            for f in report["blocked"]:
                print(f"  {f}")
            print()
        if report["findings"]:
            for f in report["findings"]:
                print(f"FINDING: {f}")
        else:
            print("no finding: the count matches the tree and no REVIEWED row is stale.")

    if args.check and report["findings"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
