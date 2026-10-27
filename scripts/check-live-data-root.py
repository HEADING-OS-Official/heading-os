#!/usr/bin/env python3
"""Ratchet: how many tests are exempted from the universal data-root pin.

Usage:
    python scripts/check-live-data-root.py              # report
    python scripts/check-live-data-root.py --check      # exit 1 on a NEW site
    python scripts/check-live-data-root.py --json
    python scripts/check-live-data-root.py --write-baseline   # shrink only

What this replaces, and why the subject changed
-----------------------------------------------
The overlay reachability baseline (moved 2026-10-16 to the data registry name
`engine-config-history`) froze a CHILD-PROCESS COUNT: how
many subprocesses of a full suite run could resolve the operator's live data
root. That number sat in the push path and the suite went red when it rose.

It was the wrong quantity to freeze. MEASURED 2026-09-06 and recorded in the
baseline file itself, nine full runs over ONE UNCHANGED TREE gave 6010, 6115,
6174, 6209, 6265, 6276, 6361 and 6370: a spread of 360 on a mean of 6196, plus
or minus three percent, because a child count depends on how the scheduler
packed the workers. A shrink-only ceiling fitted to a quantity that moves with
the scheduler, sitting in front of a push, produces a red gate carrying no
information about the code. It did exactly that on 2026-09-20 and cost the
operator two twelve-minute runs and an hour.

Since 2026-09-21 `tests/conftest.py` pins `HEADING_OS_DATA` to the test's own
`tmp_path` for EVERY test, and `@pytest.mark.live_data_root` is the only way
out. So the marker is now the whole exposure: a test without one cannot reach
the live root, and a test with one is the entire list of those that can.

The marker count is a different KIND of number. It lives in the SOURCE. It does
not move with the scheduler, the machine load, or how xdist packed the run. And
it fires here, at the moment somebody writes a marker, rather than twelve
minutes into a suite.

Scope, precisely (`.claude/rules/scope-claims.md`)
--------------------------------------------------
This reads `tests/**/test_*.py` from the WORKING TREE and parses each with
`ast`. It answers two questions per file and nothing else.

**Where is the marker applied?** A decorator on a test function or class, or a
module-level `pytestmark`. Asked of the AST, never of the text: a substring
scan goes red the moment a docstring quotes the marker to explain it, which
teaches people to stop explaining. This very docstring is an example of prose
carrying the string.

One level of module-level name binding is resolved, because the tree already
uses an alias. MEASURED 2026-09-21 over this tree: a detector that matched only
`pytest.mark.<name>` found 24 sites and missed 9. Two files bind the marker to a
module-level name once and then apply that name four and five times, and a
detector that does not follow the binding sees none of those nine.
Resolving it takes the count to 33.

**Does a reason sit beside it?** The marker's registered description in
`pyproject.toml` requires the reason to be in a comment next to the marker. So
this collects the inline comment on the decorator's own line plus the
contiguous comment block immediately above it, and requires the joined text to
reach `MIN_REASON_CHARS`. The comments come from `tokenize`, so a `#` inside a
string literal is not mistaken for one.

WHAT THAT CHECK CANNOT SEE, stated plainly rather than implied by silence. It
is a PRESENCE-AND-LENGTH test. It cannot tell a real reason from a long
sentence that says nothing, it cannot tell whether the reason is true, and it
cannot tell whether the exemption is warranted. A comment reading `# needed`
fails on length; a comment of forty characters of filler passes. Judging the
words is the reviewer's, and the floor exists so that the reviewer has
something to read at all. MEASURED 2026-09-21: the shortest real reason in the
tree is 136 characters, so the floor of 40 sits well under every honest site
and above the one-word shapes it is aimed at.

It also says nothing about whether a marked test is RIGHT to be marked, and
nothing about child processes. A marked test whose child writes to the live
overlay is caught by `data-root-bypass-guard` and the in-process overlay write
guard, which are untouched by this file and refuse exactly what they refused
before.

Why a ratchet and not a flat zero
---------------------------------
Zero is not today's tree and cannot be: some tests' SUBJECT is the operator's
own workspace, and for those a scratch root does not ask a smaller version of
the question, it asks a different one. A site of that kind belongs in the baseline
and is not debt to be reduced. Read the current count from a run.

`--write-baseline` only ever REMOVES entries, so a newly-written exemption
cannot be laundered in by re-running the writer.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import sys
import tokenize
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.colors import BOLD, GRAY, GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils.test_tree import (  # noqa: E402
    all_test_files, corpus_floor)
from scripts.utils.workspace import get_default_tz, get_workspace_root  # noqa: E402

ROOT = get_workspace_root()
BASELINE_PATH = ROOT / "config" / "live-data-root-baseline.json"

#: The marker name, which is also the `LIVE_DATA_ROOT_MARKER` constant in
#: `tests/conftest.py` and the registered marker in `pyproject.toml`. Nothing
#: pins the three spellings together; change all three at once.
MARKER = "live_data_root"

# The corpus floor is derived, not a constant: scripts/utils/test_tree.corpus_floor.

#: The floor on a reason's length. See the scope section of the module
#: docstring for what this can and cannot establish.
MIN_REASON_CHARS = 40

#: How many binding hops to follow before giving up. One is enough for today's
#: tree; five matches `scripts/check-test-vacuity.py`, whose resolver this is
#: modelled on, so the two behave the same way on an alias chain.
MAX_HOPS = 5


# ============================================================
# The rule, pure, so it can be exercised on synthetic source
# ============================================================

def _module_bindings(tree: ast.Module) -> dict[str, ast.expr]:
    """Name -> the expression last assigned to it at module level."""
    out: dict[str, ast.expr] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets = [stmt.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                out[target.id] = stmt.value
    return out


def expression_applies_marker(node: ast.expr, binds: dict[str, ast.expr],
                              _hops: int = 0) -> bool:
    """Whether this expression is, or resolves to, an application of the marker.

    Four shapes reach the same place and all four are real in this tree:
    `pytest.mark.live_data_root`, the same called with arguments, a module-level
    alias bound to either, and a list or tuple of marks holding one (the
    `pytestmark` shape).

    An unresolvable name answers False rather than True, and that is the
    over-reporting direction HERE. A site this misses is a marker with no
    baseline entry, which `--check` refuses on the next run; answering True on
    every unknown name would put unrelated decorators in the baseline and blunt
    the ratchet instead.
    """
    if _hops > MAX_HOPS:
        return False
    if isinstance(node, ast.Attribute):
        return node.attr == MARKER
    if isinstance(node, ast.Call):
        return expression_applies_marker(node.func, binds, _hops + 1)
    if isinstance(node, (ast.List, ast.Tuple)):
        return any(expression_applies_marker(e, binds, _hops + 1)
                   for e in node.elts)
    if isinstance(node, ast.Name):
        source = binds.get(node.id)
        if source is None:
            return False
        return expression_applies_marker(source, binds, _hops + 1)
    return False


def comments_by_line(source: str) -> dict[int, str]:
    """Line number -> the comment text on that line, from the tokenizer.

    The tokenizer rather than a `#` scan, for the same reason the sites come
    from the AST: a `#` inside a string literal is not a comment, and a test
    file that quotes one in its docstring would otherwise hand a site a reason
    it does not have.
    """
    out: dict[int, str] = {}
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type == tokenize.COMMENT:
                out[tok.start[0]] = tok.string.lstrip("#").strip()
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # A file that does not tokenize is a different failure, reported loudly
        # by pytest itself. Returning what was collected leaves the sites in
        # that file looking reason-less, which is the strict direction.
        pass
    return out


def reason_for(lineno: int, comments: dict[int, str]) -> str:
    """The comment text adjacent to a marker application, joined.

    Adjacent means: the inline comment on the marker's own line, plus the
    unbroken run of comment lines directly above it. A blank line or a line of
    code ends the run, so a paragraph explaining the function two decorators up
    does not lend its words to a marker that has none.
    """
    parts: list[str] = []
    above = lineno - 1
    while above in comments:
        parts.append(comments[above])
        above -= 1
    parts.reverse()
    if lineno in comments:
        parts.append(comments[lineno])
    return " ".join(p for p in parts if p).strip()


def marker_sites(rel_path: str, source: str) -> list[tuple[str, int, str]]:
    """`(site, lineno, reason)` for every application of the marker in one file.

    Pure: takes source text, returns findings. Deleting the line that appends a
    finding changes a live result, which is what makes this testable on
    synthetic prose rather than only on the tree.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Reported loudly by pytest itself; swallowing it here would be this
        # script committing the defect it exists to find.
        return []

    binds = _module_bindings(tree)
    comments = comments_by_line(source)
    found: list[tuple[str, int, str]] = []

    for stmt in tree.body:
        if (isinstance(stmt, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "pytestmark"
                        for t in stmt.targets)
                and expression_applies_marker(stmt.value, binds)):
            found.append((f"{rel_path}::<pytestmark>", stmt.lineno,
                          reason_for(stmt.lineno, comments)))

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        for dec in node.decorator_list:
            if expression_applies_marker(dec, binds):
                found.append((f"{rel_path}::{node.name}", dec.lineno,
                              reason_for(dec.lineno, comments)))

    return sorted(found)


# ============================================================
# The tree
# ============================================================

def scan(root: Path) -> tuple[list[tuple[str, int, str]], int]:
    """(sites, files_read). The file count is the anti-vacuity floor's input."""
    sites: list[tuple[str, int, str]] = []
    read = 0
    for path in all_test_files(root):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # Never swallow: an unreadable test file is a fault, and reporting
            # a clean tree over one is exactly this script's own defect shape.
            print(f"{RED}cannot read {path}: {exc}{RESET}", file=sys.stderr)
            continue
        read += 1
        sites.extend(marker_sites(path.relative_to(root).as_posix(), source))
    return sorted(sites), read


def load_baseline() -> set[str]:
    if not BASELINE_PATH.exists():
        return set()
    try:
        data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{RED}baseline unreadable: {exc}{RESET}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("frozen"), list):
        raise SystemExit(f"{RED}baseline malformed: expected {{'frozen': [...]}}{RESET}")
    return set(data["frozen"])


def write_baseline(frozen: set[str], current: list[str]) -> int:
    """Shrink only. An entry that is no longer a site leaves; a new one never
    enters. Otherwise re-running the writer would launder a fresh exemption.

    EVERY OTHER KEY IN THE FILE IS PRESERVED, and that is not politeness. The
    sibling ratchet `scripts/check-test-vacuity.py` rebuilt its baseline from a
    literal, and MEASURED 2026-09-16 the first `--write-baseline` after somebody
    hand-wrote a provenance sentence into the JSON silently dropped it: a file a
    program rewrites cannot carry a note the program does not know about. This
    one reads what is there and replaces exactly two keys, so the reasoning
    beside the numbers survives a run of the writer.
    """
    kept = sorted(frozen & set(current))
    removed = sorted(frozen - set(current))
    payload: dict = {}
    if BASELINE_PATH.exists():
        try:
            existing = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"{RED}baseline unreadable: {exc}{RESET}") from exc
        if isinstance(existing, dict):
            payload.update(existing)
    payload.setdefault("_comment", (
        "Frozen @pytest.mark.live_data_root sites for "
        "scripts/check-live-data-root.py. Shrink-only: --write-baseline "
        "removes entries that no longer exist and never adds new ones. A new "
        "exemption is argued and approved, not frozen by re-running the writer."
    ))
    payload["measured"] = datetime.now(get_default_tz()).date().isoformat()
    payload["frozen"] = kept
    BASELINE_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False)
                             + "\n", encoding="utf-8")
    print(f"{GREEN}baseline written: {len(kept)} frozen, {len(removed)} removed{RESET}")
    for entry in removed:
        print(f"  {GRAY}-{RESET} {entry}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="exit 1 on a new exemption or one with no reason")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--write-baseline", action="store_true",
                    help="rewrite the baseline, removing entries that are gone")
    args = ap.parse_args()

    sites, files_read = scan(ROOT)

    floor = corpus_floor(ROOT)
    if files_read < floor:
        message = (f"read {files_read} test files, below the floor of {floor}; "
                   f"refusing to report a verdict over a corpus this small")
        if args.json:
            print(json.dumps({"refused": message}, indent=2))
        else:
            print(f"{RED}{BOLD}REFUSED{RESET} {message}", file=sys.stderr)
        return 2

    keys = [site for site, _, _ in sites]
    frozen = load_baseline()

    if args.write_baseline:
        return write_baseline(frozen, keys)

    new = [site for site in keys if site not in frozen]
    stale = sorted(frozen - set(keys))
    reasonless = [(site, lineno) for site, lineno, reason in sites
                  if len(reason) < MIN_REASON_CHARS]

    if args.json:
        print(json.dumps({"files_read": files_read, "sites": keys, "new": new,
                          "reasonless": [s for s, _ in reasonless],
                          "stale_baseline": stale,
                          "frozen": sorted(frozen)}, indent=2))
        return 1 if (args.check and (new or reasonless)) else 0

    print(f"{BOLD}live-data-root{RESET}  {files_read} files, {len(keys)} "
          f"exemption(s), {len(new)} new, {len(frozen)} frozen")

    if stale:
        print(f"{GRAY}{len(stale)} baseline entries no longer exist; "
              f"run --write-baseline to drop them{RESET}")

    if not new and not reasonless:
        print(f"{GREEN}OK -- no new exemption from the data-root pin, and every "
              f"one carries a comment.{RESET}")
        print(f"{GRAY}Presence and length only; whether the words are a real "
              f"reason is the reviewer's call.{RESET}")
        return 0

    if new:
        print(f"\n{RED}{BOLD}{len(new)} new exemption(s) from the data-root "
              f"pin{RESET}")
        for entry in new:
            print(f"  {RED}x{RESET} {entry}")
        print(f"\n{YELLOW}A marked test, and every child process it spawns, "
              f"resolves the operator's real data overlay instead of a "
              f"tmp_path.{RESET}")
        print(f"{YELLOW}Use it only when the test's SUBJECT is that tree. A "
              f"test that merely resolves a path does not need one.{RESET}")

    if reasonless:
        print(f"\n{RED}{BOLD}{len(reasonless)} exemption(s) with no reason "
              f"beside them{RESET}")
        for entry, lineno in reasonless:
            print(f"  {RED}x{RESET} {entry}  (line {lineno})")
        print(f"\n{YELLOW}Put the reason in a comment on the marker's own line "
              f"or directly above it, at least {MIN_REASON_CHARS} "
              f"characters.{RESET}")

    return 1 if args.check else 0


if __name__ == "__main__":
    sys.exit(main())
