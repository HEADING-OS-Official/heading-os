#!/usr/bin/env python3
"""Nothing left the memory index without landing somewhere that still loads.

On 2026-09-20 the rules of conduct moved out of `MEMORY.md` (`memory-index`), which
the harness truncates from the END past 200 lines or 25,000 UTF-16 units and
tells nobody, into `CLAUDE.private.md` in the private overlay, reached by an `@`
import from `CLAUDE.operational.md`. MEASURED that day: the index stood at 179
lines and 25,207 units, so it was already losing its last three lines on every
load; the instruction chain that now carries the rules has no aggregate cap, and
its per-file threshold only prints a warning.

A move like that has one failure mode worth a gate: a rule that was in the index
yesterday is in neither file today, and nothing says so. This script answers that
one question from the archive written at the time of the move, and it answers it
by identifier rather than by prose, because prose was rewritten in the move and
identifiers were not.

Usage:
    python scripts/check-memory-preservation.py
    python scripts/check-memory-preservation.py --json
    python scripts/check-memory-preservation.py --skip-without-overlay

WHY THE FLOORS EXIST, and this is the part to read before editing anything here.
Every check below is a comparison between two derived sets, and a comparison
between two EMPTY sets passes. MEASURED 2026-09-20 against the validator shipped
with the optimization package this archive came from: emptying `budgets["files"]`
in a copy of its input made its whole size gate evaporate in silence -- `passed`,
zero errors, `file_sizes: {}`, combined size 0, exit 0. A package preserving
nothing scored identically to one preserving everything. That is the defect
`scripts/check-test-vacuity.py` exists to catch in this repository's tests, and a
gate is not exempt from it. So every corpus this script walks carries a floor
with the count measured on the day, asserted OUTSIDE the loop, and a floor that
fails is an error and never a warning.

What this script does NOT establish, stated here rather than left to be found.
It checks that an identifier survived, not that the sentence behind it still
means what it meant: the move rewrote wording, and only a human reading both
columns can settle that. It does not ask whether the policy file actually LOADS
-- that is `memory_health.py`'s reachability check and a live probe for the
marker, deliberately not a second copy here. And it reads the archive as given:
if the archive itself were wrong on the day it was written, this script inherits
that and cannot see it.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.colors import BOLD, CYAN, GRAY, GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils.paths import get_data_root  # noqa: E402
from scripts.utils.data_path import DataPathError, MissingRequiredSource, data_read_required  # noqa: E402

# ============================================================
# Configuration
# ============================================================

# The archived move this check guards, inside the registry's `memory-archive`.
# The directory is resolved by name; the entry below it is this check's own
# subject, one dated move, and names no address of its own.
ARCHIVE_ENTRY = "2026-09-20-index-to-policy"

# The marker the policy file carries so a live session can be ASKED whether the
# file arrived rather than assumed to hold it. Its absence is an error here
# because a probe that has nothing to look for reports success.
POLICY_LOAD_MARKER = "HEADING-PRIVATE-POLICY-LOADED-2026-09-20"

# Floors, all MEASURED 2026-09-20 on the archive written that day. Each is the
# number observed, not a round number chosen to be safe: a floor below the real
# count is a floor that passes over a corpus half its size.
FLOOR_SOURCE_ENTRIES = 149        # bullets in the pre-move index
FLOOR_RULE_ATOMS = 83             # M-identifiers the map assigns them to
FLOOR_ATOMS_IN_POLICY = 80        # of those 83, how many must be found in the file
CEILING_REVIEW_ONLY = 14          # entries demoted to review-only; more is a loss

# The five maintenance rules the package proposed are not the operator's directives.
# Three were adopted on his approval of 2026-09-20 and two were declined because
# this workspace already states them in its own words. Declining is recorded
# here so that a later run cannot read their absence as a defect, and adopting
# is recorded so that a silent deletion still fails.
ADOPTED_ADDITIONS = ("K001", "K003", "K005")
DECLINED_ADDITIONS = ("K002", "K004")

# Identifiers the map assigns that this workspace's policy file deliberately does
# NOT carry, each with the reason, one at a time. A bare list would be an
# allowlist that grows whenever a rule is inconvenient; the prose is the cost of
# adding a line, and it is meant to be.
RESOLVED_ATOMS = {
    "M046.3": "The package left 'retain that local restriction pending review' "
              "as its own atom because it could not see which restriction was "
              "meant. Here it is the pinned interpreter, carried in full by "
              "M046.2 (.venv Python 3.11, never a bare `python` or "
              "`sys.executable` for children). Nothing is pending.",
    "M047.2": "The package could not read `.claude/rules/craft-silent-failure.md` "
              "and so added 'verify that file before relying on it'. In this "
              "tree the file exists, is path-scoped, and loads itself on work "
              "touching tests, scripts, hooks, skills, agents, rules, config or "
              "reference. VERIFIED 2026-09-20 by reading it. The caveat "
              "described the package's blindness, not a property of the rule.",
}

ATOM_RE = re.compile(r"\[([MK]\d{3}(?:\.\d+)?)\]")


# ============================================================
# Loading
# ============================================================

def load_inputs(data_root: Path) -> dict:
    """Every file this check reads, or a dict naming the first one missing."""
    # A name the tree's registry does not carry is NOT a missing input: it is a
    # registry that cannot answer, and `--skip-without-overlay` must never read
    # it as "no overlay here", or the gate switches itself off on the one tree
    # it guards. Refused by `main()` with exit 2.
    #
    # All three names are MANDATORY sources, so they are read through the operation
    # that refuses their absence (HOS-5 section 4). Its refusal IS the missing
    # answer, naming every address it looked at, and it stays the `missing`
    # case so `--skip-without-overlay` still recognises a clone with no
    # overlay. Each is asked separately, so one absence does not hide another.
    # Spelled at each call, never looped over a variable: `leak-guard` can only
    # say which data a front-door call names when the name is a literal.
    missing: list[str] = []
    found: dict[str, Path] = {}
    try:
        found["archive"] = data_read_required("memory-archive", data_root=data_root)[0]
    except MissingRequiredSource as exc:
        missing.append(str(exc))
    except DataPathError as exc:
        return {"unresolved": str(exc)}
    try:
        found["index"] = data_read_required("memory-index", data_root=data_root)[0]
    except MissingRequiredSource as exc:
        missing.append(str(exc))
    except DataPathError as exc:
        return {"unresolved": str(exc)}
    try:
        found["policy"] = data_read_required("private-policy", data_root=data_root)[0]
    except MissingRequiredSource as exc:
        missing.append(str(exc))
    except DataPathError as exc:
        return {"unresolved": str(exc)}
    wanted: dict[str, Path] = {}
    if "policy" in found:
        wanted["policy"] = found["policy"]
    if "archive" in found:
        wanted["map"] = found["archive"] / ARCHIVE_ENTRY / "preservation-map.json"
        wanted["original"] = found["archive"] / ARCHIVE_ENTRY / "MEMORY-original.md"
    if "index" in found:
        wanted["index"] = found["index"]
    missing += [str(p) for p in wanted.values() if not p.is_file()]
    if missing:
        return {"missing": missing}
    return {
        "paths": wanted,
        "map": json.loads(wanted["map"].read_text(encoding="utf-8")),
        "original_bytes": wanted["original"].read_bytes(),
        "policy_text": wanted["policy"].read_text(encoding="utf-8"),
        "index_text": wanted["index"].read_text(encoding="utf-8"),
    }


# ============================================================
# Checks
# ============================================================

def check_archive_integrity(inputs: dict, errors: list) -> None:
    """The archived snapshot is the one the map was written against."""
    claimed = inputs["map"].get("source_sha256")
    actual = hashlib.sha256(inputs["original_bytes"]).hexdigest()
    if not claimed:
        errors.append("preservation-map.json carries no source_sha256, so the "
                      "archived snapshot is not pinned to anything.")
    elif claimed != actual:
        errors.append(f"MEMORY-original.md does not match the map's pin: "
                      f"archive {actual}, map claims {claimed}.")


def check_floors(inputs: dict, errors: list, counts: dict) -> None:
    """Refuse a corpus that collapsed, before anything is compared against it.

    This runs FIRST and its failures are fatal, because every check after it is
    a set comparison and an empty set satisfies one.
    """
    m = inputs["map"]
    counts["source_entries"] = len(m.get("source_entries") or [])
    counts["rule_atoms"] = len(m.get("compact_rule_atoms") or [])
    counts["review_only"] = int(m.get("review_only_entries") or 0)

    if counts["source_entries"] < FLOOR_SOURCE_ENTRIES:
        errors.append(f"source_entries is {counts['source_entries']}, below the "
                      f"floor of {FLOOR_SOURCE_ENTRIES} measured 2026-09-20. "
                      f"Every check below compares sets, and a shrunken corpus "
                      f"passes them all.")
    if counts["rule_atoms"] < FLOOR_RULE_ATOMS:
        errors.append(f"compact_rule_atoms is {counts['rule_atoms']}, below the "
                      f"floor of {FLOOR_RULE_ATOMS} measured 2026-09-20.")
    if counts["review_only"] > CEILING_REVIEW_ONLY:
        errors.append(f"review_only_entries is {counts['review_only']}, above "
                      f"the ceiling of {CEILING_REVIEW_ONLY}. Demoting more "
                      f"entries than the move demoted is a loss of rules, and "
                      f"it is silent.")


def check_every_source_entry_has_a_home(inputs: dict, errors: list,
                                        counts: dict) -> None:
    """No bullet from the pre-move index vanished.

    A bullet is accounted for when the map gives it at least one destination.
    An entry with an empty destination list is the exact shape this whole
    exercise exists to prevent, so it is named one by one rather than counted.
    """
    homeless = [e.get("id", "<no id>") for e in inputs["map"]["source_entries"]
                if not e.get("destinations") and e.get("status") != "review-only"]
    counts["homeless_source_entries"] = len(homeless)
    counts["review_only_named"] = sum(1 for e in inputs["map"]["source_entries"]
                                      if e.get("status") == "review-only")
    # A review-only entry is accounted for BY BEING NAMED as one, and the map
    # must name as many as it counts. Two numbers that can disagree are how a
    # demotion goes quiet: the count stays 14 while the list empties.
    if counts["review_only_named"] != counts["review_only"]:
        errors.append(f"the map counts {counts['review_only']} review-only "
                      f"entries and names {counts['review_only_named']}. A "
                      f"demoted rule that is counted but not named cannot be "
                      f"found again.")
    if homeless:
        shown = ", ".join(homeless[:12])
        more = "" if len(homeless) <= 12 else f" (+{len(homeless) - 12} more)"
        errors.append(f"{len(homeless)} source entr(ies) have no destination at "
                      f"all: {shown}{more}.")


def check_atoms_reached_the_policy(inputs: dict, errors: list,
                                   counts: dict) -> None:
    """Each identifier the map assigns is present in the file that now loads."""
    present = set(ATOM_RE.findall(inputs["policy_text"]))
    counts["atoms_in_policy"] = len(present)

    declared = [a.get("id") for a in inputs["map"]["compact_rule_atoms"]]
    # An atom may be carried under its parent's identifier when the move merged
    # two atoms into one line; that is why the parent counts as coverage.
    def _covered(atom):
        return (atom in present or atom.split(".")[0] in present
                or atom in RESOLVED_ATOMS)

    missing = [a for a in declared if not _covered(a)]
    counts["atoms_missing_from_policy"] = len(missing)
    counts["atoms_resolved_elsewhere"] = len(
        [a for a in declared
         if a in RESOLVED_ATOMS and a not in present
         and a.split(".")[0] not in present])
    if missing:
        shown = ", ".join(missing[:12])
        more = "" if len(missing) <= 12 else f" (+{len(missing) - 12} more)"
        errors.append(f"{len(missing)} rule identifier(s) in the map are in "
                      f"neither the policy file nor a parent of one, and are "
                      f"not recorded in RESOLVED_ATOMS with a reason: "
                      f"{shown}{more}.")

    # A resolved entry that no longer names a real atom is a comment guarding
    # nothing, and it would sit here unnoticed forever.
    stale = [a for a in RESOLVED_ATOMS if a not in declared]
    if stale:
        errors.append(f"RESOLVED_ATOMS names {', '.join(sorted(stale))}, which "
                      f"the map does not declare. An exemption guarding nothing "
                      f"is a claim nobody re-reads.")

    found = len([a for a in declared if _covered(a)])
    if found < FLOOR_ATOMS_IN_POLICY:
        errors.append(f"only {found} of the map's rule identifiers were found in "
                      f"the policy file, below the floor of "
                      f"{FLOOR_ATOMS_IN_POLICY} measured 2026-09-20.")


def check_maintenance_additions(inputs: dict, errors: list) -> None:
    """The three adopted additions are present and the two declined are not.

    Both halves matter. A missing adopted rule is a rule that stopped applying;
    a present declined rule is this workspace being handed an instruction its
    operator did not give, which `K001` is itself about.
    """
    present = set(ATOM_RE.findall(inputs["policy_text"]))
    for k in ADOPTED_ADDITIONS:
        if k not in present:
            errors.append(f"{k} was adopted on 2026-09-20 and is not in the "
                          f"policy file.")
    for k in DECLINED_ADDITIONS:
        if k in present:
            errors.append(f"{k} was declined on 2026-09-20 and must not appear "
                          f"in the policy file as an instruction.")


def check_load_marker(inputs: dict, errors: list) -> None:
    """The probe has something to look for."""
    if POLICY_LOAD_MARKER not in inputs["policy_text"]:
        errors.append(f"the policy file carries no loading marker "
                      f"({POLICY_LOAD_MARKER}), so a live session cannot be "
                      f"asked whether it arrived and a probe for it would "
                      f"report a false negative forever.")


def check_index_did_not_grow_back(inputs: dict, warnings: list,
                                  counts: dict) -> None:
    """The index is a router now; rules belong on the uncapped channel.

    A warning and not an error: the index legitimately keeps a small number of
    rules whose violation cannot be undone, deliberately duplicated against the
    day the hook that serves a YARD fails. What it must not do is quietly
    reacquire the whole set.
    """
    text = inputs["index_text"]
    counts["index_lines"] = text.count("\n") + 1
    counts["index_utf16"] = len(text.encode("utf-16-le")) // 2
    counts["index_bullets"] = sum(1 for line in text.split("\n")
                                  if line.startswith("- "))
    if counts["index_utf16"] > 25000:
        warnings.append(f"the index is {counts['index_utf16']} UTF-16 units, "
                        f"over the harness budget of 25000, so the harness is "
                        f"truncating its end and saying nothing.")
    elif counts["index_bullets"] > 40:
        warnings.append(f"the index carries {counts['index_bullets']} bullets. "
                        f"It was cut to a router plus the irreversible rules on "
                        f"2026-09-20; check it is not collecting rules again.")


# ============================================================
# Output
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true",
                        help="machine-readable result on stdout")
    parser.add_argument("--skip-without-overlay", action="store_true",
                        help="exit 0 when the private overlay is absent, as on "
                             "a public clone, instead of reporting its files "
                             "missing")
    args = parser.parse_args()

    data_root = Path(get_data_root())
    inputs = load_inputs(data_root)
    if "unresolved" in inputs:
        print(f"{RED}{BOLD}REFUSED{RESET} - a registry name did not resolve, so "
              f"nothing was checked: {inputs['unresolved']}", file=sys.stderr)
        return 2

    if "missing" in inputs:
        if args.skip_without_overlay:
            if args.json:
                print(json.dumps({"skipped": True, "missing": inputs["missing"]}))
            else:
                print(f"{GRAY}skipped: no private overlay here "
                      f"({len(inputs['missing'])} file(s) absent){RESET}")
            return 0
        if args.json:
            print(json.dumps({"passed": False, "missing": inputs["missing"]}))
        else:
            print(f"{RED}{BOLD}BLOCKED{RESET} - the preservation archive or the "
                  f"files it describes are not here:")
            for p in inputs["missing"]:
                print(f"  {p}")
            print(f"{GRAY}Pass --skip-without-overlay on a clone with no private "
                  f"overlay.{RESET}")
        return 1

    errors: list = []
    warnings: list = []
    counts: dict = {}

    check_archive_integrity(inputs, errors)
    check_floors(inputs, errors, counts)
    if errors:
        # Everything below compares sets derived from the corpus the floors just
        # refused. Running them would print reassuring zeros.
        _report(args, False, errors, warnings, counts)
        return 1

    check_every_source_entry_has_a_home(inputs, errors, counts)
    check_atoms_reached_the_policy(inputs, errors, counts)
    check_maintenance_additions(inputs, errors)
    check_load_marker(inputs, errors)
    check_index_did_not_grow_back(inputs, warnings, counts)

    _report(args, not errors, errors, warnings, counts)
    return 1 if errors else 0


def _report(args, passed: bool, errors: list, warnings: list,
            counts: dict) -> None:
    if args.json:
        print(json.dumps({"passed": passed, "errors": errors,
                          "warnings": warnings, "counts": counts}, indent=2))
        return
    print(f"{BOLD}{CYAN}Memory preservation{RESET} "
          f"{GRAY}index -> CLAUDE.private.md, 2026-09-20{RESET}")
    for key in ("source_entries", "rule_atoms", "atoms_in_policy",
                "review_only", "index_lines", "index_utf16", "index_bullets"):
        if key in counts:
            print(f"  {GRAY}{key:<24}{RESET} {counts[key]}")
    for w in warnings:
        print(f"  {YELLOW}warning{RESET} {w}")
    for e in errors:
        print(f"  {RED}error{RESET}   {e}")
    if passed:
        print(f"{GREEN}OK{RESET} - every rule the index carried is accounted for.")
    else:
        print(f"{RED}{BOLD}BLOCKED{RESET} - {len(errors)} error(s) above.")


if __name__ == "__main__":
    sys.exit(main())
