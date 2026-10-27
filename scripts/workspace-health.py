#!/usr/bin/env python3
"""
Workspace Health Check for HEADING OS

Validates file references, context freshness, agent counts,
pipeline health and people completeness. An outputs inventory section ran
here until 2026-09-25; HOS-5 section 6 removed it, because the legacy outputs
directory had drained to the HEADING OS 2 move's own working directory.

Usage:
    python scripts/workspace-health.py                    # full health check
    python scripts/workspace-health.py --section context  # run one section
    python scripts/workspace-health.py --section refs     # check references only
    python scripts/workspace-health.py --section counts   # check agent counts
    python scripts/workspace-health.py --section pipeline # check pipeline health
    python scripts/workspace-health.py --section people   # check people completeness
    python scripts/workspace-health.py --section datastore # check datastore status

Sections (12): refs, context, counts, pipeline, people, datastore, homes,
docs-sync, skill-router, doc-versions, daemon-state, extras.
"""

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.colors import GREEN, YELLOW, RED, CYAN, BOLD, RESET
from scripts.utils.data_path import (DataPathError, MissingRequiredSource, data_path, data_read,
                                     data_read_required)
from scripts.utils.repo_files import ignored_paths_or_none
from scripts.utils.workspace import (
    get_workspace_root, get_templates_dir, get_data_root, get_default_tz,
)
from scripts.utils.data_homes import homes as data_homes, resolve as resolve_name

WORKSPACE = get_workspace_root()

COMMANDS_DIR = WORKSPACE / ".claude" / "commands"
SKILLS_DIR = WORKSPACE / ".claude" / "skills"


# Fan-out: the context and datastore records sit in several homes and no single
# registry name covers a directory, so those sections walk registry groups
# (`context-homes`, `datastore-homes`) rather than one name (HOS-240,
# 2026-10-13). Every section degrades on an absent record and returns its own
# issue count; a raise would cost its verdict through `run_one`.


# The runtime catalog of every script, reference file and system. It lives in
# the private data overlay, so it is absent on a public engine clone; resolved
# through the data-root seam, never as a literal.
#
# This is the PRE-MOVE spelling. It stopped being the seam's `legacy=` argument
# on 2026-09-22, when `reference_index_path()` moved to the registry name
# `workspace-overview`, and it is kept for the one remaining use: the "not
# present" warning prints it as the documentation form, and on a clone with no
# overlay it is also the address the front door answers. Never join it onto the
# data root directly: call `reference_index_path()` below.
REFERENCE_INDEX_RELPATH = Path("reference") / "workspace-overview.md"


def reference_index_path() -> Path:
    """The reference index, by its registry name `workspace-overview`."""
    return data_path("workspace-overview")


def ok(msg):
    print(f"  {GREEN}OK{RESET}  {msg}")


def warn(msg):
    print(f"  {YELLOW}WARN{RESET}  {msg}")


def action(msg):
    print(f"  {RED}ACTION{RESET}  {msg}")


def header(title):
    print(f"\n{BOLD}{CYAN}=== {title} ==={RESET}")


# One backticked token. The index writes paths inline in prose and bullets, not
# in a table, so nothing here may key off `|`.
_BACKTICK_TOKEN = re.compile(r"`([^`\s]+)`")

# A workspace-relative file: at least one directory component, a dotted suffix,
# and only characters a real path uses. The leading class excludes `/`, `~`,
# `<` and `{`, which is what drops absolute paths, home paths and the
# `<data-root>/...` documentation form.
_RELATIVE_FILE = re.compile(
    r"^[A-Za-z0-9_.][A-Za-z0-9._/-]*/[A-Za-z0-9._-]+\.[A-Za-z0-9]{1,5}$")

# Path-shaped but not a literal path: a parent traversal in an example, or a
# date stamp standing in for the run that will produce the file.
_NOT_A_LITERAL_PATH = re.compile(r"\.\.|YYYY|MM-DD|HH-MM")


# Two registries of paths whose absence is not a documentation defect. Both are
# EXACT tokens, never shapes: a rule of the form "ignore anything that looks
# like an example" widens silently, and the whole point of this section is that
# its findings can be believed. Neither may accumulate cover for a reference
# nobody writes any more. An entry the live index no longer names used to fail a
# run of its own, kept out of this section because the section also runs over
# scratch fixtures and would judge every one of them against the operator's
# index. That run was parked in the 2026-09-21 test-suite split, so both
# registries below can now accumulate cover for references nobody writes, and
# the first symptom is a finding that quietly stops being reported.

# A stand-in inside a sentence ABOUT paths, naming no real file.
PLACEHOLDER_PATHS = {
    "scripts/x.py":
        "stand-in for any root-relative script in the shell-drift guard's "
        "description; names no script that has ever existed",
    ".claude/hooks/x.py":
        "the same stand-in, in the same sentence, for the hooks directory",
}

# A path whose absence is its normal state, and which no root's ignore rules
# exclude, so `_ignored_by_every_root` cannot derive it. Anything git already
# ignores belongs there and must NOT be duplicated here. Two shapes qualify: a
# file written only while something runs, and an override the resolver that
# reads it documents as normally absent.
ABSENT_BY_DESIGN_PATHS = {
    # The two `outputs/` keys carry `leak-guard: ok` because this dict is a
    # REGISTRY OF STRINGS, not a path builder. Both are only ever the right
    # operand of `in` (lines 419 and 423) or a message lookup (line 463); no
    # branch joins either to a root, which is the construction the guard
    # exists to stop. They are the comparison-key case its own suppression
    # comment names.
    "outputs/operations/handoff.md":  # leak-guard: ok (comparison key)
        "written only when a session hands off, read by /prime at the next "
        "start, then moved into the handoff-archive records (resolve the name "
        "with scripts/data-path.py path handoff-archive)",
    "outputs/browser/browser-cdp.json":  # leak-guard: ok (comparison key)
        "the CDP endpoint scripts/browser.py writes while a browser is "
        "attached, and removes when it is not",
    "config/claude-models.json":
        "the optional first step of the Claude model resolution order, which "
        "the index itself records as normally absent; the cache and the live "
        "API answer when it is",
    ".claude/ralph-loop.local.md":
        "written by the ralph-loop plugin for the duration of a loop and "
        "removed on every terminal path it has",
}

# Paths that are absent because the thing was DELETED, and are named on purpose
# in a record that says so. A different class from ABSENT_BY_DESIGN_PATHS above,
# which is about files that come and go at runtime.
#
# Why this exists. The reference index is a catalogue, and a catalogue that
# silently drops a retired subsystem is how someone re-creates it. So a removal
# gets a bullet naming what went, in which commit, and what carries the property
# now. Every one of those bullets then read here as a MISSING reference: measured
# 2026-09-02, 24 of the 27 flagged paths were removal records, and each one is a
# red ACTION line the operator can do nothing about. A check whose findings are
# mostly unactionable is a check that gets skimmed, and the three real ones were
# sitting in the same list.
#
# Hand-maintained on purpose, and narrowly. The alternative considered was a
# regex over the sentence around the path ("RETIRED", "was removed", ...), which
# is a guess about prose: it would suppress a live path in a sentence that merely
# mentions a deletion, and miss a removal record worded any other way. A named
# path with a stated reason is a claim someone made and can be held to, and the
# floor below holds it: an entry whose path EXISTS again fails, so this registry
# cannot outlive its sites.
REMOVED_AND_RECORDED_PATHS = {
    "config/classification.json":
        "replaced by config/routing-map.yaml, removed 2026-06-14",
    "corporate/requirements.txt":  # leak-guard: ok (a removal record keyed by the deleted path; the revival check refuses if it exists again)
        "the executive workspace's copy-into-workspace path; the copy step "
        "was deleted, and HOS-130 removed the executive model 2026-09-25",
    "docs/SETUP-GUIDE.md":
        "retired doc; its links were repointed to docs/DEPLOYMENT.md",
    "reference/31c-docs-dark-theme.css":
        "removed with the setup-guide builder",
    "scripts/build-setup-guide-html.py":
        "removed with docs/SETUP-GUIDE.md and the dark theme",
    "scripts/canary-smoke.py":
        "deleted 2026-08-23 with the canary gate (22e6997)",
    "scripts/depth-gate.py":
        "deleted 2026-08-07 with the depth calibration (28ce7e4)",
    "scripts/eval-drift-daemon.py":
        "retired 2026-08-03 (58aa77d); its input set was empty by construction",
    "scripts/export-sync.py":
        "left with the AIOS segregation, 2026-04-25",
    "scripts/promote-corporate.py":
        "deleted 2026-08-23 with the canary gate (22e6997)",
    "scripts/rollback-corporate.py":
        "deleted 2026-08-23 with the canary gate (22e6997)",
    "scripts/scrutinize-fp-aggregate.py":
        "removed 2026-08-09 with its _fp_aggregate.md",
    "scripts/slice-cycle-time.py":
        "deleted 2026-08-07; it read a ledger nothing writes (70e10bb)",
    "scripts/slice-depth.py":
        "deleted 2026-08-07 with the depth calibration (28ce7e4)",
    "scripts/slice-rollback.py":
        "deleted 2026-08-07; it read a ledger nothing writes (70e10bb)",
    "scripts/sync-all-execs.py":
        "no-op stub removed 2026-08-20 (0ce4506)",
    "scripts/utils/canopus_freeze.py":
        "deleted 2026-08-07 with the freeze lifecycle (28ce7e4)",
    "scripts/utils/canopus_gate.py":
        "deleted 2026-08-07 with the freeze lifecycle (8520623)",
    "scripts/utils/slice_depth.py":
        "deleted 2026-08-07 with the depth calibration (28ce7e4)",
    "scripts/workspace-sync.py":
        "the copy-and-orphan-delete sync engine, removed 2026-06-26",
    "tests/test_canary_branch_switch.py":
        "deleted 2026-08-23 with scripts/canary-smoke.py (22e6997)",
    "tests/test_eval_drift_aggregation.py":
        "deleted 2026-08-03 with the eval-drift daemon (58aa77d)",
    "tests/test_slice_rollback.py":
        "deleted 2026-08-07 with scripts/slice-rollback.py (70e10bb)",
    "config/audit-rotation-ledger.json":
        "moved to the data repository 2026-10-16, registry name "
        "audit-rotation-ledger",
    "config/automation-hold.json":
        "deleted 2026-10-16 in the engine audit; nothing read it",
    "scripts/install-memory-auto-retire-timer.sh":
        "deleted 2026-10-16 with scripts/memory-auto-retire.py",
    "scripts/memory-auto-retire.py":
        "deleted 2026-10-16 on the operator's word; memory is never "
        "retired on a clock",
    "scripts/utils/firefox_cookies.py":
        "deleted 2026-10-16 in the engine audit; no caller was left",
    "scripts/utils/lemonade.py":
        "deleted 2026-10-16 in the engine audit; no caller was left",
}


def _resolves_under(path_str: str, roots) -> bool:
    """Does `path_str` name a file that exists under any of `roots`?

    Two forms, because the index writes both. The plain form is relative to a
    root (`scripts/send-email.py`, or a record under the data root). The other is written
    from the directory that HOLDS the roots, and appears wherever the index has
    to distinguish the two stores by repo (`<data repo dir>/<store>/index.db`
    beside `.heading-os/.memory-index-code/index.db`). The leading
    component is compared against each root's OWN directory name, so a clone
    that lives somewhere else, or under a different name, still resolves; no
    repo name is written down here.
    """
    head, _, tail = path_str.partition("/")
    for root in roots:
        if (root / path_str).exists():
            return True
        if tail and head == root.name and (root / tail).exists():
            return True
    return False


def _ignored_by_every_root(paths, roots) -> set:
    """The subset of `paths` that EVERY root's ignore rules exclude.

    A runtime artefact is derived here rather than listed: a path no root would
    ever track is a file something writes while it runs, and its absence is the
    normal state, not an action item. A daemon's `daemon.pid` and
    `.daemon-state/config.yaml` are the shape.

    EVERY root, never any root. The engine ignores whole DATA directories -
    `plans/`, `outputs`, the top-level `datastore`, `threads/` - because they
    belong to the overlay, not because anything about them is runtime. MEASURED
    2026-09-02 over this index: the union rule matched 28 paths and the
    intersection 3, and 25 of the 28 were the archived-plan and stale-thread
    references this section exists to report. A union here would have deleted
    the findings instead of the noise.

    Fails toward over-reporting: if git cannot answer (not installed, not a
    repository), nothing is excluded and the paths stay in the report.
    """
    if not paths:
        return set()
    excluded = set(paths)
    for root in roots:
        try:
            ignored = ignored_paths_or_none(sorted(paths), root)
        except OSError:
            # `ignored_paths_or_none` reports git's own non-verdict exit codes
            # as None, but it cannot report a git that never STARTED (not on
            # PATH), which surfaces here as FileNotFoundError. So this branch
            # stays. The old `subprocess.SubprocessError` half is dropped: the
            # shared helper passes no `timeout=`, so TimeoutExpired cannot
            # arise, and it passes `check=False`, so neither can
            # CalledProcessError. That does drop the 30s bound this call used to
            # carry, which is the same unbounded shape the two other callers of
            # the helper already run with over the whole tree.
            return set()
        # None = git could not answer (not a repository, bad option). The two
        # verdict exits, 0 (some path ignored) and 1 (none), come back as a set.
        if ignored is None:
            return set()
        excluded &= ignored
        if not excluded:
            return set()
    return excluded


def _archived_plan(path_str: str, plan_homes):
    """Where an active-plan reference went, if it was archived.

    `.claude/rules/documentation.md` § Plans Lifecycle makes archiving the
    normal end of every plan: it is `git mv`'d into a `<year>/` directory of the
    archive beside the active plans. The
    file still exists and the index's pointer at it no longer does, which is a
    real finding with a known fix, so this returns the destination rather than
    suppressing it. Only a pointer whose first segment is `plans`, followed by
    one file name, is considered; one already under `archive/` is left alone.

    `plan_homes` pairs each home with the root it was derived from, so the
    destination is reported relative to the root it was found under.
    """
    head, _, tail = path_str.partition("/")
    if head != "plans" or not tail or "/" in tail:
        return None
    for root, home in plan_homes:
        archive = home / "archive"
        if not archive.is_dir():
            continue
        for year in sorted(archive.iterdir()):
            if (year / tail).exists():
                return (year / tail).relative_to(root).as_posix()
    return None


# Sections that RAN but verified nothing: an absent target, an empty corpus. A
# zero from one of these means "no answer", not "clean". Without this the
# summary folds the two together and prints "Section 'refs' passed." one line
# under the section saying it checked 0 paths, which is the vacuous-pass shape
# `.claude/rules/scope-claims.md` names, one level up from the section itself.
INCONCLUSIVE: list[str] = []


def inconclusive(section: str, why: str) -> None:
    """Record that `section` produced no verdict. Never an issue count."""
    INCONCLUSIVE.append(f"{section}: {why}")


def _reference_index_paths(text: str):
    """Split the index's backticked tokens into literal paths and skips.

    Returns (paths, skipped). `skipped` is the count of tokens that look like a
    path but cannot be existence-checked, so the report can say what it left
    out rather than imply it covered everything (`.claude/rules/scope-claims.md`).
    """
    literal, skipped = set(), set()
    for token in _BACKTICK_TOKEN.findall(text):
        tail = token.rsplit("/", 1)[-1]
        if "/" not in token or "." not in tail:
            # A bare filename, a flag, a config key, ordinary code prose. The
            # index is full of these (`.env`, `SKILL.md`, `push-all.py`); they
            # name no directory, so no root can resolve them.
            continue
        if _RELATIVE_FILE.match(token) and not _NOT_A_LITERAL_PATH.search(token):
            literal.add(token)
        else:
            skipped.add(token)
    return sorted(literal), len(skipped)


def check_reference_validation():
    """Check that the paths named in the reference index exist.

    The index is the data overlay's runtime catalog of every script, reference
    file and system, resolved through the data-root seam.

    History, because the previous target is a trap worth not re-entering. This
    check read the ENGINE CLAUDE.md, looking for a "Reference Resources" table
    that file has never carried (`git log -S` finds no commit that added one),
    so the loop ran zero times and the section printed an unconditional green,
    "All reference paths resolve to existing files", over zero paths. Anchoring
    that scan at a markdown HEADING rather than a bare substring was itself a
    fix: `"Reference Resources" in line` fired on any prose sentence carrying
    the phrase, and the flag then stayed on until the next `## `, so unrelated
    table rows were existence-checked and failed the run. That reasoning still
    binds anything that scans for a named section, and it is why nothing below
    matches a heading or a phrase: the whole index file IS the reference
    section, so there is no section to find and no substring to guess at.

    Zero is never a pass, and since 2026-09-02 it is not a zero either. A
    present, readable index over which this section inspected NOTHING is a
    checker that stopped reading, so it REFUSES: one issue, non-zero exit, the
    floor `scripts/ste-check.py` and `scripts/validate-crm-schema.py` already
    carry over their own corpora. The narrower outcome above it - no index at
    all, which is the normal state of a public clone with no data overlay - is
    deliberately NOT folded into that floor. There is no corpus to have read, and
    exiting 1 on every public clone would retire the tool rather than arm it. It
    stays inconclusive, which the summary already refuses to print as clean.
    """
    header("Reference Validation")

    data_root = get_data_root()
    index = reference_index_path()
    if not index.exists():
        # First-class outcome, not an edge case: a public engine clone has no
        # data overlay, so the index is simply absent. A missing overlay is not
        # a missing file (the sibling `check_docs_sync` treats templates/ the
        # same way).
        warn(f"reference index not present at "
             f"<data-root>/{REFERENCE_INDEX_RELPATH.as_posix()}; "
             f"0 paths checked (this section verified nothing). Every path it "
             f"would name is not checkable on this clone, never missing")
        inconclusive("refs", "no reference index (no data overlay on this clone)")
        return 0

    try:
        index_text = index.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # `main`'s loop was UNGUARDED when this was written, so a read that
        # raised here did not fail one section: it ended the run with a
        # traceback and no summary at all. `run_one` now catches it too, and
        # this handler still earns its place: it names the FILE and keeps the
        # section's own verdict, where `run_one` can only say the section
        # died. That was MEASURED on 2026-09-01 in
        # `check_context_freshness` and fixed there; four sibling reads in this
        # same file, this one included, kept the defect. Reported as an issue
        # rather than skipped, because a reference check that could not open the
        # index has not found the references sound.
        action(f"reference index could not be read ({type(exc).__name__}); "
               f"0 paths checked")
        inconclusive("refs", "the reference index could not be read")
        return 1

    paths, skipped = _reference_index_paths(index_text)

    # Which root each path is relative to. The workspace is two layered roots,
    # and the index names files in both: `scripts/send-email.py` is engine,
    # the `pipeline` record is data overlay. A path resolves if EITHER root
    # holds it; it is missing only when neither does. Deriving the root from
    # `get_routing_destination()` instead was measured and rejected: routing is
    # a sharing decision, not a location, so `corporate` files such as
    # `docs/GETTING-STARTED.md` physically sit in the overlay and 11 real index
    # paths resolved in the root opposite the one routing named. Those 11 would
    # print as "Missing:" every run, which is how a useful check gets switched
    # off. The cost of the union rule is that it cannot detect a file filed
    # under the wrong root; that is `scripts/classification-health.py`'s job.
    roots = [WORKSPACE]
    if data_root != WORKSPACE:
        roots.append(data_root)

    if not paths:
        action("the reference index names no workspace-relative file paths; "
               "0 paths checked, 0 inspected (this section verified nothing "
               "and refuses to report clean)")
        inconclusive("refs", "the reference index names no paths")
        return 1

    unresolved = [p for p in paths if not _resolves_under(p, roots)]
    resolved = len(paths) - len(unresolved)

    placeholder = [p for p in unresolved if p in PLACEHOLDER_PATHS]
    by_design = [p for p in unresolved if p in ABSENT_BY_DESIGN_PATHS]
    recorded = [p for p in unresolved if p in REMOVED_AND_RECORDED_PATHS]
    rest = [p for p in unresolved
            if p not in PLACEHOLDER_PATHS
            and p not in ABSENT_BY_DESIGN_PATHS
            and p not in REMOVED_AND_RECORDED_PATHS]
    runtime = sorted(_ignored_by_every_root(rest, roots))
    rest = [p for p in rest if p not in set(runtime)]

    # The registry may not outlive its sites. A path listed as removed that now
    # EXISTS again is either a re-creation nobody updated the record for, or an
    # entry added to silence a finding that was real. Both are worse than the
    # noise this registry removes, so both fail here rather than passing quietly.
    revived = sorted(p for p in REMOVED_AND_RECORDED_PATHS
                     if _resolves_under(p, roots))
    for path_str in revived:
        action(f"Recorded as removed, but present again: {path_str} - "
               f"{REMOVED_AND_RECORDED_PATHS[path_str]}. Update the record and "
               f"drop the REMOVED_AND_RECORDED_PATHS entry.")

    # Where an archived plan can be, resolved ONCE for this section and paired
    # with the root each home is reported relative to. The loop this replaces
    # ran inside `_archived_plan` and asked the migration seam per path per
    # root; splitting it here is what let the overlay pass go to the front door
    # while the ENGINE clone's `plans/` stays an ordinary engine path. The
    # front door has no address for the engine clone and is not handed one.
    plan_homes = [(root, root / "plans") for root in roots if root != data_root]
    if data_root in roots:
        try:
            plan_homes += [(data_root, home) for home in data_read("plans")]
        except DataPathError as exc:
            # Loud, and NOT `return 1`. The three front-door reads elsewhere in
            # this file end their section because it cannot proceed without the
            # file they name; this one is a refinement on top of a verdict the
            # section can still produce. Losing it downgrades "Moved:" to
            # "Missing:", which is precisely the silent half this function was
            # written to replace, so the downgrade is announced as an issue and
            # the rest of the section still reports.
            action(f"the archived-plan homes under the data root could not be "
                   f"resolved ({type(exc).__name__}: {exc}); an index pointer "
                   f"at an archived plan will print as Missing, not Moved")

    moved = [(p, dest) for p in rest if (dest := _archived_plan(p, plan_homes))]
    missing = [p for p in rest if p not in {p for p, _ in moved}]

    for path_str, dest in moved:
        action(f"Moved: {path_str} is now {dest}")
    for path_str in missing:
        action(f"Missing: {path_str}")

    # Inspected means the existence question was actually ASKED of that path.
    # An excluded path was never asked, so it cannot be counted as coverage -
    # which is what makes the floor below reachable rather than decorative: an
    # index whose every path landed in an exclusion bucket has been read by a
    # section that settled nothing.
    inspected = (len(paths) - len(placeholder) - len(by_design)
                 - len(recorded) - len(runtime))
    flagged = len(moved) + len(missing) + len(revived)

    if skipped:
        ok(f"{skipped} path-shaped token(s) skipped as globs, placeholders, "
           f"absolute or home-relative; not checked")
    for path_str in sorted(placeholder):
        ok(f"placeholder, not checked: {path_str} - {PLACEHOLDER_PATHS[path_str]}")
    for path_str in sorted(by_design):
        ok(f"absent by design, not checked: {path_str} - "
           f"{ABSENT_BY_DESIGN_PATHS[path_str]}")
    for path_str in runtime:
        ok(f"absent by design, not checked: {path_str} - ignored by every "
           f"root in this workspace, so nothing ever tracks it")
    for path_str in sorted(recorded):
        ok(f"removed and recorded, not checked: {path_str} - "
           f"{REMOVED_AND_RECORDED_PATHS[path_str]}")

    # Coverage beside verdict, both out of the same arithmetic: a section that
    # stopped reading cannot look clean, because the count it inspected is
    # printed next to the count it flagged (`.claude/rules/scope-claims.md`).
    # Every excluded bucket is named with its size for the same reason - silence
    # about an exclusion reads as coverage.
    coverage = (f"{resolved} of {len(paths)} reference path(s) resolve; "
                f"{inspected} inspected, {flagged} flagged "
                f"({len(moved)} moved, {len(missing)} missing, "
                f"{len(revived)} recorded-but-present); excluded: "
                f"{len(runtime) + len(by_design)} absent by design, "
                f"{len(recorded)} removed and recorded, "
                f"{len(placeholder)} prose placeholder(s)")
    if inspected == 0:
        action(f"{coverage}; this section verified nothing and refuses to "
               f"report clean")
        inconclusive("refs", "every reference path was excluded or unsettled")
        return max(flagged, 1)
    (warn if flagged else ok)(coverage)
    return flagged


def check_context_freshness(max_days=30):
    """Check freshness markers on context files."""
    header("Context Freshness")
    issues = 0
    today = datetime.now(get_default_tz())

    # Every home the context records went to, not one directory: they fan out
    # across several homes and two records carry no registry name, so an
    # enumeration of names would report them fresh by omission. C3: the
    # context homes and files by registry name, so a home or a file the
    # subject-tree move carried away is still checked.
    context_files = sorted(
        {f for home in data_homes("context-homes")  # HOS-240: the group alone, the manifest is no longer asked
         for f in home.glob("*.md")}
        | {f for f in data_homes("context-files") if f.is_file()})
    if not context_files:
        action("No context files found!")
        return 1

    for f in sorted(context_files):
        try:
            content = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # No handler at all sat here until 2026-09-01. This is a HEALTH
            # CHECK, and it ran before `/push-updates`. MEASURED that day on
            # two context files, one clean and one carrying a lone 0xe9: it
            # reported on the clean file, then died on the next one with a
            # traceback naming a codec, a byte and an offset but no filename,
            # so every later context file went unchecked and the run produced
            # no verdict at all.
            #
            # Counted as an issue rather than skipped quietly: a freshness
            # check that cannot read a file has not found it fresh.
            action(f"{f.name}: could not be read ({type(exc).__name__}), so "
                   f"its freshness is unknown")
            issues += 1
            continue
        lines = content.split("\n")[:10] if content else []

        # Check for freshness marker: > Last verified|updated: YYYY-MM-DD (first 10 lines).
        # Both verbs count as a freshness signal — both carry a date stamp; the
        # workspace convention uses "Verified" (re-confirmed) and "Updated" (content changed).
        match = None
        for line in lines:
            match = re.match(r">\s*Last (?:verified|updated):\s*(\d{4}-\d{2}-\d{2})", line, re.IGNORECASE)
            if match:
                break
        if match:
            # The regex validates digit SHAPE, never the calendar, so
            # `> Last verified: 2026-02-31` reached `strptime` and raised
            # ValueError. `main` ran these checks in an unguarded loop when
            # this was written, so one malformed marker aborted every remaining
            # section with a traceback and no summary, in front of
            # `/push-updates`. `run_one` now catches that; this handler stays
            # because it names the marker rather than only the section. The sibling
            # `check_doc_versions` wraps the identical parse; this one was
            # missed. A date nobody can parse is an unverified file, not a
            # crash, so it counts as an issue and the run continues.
            try:
                verified_date = datetime.strptime(
                    match.group(1), "%Y-%m-%d").replace(tzinfo=get_default_tz())
            except ValueError:
                warn(f"{f.name}: malformed freshness date {match.group(1)}; "
                     f"freshness NOT checked")
                issues += 1
                continue
            age_days = (today - verified_date).days
            if age_days > max_days:
                warn(f"{f.name}: Last verified {age_days} days ago ({match.group(1)})")
                issues += 1
            else:
                ok(f"{f.name}: Verified {age_days} days ago ({match.group(1)})")
        else:
            # Fall back to file modification time
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=get_default_tz())
            age_days = (today - mtime).days
            if age_days > max_days:
                warn(f"{f.name}: No freshness marker, modified {age_days} days ago")
                issues += 1
            else:
                warn(f"{f.name}: No freshness marker (modified {age_days}d ago - add one)")

    return issues


def check_agent_counts():
    """Count the commands and skills on disk, and flag lowercase `skill.md`.

    Scope, stated because the old docstring and the old section title overstated
    it: this opens nothing and compares nothing. It counts `.claude/commands/*.md`
    and the `.claude/skills/` directories carrying a SKILL.md, prints both lists,
    and returns one issue per skill whose manifest is lowercase. The docstring
    said "compare to CLAUDE.md" and the header said "Verification" while no line
    in the function reads CLAUDE.md, so a CLAUDE.md claiming 22 commands over a
    tree of 19 passed green under a title promising the opposite
    (`.claude/rules/scope-claims.md`). Naming the count is the whole method, so
    the title now says so.
    """
    header("Agent Count (commands and skills found on disk)")
    issues = 0

    # Count actual commands
    commands = list(COMMANDS_DIR.glob("*.md")) if COMMANDS_DIR.exists() else []
    command_count = len(commands)

    # Count actual skills
    skills = [d for d in SKILLS_DIR.iterdir() if d.is_dir() and ((d / "SKILL.md").exists() or (d / "skill.md").exists())] if SKILLS_DIR.exists() else []
    skill_count = len(skills)

    ok(f"Commands found: {command_count} in .claude/commands/")
    ok(f"Skills found: {skill_count} in .claude/skills/")

    # List them
    print(f"\n  Commands: {', '.join(sorted(f.stem for f in commands))}")
    print(f"  Skills: {', '.join(sorted(d.name for d in skills))}")

    # Check for lowercase skill.md (should be SKILL.md)
    for d in skills:
        if (d / "skill.md").exists() and not (d / "SKILL.md").exists():
            warn(f"{d.name}/skill.md should be SKILL.md (inconsistent naming)")
            issues += 1

    return issues


def check_pipeline_health():
    """Parse pipeline.md for stale deals and missing data."""
    header("Pipeline Health")
    issues = 0

    # The handler keeps this section's own verdict: `run_one` would turn a
    # front-door refusal into "section could not run". A mandatory source,
    # read through the operation that refuses its absence (HOS-5 section 4).
    try:
        pipeline_file = data_read_required("pipeline")[0]
    except MissingRequiredSource as exc:
        action(f"pipeline.md not found! {exc}")
        return 1
    except DataPathError as exc:
        action(f"pipeline.md address could not be resolved "
               f"({type(exc).__name__}); pipeline health NOT checked")
        return 1

    try:
        content = pipeline_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # See `check_reference_validation` above: `main`'s loop was unguarded
        # when this was written, so raising here ended the whole health run
        # with no verdict. `run_one` now catches it; this handler stays because
        # it names the file. pipeline.md is
        # operator-authored data in the private overlay, which is precisely
        # where a stray non-UTF-8 byte arrives from (a paste out of Word, a
        # Latin-1 company name).
        action(f"pipeline.md could not be read ({type(exc).__name__}); "
               f"pipeline health NOT checked")
        return 1

    # The comment here used to promise TBD, placeholder AND empty fields.
    # `placeholder_count` was assigned and never read, nothing anywhere looked
    # at empty cells, and the TBD number was a whole-file substring count
    # printed as "N TBD fields". Same class the sibling
    # `check_people_completeness` already carries a fix-comment for: say what
    # the method counted, and count the things the comment promises.
    tbd_count = content.lower().count("tbd")
    next_action_missing = content.lower().count("[next action]")

    # Table cells, so "placeholder" and "empty" mean something. A row is
    # `| a | b | c |`; the cells are what sits between the pipes.
    table_lines = [ln for ln in content.split("\n")
                   if ln.strip().startswith("|") and "---" not in ln]
    cells = [c.strip() for ln in table_lines for c in ln.strip().strip("|").split("|")]
    placeholder_cells = [c for c in cells if re.fullmatch(r"\[[^\]]*\]", c)]
    empty_cells = [c for c in cells if c == ""]

    if tbd_count > 0:
        warn(f"{tbd_count} 'TBD' occurrence(s) anywhere in pipeline.md "
             f"(a whole-file substring count, not a field count)")
        issues += 1
    if next_action_missing > 0:
        warn(f"{next_action_missing} missing next actions in pipeline.md")
        issues += 1
    if placeholder_cells:
        warn(f"{len(placeholder_cells)} table cell(s) hold only a "
             f"[placeholder] in pipeline.md")
        issues += 1
    if empty_cells:
        warn(f"{len(empty_cells)} empty table cell(s) in pipeline.md")
        issues += 1

    # Check file size (thin pipeline is a signal)
    size = pipeline_file.stat().st_size
    if size < 3000:
        warn(f"pipeline.md is thin ({size} bytes) - may need enrichment")
    else:
        ok(f"pipeline.md size: {size} bytes")

    # Count table rows (rough deal count)
    table_rows = [line for line in content.split("\n") if line.startswith("|") and "---" not in line and "Company" not in line and "Investor" not in line]
    deal_count = len([r for r in table_rows if r.strip() != "|"])
    ok(f"Approximately {deal_count} entries in pipeline tables")

    return issues


def check_people_completeness():
    """Parse people.md for incomplete entries."""
    header("People Completeness")
    issues = 0

    # Same handler shape and reason as `check_pipeline_health` above.
    try:
        people_file = data_read_required("people")[0]
    except MissingRequiredSource as exc:
        action(f"people.md not found! {exc}")
        return 1
    except DataPathError as exc:
        action(f"people.md address could not be resolved "
               f"({type(exc).__name__}); people completeness NOT checked")
        return 1

    try:
        content = people_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # Same class and same file as the two above. people.md holds names, so
        # it is the single likeliest context file to carry a byte that is not
        # valid UTF-8.
        action(f"people.md could not be read ({type(exc).__name__}); "
               f"people completeness NOT checked")
        return 1

    # Check for placeholder patterns
    add_patterns = re.findall(r"\[Add[^\]]*\]", content)
    if add_patterns:
        warn(f"{len(add_patterns)} placeholder fields ([Add ...]) in people.md")
        issues += 1

    # Say what this counts. It is a whole-file substring count with no column or
    # field context, so a "TBD" in a role, a company or a next-step column lands
    # here too -- calling every hit a missing EMAIL was a claim the method never
    # established.
    placeholders = content.lower().count("tbd") + content.lower().count("[email]")
    if placeholders > 0:
        warn(f"{placeholders} 'TBD' or '[email]' placeholder(s) anywhere in "
             f"people.md (not necessarily in an email column)")
        issues += 1

    size = people_file.stat().st_size
    ok(f"people.md size: {size} bytes")

    return issues


def check_datastore():
    """Check DataStore status, across every home the datastore fanned out to.

    The old top-level `datastore` directory is the fan-out shape, not the
    hundred-per-cent-move shape:
    MEASURED 2026-09-18 on the operator's overlay, `moved_homes` resolved EIGHT
    directories for it - the legacy `<data>/datastore` (7 files left behind) plus
    `asset/brand`, `build/runbook`, `("canon", "reference")`, `inbound/capture`, (leak-guard: ok, history)
    `inbound/document`, `research/dossier` and `work/deliverable`. The eight
    expected subdirectories sit one per home: `brand/` under `asset/brand` (206 (leak-guard: ok, history)
    files), `content/` and `corporate/` under `work/deliverable` (109 and 4),
    `events/` under `inbound/capture` (13), `intelligence/` and `investment/`
    under `research/dossier` (608 and 37), `operations/` under `build/runbook`
    (81), `products/` under `("canon", "reference")` (131). So naming ONE kind would
    have reported one subdirectory and called the other seven missing.

    Every count below is therefore summed over the homes that exist, and the
    section prints how many it searched, so a run that found one home does not
    read like a run that found eight.
    """
    header("DataStore Status")
    issues = 0

    # Several homes and no registry name for the datastore directory as a
    # whole, so the registry group `datastore-homes` is walked (HOS-240,
    # 2026-10-13). The per-home counts below are the measurement.
    homes = list(data_homes("datastore-homes"))
    if not homes:
        action("no DataStore home found!")
        return 1
    ok(f"{len(homes)} datastore home(s) searched")

    # One named file, so `data_path` and never `data_read`. The handler keeps
    # the home report above when the name does not resolve (2026-09-24).
    try:
        index_file = data_path("datastore-index")
    except DataPathError as exc:
        action(f"datastore/INDEX.md address could not be resolved "  # leak-guard: ok (report text; the path is named, not built)
               f"({type(exc).__name__}); its presence NOT checked")
        issues += 1
    else:
        if not index_file.exists():
            action("datastore/INDEX.md not found!")  # leak-guard: ok (report text; the path is named, not built)
            issues += 1
        else:
            ok("INDEX.md exists")

    # Check subdirectories. `corporate` here is a DataStore CATEGORY (company
    # documents), not the executive workspace's `corporate/` layer: HOS-133
    # checked on 2026-09-25 that the category still holds files under
    # `work/deliverable`, and kept it.
    expected_dirs = ["brand", "content", "corporate", "events", "intelligence", "investment", "operations", "products"]
    # A category with a registry name of its own is also looked for at that
    # name's address, which is where it goes when the C3 move takes it out of
    # its home. The others have no name yet and are looked for under a home.
    # `content` and `intelligence` (HOS-234 part 9): the C3 move takes the
    # LinkedIn archive and the competitor dossiers, the bulk of each, out of
    # every home. A named address already inside a home's category directory
    # is that directory's own content and is not counted twice.
    category_names = {"brand": "brand-kit", "events": "bootcamp-events",
                      "content": "linkedin-archive",
                      "intelligence": "competitor-dossiers"}
    for d in expected_dirs:
        present = [h / d for h in homes if (h / d).is_dir()]
        named = resolve_name(category_names[d]) if d in category_names else None
        if (named is not None and named.is_dir()
                and not any(named.is_relative_to(p) for p in present)):
            present.append(named)
        if present:
            # `glob("*")` counted DIRECTORIES as files, so `brand/` reported
            # "5 file(s)" for five subfolders holding 192 documents -- a number
            # that was neither the file count at that level (0) nor the document
            # count in the subtree. A subdir holding only empty folders also
            # escaped the "awaiting documents" warning.
            file_count = sum(1 for dir_path in present
                             for p in dir_path.rglob("*") if p.is_file())
            if file_count > 0:
                ok(f"{d}/: {file_count} document(s)")
            else:
                warn(f"{d}/: empty - awaiting documents")
        else:
            action(f"{d}/ directory missing")
            issues += 1

    # Count total documents
    total_docs = sum(1 for store in homes for _ in store.rglob("*")
                     if _.is_file() and _.name != "INDEX.md")
    if total_docs == 0:
        warn("DataStore has no documents yet - add source-of-truth files")
        issues += 1
    else:
        ok(f"Total documents in DataStore: {total_docs}")

    return issues


def _docs_path(name: str) -> Path:
    """Resolve a synced doc's distribution copy.

    The engine's `docs/` first, then the registry name `spec-docs` in the data
    repository, resolved only when the engine copy is absent; the engine path
    is returned when neither exists, so a genuinely missing doc still flags.
    """
    engine = WORKSPACE / "docs" / name
    if engine.exists():
        return engine
    data = data_path("spec-docs") / name
    if data.exists():
        return data
    return engine


def check_home_reconciliation() -> int:
    """Where the MANIFEST and the TREE disagree about which homes exist.

    It REPORTS and resolves nothing, by the operator's decision of 2026-09-24:
    absorbing the tree into the record would turn a mistaken directory into a
    declared home. A home on disk that no kind declares is an ACTION and
    counted; a declared home not yet on disk is a WARN per kind and not
    counted, because it is the normal state of a kind that has not flipped.
    Prefixes are derived from the table at every depth, never typed. Floors: no
    family roots means nothing to reconcile; family roots with no manifest is a
    finding.
    """
    header("Home Reconciliation")

    # HOS-240 (2026-10-13): kept, class (d): this section reports where the
    # legacy family manifest and the tree disagree. No fan-out reader asks the
    # manifest any more (they walk registry groups), so it is a report on the
    # migration's own table, history of the move.
    from scripts.heading_os_2.families import family_roots
    from scripts.heading_os_2.resolve import NoManifestOnThisTree, load_per_file

    root = get_data_root()
    families = family_roots()
    fanned_out = any((root / family).is_dir() for family in families)
    try:
        index = load_per_file(data_root=root)
    except NoManifestOnThisTree:
        if fanned_out:
            action("this tree carries family roots and no HEADING OS 2 "
                   "manifest, so nothing declares which kinds hold what; the "
                   "fan-out answers with the legacy directory alone and every "
                   "home the move created is NOT searched")
            return 1
        ok("no family roots and no manifest; nothing has fanned out, nothing "
           "to reconcile")
        return 0
    if not fanned_out:
        ok("no family roots on disk; nothing has fanned out, nothing to "
           "reconcile")
        return 0

    # ONE pass over the table. For each kind, every legacy directory prefix it
    # declares a record under, at every depth. The kind's own two leading
    # segments come off first, the way `kinds_under` normalises the same rows:
    # a row's path column carries the legacy spelling until that kind flips and
    # `<family>/<leaf>/<the whole legacy path>` afterwards. A row with no
    # directory above it is a loose file at the data root and declares nothing.
    declared: dict[tuple[str, str], set[str]] = {}
    for kind, rels in index.files_by_kind.items():
        mine = declared.setdefault(kind, set())
        for rel in rels:
            parts = rel.split("/")
            if len(parts) > 2 and parts[0] == kind[0] and parts[1] == kind[1]:
                parts = parts[2:]
            for depth in range(1, len(parts)):
                mine.add("/".join(parts[:depth]))
    known = {prefix for mine in declared.values() for prefix in mine}
    if not known:
        ok("the manifest declares no record under any directory; nothing to "
           "reconcile")
        return 0

    issues = 0

    # Direction one. Every leaf that exists is asked whether it carries a tail
    # the manifest knows about but does not place THERE. This is the tree half,
    # and it is the half `moved_homes` answers today; the reason it ships in the
    # same change as `homes_under` rather than after it is that once the seam
    # goes there is nothing left that enumerates a home nobody declared.
    undeclared: list[tuple[tuple[str, str], str]] = []
    for family in families:
        base = root / family
        if not base.is_dir():
            continue
        try:
            leaves = sorted(base.iterdir())
        except OSError as exc:
            warn(f"{family}/ could not be listed ({type(exc).__name__}: {exc}); "
                 f"its leaves were NOT reconciled")
            continue
        for leaf in leaves:
            if not leaf.is_dir():
                continue
            mine = declared.get((family, leaf.name), set())
            # SHALLOWEST ONLY. `known` carries every ancestor prefix, so an
            # undeclared subtree matches at each of its depths: a planted
            # `<leaf>/wide/inner/queue` reported three times, once for each
            # level, and three lines about one directory is two ways of saying
            # nothing. A home whose parent is already named adds no fact.
            hits = [prefix for prefix in sorted(known - mine)
                    if (leaf / prefix).is_dir()]
            shallowest = [p for p in hits
                          if not any(p.startswith(q + "/") for q in hits)]
            undeclared.extend(((family, leaf.name), p) for p in shallowest)
    for (family, leaf), prefix in undeclared:
        action(f"{family}/{leaf}/{prefix} is on disk and no kind declares "
               f"{prefix}/ there; the fan-out misses it")
        issues += 1

    # Direction two, rolled up per kind.
    absent: dict[tuple[str, str], int] = {}
    for kind, mine in declared.items():
        missing = sum(1 for prefix in mine
                      if not (root / kind[0] / kind[1] / prefix).is_dir())
        if missing:
            absent[kind] = missing
    for kind in sorted(absent):
        warn(f"{kind[0]}/{kind[1]}: {absent[kind]} declared home(s) not on disk "
             f"(that kind has not flipped)")

    if not undeclared:
        ok(f"{len(known)} declared prefix(es) checked at every depth; every "
           f"home on disk is declared")
    if absent:
        ok(f"{sum(absent.values())} declared home(s) across {len(absent)} "
           f"kind(s) not yet created; reported, not reconciled")
    return issues


# `check_root_agreement` stood here until 2026-09-25; HOS-132 removed the
# two helpers it compared, so there is one data root and nothing to agree.


def check_docs_sync() -> int:
    """Verify templates/ and docs/ are in sync for the 2 shared documentation files.

    The sync-docs.py PostToolUse hook auto-copies templates/ to docs/. Drift means
    either the hook failed or someone edited docs/ directly. Either way, investigate.

    templates/ is the registry name `templates` in the data repository; the
    docs/ copy is resolved by `_docs_path` (engine `docs/` first, then
    `spec-docs`). EMERGENCY-PROCEDURES left this list on 2026-09-22: the
    template and the `spec-docs` page are required to differ, so a byte-compare
    could only fail. `check_doc_versions` below still reads its template marker.
    """
    header("Docs/Templates Consistency")
    issues = 0
    synced_files = [
        "GETTING-STARTED.md", "GETTING-STARTED.html",
    ]
    templates_dir = get_templates_dir()
    if not templates_dir.is_dir():
        # Same state its sibling `check_doc_versions` already treats as
        # legitimate below. Without this, a bare public engine clone
        # emitted one ACTION per file ("missing from templates/"),
        # `workspace-health.py` exited 1, and the check that stands in front of
        # `/push-updates` failed on every clone without the private overlay. A
        # missing overlay is not a missing file.
        # The resolved PATH, not just the verdict. Until 2026-09-18 this line
        # said "no data overlay" and nothing else, and it said it on a machine
        # where the overlay was present and held all six documents: the move
        # had carried `templates/` to the address now named `templates` and
        # `get_templates_dir()` still answered `<data>/templates`. The sentence
        # asserted a fact about the OVERLAY that the check had not established;
        # all it had established was that one directory was absent. Printing
        # which directory is what turns six seconds of reading into the answer.
        warn(f"templates/ is not present at {templates_dir} (no data overlay); "
             f"0 file pairs compared")
        return 0
    for name in synced_files:
        tpl = templates_dir / name
        doc = _docs_path(name)
        if not tpl.exists():
            action(f"{name}: missing from templates/")  # leak-guard: ok (report text)
            issues += 1
            continue
        if not doc.exists():
            action(f"{name}: missing from docs/ (sync-docs.py failed or never fired)")
            issues += 1
            continue
        try:
            if tpl.read_bytes() != doc.read_bytes():
                action(f"{name}: templates/ and docs/ out of sync (re-save templates/ to trigger sync)")  # leak-guard: ok (report text)
                issues += 1
            else:
                ok(f"{name}: synced")
        except OSError as e:
            # An unreadable copy is not a synced copy. This was a WARN that did
            # not count, so a run where every comparison failed to read still
            # returned 0 and the summary said "All checks passed." -- the guard
            # reporting clean exactly when it could verify nothing.
            action(f"{name}: read failed ({e}); sync NOT verified")
            issues += 1
    return issues


def check_skill_router_coverage() -> int:
    """Cross-reference .claude/rules/skill-router.md against .claude/skills/ directory listing.

    Every skill directory should either:
    - Appear in the router registry (table rows matching `/skill-name`), or
    - Be explicitly listed as NEVER auto-trigger (e.g., /prime, /osint-advanced)
    - Be a plugin-namespaced skill (documented in the plugin doctrine section)

    A skill in .claude/skills/ without any mention in the router is silently orphaned.

    The match is boundary-anchored. A bare `f"/{name}" in router_text` reported
    `osint` as covered on the strength of the `/osint-advanced` row alone, and
    `queue` on `/queue-draft`; both pairs were live in this repo then, so for
    those names the check could not detect the very class it advertises.
    """
    header("Skill Router Coverage")
    issues = 0
    router_file = WORKSPACE / ".claude" / "rules" / "skill-router.md"
    skills_dir = WORKSPACE / ".claude" / "skills"
    if not router_file.exists():
        action("skill-router.md missing")
        return 1
    if not skills_dir.exists():
        action(".claude/skills/ missing")
        return 1
    try:
        router_text = router_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # An unreadable router is the same outcome as a missing one for this
        # check: no skill can be shown to be covered. The branch above already
        # returns 1 for the missing case.
        action(f"skill-router.md could not be read ({type(exc).__name__}); "
               f"0 skills checked for coverage")
        return 1
    skill_dirs = [d.name for d in sorted(skills_dir.iterdir()) if d.is_dir() and not d.name.startswith(".")]
    for name in skill_dirs:
        # A skill name may not run straight into another name character, so
        # `/osint` no longer matches inside `/osint-advanced`.
        if re.search(rf"/{re.escape(name)}(?![\w-])", router_text):
            ok(f"{name}: mentioned in skill-router.md")
        else:
            action(f"{name}: not mentioned in skill-router.md")
            issues += 1
    return issues


def check_doc_versions(max_age_days: int = 90) -> int:
    """Verify three shared templates carry `version:` + `last-updated:` markers.

    Scope: this opens exactly the three `.md`/`.template` files listed in
    `tracked` below. It opens nothing under `docs/` and no `.html` file.
    `check_docs_sync` byte-compares the synced GETTING-STARTED pair, so a
    drifted marker in a docs/ copy is caught there instead.

    One of the three tracked templates, GETTING-STARTED.md, is a synced
    document. EMERGENCY-PROCEDURES stopped being synced on 2026-09-22 and stays
    here deliberately: this check reads the TEMPLATE's own marker, and a clean
    result says nothing about the `EMERGENCY-PROCEDURES` page in `spec-docs`.

    A missing file (with templates/ present) counts as an issue. A stale date
    does not: staleness is a refresh signal, and blocking a push on it was never
    the contract in `.claude/rules/documentation.md`. The stale count is printed
    so it is not silent either.
    """
    header(f"Shared Doc Version Markers (freshness threshold: {max_age_days} days)")
    issues = 0
    stale = 0
    version_pattern = re.compile(r"<!--\s*version:\s*(\S+?)\s*\|\s*last-updated:\s*(\d{4}-\d{2}-\d{2})\s*-->")
    today = datetime.now(get_default_tz()).date()
    templates_dir = get_templates_dir()
    if not templates_dir.is_dir():
        # A bare public engine clone has no data overlay, so there is no
        # templates/ tree to version-check. That is a legitimate state, unlike
        # a file missing from a templates/ that DOES exist.
        # The same sentence as its sibling above, and the same repair: name the
        # directory that was not there, so an absent overlay and a directory
        # that has MOVED are not reported with one word.
        warn(f"templates/ is not present at {templates_dir} (no data overlay); "
             f"0 docs version-checked")
        return 0
    tracked = [
        templates_dir / "GETTING-STARTED.md",
        templates_dir / "EMERGENCY-PROCEDURES.md",
        templates_dir / "CLAUDE.md.template",
    ]
    for f in tracked:
        # The parent comes from `templates_dir` above, which `get_templates_dir()`
        # resolved (logical name `templates`), never from a literal: `f` is under
        # the data root now, so `relative_to(WORKSPACE)` would raise, and a
        # spelled `templates/` would be the pre-reform address rather than the
        # directory this run actually read. MEASURED 2026-09-23 against the live
        # overlay: the printed labels are byte-identical to the literal's,
        # and they now follow the directory if it moves again.
        label = f"{templates_dir.name}/{f.name}"
        if not f.exists():
            # templates/ exists but this member of the sync set does not: the
            # set is incomplete, which is a defect, not a note. It used to WARN
            # and `continue`, so an entirely missing template set still returned
            # 0 and the run printed "All checks passed."
            action(f"{label}: missing from templates/")  # leak-guard: ok (report text)
            issues += 1
            continue
        try:
            first_lines = f.read_text(encoding="utf-8").splitlines()[:3]
        except (OSError, UnicodeDecodeError) as exc:
            # The last of the five reads in this file that had no decode
            # guard. A template
            # whose version marker cannot be read is an unverified template,
            # which is what the two `continue` branches below already count.
            action(f"{label}: could not be read ({type(exc).__name__}); "
                   f"version marker NOT checked")
            issues += 1
            continue
        first_block = "\n".join(first_lines)
        match = version_pattern.search(first_block)
        if not match:
            # "in the first 3 lines", because that is the window searched two
            # lines up. The message used to say "on line 1", a stricter contract
            # than the one enforced: a compliant marker on line 2 passes, so an
            # operator following the remediation moved a marker that was already
            # fine.
            action(f"{label}: missing version marker in the first 3 lines")
            issues += 1
            continue
        version, date_str = match.groups()
        try:
            doc_date = date.fromisoformat(date_str)
        except ValueError:
            warn(f"{label}: malformed date {date_str}")
            issues += 1
            continue
        age = (today - doc_date).days
        if age > max_age_days:
            stale += 1
            warn(f"{label}: v{version}, last-updated {date_str} ({age} days old - consider refresh)")
        else:
            ok(f"{label}: v{version}, last-updated {date_str} ({age} days)")
    if stale:
        warn(f"{stale} of {len(tracked)} shared doc(s) past the {max_age_days}-day "
             f"threshold (reported, not counted as an issue)")
    return issues


def check_daemon_state_perms() -> int:
    """F-9.2: verify the heartbeat store is not world-writable.

    The heartbeat store, `daemon_heartbeat.heartbeats_dir()` (the data
    repository's `runtime:daemon-heartbeat/tmp`), holds the per-daemon beats
    from sentinel, sync-exchange and every other daemon. World-writable there means
    another user on this machine can forge a liveness beat, and the fleet grid
    believes beats. The check is unconditional.
    """
    import stat as _stat
    header("Daemon state directory permissions")
    # The heartbeats left the engine checkout's `.daemon-state/` for the data
    # repository on 2026-09-25 (HOS-5 section 7); the question is unchanged.
    from scripts.utils import daemon_heartbeat
    from scripts.utils.data_path import DataPathError

    issues = 0
    try:
        state_dir = daemon_heartbeat.heartbeats_dir()
    except DataPathError as exc:
        warn(f"heartbeat store unresolvable ({exc}); permissions NOT checked")
        return issues

    if not state_dir.is_dir():
        warn("heartbeat store absent; permissions NOT checked "
             "(no daemon has beaten on this machine, or it was removed - "
             "this check cannot tell which)")
        return issues

    mode = _stat.S_IMODE(state_dir.stat().st_mode)
    if mode & 0o002:
        action(f"heartbeat store is world-writable (mode {oct(mode)}); "
               f"run: chmod o-w {state_dir}")
        issues += 1
    else:
        ok(f"heartbeat store is not world-writable (mode {oct(mode)})")
    return issues


# F-7.1: one representative installed DISTRIBUTION per optional-dependency extra.
# Presence of the distribution means the extra is installed ("Armed"); absence
# means the capability is Dormant on this install. Distribution presence
# (importlib.metadata) is used deliberately instead of importability: a workspace
# script like scripts/firecrawl.py sits on sys.path[0] when this file is run as
# `python scripts/workspace-health.py` and would shadow the real `firecrawl`
# module, giving a false reading. Distribution names are immune to that shadow.
_EXTRAS_DISTS = {
    "email": "exchangelib",
    "telegram": "Telethon",
    "browser": "playwright",
    "documents": "python-pptx",
    "media": "yt-dlp",
    "dashboard": "fastapi",
    "ai-extra": "openai",
    "observability": "langfuse",
    "research": "firecrawl-py",
}


def check_extras_importability() -> int:
    """F-7.1: report which optional-dependency extras are Armed vs Dormant.

    Informational only - always returns 0. Dormant is a legitimate state (an
    adopter runs `uv sync` core-only and arms a capability with
    `uv sync --extra <name>`), so a Dormant extra is never an ACTION.
    """
    from importlib import metadata as _md
    header("Capability extras (Armed / Dormant)")
    armed = 0
    for extra, dist in _EXTRAS_DISTS.items():
        try:
            _md.version(dist)
            present = True
        except _md.PackageNotFoundError:
            present = False
        if present:
            ok(f"{extra}: Armed ({dist} installed)")
            armed += 1
        else:
            print(f"  {CYAN}--{RESET}    {extra}: Dormant (uv sync --extra {extra})")
    print(f"\n  {armed}/{len(_EXTRAS_DISTS)} extras Armed on this install.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Workspace Health Check")
    parser.add_argument(
        "--section",
        choices=[
            "refs", "context", "counts", "pipeline", "people",
            "datastore", "homes", "docs-sync", "skill-router", "doc-versions",
            "daemon-state", "extras",
        ],
        help="Run only a specific check section",
    )
    parser.add_argument("--max-days", type=int, default=30,
                        help="Maximum age in days for context freshness (default: 30)")
    args = parser.parse_args()

    print(f"\n{BOLD}Workspace Health Check{RESET}")
    print(f"Workspace: {WORKSPACE}")
    print(f"Date: {datetime.now(get_default_tz()).strftime('%Y-%m-%d %H:%M')}")

    total_issues = 0
    INCONCLUSIVE.clear()
    checks = {
        "refs": check_reference_validation,
        "context": lambda: check_context_freshness(args.max_days),
        "counts": check_agent_counts,
        "pipeline": check_pipeline_health,
        "people": check_people_completeness,
        "datastore": check_datastore,
        "homes": check_home_reconciliation,
        "docs-sync": check_docs_sync,
        "skill-router": check_skill_router_coverage,
        "doc-versions": check_doc_versions,
        "daemon-state": check_daemon_state_perms,
        "extras": check_extras_importability,
    }

    def run_one(name, check_fn) -> int:
        """One section, whose crash costs that section and no other.

        Guarding the CALL fixes the class: MEASURED 2026-09-01, a `pipeline`
        record holding one invalid byte took the whole health run down through
        an unguarded loop. Counted as an issue, never swallowed. The per-read
        handlers stay, because they name the FILE and keep the section's own
        verdict where this can only say which section died.
        """
        try:
            return check_fn()
        except Exception as exc:  # noqa: BLE001 - one section must not end the run
            action(f"section '{name}' could not run ({type(exc).__name__}: {exc}); "
                   f"it verified nothing")
            return 1

    if args.section:
        ran = [args.section]
        total_issues = run_one(args.section, checks[args.section])
    else:
        ran = list(checks)
        for name, check_fn in checks.items():
            total_issues += run_one(name, check_fn)

    # Summary. "All checks passed." was printed after `--section extras` too --
    # one section, and one whose own docstring says it is informational and
    # always returns 0. The summary now names the coverage it had.
    header("Summary")
    skipped = len(checks) - len(ran)
    # A section that verified nothing is named before any pass line is printed.
    # "0 paths checked (this section verified nothing)" followed by "Section
    # 'refs' passed." told the operator two opposite things four lines apart,
    # and only the second one survives being skim-read.
    for note in INCONCLUSIVE:
        print(f"  {YELLOW}INCONCLUSIVE{RESET}  {note}")
    if total_issues == 0 and INCONCLUSIVE:
        ran_word = f"Section '{ran[0]}'" if skipped else f"All {len(ran)} checks"
        print(f"  {YELLOW}{BOLD}{ran_word} found no issues, but "
              f"{len(INCONCLUSIVE)} section(s) above verified nothing.{RESET}")
        if skipped:
            print(f"  {YELLOW}The other {skipped} section(s) did not run.{RESET}")
    elif total_issues == 0 and skipped:
        print(f"  {GREEN}{BOLD}Section '{ran[0]}' passed.{RESET} "
              f"{YELLOW}The other {skipped} section(s) did not run.{RESET}")
    elif total_issues == 0:
        print(f"  {GREEN}{BOLD}All {len(ran)} checks passed.{RESET}")
    elif skipped:
        print(f"  {YELLOW}{BOLD}{total_issues} issue(s) found in section "
              f"'{ran[0]}'; the other {skipped} section(s) did not run.{RESET}")
    else:
        print(f"  {YELLOW}{BOLD}{total_issues} issue(s) found.{RESET}")

    sys.exit(0 if total_issues == 0 else 1)


if __name__ == "__main__":
    main()
