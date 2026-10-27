#!/usr/bin/env python3
"""Classification health check - audit workspace file classification.

Walks a NAMED tree and resolves each file through `get_routing_destination()`,
the resolver every publish, push and commit gate in this workspace actually
asks. Two values, `engine` / `private`, because those are the two the
resolver returns; a map that still declares the retired `corporate` (HOS-132)
fails this check. Reports summary stats per tree and flags paths no rule
governs.

Usage:
    python scripts/classification-health.py                  # both trees
    python scripts/classification-health.py --tree engine    # this checkout only
    python scripts/classification-health.py --tree data      # the private overlay only
    python scripts/classification-health.py --json           # JSON output
    python scripts/classification-health.py --unclassified   # only paths with no rule

WHICH TREE, AND WHY IT IS AN ARGUMENT. Until 2026-09-23 this walked
`get_workspace_root()` and nothing else, which is the ENGINE checkout, while the
routing map's whole purpose is the split between that checkout and the private
data overlay beside it. One number cannot describe two trees, so the default walks BOTH and reports them separately. A
clone with no separate overlay (`data_overlay_present()` is False: a public
clone, CI) walks the engine alone and says so on its own line rather than
printing a total that silently means half of what it says.

The report counts the resolver's own values, never a collapsed scale: a
health check that answers confidently in the reassuring direction is worse than
one that refuses (2026-09-23). Two past defects rest on review, not a check: a
`main()` that returned nothing, and a walk that counted files git ignores.
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.workspace import (
    LEGAL_DESTINATIONS, RETIRED_DESTINATIONS, RoutingRefused,
    data_overlay_present, get_data_root, get_workspace_root,
    get_routing_destination, load_routing_map, matched_routing_rule,
)
from scripts.utils.colors import GRAY, GREEN, RED, CYAN, BOLD, RESET
from scripts.utils.repo_files import not_ignored

# Directories to skip entirely.
#
# This set is a SPEED filter now, not the correctness boundary. git decides what
# is ignored, below; these names are pruned before git is asked because walking
# them is slow and pointless. Removing a name from here changes no verdict, only
# the time taken. Adding one CAN hide a tracked file, so add nothing that git
# tracks.
SKIP_DIRS = {
    ".git", ".sync", ".sentinel", ".sessions", "__pycache__",
    "node_modules", "chrome-profile",
}

# File patterns to skip
SKIP_FILES = {".DS_Store", "Thumbs.db", ".gitignore", ".gitattributes"}


def walk_workspace(root: Path) -> list[str]:
    """Every file in the workspace that git does not ignore, relative to root.

    The hand-written skip list above cannot know what `.gitignore` says, and the
    gap was not theoretical. MEASURED 2026-08-29 on this repository: this sweep
    returned 2363 files and git ignores 427 of them, eighteen percent. Among
    them `.claude/settings.local.json`, a stale `.bak-...~` file, the marp
    web-font binaries and a scratch `.marp-src-*.md`. The operator reads this
    report to judge whether the engine/data split is holding, and every one of
    those rows is a file no split decision applies to.

    The `.claude` carve-out below is what made it worst. Hidden directories are
    skipped except `.claude`, and `.claude/worktrees/` is where agent worktrees
    are checked out. A worktree is a full second copy of the repository, so
    while one exists this sweep would count the whole tree twice and classify
    every file in the copy.

    `git check-ignore` is the only thing that knows the answer, and
    `scripts/utils/repo_files.py` RAISES when git cannot answer rather than
    degrading to "nothing is ignored" -- that degradation is the silent failure
    this call exists to prevent.
    """
    walked = []
    for item in sorted(root.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(root)
        parts = rel.parts
        # Prune the expensive subtrees before asking git (speed only).
        if any(part in SKIP_DIRS for part in parts):
            continue
        # Skip hidden directories (except .claude)
        if any(part.startswith(".") and part != ".claude" for part in parts[:-1]):
            continue
        # Skip specific files
        if rel.name in SKIP_FILES:
            continue
        walked.append(item)

    kept = not_ignored(walked, root)
    return [str(p.relative_to(root)).replace("\\", "/") for p in kept]


# The resolver's own answer set, in the order a reader should scan it: widest
# destination first. The same two as `LEGAL_DESTINATIONS`, imported from the
# resolver below, so the two cannot drift apart.
DESTINATIONS = ("engine", "private")

# Which root each tree name means. Resolved through the seams in
# `scripts/utils/workspace.py`, never by joining a constant onto a checkout, so
# a caller that redirects one redirects both.
TREE_ROOTS = {"engine": get_workspace_root, "data": get_data_root}


def tree_root(tree: str) -> Path:
    """The root `tree` names. Raises on a name the resolver does not know."""
    try:
        return TREE_ROOTS[tree]()
    except KeyError:
        raise ValueError(
            f"tree must be one of {sorted(TREE_ROOTS)}, got {tree!r}") from None


def classify_files(root: Path, tree: str) -> dict:
    """Classify every file under `root` as a member of `tree`, and return results.

    THE TREE IS HALF OF THE QUESTION and it is passed explicitly, never
    inferred. `get_routing_destination()`'s own docstring records why: the rule
    keys are path shaped, the data overlay uses some of the same top-level names
    the engine does, and MEASURED 2026-09-13 nineteen overlay files resolved
    `engine` when asked as a bare relative path. A report is the last place that
    inference belongs, because it is asked precisely when somebody doubts the
    split.

    Three lists. `engine` and `private` partition every walked file by the
    resolver's own two values. `unclassified` OVERLAPS them rather than
    replacing a slice of any: it re-reports whichever files matched no rule at
    all and took the tree's default.

    Which bucket a default-taker lands in is a property of the TREE and not of
    this function. Under the engine checkout the default is `engine`; under the
    data overlay it is `private`. Both are read from the map, so this docstring
    states no number that could drift out of step with it.

    "Unclassified" is a real, askable question: it means the routing map matched
    no rule and the path took a default. `matched_routing_rule` answers it, and
    is asked with the same `tree=` so the two halves cannot disagree.

    The second `tree=` changes no answer for relative paths today (MEASURED
    2026-09-23); it is passed so the two calls stay one question asked twice.
    """
    files = walk_workspace(root)
    buckets: dict[str, list[str]] = {d: [] for d in DESTINATIONS}
    unclassified = []

    for f in files:
        # A refusal is the contract's third answer (HOS-232): an engine file
        # outside the allowlist, a nested repository, a link into the data
        # root. It gets a bucket named for its reason, never `private`.
        try:
            dest = get_routing_destination(f, tree=tree)
        except RoutingRefused as exc:
            dest = f"refused ({exc.reason})"
        if matched_routing_rule(f, tree=tree) is None:
            unclassified.append(f)
        # A destination outside the two is the map answering with something no
        # consumer handles. Reported as its own bucket rather than dropped: a
        # file silently missing from every count is the failure mode this whole
        # report exists to catch.
        buckets.setdefault(dest, []).append(f)

    return {
        "tree": tree,
        "root": str(root),
        "total": len(files),
        "buckets": buckets,
        "unclassified": unclassified,
    }


def _tree_heading(results: dict) -> str:
    """The line that names the tree a block of counts is about.

    Every printing path calls this, so no run of this report can produce a
    number without the tree beside it. That is the defect of 2026-09-22 in one
    sentence: the counts were right about a tree the output never named.
    """
    return (f"  {BOLD}{results['tree']} tree{RESET}  {GRAY}{results['root']}{RESET}")


def print_report(results_by_tree: list[dict], notes: list[str]):
    """Print the colored terminal report, one block per tree walked."""
    print(f"\n{BOLD}Classification Health Report{RESET}")
    print(f"{'=' * 60}")
    for results in results_by_tree:
        print(_tree_heading(results))
        print(f"    Total files:  {results['total']}")
        for dest in DESTINATIONS:
            colour = {"engine": GREEN, "private": CYAN}[dest]
            print(f"    {colour}{dest:<11}{len(results['buckets'][dest]):>6}{RESET}")
        for dest, files in sorted(results["buckets"].items()):
            if dest not in DESTINATIONS:
                print(f"    {RED}{dest:<11}{len(files):>6}  "
                      f"refused by the routing contract, or not a destination "
                      f"any consumer handles{RESET}")
        # Reported, not alarmed. Taking a tree default is the DESIGNED outcome
        # for shareable engine code (`.claude/rules/classification.md`: "A new
        # file left at the engine default needs no entry only when it is
        # genuinely shareable code"), so most of this count is correct and
        # colouring it red would make every healthy run look broken. What the
        # number is for is the inverse: a path that should NOT take the default
        # has no rule saying so.
        if results["tree"] == "data":
            # No data path has a rule since HOS-232: the data repository is
            # private by repository, so this count is the whole tree by design.
            print(f"    {GRAY}private by repository, no per-path rule "
                  f"(HOS-232): {len(results['unclassified'])}{RESET}")
        else:
            print(f"    {GRAY}no explicit rule (took the {results['tree']} default): "
                  f"{len(results['unclassified'])}{RESET}  "
                  f"{GREEN}--unclassified to list{RESET}")
        print()
    for note in notes:
        print(f"  {GRAY}not walked: {note}{RESET}")
    if notes:
        print()


def print_unclassified(results_by_tree: list[dict], notes: list[str]):
    """Print the paths no routing rule governs, grouped by top-level directory.

    This is a REVIEW list, not a defect list. A default is the right answer for
    most paths in the tree it defaults in, so the grouped counts come first: the
    question worth asking is whether any GROUP here should have taken a
    different destination, not whether each of two thousand engine files needs a
    line in the map.
    """
    m = load_routing_map()
    defaults = {"engine": m.get("default", "private"),
                "data": m.get("data_default", "private")}
    for results in results_by_tree:
        unclassified = sorted(results["unclassified"])
        default = defaults[results["tree"]]
        print(f"\n{BOLD}Paths with no explicit routing rule "
              f"({len(unclassified)}){RESET}")
        print(_tree_heading(results))
        print(f"{'-' * 60}")
        if not unclassified:
            print(f"  {GREEN}none: every scanned path matches a rule{RESET}\n")
            continue
        print(f"  All of these take the {results['tree']} default: "
              f"{CYAN}{default}{RESET}.")
        print(f"  {GRAY}Normal where that default is the right answer. Check "
              f"whether any group below should not be {default}.{RESET}\n")
        groups: dict[str, int] = {}
        for f in unclassified:
            head = f.split("/", 1)[0] if "/" in f else "(tree root)"
            groups[head] = groups.get(head, 0) + 1
        for head, count in sorted(groups.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>5}  {head}")
        print(f"\n{GRAY}  Full list:{RESET}")
        for f in unclassified:
            print(f"    {f}")
        print(f"\n  To pin one: add its path under `rules:` in the routing map "
              f"for its tree, then re-run this check.\n")
    for note in notes:
        print(f"  {GRAY}not walked: {note}{RESET}")


def print_json(results_by_tree: list[dict], notes: list[str]):
    """Print JSON output, keyed by tree.

    The old `corporate_count` / `ceo_only_count` keys left on 2026-09-23 with
    the two-value collapse they named.
    """
    output = {
        "trees": {
            results["tree"]: {
                "root": results["root"],
                "total": results["total"],
                "counts": {d: len(f) for d, f in sorted(results["buckets"].items())},
                "files": {d: sorted(f) for d, f in sorted(results["buckets"].items())},
                "unclassified_count": len(results["unclassified"]),
                "unclassified_files": sorted(results["unclassified"]),
            }
            for results in results_by_tree
        },
        "not_walked": notes,
    }
    print(json.dumps(output, indent=2))


def routing_map_problems() -> list[str]:
    """Problems with `config/routing-map.yaml`, as operator-readable lines.

    `load_routing_map()` FAILS CLOSED, which is right for a resolver and
    invisible here: a missing file, an unreadable stat, or unparseable YAML all
    return `{"default": "private", "rules": {}}` and say nothing. This report
    then classifies every file against an empty map and prints a full-looking
    summary with no hint that the map never loaded.

    A health check for classification is the one place that state has to be
    named. Anything this returns is a failure, not a warning: the resolver every
    other gate in the workspace calls is not answering from the map on disk.
    """
    problems: list[str] = []
    path = get_workspace_root() / "config" / "routing-map.yaml"
    if not path.is_file():
        problems.append(f"routing map is not a file: {path}")
        return problems

    m = load_routing_map()
    if not m.get("rules"):
        problems.append(
            "routing map carries no rules, so every path takes the default; "
            "this is what a failed load looks like from the outside"
        )
        return problems

    # The map IN EFFECT never carries an illegal destination, because the
    # loader coerces one to 'private' and moves on. That coercion is correct
    # (a typo must not let a CEO subtree fall through to the public default)
    # and it is announced only on stderr, which nothing reads. So the check
    # that matters is not "is the effective map legal" -- it always is -- but
    # "does the map ON DISK say something the loader had to silently rewrite".
    # A one-character typo in one value reclassifies a whole subtree, and this
    # report is where an operator would look for it.
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        problems.append(f"routing map does not parse: {exc}")
        return problems
    if not isinstance(raw, dict):
        problems.append(
            f"routing map is a {type(raw).__name__}, not a mapping"
        )
        return problems

    raw_default = raw.get("default", "private")
    if raw_default not in LEGAL_DESTINATIONS:
        problems.append(
            f"routing map on disk says default: {raw_default!r}, which is not "
            f"one of {sorted(LEGAL_DESTINATIONS)}; the loader silently used "
            f"{m['default']!r} instead"
        )
    raw_data_default = raw.get("data_default", "private")
    if raw_data_default not in LEGAL_DESTINATIONS:
        problems.append(
            f"routing map on disk says data_default: {raw_data_default!r}, "
            f"which is not one of {sorted(LEGAL_DESTINATIONS)}"
            f"{_retired(raw_data_default)}; the loader silently used 'private' "
            f"instead"
        )
    raw_rules = raw.get("rules") or {}
    if isinstance(raw_rules, dict):
        for key, value in sorted(raw_rules.items()):
            if value not in LEGAL_DESTINATIONS:
                problems.append(
                    f"routing map on disk gives {key!r} the destination "
                    f"{value!r}, which is not one of "
                    f"{sorted(LEGAL_DESTINATIONS)}{_retired(value)}; the loader "
                    f"silently used 'private' instead"
                )
    return problems


def _retired(value) -> str:
    """The clause naming a retired destination, or nothing."""
    if isinstance(value, str) and value in RETIRED_DESTINATIONS:
        return f" (`{value}` was retired on 2026-09-25, HOS-132)"
    return ""


def trees_requested(choice: str) -> tuple[list[str], list[str]]:
    """The trees to walk for `choice`, and a note per tree that cannot be walked.

    `both` is the default and degrades: a clone with no separate data overlay
    (a public clone, CI) has no second tree, so it walks the engine and carries
    a note. `data` on such a clone does NOT degrade: the caller asked for one
    specific tree and there is nothing to answer with, so `main` refuses.
    Returning an empty list is how that reaches the caller, and the floor there
    is what turns it into a refusal.
    """
    if choice == "engine":
        return ["engine"], []
    missing = ("this clone has no separate data overlay "
               "(data_overlay_present() is False), so there is no data tree "
               "to walk")
    if choice == "data":
        return ([], [f"data: {missing}"]) if not data_overlay_present() else (["data"], [])
    if not data_overlay_present():
        return ["engine"], [f"data: {missing}"]
    return ["engine", "data"], []


def main() -> int:
    parser = argparse.ArgumentParser(description="Classification health check")
    parser.add_argument(
        "--tree", choices=("engine", "data", "both"), default="both",
        help="which tree to walk: the engine checkout, the private data "
             "overlay, or both with separate totals (default: both)",
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--unclassified", action="store_true", help="List unclassified files only")
    args = parser.parse_args()

    # Every branch below resolves paths through the routing map, so an
    # unloadable map makes every verdict meaningless. Checked first, on stderr
    # so `--json` stays parseable, and it is a REFUSAL rather than a warning.
    problems = routing_map_problems()
    if problems:
        for line in problems:
            print(f"{RED}[FAIL]{RESET} {line}", file=sys.stderr)
        return 1

    trees, notes = trees_requested(args.tree)
    if not trees:
        for note in notes:
            print(f"{RED}[FAIL]{RESET} nothing was walked. {note}", file=sys.stderr)
        return 1

    results_by_tree = []
    for tree in trees:
        root = tree_root(tree)
        try:
            results = classify_files(root, tree)
        except RuntimeError as exc:
            # `not_ignored` raises when git cannot say what it ignores, which is
            # the one failure that must never degrade into "nothing is ignored".
            # Named per tree rather than left as a traceback, because a report
            # that dies halfway has already printed counts for the tree before
            # it and a reader takes those for the whole answer.
            print(f"{RED}[FAIL]{RESET} the {tree} tree at {root} could not be "
                  f"walked: {exc}", file=sys.stderr)
            return 1

        # A PASS OVER AN EMPTY CORPUS IS NOT A PASS, and the floor names the
        # tree and the root because standing on "a walk happened" is what let
        # the 2026-09-22 run report confidently about the wrong tree. Zero files
        # here means the root is not where this tree lives: the counts would all
        # print a clean-looking zero and the reader would have no line to check
        # them against.
        if not results["total"]:
            print(f"{RED}[FAIL]{RESET} the walk over the {tree} tree at {root} "
                  f"returned 0 files, so nothing was classified; a pass over an "
                  f"empty corpus is not a pass", file=sys.stderr)
            return 1
        results_by_tree.append(results)

    if args.json:
        print_json(results_by_tree, notes)
    elif args.unclassified:
        print_unclassified(results_by_tree, notes)
    else:
        print_report(results_by_tree, notes)
    return 0


if __name__ == "__main__":
    # `main()` used to end without a return and the call here discarded it, so
    # the CI step named "Classification health" could not fail on any input. A
    # deliberately corrupted routing map produced a full report and exit 0.
    sys.exit(main())
