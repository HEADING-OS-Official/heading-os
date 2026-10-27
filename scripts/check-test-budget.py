#!/usr/bin/env python3
"""Three ceilings over this repository's test code, and every one of them may only fall.

WHY A CEILING EXISTS AT ALL, WHICH IS THE WHOLE DIAGNOSIS IN ONE PARAGRAPH.
MEASURED 2026-09-21 on this tree, before the HOS-54 split: THREE checks REQUIRED
test files to exist (`_MIN_TEST_FILES = 800` in the day-mode blind-spot test,
`MIN_CORPUS = 400` in `scripts/check-test-vacuity.py` and the same constant in
`scripts/check-live-data-root.py`) and nine ratchets under `config/` permitted
growth and forbade shrinkage. There was no ceiling anywhere in the tree. So the
accumulation from 228 test files to 1380 in twelve weeks, 24 a day, 437 120
lines of test code against 236 422 lines of the code it tests, was not a lapse
of discipline. The tree said, in code, that removal was forbidden and addition
was free. A rule written in prose sets the rate; these numbers set the
DIRECTION, and the direction is what produced the 380.16 s push gate the
operator could not work through.

WHY THREE NUMBERS AND NOT ONE. One ceiling moves the growth to the next
dimension. A files-only cap turns 24 new files a day into 24 new functions a
day, and a files-and-functions cap turns it into functions that grow without
bound. The three together leave nowhere for it to go.

WHAT IS COUNTED, AND WHY IT IS EVERY `.py` UNDER `tests/` RATHER THAN THE
`test_*.py` THIS REPOSITORY'S OTHER CHECKS WALK. A ceiling counting only files
named `test_*.py` is escaped by putting the code in a helper module beside them,
and `tests/conftest.py` alone stood at 126 829 bytes on 2026-09-21, which is
larger than most of the files it supports. A boundary a sibling file walks
around is not a boundary.

THE RAISE IS THE POINT, AND IT IS NOT HELD HERE. Lowering is free: this hook
rewrites the file down to the measured truth and asks you to re-stage it, which
is the ordinary pre-commit idiom. A RAISE is refused by
`scripts/check-test-budget-raise.py` at the `commit-msg` stage, because a raise
has to be visible in something an agent cannot quietly produce, and the commit
message is where this repository already puts that kind of marker.

NOT CLAIMED. This counts bytes and syntax, never value. It cannot tell a test
that earns its place from one that does not, and it is not evidence that the
tests which remain are good ones. It also says nothing about the parked suite,
which is not in this repository and is not counted; `config/parked-tests.json`
holds that number.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.test_tree import LIVE  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BUDGET_REL = "config/test-budget.json"
CEILINGS = ("test_files", "test_functions", "test_code_lines")


def _python_files(root: Path) -> list[Path]:
    """Every `.py` under the live test tree, sorted. Files only."""
    base = root / LIVE
    if not base.is_dir():
        return []
    return sorted(
        p for p in base.rglob("*.py")
        if p.is_file() and "__pycache__" not in p.parts
    )


def _test_functions(path: Path) -> int:
    """`def test_*` in one file, at module level or inside a class.

    ASKS THE AST, NOT THE TEXT, per obligation 8 of the Evidence Standard. A
    substring scan for `def test_` goes red the moment a docstring or a comment
    quotes the pattern to explain it, which teaches people to stop explaining.
    A file that does not parse counts 0 and is reported, never skipped in
    silence.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"),
                         filename=str(path))
    except SyntaxError:
        return -1
    return sum(
        1 for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )


def _code_lines(path: Path) -> int:
    """Lines of test CODE: no blanks, no comments, no docstrings.

    A CEILING THAT COUNTS COMMENTS PUNISHES EXPLANATION, and this workspace
    already names that failure from the other direction: obligation 8 of the
    Evidence Standard exists because a substring scan goes red the moment a fix
    quotes the bad pattern to explain it, "which teaches people to stop
    explaining". A line budget over raw lines teaches exactly the same thing,
    and the docstrings in these files ARE the record of what each guarantee is
    and what it does not claim.

    MEASURED 2026-09-21, and this is not hypothetical: the first commit after
    this gate went in was refused over ten lines of COMMENT added to
    `tests/conftest.py`, explaining why a stale exemption was being kept rather
    than narrowed. Raising the ceiling for a comment would have been the first
    negotiation, on day one, which is the failure mode the ceiling exists to
    stop. Narrowing what is counted was the honest repair, and it leaves the
    file and function ceilings untouched.

    A file that does not parse falls back to non-blank lines and is reported by
    the caller, never skipped in silence.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return sum(1 for line in text.splitlines() if line.strip())

    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            docstring_lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))

    count = 0
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or number in docstring_lines:
            continue
        count += 1
    return count


def measure(root: Path) -> dict[str, int]:
    """The three numbers, from the tree as it stands now."""
    files = _python_files(root)
    lines = 0
    functions = 0
    unparseable: list[str] = []
    for path in files:
        lines += _code_lines(path)
        count = _test_functions(path)
        if count < 0:
            unparseable.append(str(path.relative_to(root)))
        else:
            functions += count
    if unparseable:
        print("test-budget: these files do not parse, so their test functions "
              "are uncounted and the function ceiling is measuring LESS than "
              "the tree holds: " + ", ".join(unparseable), file=sys.stderr)
    return {
        "test_files": len(files),
        "test_functions": functions,
        "test_code_lines": lines,
    }


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: dict) -> None:
    """Atomic, per the workspace's persistence rule: tmp file then replace."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="enforce the ceilings; lower them when the tree is below")
    parser.add_argument("--measure", action="store_true",
                        help="print the three numbers and exit 0")
    parser.add_argument("--seed", action="store_true",
                        help="write the ceilings from the tree as it stands now")
    args = parser.parse_args(argv)

    budget_path = ROOT / BUDGET_REL
    now = measure(ROOT)

    if args.measure:
        print(json.dumps(now, indent=2))
        return 0

    if args.seed:
        payload = _load(budget_path) if budget_path.exists() else {}
        payload.update(now)
        _write(budget_path, payload)
        print(f"test-budget: seeded {BUDGET_REL} from the tree: "
              + ", ".join(f"{k}={now[k]}" for k in CEILINGS))
        return 0

    if not budget_path.exists():
        print(f"test-budget: {BUDGET_REL} does not exist, so there is no "
              f"ceiling and this gate is checking nothing. Run "
              f"`.venv/bin/python scripts/check-test-budget.py --seed`.",
              file=sys.stderr)
        return 1

    ceilings = _load(budget_path)
    missing = [k for k in CEILINGS if not isinstance(ceilings.get(k), int)]
    if missing:
        print(f"test-budget: {BUDGET_REL} carries no integer ceiling for "
              f"{', '.join(missing)}. A ceiling that is absent is not a ceiling "
              f"at zero, it is a gate checking nothing, so this REFUSES rather "
              f"than passing.", file=sys.stderr)
        return 1

    over = [(k, now[k], ceilings[k]) for k in CEILINGS if now[k] > ceilings[k]]
    if over:
        print("test-budget: REFUSED. The test code exceeds its ceiling.",
              file=sys.stderr)
        for key, actual, ceiling in over:
            print(f"  {key}: {actual} > {ceiling} (over by {actual - ceiling})",
                  file=sys.stderr)
        print("\nThe ceiling is not raised to fit the commit. Either the new "
              "test belongs to failure A (a private byte reaching this public "
              "repository) or failure B (the two repositories merging, or a "
              "yard acting on something live), in which case fold it into the "
              "file that already holds that guarantee as a new test FUNCTION; "
              "or it does not, in which case it ships as a `Fix-Note:` line in "
              "the commit message instead. See .claude/rules/"
              "development-standards.md, Phase 5.\n"
              "\nIf the operator has decided the ceiling must rise, he raises "
              f"it in {BUDGET_REL} and the commit message carries the marker "
              "that scripts/check-test-budget-raise.py requires.",
              file=sys.stderr)
        return 1

    under = [k for k in CEILINGS if now[k] < ceilings[k]]
    if under:
        ceilings.update({k: now[k] for k in under})
        _write(budget_path, ceilings)
        print("test-budget: the tree is below its ceiling, so the ceiling came "
              "down with it. Re-stage " + BUDGET_REL + ":", file=sys.stderr)
        for key in under:
            print(f"  {key}: now {now[key]}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
