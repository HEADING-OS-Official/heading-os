#!/usr/bin/env python3
"""Keep the README / SECURITY front-door "By the numbers" figures honest (F-8.3).

README.md and SECURITY.md each carry an enforcement-layer count that must come
from the tree, not from a hand-typed guess. This guard derives that count from
the contract that enumerates the layers and asserts every front door agrees with
it and with the others.

The segregation contract lives in the data repository under the
registry name `spec-docs` (since 2026-09-25, HOS-131). On a clone with no data
repository (CI included) the guard holds the front doors against EACH OTHER and
says which of the two it did. It never falls back to an engine copy.

The pages checked are exactly `FRONT_DOORS`: README.md and SECURITY.md. Name a
page here in the SAME change that adds it to `FRONT_DOORS`, so a reader never
mistakes a checked page for an unchecked one.

Derived vs asserted:
  * enforcement-layer count: DERIVED by counting the numbered rows of the
    enforcement-layer table in the segregation contract (`spec-docs` in the data
    repository since 2026-09-25), which is the only page that enumerates the
    layers rather than quoting a numeral. Every page in `FRONT_DOORS` must agree
    with that count and with the others, which catches an accidental divergence
    between the front doors.
  * security-test count: REMOVED 2026-09-21 and deliberately not replaced;
    see the block below `derive_layer_count`.

Exit 0 when every figure matches; exit 1 (with a diff) on any mismatch.

Usage:
    python scripts/dev/check-readme-numbers.py            # check, exit non-zero on mismatch
    python scripts/dev/check-readme-numbers.py --quiet    # only print on mismatch
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils.colors import BOLD, GREEN, RED, RESET  # noqa: E402
from scripts.utils.data_path import data_path  # noqa: E402
from scripts.utils.workspace import get_workspace_root  # noqa: E402

ROOT = get_workspace_root()

# The engine/data enforcement layers, COUNTED from the numbered table in the
# segregation contract, which is the only page that enumerates them rather than
# quoting a numeral. The contract left the engine's docs/ on 2026-09-25
# (HOS-131) for `spec-docs` in the data repository.
#
# This was `EXPECTED_LAYERS = 6`, described in the docstring below as "a fixed
# architectural constant, not a fluctuating count". It fluctuated: the content
# guard became layer 7 on 2026-08-31, the contract and SECURITY.md were updated,
# and this guard went on asserting 6 against the two front doors that still said
# 6 -- so the guard reported "in sync" across a set that disagreed with the
# authority. A count a human must remember to update in two places is the defect.
_LAYER_ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|", re.MULTILINE)


def derive_layer_count() -> tuple[int | None, str]:
    """Count the numbered rows of the contract's enforcement-layer table.

    Returns `(count, where)`, or `(None, why)` when the contract cannot be
    reached: no data repository, or no contract in it. `main` then checks the
    front doors against each other and prints `why`, so a run that had no
    authority never reads like one that did.

    Refuses rather than guesses: the rows must be numbered 1..N with no gap, and
    there must be at least the six that existed in 2026-06. A table that stops
    parsing (a reformat, a blank line orphaning a row) collapses the count, and a
    silent low number here would quietly demand that every front door shrink.
    """
    try:
        contract = data_path("spec-docs") / "engine-data-segregation-contract.md"
    except Exception as exc:  # noqa: BLE001 - no data repository is a narrower check, not a crash
        return None, f"the data repository could not be resolved ({exc})"
    if not contract.is_file():
        return None, f"{contract} is absent"
    text = contract.read_text(encoding="utf-8")
    heading = text.find("## The ")
    body = text[heading:] if heading != -1 else text
    numbers = [int(m) for m in _LAYER_ROW_RE.findall(body)]
    if numbers != list(range(1, len(numbers) + 1)):
        raise SystemExit(
            f"{contract}: enforcement-layer rows are not numbered "
            f"1..N with no gap: {numbers}. Fix the table; this guard reads it as the "
            f"authority for every other page's numeral."
        )
    if len(numbers) < 6:
        raise SystemExit(
            f"{contract}: parsed only {len(numbers)} enforcement-layer "
            f"rows; the contract has held at least 6 since 2026-06. The table almost "
            f"certainly stopped parsing rather than shrinking."
        )
    return len(numbers), "the segregation contract in the data repository"


# ---------------------------------------------------------------------------
# REMOVED 2026-09-21: the "N security tests" front-door figure and its
# `pytest --collect-only` derivation. Do not restore it: a published count of
# test functions rewards adding tests, and the suite is deliberately reduced to
# the protection surface (HOS-54). If a security figure is ever wanted, derive
# which guarantees have a file, never the population of test functions.
# ---------------------------------------------------------------------------

# Front-door pages that must carry matching figures. A page joins only if it
# carries an enforcement-layer numeral; one that carries none trips the
# "carries neither figure" refusal below (why ROADMAP.md left on 2026-09-21).
FRONT_DOORS = [ROOT / "README.md", ROOT / "SECURITY.md"]

# Digits OR the spelled word. README.md:62 wrote "six enforcement layers" in
# prose while README.md:73 wrote "6" in a bullet, and a digits-only pattern read
# only the bullet -- so the same page could carry two different counts and pass.
_LAYER_WORDS = {
    "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
# The NOUN varies too. Measured 2026-08-31: with the pattern pinned to
# "enforcement layers", three pages carried a stale six in wordings it could not
# see -- "six mechanical layers" on README.md and docs/index.html, and "the six
# layers" in the contract's own change-control clause. All three sat beside a
# corrected seven and the guard reported the set in sync. An adjective is the
# cheapest thing for a writer to vary, so it must be the cheapest thing for the
# pattern to allow.
_LAYER_RE = re.compile(
    r"(\d+|" + "|".join(_LAYER_WORDS) + r")\s+(?:\w+\s+)?layers", re.IGNORECASE
)

def _extract(pattern: re.Pattern[str], text: str, path: Path, label: str) -> int | None:
    """The figure this page carries, or None if it does not carry one.

    A missing figure was not a defect while two figures were checked here: a page
    could carry one and not the other, and demanding both would make the page
    serve the guard rather than the reader. Since the security-test count was
    removed on 2026-09-21 there is only one figure left, so `None` from this
    function now means the page carries nothing at all. `main` refuses that
    separately, and the refusal matters more than it did: a watched page checked
    for nothing reads as covered while covering nothing.
    """
    matches = pattern.findall(text)
    if not matches:
        return None
    values = {_LAYER_WORDS.get(m.lower()) or int(m) for m in matches}
    if len(values) > 1:
        raise SystemExit(
            f"{RED}{path.relative_to(ROOT)}: inconsistent '{label}' figures {sorted(values)} "
            f"within the same page.{RESET}"
        )
    return values.pop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the front-door enforcement-layer figures against the segregation contract and each other")
    parser.add_argument("--quiet", action="store_true", help="Only print on mismatch")
    args = parser.parse_args()

    expected, authority = derive_layer_count()
    problems: list[str] = []
    stated: dict[str, int] = {}
    for path in FRONT_DOORS:
        text = path.read_text(encoding="utf-8")
        layers = _extract(_LAYER_RE, text, path, "enforcement layers")
        rel = path.relative_to(ROOT)
        if layers is None:
            problems.append(
                f"{rel}: carries no enforcement-layer figure, so listing it here "
                f"checks nothing. Add a figure or drop the page from FRONT_DOORS."
            )
            continue
        stated[str(rel)] = layers
        if expected is not None and layers != expected:
            problems.append(f"{rel}: says {layers} enforcement layers, expected {expected}")
    if expected is None and len(set(stated.values())) > 1:
        problems.append(
            f"the front doors disagree on the enforcement-layer count: {stated}"
        )

    watched = FRONT_DOORS
    if problems:
        print(f"{RED}{BOLD}Documented numbers out of sync:{RESET}", file=sys.stderr)
        for p in problems:
            print(f"  {RED}- {p}{RESET}", file=sys.stderr)
        print(
            f"\nFix the figure(s) in {', '.join(str(p.relative_to(ROOT)) for p in watched)} "
            f"to match, then re-run this guard.",
            file=sys.stderr,
        )
        return 1

    if expected is None:
        # Said even under --quiet: a check that ran without its authority is a
        # narrower check, and a silent pass would read as the full one.
        print(
            f"check-readme-numbers: authority not checked, {authority}; the front "
            f"doors were held against each other only."
        )
    if not args.quiet:
        figure = expected if expected is not None else next(iter(stated.values()))
        print(
            f"{GREEN}Documented numbers in sync: "
            f"{figure} enforcement layers across "
            f"{', '.join(str(p.relative_to(ROOT)) for p in watched)}"
            f"{', matching ' + authority if expected is not None else ''}.{RESET}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
