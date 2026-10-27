"""The operator's private overlay is not scratch space.

Two halves of one guard, and neither replaces the other.

* A whole-tree snapshot at arm time and again at the end, which catches a write
  made by a CHILD process that no wrapper in this interpreter could see, and
  cannot say which caller made it.
* Wrappers over the write primitives, which refuse an in-process write at the
  moment it is attempted, so the traceback names the caller.

MOVED here from `tests/conftest.py` on 2026-08-31, with no change to what it
refuses. It lived in a conftest, so it armed under pytest and nowhere else, and
that bound was measured the hard way: a scratch probe run as a plain
`.venv/bin/python` called an entry point blind, `openpyxl` saved over a real
18,857-byte operator workbook, and the guard that would have refused the write
was never imported. The lesson, recorded in memory: a probe never calls an entry
point blind; it arms the guard first, or it does not run.

Outside pytest it is armed in every process of the venv by the `.pth` line that
`scripts/overlay-guard-install.py` installs, whenever `HEADING_OS_OVERLAY_GUARD`
names a mode.

A migration freeze layer lived here from 2026-09-14 until its retirement on
2026-09-20; the ordinary guard did not change with it.
"""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path
import contextlib


def _shared_exclusions():
    """The shared exclusion table, loaded BY PATH and never as `scripts.utils.x`.

    A plain `from scripts.utils.excluded_dirs import ...` at the top of this file
    would be correct in every process but one, and that one is the process this
    module was written for. The arming `.pth` imports this module during
    `site.py`, before any user code runs, and it deliberately loads it by path
    under a private name so that the top-level name `scripts` is NOT bound in
    `sys.modules` for the rest of the interpreter. A top-level package import
    here undoes that from the inside.

    MEASURED 2026-08-31, the first time this was got wrong: 62 tests went red and
    every `python -m scripts.x` run from a directory with its own `scripts/`
    package resolved against the engine instead, with
    `No module named scripts.improve_description`. A check held that arming does
    not decide what the `scripts` package means, and it caught exactly this
    import again on 2026-09-21; since the 2026-09-21 test-suite split nothing
    would catch the third time.

    The real module is preferred when some earlier caller has already imported
    it, so an ordinary process holds one table rather than two.
    """
    import importlib.util

    key = "_heading_os_excluded_dirs"
    module = sys.modules.get("scripts.utils.excluded_dirs") or sys.modules.get(key)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            key, Path(__file__).resolve().parent / "excluded_dirs.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return module


_SHARED_EXCLUSION_REASONS = _shared_exclusions().REASONS

# ============================================================
# The operator's overlay is not scratch space
# ============================================================
#
# Anything that resolves a path through `get_data_root()` reads HEADING_OS_DATA
# from the environment. A child process launched with `cwd=tmp_path` and no env
# override therefore writes into the operator's REAL overlay.
#
# Measured 2026-08-27, twice in one day:
#
#   * 114 archives named `..._handoff_compact-manual_probe-session.md` had
#     accumulated in `outputs/operations/handoff-archive/`, and the shared  # leak-guard: ok (history: where the archives had accumulated then)
#     `.latest/summary.md` and `.latest/prompt.md` - the pair `/next` reads as
#     "the newest handoff in this workspace" - were pointing at one of them.
#   * A mutation-testing run put a `MEMORY.md` writer back into `thread.py open`,
#     to prove the guard against it. The CLI tests that call `open` did not pin
#     HEADING_OS_DATA, so the mutant truncated the operator's live 20 KB memory
#     index to a 20-byte stub. It was restored, and the lesson is that the first
#     guard was scoped to one directory while the hazard is the whole overlay.
#
# So this watches both, and watches CONTENT as well as membership: a truncation
# in place adds no file and removes none. Names and sizes only, at session start
# and session finish, and nothing at all when there is no overlay on disk.

# The comment above ends "the hazard is the whole overlay", and then the fix
# watched two directories. On 2026-08-29 the third one was found the same way as
# the first two: a mutation harness reverted `StateManager.__init__` in
# scripts/email-intelligence.py to its import-time default, ran the email-intel
# tests in the main tree, and four runs of `main()` rewrote
# `outputs/operations/email-intelligence/state.json` in the live overlay. The  # leak-guard: ok (history: the file the incident rewrote, at its address then)
# guard said nothing, because that path was not one of the two. Measured the same
# day: of four writes into a fake overlay, three drew no complaint.
#
# So the snapshot is now the WHOLE overlay, minus a named few that change on
# their own. Walking it costs about 50 ms for roughly 11,000 files, which is
# nothing beside a 100-second suite.
#
# Each exclusion needs a reason, and "it was noisy" is not one. A rebuildable
# index or a credential file that a daemon refreshes is genuinely not the
# operator's data; an output, a CRM record or a thread is.
#
# REBUILDABLE is the ground for the two memory indexes, and it is the only one.
# Until 2026-09-05 the reason beside them said "a live file watcher", which no
# process provides: measured that day, the only index watcher on this machine
# is CodeGraph's, and `.memory-index*` are rebuilt by a command someone runs.
# The exclusions were right and the reason was not, which is worse than a wrong
# exclusion, because the reason is what the next person reasons from.
#
# The operator's holding pens are NOT written out below. One of them was added
# here on 2026-09-20, and it is the one exclusion in this set that bought TIME
# rather than quiet: MEASURED that day, 309,333 of the overlay's 327,195 files
# sat under that single name, 94.5% of a walk that runs at the start of every
# pytest worker and twice inside every `git commit`. He deleted the folder by
# hand the same day. The name stays, because an exclusion that matches nothing
# is cheap and a re-gathered folder is not.
#
# It now comes from `scripts/utils/excluded_dirs.py`, which is the single place
# five separate files used to spell it (HOS-28), and which carries the declared
# on-disk state so that the day it stops matching, something says so.
_UNWATCHED = {
    **_SHARED_EXCLUSION_REASONS,
    ".git": "git's own object store, rewritten by any git command",
    ".memory-index": "a rebuildable search index; nothing watches it, it is rebuilt on demand",
    ".memory-index-code": "the same, for code",
    ".codegraph": "the CodeGraph index, rebuilt by its own watcher",
    ".sessions": "runtime credentials refreshed by the daemons",
}

# Two subtrees of the RUNTIME STORE, not of the overlay's top level, which the
# snapshot does not walk either. `_UNWATCHED` holds top-level names only, and
# these sit one level down, under the store whose directory name the registry
# decides (`_record_dir_rel`), so they are a set of their own.
#
# Bought for TIME, like the holding pen above. MEASURED 2026-10-12 on this
# machine: these two, with two Cognee archives the operator deleted the same
# day, held over nine tenths of the overlay's files, and the snapshot walked
# them twice per pytest session, in every yard bootstrap's two sessions among
# them. Pruning the four took one snapshot from about five seconds to under
# half a second.
#
# NOT in `excluded_dirs.SHARED`, on purpose: that table is read by the memory
# walkers too, and what the memory index reads must not change with this.
# Everything else under the store, its hook state included, stays watched.
_UNWATCHED_IN_STORE = {
    "checkout-scratch": (
        "per-checkout scratch (registry name `checkout-scratch`), the working "
        "space every checkout writes while it works, a parallel yard's included; "
        "a scratch file is not a record, and another checkout's writes there "
        "would read as this run's"),
    "hindsight-snapshot": (
        "the daily derived copy of what lives only in Hindsight, replaced whole "
        "by `scripts/hindsight-snapshot.py take` from its own timer; a run that "
        "overlapped a take would report the whole copy as changed"),
}

_WATCH_BEFORE = None

# Children that could reach the operator's live overlay, as (test node id,
# command). RECORDED, never refused: forcing a scratch root into every child
# would change what a run is permitted to do and could hide a real defect, while
# recording costs nothing and turns "the overlay changed and nothing can say who"
# into a short suspect list. Capped, because a 19,000-test suite spawns a lot.
_CHILD_SPAWNS: list[tuple[str, str]] = []

# How many children reached the live overlay, counted WITHOUT the cap that
# bounds the example list. The two were one number until 2026-09-03, and the
# report printed "200 child process(es) ran with the live data root reachable"
# when 200 was `_CHILD_SPAWN_CAP` and the true figure was unknown and larger. A
# ceiling presented as a count is the same defect the surrounding message has:
# stating more than the method established.
_CHILD_SPAWN_COUNT = 0
_CHILD_SPAWN_CAP = 200

# Per-test attribution: nodeid -> [count, one example command]. The flat example
# list above is capped at 200 records, so under `-n auto` it fills with whatever
# the FIRST 200 spawns were and says nothing about the other ten thousand.
# MEASURED 2026-09-04: a full sharded run reported 10069 reachable children and
# every one of the 16 printed examples was `<unknown test> -> .venv/bin/python -u
# -c import sys;exec(...)`, the execnet bootstrap of the xdist workers -- because
# the CONTROLLER is the only process whose examples the report ever saw, and the
# controller spawns nothing but workers. 99.8% of the enforced number had no
# nodeid, no command, and no way to act on it.
#
# A dict keyed by nodeid is bounded by the number of DISTINCT spawning tests
# rather than by the spawn count, which is the shape that survives ten thousand
# spawns from a few hundred tests. It is capped anyway, because nothing
# guarantees that ratio.
#
# THE HONEST LIMIT: past the cap, a nodeid that has never been seen before is
# counted in `_CHILD_SPAWN_COUNT` and dropped from this map, tallied in
# `_CHILD_SPAWN_UNATTRIBUTED`. The map is therefore a lower bound per test and a
# possibly-incomplete list of tests. The example command is the FIRST one seen
# for that nodeid, not a summary of all of them.
_CHILD_SPAWNS_BY_TEST: dict[str, list] = {}
_CHILD_SPAWN_BY_TEST_CAP = 2000
_CHILD_SPAWN_UNATTRIBUTED = 0

# The key a spawn gets when `PYTEST_CURRENT_TEST` is unset: session-level and
# import-time spawns, and every spawn the xdist CONTROLLER makes. A named
# constant because two files now compare against it, and a second spelling of a
# sentinel is a sentinel that stops matching.
UNKNOWN_TEST = "<unknown test>"

# Wall-clock moment `_WATCH_BEFORE` was taken. Recorded because the overlay is a
# LIVE tree: a concurrent agent, a daemon or the operator can create a file in it
# while the suite runs, and a reader comparing the snapshot against a later walk
# of the disk would call that file "missed by the snapshot". It was not missed;
# it did not exist yet. Anything wanting to audit the snapshot's coverage must
# ignore files younger than this.
_WATCH_BEFORE_AT = None


_LIVE_OVERLAY_LABEL = "operator overlay"
_ENV_ROOT_LABEL = "data root in use"


def _structural_overlay_root():
    """The operator's real overlay, derived from THIS FILE's location alone.

    NOT `get_data_root()`, and that is the entire point of this function.
    `get_data_root()` honours `HEADING_OS_DATA` (scripts/utils/paths.py), so
    until 2026-08-31 the guard asked the environment where the operator's data
    was — and the environment is the one thing a test session can change.

    Measured that day, with nothing written: launched plainly, the snapshot
    watched 10,919 real files and `_OVERLAY_PREFIXES` named the real overlay.
    Launched as `HEADING_OS_DATA=<scratch> pytest`, which is the remedy every
    isolation fix in this repository recommends and what a careful operator and
    CI both do, the snapshot watched 0 files and the prefix named the scratch
    directory. Both halves of the protection moved off the operator's data at
    once, for the whole session, silently. A guard has to ask about the WRITE,
    not about the environment.

    Structural, like `_FALLBACK_ROOT` in scripts/utils/paths.py: this file is
    `<engine>/scripts/utils/overlay_write_guard.py`, so the engine root is three
    parents up and the sibling data repo is beside it. No environment variable
    reaches it. A clone with no sibling overlay (a fresh public clone, CI) gets
    None and the guard stays off, exactly as before.

    The depth was asserted by a test, because it is the one number the move from
    `tests/conftest.py` changed, and since the 2026-09-21 test-suite split
    nothing asserts it. Get it wrong by one and this returns None or a stranger,
    the whole guard arms over nothing, and nothing anywhere goes red: a test of
    this guard drives it against a pretend overlay, so it never saw the real
    depth either. Count the parents by hand when you move this file.

    Only `.heading-os-data` is returned. Other repositories beside it are not
    watched but refused outright by `_foreign_roots()` (2026-09-23), because no
    write from this workspace into somebody else's tree is allowed at all.
    """
    engine = _main_clone_root(Path(__file__).resolve().parents[2])
    sibling = engine.parent / ".heading-os-data"
    try:
        return sibling.resolve() if sibling.is_dir() else None
    except OSError:
        return None


def _main_clone_root(engine):
    """`engine` when it is the main clone, else the main clone it belongs to.

    The sibling-overlay rule above holds for the MAIN clone and for nothing
    else. A git worktree lives wherever it was created: this repository's YARDs
    sit at `<workspaces>/.yard/.heading-os/<task>`, three levels away from the
    overlay, so `engine.parent / ".heading-os-data"` names a directory that has
    never existed.

    MEASURED 2026-09-03 in the YARD at `.yard/.heading-os/test-123`:
    `_structural_overlay_root()` returned None, `_watched_roots()` was empty,
    and `arm_process_wide()` therefore wrapped nothing while reporting itself
    armed. Five tests covered exactly that and had been SKIPPED, because the
    `.pth` they require is removed by `uv sync` and the bootstrap never put it
    back, so the suite could not say it either. Those tests went with the
    2026-09-21 test-suite split, so the structural derivation below is now held
    by reading it. Every YARD on this machine ran unguarded, and the report said
    otherwise. CLAUDE.md § HELM and YARD states the rule this broke: derive the
    tree from the current checkout, never from a constant.

    Import-free on purpose, like its caller. This runs from a `.pth` during
    `site.py`, and importing anything under `scripts.` there binds the name
    `scripts` in `sys.modules` for the whole interpreter -- the defect that put
    62 tests red on 2026-08-31 and the reason `_structural_overlay_root` walks
    `__file__` instead of calling `get_data_root()`. `git rev-parse` is not
    available either: no subprocess at interpreter startup.

    So the link is read the way git writes it. A worktree's `.git` is a FILE
    holding `gitdir: <main>/.git/worktrees/<name>`; the main clone's `.git` is a
    directory. Anything unexpected returns `engine` unchanged, which is the
    previous behaviour and fails toward the public-clone case rather than toward
    a stranger's directory.
    """
    dotgit = engine / ".git"
    try:
        if dotgit.is_dir() or not dotgit.is_file():
            return engine
        text = dotgit.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return engine
    if not text.startswith("gitdir:"):
        return engine
    gitdir = Path(text[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        # `normpath`, not `resolve`. The `..` segments have to go, or `parents`
        # below counts them as components and returns a path spelled through
        # the worktree it was supposed to leave. `resolve` would also follow
        # symlinks, and a repository reached through one is still that
        # repository; the question here is spelling, not identity.
        gitdir = Path(os.path.normpath(engine / gitdir))
    # <main>/.git/worktrees/<name>  ->  <main>
    parents = gitdir.parents
    if len(parents) < 3 or parents[0].name != "worktrees" or parents[1].name != ".git":
        return engine
    return parents[2]


def _overlay_root():
    """The overlay THIS SESSION's data root points at, or None.

    Still environment-sensitive on purpose: it answers "where is this run
    writing", which is a different question from "where is the operator's data".
    The guard unions both — see `_watched_roots()`.
    """
    try:
        from scripts.utils.paths import data_overlay_present
        from scripts.utils.workspace import get_data_root
    except ImportError:
        return None
    # EVERY exception, not `OSError` and not `ImportError`. MEASURED 2026-08-31,
    # the day the `.pth` landed: `HEADING_OS_DATA` pointing at a directory that
    # does not exist makes `paths.env_data_root()` raise `DataRootError`, which
    # is neither. It escaped this function, escaped `_watch_snapshot()`, escaped
    # `arm()`, and was swallowed by the `except Exception: pass` inside the .pth
    # line, so the guard did not arm and said nothing. A mistyped variable
    # switched the whole guard off in silence, which is the exact failure this
    # guard was rewritten to stop the environment from causing.
    #
    # Returning None is the safe direction and not a shrug: the STRUCTURAL root
    # is resolved separately from `__file__` and no variable reaches it, so the
    # operator's real overlay stays watched. What is lost is only the second,
    # session-scoped root, which by definition cannot be resolved when the
    # variable naming it is broken.
    try:
        if not data_overlay_present():
            return None
        root = get_data_root().resolve()
    except Exception:  # noqa: BLE001 - see above; a broken variable must not disarm
        return None
    return root if root.is_dir() else None


def _watched_roots():
    """{label: root} — every root this run must not write without saying so.

    The union of the two questions above. In the ordinary case they are the same
    directory and there is one label, which is what every earlier run saw.

    When they differ, both are watched. The structural one because it is the
    operator's data and no `HEADING_OS_DATA` may move the guard off it. The
    session one because a run pointed at a scratch root still must not scatter
    private records into it unnoticed: those are exactly the writes that would
    have hit the real overlay had the variable been absent, and reporting them
    is how the scratch remedy stays honest instead of merely quiet.

    One function, so a test can drive the real snapshot over a fake overlay by
    replacing this and nothing else.
    """
    roots = {}
    structural = _structural_overlay_root()
    if structural is not None:
        roots[_LIVE_OVERLAY_LABEL] = structural
    session = _overlay_root()
    if session is not None and session != structural:
        roots[_ENV_ROOT_LABEL] = session
    return roots


# ============================================================
# The one switch, and it is the whole of the lift
# ============================================================
#
# Operator instruction 2026-09-23, translated: "stop all writes to every other
# repository (corporate, executives). When the migration is done I will rework
# this logic. For now we treat everything in `.heading-os-data` as mine only,
# private, going to nobody else."
#
# TEMPORARY BY HIS OWN WORDS, until the HEADING OS 2 migration is finished,
# after which he intends to rework this logic himself. So the ban had to be one
# movement rather than six repairs in six tools, and this constant is that
# movement: set it to False and every layer below goes quiet at once, because
# all three read `_FOREIGN_PREFIXES` and nothing else decides any of them.
#
#     `_FOREIGN_TREES_ARE_READ_ONLY = False`
#
# restores the behaviour of 2026-09-22 completely: in-process writes into a
# neighbouring repository, writes inside a repository nested in the engine
# checkout, and mutating subprocesses aimed at either. Nothing else has to be
# touched, in this file or in any other, and that is the requirement rather than
# a convenience.
#
# What it does NOT restore, because it never removed it: the operator's own
# overlay stays watched exactly as it was, by `_watched_roots()`, which this
# constant does not reach.
_FOREIGN_TREES_ARE_READ_ONLY = True


def _foreign_roots():
    """Every repository beside or inside the engine clone that is somebody else's.

    The operator's workspace root holds his two repositories and a number of
    trees that are not his to change. Operator instruction 2026-09-23, on the
    work then running: the two repositories named below are the whole subject,
    "no other repository (not corporate, not other executives') is
    concerned, and nothing there may change" (translated). That sentence is the one this
    function exists to turn into a mechanism, because a prohibition given as
    words is a guard nobody has seen refuse.

    MEASURED 2026-09-23 in the main clone, by walking the workspace root: 15
    immediate children, 8 of them git repositories. Two are the operator's own,
    and SIX are not: four executive overlays, a corporate clone, and one more
    repository that the issue's hand-written list of five had missed. That miss
    is the argument for deriving the set rather than naming it. Before this
    function existed, `_watched_roots()` returned one root and nothing anywhere
    refused a write into any of the six.

    DERIVED, three tests deep, and none of them is a name:

    * an immediate child DIRECTORY of the workspace root,
    * which carries a `.git` entry, so it is a repository rather than a scratch
      directory, a cache or the directory the yards live under, and
    * which is neither of the operator's two trees, asked of `_main_clone_root`
      so that a WORKTREE of either resolves back to its own clone and is not
      mistaken for a stranger's.

    The third test is the one that would hurt to get wrong. A yard is a worktree
    of the engine, and a yard writes its own checkout constantly; today they sit
    two levels down under a directory that is not itself a repository, so they
    are already outside this walk, and the check costs one file read and means
    the answer does not depend on that staying true.

    A ROSTER IS NEVER READ. The executives' slugs live in the operator's private
    overlay and this file ships in a public repository, so a list of names here
    would be both a leak and stale the first time somebody is added; stale here
    means silently unguarded. Nothing below knows a single name.

    Import-free and subprocess-free, like everything else on this path: it runs
    from a `.pth` during `site.py`. See `_main_clone_root` for why.
    """
    engine = _main_clone_root(Path(__file__).resolve().parents[2])
    ours = set()
    for known in (engine, _structural_overlay_root()):
        if known is None:
            continue
        try:
            ours.add(known.resolve())
        except OSError:
            continue
    # TWO DIRECTORIES ARE WALKED, not one, and the second is layer 2.
    #
    # A repository can also sit INSIDE the engine checkout. On an executive
    # workspace the corporate clone does exactly that, and the sibling rule
    # above cannot reach it: it is not beside the workspace root, it is nested.
    # MEASURED 2026-09-23 on a provisioner-built fixture, through real entry
    # points: `context-freshness.py stamp-all` wrote a freshness stamp inside
    # that clone and two file operations at `calibration_store.current_path()`
    # went through, 16 bytes, refused by nobody (HOS-96).
    #
    # DERIVED and not named, for the same reason as the sibling rule: "an
    # immediate child directory of the engine that carries a `.git`" is a
    # nested repository whatever it is called, so a second clone landing there
    # under another name is covered on the day it appears rather than on the
    # day somebody remembers this list. MEASURED 2026-09-23 in this checkout:
    # 48 children, 0 of them nested repositories, so the rule adds nothing here
    # and everything on an executive workspace.
    #
    # THE SHAPE IS `refuse_if_inside_engine` in `scripts/datastore-map.py`,
    # which refuses a write inside the engine checkout and says why. It is not
    # imported: that module is a CLI script and this one runs from a `.pth`
    # during `site.py`, where importing anything under `scripts.` binds the name
    # for the whole interpreter. The reasoning is reused; the code cannot be.
    #
    # Scanned SEPARATELY, so that one unreadable directory costs its own half
    # and not both. A single `try` around the pair would answer "no foreign
    # trees anywhere" the first time either one could not be read, which is a
    # guard disarming itself over an unrelated permission error.
    entries = []
    for directory in (engine.parent, engine):
        try:
            entries.extend(os.scandir(directory))
        except OSError:
            continue
    foreign = set()
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            if not os.path.lexists(f"{entry.path}{os.sep}.git"):
                continue
            candidate = Path(entry.path).resolve()
            if candidate in ours or _main_clone_root(candidate).resolve() in ours:
                continue
        except OSError:
            continue
        # BOTH SPELLINGS, when they differ. The refusal below is a substring
        # test on the path as the caller wrote it, and a caller builds these
        # paths by joining a name onto the workspace root rather than by
        # resolving anything, so the unresolved spelling is the one that
        # usually arrives. The resolved one is kept as well, because a tree
        # reached through a link is still that tree.
        foreign.add(Path(entry.path))
        foreign.add(candidate)
    return tuple(sorted(foreign))


def _foreign_prefixes():
    """`_foreign_roots()` as the path prefixes the three layers compare against.

    The ONE place the lift switch is read. Empty means every layer is quiet,
    which is why flipping `_FOREIGN_TREES_ARE_READ_ONLY` is the whole of it.
    """
    if not _FOREIGN_TREES_ARE_READ_ONLY:
        return ()
    return tuple(f"{root}{os.sep}" for root in _foreign_roots())


def _snapshot_one(root):
    """{relpath: size} for one root, minus the _UNWATCHED subtrees.

    The exclusions PRUNE, and until 2026-09-20 they did not. `rglob("*")`
    descended into every excluded tree and the filter threw the results away
    afterwards, so an exclusion saved the dictionary entry and paid the walk.

    MEASURED 2026-09-20 on this machine: the overlay held 327,195 files, of
    which 309,333 (94.5%) sat under one excluded top-level name. One walk took
    11 seconds idle and 19 to 21 with sixteen suite workers walking at once.
    That is roughly twenty seconds at the start of every worker, before the
    first test, and the comment above this function still said "about 50 ms for
    roughly 11,000 files", which was true when it was written.

    The same walk runs twice inside one `git commit`: two pre-commit hooks each
    start their own pytest, so each commit paid it twice. MEASURED the same day
    over the whole commit gate: 52.8 seconds, of which those two hooks were
    46.8, and the other thirty-two hooks together were under five.

    `os.walk` with `topdown=True` lets the excluded names be removed from
    `dirnames` before the walk descends, which is the only way to not pay for a
    tree you have already decided to ignore. Behaviour is otherwise identical:
    the same relative paths, the same sizes, the same silent skip on `OSError`.
    """
    entries = {}
    root_str = str(root)
    # Asked once per walk, not per directory. None matches no prefix, which is
    # right: with no readable store name there is no record to prune either.
    record_rel = _record_dir_rel()
    record_prefix = record_rel + "/" if record_rel is not None else None
    # The store is the record path minus its fixed tail. With no readable store
    # name nothing is pruned there, so the two are walked: slower, never blind.
    record_tail = "/" + "/".join(_RECORD_SERVICE_KIND)
    store_prefix = (record_rel[:-len(record_tail)] + "/"
                    if record_rel is not None and record_rel.endswith(record_tail)
                    else None)
    for dirpath, dirnames, filenames in os.walk(root_str, topdown=True,
                                                onerror=None):
        rel_dir = os.path.relpath(dirpath, root_str)
        if rel_dir == ".":
            # Only at the top: `_UNWATCHED` holds top-level names, so a nested
            # directory that happens to share one of those names is watched,
            # exactly as it was before this became a prune.
            dirnames[:] = [d for d in dirnames if d not in _UNWATCHED]
            rel_prefix = ""
        else:
            rel_prefix = rel_dir.replace(os.sep, "/") + "/"
            if rel_prefix == record_prefix:
                # This guard's own record, and nothing else: see
                # `record_log_path`. Pruned so a record is not a finding.
                dirnames[:] = []
                continue
            if rel_prefix == store_prefix:
                # Pruned before descent, see `_UNWATCHED_IN_STORE`.
                dirnames[:] = [d for d in dirnames if d not in _UNWATCHED_IN_STORE]
        for name in filenames:
            # String concatenation rather than `os.path.join` (banned by
            # convention in live scripts) or `Path(...)`. A `Path` per entry is what this walk was rewritten to stop
            # paying: MEASURED 2026-09-20, constructing one and calling two
            # `stat`-shaped methods on it is most of the 9.55 s this snapshot
            # cost over 332,054 files. `dirpath` comes from `os.walk` and
            # never carries a trailing separator, so one `/` is exact.
            path = f"{dirpath}{os.sep}{name}"
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            entries[rel_prefix + name] = st.st_size
    return entries


def _watch_snapshot():
    """{label: (directory, {relpath: size})} for every watched root.

    One label per root, because the unit being protected is a whole overlay, not
    a list of interesting places in it. Sizes are always taken: a truncation in
    place adds no file and removes none, which is how the memory index was lost
    in 2026-08.
    """
    return {
        label: (root, _snapshot_one(root))
        for label, root in _watched_roots().items()
    }


# The snapshot above is a post-mortem: it says the overlay changed, after the
# run, and it cannot say WHICH test did it. It also catches a child process,
# which nothing in this interpreter can. The guard below is its opposite half:
# it refuses an in-process write at the moment it is attempted, so the traceback
# names the test. Neither replaces the other.
#
# The check is a substring test on the path, done before any resolve, because it
# runs on every `open()` in a 15,000-test suite. A relative path, a symlink or a
# `..` walk therefore slips past it. That is honest and deliberate: this guards
# against an accident, and the accidents all look like an absolute path built
# from `get_data_root()`. The snapshot is what covers the rest.

# A TUPLE, and renamed from the singular `_OVERLAY_PREFIX` it replaced on
# 2026-08-31. Renamed rather than widened in place so that a caller still setting
# the old name arms nothing and fails, instead of handing a bare string to code
# that iterates it and silently guarding twenty-six single characters.
_OVERLAY_PREFIXES = ()          # set in pytest_sessionstart
_WRITE_MODE_CHARS = frozenset("wxa+")

# The trees this workspace may not write AT ALL, from `_foreign_prefixes()`.
#
# A SEPARATE tuple from `_OVERLAY_PREFIXES` and not an addition to it, because
# the two mean opposite things and one sentence satisfies both readings. Adding
# a stranger's overlay to the watched set would give this workspace a SECOND
# root it is allowed to write, subject to the mode branches, and GUARD mode
# allows any write whose caller is a tracked file: every tool in this repository
# is tracked, so the six trees would have gained a guard that refuses nobody.
# The refusal below is ahead of the mode branches for that reason. It is the
# same layer the retired migration freeze occupied, and it refuses for the same
# kind of reason: not "who is calling", but "this is not yours".
_FOREIGN_PREFIXES = ()


class OverlayWriteRefused(RuntimeError):
    """A test tried to write the operator's live data."""


# ------------------------------------------------------------
# Two modes, and only one of them refuses
# ------------------------------------------------------------
#
# REFUSE is what a test run wants and it is the default: a write to the overlay
# raises, and the traceback names the caller.
#
# RECORD exists because arming this guard in EVERY process cannot start by
# refusing. The overlay is where the operator's work legitimately lands, and the
# list of writers that belong there is long: `thread.py`, `crm.py`,
# `send-email.py`, `action-queue.py`, the sync daemons, every artifact writer.
# A hand-written allowlist of them is the defect this workspace keeps finding,
# because a hand-written list falls behind the tree. So the refusal rule gets
# DERIVED from a measurement, and RECORD is the measurement: it logs who wrote
# what, from which entry point, and allows the write.
#
# Nothing reads the log automatically and nothing refuses on it. It is data for
# a decision that has not been taken yet.
MODE_REFUSE = "refuse"
MODE_RECORD = "record"

# GUARD is the third mode and the one meant to be always on. It refuses a write
# whose innermost workspace frame is a file GIT DOES NOT TRACK, and allows (and
# records) one from a tracked file.
#
# DERIVED, not chosen. `scripts/overlay-writer-census.py` swept 1360 Python files
# under scripts/, .claude/hooks/, .claude/skills/ and tests/ on 2026-08-31 and
# found 184 that both reach a data-root resolver and call a write primitive.
# ALL 184 are git-tracked; none is not. The write that destroyed a real operator
# workbook came from `.tmp/frozen/behaviour.py`, which `.gitignore` covers and
# which therefore cannot ever be tracked. So the boundary separates every
# legitimate writer measured from the one destructive writer observed, and it
# needs no hand-maintained allowlist of tools, which is the thing that falls
# behind the tree.
#
# What this mode does NOT establish, because a refusal has to be honest about
# its own reach:
#
# * A TRACKED file that `exec`s untracked code passes. The caller frame names the
#   tracked file, and the guard cannot see through it. Reported by the census.
# * A write with NO workspace frame in the stack window is ALLOWED and recorded,
#   never refused. That happens when workspace code is further up than the window
#   reaches, and refusing on an unidentified caller would break writes nobody has
#   diagnosed. Failing toward allow-and-log is the deliberate choice here; the
#   log is what makes it reviewable.
#
#   THAT SENTENCE WAS WRITTEN FOR ONE CAUSE AND COVERED TWO, and the second one
#   is `_entry_point_has_no_file()` below. "Workspace code is further up than the
#   window reaches" is real and it is the common case: MEASURED 2026-09-22 over
#   the 263,256 records the live journal holds, 209,955 of them (79.75%) carry no
#   caller at all, and the innermost frame of 196,356 is `aiosqlite/core.py`,
#   reached from the tracked graph-memory pilot script (removed 2026-10-04,
#   HOS-198) through a worker thread whose stack does not contain the workspace
#   frame at all. Refusing on an unidentified caller would refuse four writes in
#   five, so that stays allowed.
#
#   But a program fed to the interpreter on STANDARD INPUT has no caller frame
#   for a different reason: there is no file anywhere, and there never can be.
#   The question "is this code committed?" is not unanswered there, it is
#   answered NO. MEASURED 2026-09-22, the same journal: 854 writes into the
#   operator's real overlay came from an entry point of `-` or `-c` with no
#   tracked frame anywhere in the window, 0.32% of all records. See
#   `_refuse_overlay_path`.
# * It says nothing about a child process. Only `_CHILD_SPAWNS` sees those, and
#   only as a suspect list.
MODE_GUARD = "guard"

# OFF is the escape hatch, and it exists because GUARD is armed by default. An
# always-on control needs a way out that is not "edit a file inside
# site-packages": an operator whose work is being refused wrongly must be able to
# get past it in one command, or they will remove the arming file and the guard
# is gone for good rather than for one command.
MODE_OFF = "off"

_MODES = (MODE_REFUSE, MODE_RECORD, MODE_GUARD, MODE_OFF)
_MODE = MODE_REFUSE

# Set by `_install_overlay_write_guard()` to a writer that closes over the
# UNWRAPPED `open`. It must not be the wrapped one: a sink that logs through the
# primitive it is logging about is a recursion waiting for its first write.
_RECORD_SINK = None

# Where RECORD mode appends, inside the operator's overlay: the runtime store's
# `overlay-guard` service, `logs` kind. It was the ENGINE checkout's gitignored
# `.logs/overlay-write-record.jsonl` until 2026-09-25, when HOS-5 section 7 took
# every artifact out of the engine checkout (operator decision that day: "old way
# should go out and do not stay in the workspace"); that file had reached 872 MB.
#
# NOT asked of `scripts/utils/data_path.py`, and that is the one reason this
# module may do otherwise: it is armed from a `.pth` during `site.py`, where
# importing anything under `scripts.` binds that name for the whole interpreter
# (see `_shared_exclusions`), and `data_path` imports half the engine and a YAML
# parser. The service and kind are spelled here and must equal a service row in
# the registry and one of `runtime_dir.KINDS`. The STORE's directory is not: it
# is the registry's `runtime_store:` key (HOS-5 F1, 2026-09-25), read by
# `scripts/utils/registry_bootstrap.py`, which is standard library only and is
# loaded BY PATH exactly as `_shared_exclusions` loads its table. `data_path`
# refuses any registry that module would read differently from the YAML, so the
# record lands in the store every writer uses. The overlay's own `.gitignore` is
# expected to ignore the directory, because the record is volume and not history.
_RECORD_SERVICE_KIND = ("overlay-guard", "logs")
_RECORD_NAME = "overlay-write-record.jsonl"


def _registry_bootstrap():
    """`scripts/utils/registry_bootstrap.py`, loaded BY PATH; see `_shared_exclusions` for why."""
    import importlib.util

    key = "_heading_os_registry_bootstrap"
    module = sys.modules.get("scripts.utils.registry_bootstrap") or sys.modules.get(key)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            key, Path(__file__).resolve().parent / "registry_bootstrap.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return module


# (registry stat, default-file stat) -> store name. Keyed on inode, size, mtime
# AND ctime: a copy that restores an old mtime (`cp -p`, `rsync -t`) still moves
# ctime, which is what made an mtime-only key wrong elsewhere in this engine.
# One entry: the record sink asks on every recorded write, and the registry is
# tens of kilobytes, so a re-read per write would be the cost of the journal.
_STORE_NAME_CACHE = {}
_STORE_NAME_WARNED = []


def _stat_key(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _record_dir_rel():
    """`<store>/overlay-guard/logs` for the operator's overlay, or None where there is none.

    None too when the store's name cannot be read, and that one is said once on
    stderr: a journal that silently stops is the failure this module exists to
    make visible elsewhere, so it does not get to be silent itself.
    """
    overlay = _structural_overlay_root()
    if overlay is None:
        return None
    engine = Path(__file__).resolve().parents[2]
    boot = _registry_bootstrap()
    example = engine.joinpath(*boot.EXAMPLE_NAMES_REL)
    ignore = overlay / ".gitignore"
    key = (str(overlay), _stat_key(overlay / boot.REGISTRY_REL), _stat_key(example),
           _stat_key(ignore))
    if key not in _STORE_NAME_CACHE:
        try:
            store = boot.store_dirname_from_files(overlay, engine)
            default = boot.read_store_line(example.read_text(encoding="utf-8"), str(example))
            # A relocated store is recorded into only once its ignore rules
            # followed, the same test `runtime_dir.runtime_root` puts to every
            # writer: this sink never asks that function, and a journal of every
            # write landing somewhere git tracks is the commit nobody meant.
            problems = [] if store == default else boot.ignore_problems_in(
                ignore.read_text(encoding="utf-8"), str(ignore), store, default)
            if problems:
                raise ValueError("; ".join(problems))
        except (OSError, ValueError) as exc:
            if not _STORE_NAME_WARNED:
                _STORE_NAME_WARNED.append(True)
                print(f"[overlay-guard] the runtime store's record directory cannot be "
                      f"used, so nothing is recorded: {exc}", file=sys.stderr)
            return None
        _STORE_NAME_CACHE.clear()
        _STORE_NAME_CACHE[key] = "/".join((store,) + _RECORD_SERVICE_KIND)
    return _STORE_NAME_CACHE[key]

# One live segment's ceiling. The journal records EVERY allowed write, not only
# the refusals: the operator was asked that exact question on 2026-09-22 and
# answered record everything. So the file only ever grows, and MEASURED that day
# the live one stood at 261,308,039 bytes over 255,998 lines. A quarter-gigabyte
# JSONL file is not readable, not greppable at interactive speed, and not
# moveable, which is the whole defect; the volume itself is not.
#
# ROTATION HERE REMOVES NOTHING, and that is a design constraint rather than a
# nicety. A closed segment stays on disk for good; only the live file's NAME
# moves. So this buys a bounded, readable unit and it buys no disk space at all.
# Reclaiming space would be deleting records, which is the operator's decision
# and not this module's.
_RECORD_MAX_BYTES = 32 * 1024 * 1024

ENV_RECORD_MAX_BYTES = "HEADING_OS_OVERLAY_RECORD_MAX_BYTES"

# Frames inside this file explain nothing about who called it.
_THIS_FILE = os.path.abspath(__file__)


def record_log_path():
    """Where RECORD mode appends, or None where there is no operator overlay.

    Inside the overlay since 2026-09-25, in the one directory the snapshot does
    not walk (`_record_dir_rel()`). The sink writes through the UNWRAPPED `open`,
    so the in-process wrappers never see it, and the snapshot prune means the
    whole-tree comparison does not either: an entry cannot create the next one.
    None where `_structural_overlay_root()` finds no overlay, which is a public
    clone, where the guard watches nothing and so records nothing.
    """
    overlay = _structural_overlay_root()
    rel = _record_dir_rel()
    if overlay is None or rel is None:
        return None
    return overlay.joinpath(*rel.split("/"), _RECORD_NAME)


def record_max_bytes():
    """The live segment's ceiling. Zero or less turns rotation off entirely.

    An UNPARSEABLE value falls back to the default rather than being honoured or
    read as zero, for the same reason `resolve_mode` does it: a typo in an
    environment variable must never be the thing that changes behaviour silently.
    """
    asked = os.environ.get(ENV_RECORD_MAX_BYTES)
    if asked is None:
        return _RECORD_MAX_BYTES
    try:
        return int(asked)
    except (TypeError, ValueError):
        return _RECORD_MAX_BYTES


def _rotate_record_log(target, rename):
    """Close the live segment at the ceiling and let the next one open itself.

    `rename` is the UNWRAPPED `os.rename`, handed in by the installer for the
    same reason the sink closes over the unwrapped `open`: a journal that moves
    its own file through the primitive it is reporting on is a recursion waiting
    for its first rotation.

    THERE IS NO PATH HERE ON WHICH A SEGMENT DISAPPEARS. Nothing is unlinked,
    truncated, pruned or aged out, and nothing counts the segments. `rename` is
    the only mutation, and a rename is a deletion of whatever it lands on, so the
    destination name has to be one no other process can also choose.

    It is, twice over. The name carries the rotating process's own pid and the
    microsecond it rotated at, and reaching the ceiling twice inside one
    microsecond in one process would mean writing tens of megabytes in that
    microsecond. The `lexists` check below is the belt on top of that: it costs
    one stat and it is what goes wrong first if somebody later simplifies the
    name into something that can repeat.

    Under concurrent writers the worst outcome is an extra SHORT segment, never
    a lost line. Two processes can both see the file over the ceiling; each
    renames whatever is at the path at its own moment, to its own name, so the
    second may close a segment holding a handful of lines. A writer holding an
    open handle across a rename keeps writing into the same inode, which is now
    the closed segment and still on disk.

    THIS FUNCTION NEVER RAISES, and that is its contract rather than a courtesy.
    The RECORD is what matters and the segment boundary is not. This sink sits on
    the path of every write into the operator's data, so a guard that raises out
    of its own bookkeeping stops his work over the tidiness of a measurement. The
    caller therefore needs no handler and has none.
    """
    try:
        limit = record_max_bytes()
        if limit <= 0:
            return
        if os.stat(target).st_size < limit:
            # No file yet, or gone out from under us: OSError lands below, and
            # either way there is nothing to close.
            return
        moment = time.time()
        segment = target.with_name(
            "{stem}.{stamp}.{micros:06d}-{pid}{suffix}".format(
                stem=target.stem,
                stamp=time.strftime("%Y%m%dT%H%M%S", time.gmtime(moment)),
                micros=int(moment * 1_000_000) % 1_000_000,
                pid=os.getpid(),
                suffix=target.suffix,
            )
        )
        if os.path.lexists(segment):
            return
        rename(target, segment)
    except Exception:  # noqa: BLE001 - see the contract above; this IS the log
        # Nothing is logged from here and nothing re-raises, deliberately: there
        # is no second place to log to, and the next record still lands one line
        # below. A journal that stopped GROWING is what a reader notices.
        return


def _caller_frames(limit=8):
    """Frames outside this file, plus the innermost one inside the workspace.

    Returns `(frames, caller)`. `frames` is the raw stack, innermost first, as
    'file:line:function'. `caller` is the first of them that is workspace code.

    The second field is the one worth having. Measured while building this:
    `Path.write_text` and `Path.mkdir` both put `pathlib.py` innermost, so the
    raw top frame names the standard library on most writes and says nothing
    about who is responsible. `sys.argv[0]` names the entry point but not the
    code inside it. `caller` is the field a derived rule can group by.

    A `.venv` path counts as outside the workspace even though it sits under the
    engine root; a frame in a third-party package is no more actionable than one
    in the stdlib.

    A SYNTHETIC `co_filename` is not a path and is never the caller. Frames from
    the import machinery, `exec` and the REPL carry names like
    `<frozen importlib._bootstrap>`, `<string>` and `<stdin>`, and
    `os.path.abspath` resolves each of them against the CURRENT WORKING
    DIRECTORY. Run from the engine root, which is where everything here runs,
    `abspath("<frozen importlib._bootstrap>")` is a path under the engine that is
    not under `.venv`, so the frozen frame satisfied both tests above and was
    reported as the innermost workspace frame. MEASURED 2026-09-09: a library
    writing the overlay from inside its own import was refused with
    `Innermost workspace frame: <frozen importlib._bootstrap>:241:_call_with_frames_removed`
    and the advice to commit that file, which names nothing anyone can commit.
    Worse, it took the decision away from the branch that should have had it:
    with no real workspace frame in the window, GUARD mode is documented to allow
    and record, and it never reached that branch because a name in angle brackets
    had been mistaken for one of ours.
    """
    import sys as _sys

    engine = str(Path(__file__).resolve().parents[2])
    venv = str(Path(engine) / ".venv")

    frames, caller = [], None
    frame = _sys._getframe(1)
    while frame is not None and len(frames) < limit:
        name = frame.f_code.co_filename
        synthetic = name.startswith("<") and name.endswith(">")
        absolute = os.path.abspath(name)
        if absolute != _THIS_FILE:
            entry = f"{name}:{frame.f_lineno}:{frame.f_code.co_name}"
            frames.append(entry)
            if caller is None and not synthetic \
                    and absolute.startswith(engine) \
                    and not absolute.startswith(venv):
                caller = entry
        frame = frame.f_back
    return frames, caller


def _record_overlay_write(text, verb):
    """Append one line about an allowed write. Never raises, never blocks."""
    if _RECORD_SINK is None:
        return
    import json
    import sys as _sys

    try:
        frames, caller = _caller_frames()
        line = json.dumps({
            "ts": time.time(),
            "pid": os.getpid(),
            "verb": verb,
            "path": text,
            "argv0": _sys.argv[0] if _sys.argv else "",
            "argv": [str(a)[:80] for a in _sys.argv[1:6]],
            "pytest": os.environ.get("PYTEST_CURRENT_TEST", "").split(" (")[0],
            "caller": caller,
            "frames": frames,
        }, ensure_ascii=True)
        _RECORD_SINK(line + "\n")
    except Exception:  # noqa: BLE001 - a measurement must never break the tool
        return


# A relative name plus a `dir_fd` names a file that no prefix test can see, and
# `shutil.rmtree` is built entirely out of that pair.
#
# MEASURED 2026-08-31, on the pretend overlay `/tmp/f2probe/pretend`: `rmtree` of
# a directory holding two files raised `OverlayWriteRefused` and left
# `children left: []`. BOTH files were already deleted. Only the final
# full-path `os.rmdir(path)` was ever refused, and the traceback it produced is
# indistinguishable from a write this guard actually prevented, so the guard
# reported that it had saved data it had just destroyed.
#
# The cause is the capability-set registration below, which is correct and stays:
# it keeps `shutil._use_fd_functions` True, which routes `_rmtree_safe_fd`
# through `os.unlink(entry.name, dir_fd=topfd)` with a RELATIVE name. The prefix
# test is a substring test, and `"a.txt"` contains no prefix.
_DIR_FD_UNRESOLVED = object()

# Set at install time. `None` means "not probed"; False means this platform could
# not resolve a descriptor, in which case the capability sets are deliberately
# NOT re-registered so `shutil` falls back to its full-path walk, which every
# wrapper here can see. Either the fds are readable or the fd algorithm is off:
# both are safe, and neither refuses a delete outside the overlay.
_DIR_FD_READABLE = None


def _resolve_dir_fd_path(target, dir_fd):
    """The absolute path a (relative name, `dir_fd`) pair actually names.

    Returns `target` unchanged when there is no descriptor or the name is
    already absolute, an absolute path when the descriptor resolved, and
    `_DIR_FD_UNRESOLVED` when it did not.
    """
    if dir_fd is None:
        return target
    try:
        text = os.fspath(target)
    except TypeError:
        return target
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    if os.path.isabs(text):
        return text
    try:
        base = os.readlink(f"/proc/self/fd/{int(dir_fd)}")
    except (OSError, ValueError, TypeError, OverflowError):
        return _DIR_FD_UNRESOLVED
    # `pathlib`, not the `os.path` join helper: the convention under `scripts/`.
    return str(Path(base) / text)


def _refuse_overlay_path(target, verb, dir_fd=None):
    if _MODE == MODE_OFF:
        # An `arm(MODE_OFF)` on an already-armed process leaves the wrappers in
        # place, and every branch below this one either records or raises, so OFF
        # behaved as REFUSE: the documented escape hatch inverted into a total
        # block. MEASURED 2026-08-31. `arm()` now disarms on OFF as well, and
        # this line is the belt for a mode set by hand.
        return
    if not _OVERLAY_PREFIXES and not _FOREIGN_PREFIXES:
        return
    if dir_fd is not None:
        resolved = _resolve_dir_fd_path(target, dir_fd)
        if resolved is _DIR_FD_UNRESOLVED:
            # Anomalous: the install-time probe passed, so a descriptor should
            # resolve. Refusing is right when the alternative is deleting the
            # operator's data without being able to say whether it was theirs.
            raise OverlayWriteRefused(
                f"cannot resolve dir_fd {dir_fd!r} to a directory, so a "
                f"request to {verb} {target!r} cannot be checked against the "
                "operator's live data. Refusing rather than guessing.")
        target = resolved
    try:
        text = os.fspath(target)
    except TypeError:
        return
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    # AHEAD of every mode branch, and unconditional. RECORD would log a write
    # into somebody else's repository and let it land, and GUARD would allow it
    # outright from any tracked file, which is every tool here. The only way
    # past this is `HEADING_OS_OVERLAY_GUARD=off`, which installs no wrappers at
    # all, and that is deliberately the same one-command escape an operator
    # already has: a false refusal he can lift in one line is the cheap
    # direction, and a write into a tree that is not his cannot be undone by
    # noticing it afterwards.
    if any(prefix in text for prefix in _FOREIGN_PREFIXES):
        raise OverlayWriteRefused(_foreign_refusal_message(text, verb))
    if not any(prefix in text for prefix in _OVERLAY_PREFIXES):
        return
    if _MODE == MODE_RECORD:
        _record_overlay_write(text, verb)
        return
    if _MODE == MODE_GUARD:
        _, caller = _caller_frames()
        if caller is not None and not _caller_is_tracked(caller):
            raise OverlayWriteRefused(_refusal_message(text, verb, caller=caller))
        if caller is None and _entry_point_has_no_file() \
                and _under_live_overlay(text):
            raise OverlayWriteRefused(
                _refusal_message(text, verb, caller=None, entryless=True))
        # Allowed, and still logged. The log is the only thing that makes an
        # always-on allow decision reviewable afterwards.
        _record_overlay_write(text, verb)
        return
    raise OverlayWriteRefused(_refusal_message(text, verb))


# The three spellings `sys.argv[0]` takes when the program the interpreter is
# running was never written down: `-` for `python - <<'PY'` and for anything
# piped in with an argument after it, `-c` for `python -c '...'`, and the empty
# string for a bare `python` reading a pipe or an interactive prompt. MEASURED
# 2026-09-22 on this machine's `.venv/bin/python`, one invocation per spelling.
#
# A LIST OF THREE AND NOT A CATEGORY, deliberately, against the usual rule that
# an enumeration is a list of holes. The category here would be "the entry point
# is not a file", and CPython gives no way to ask that: every other value of
# `argv[0]` is a path, a module file or a zip, and a fourth spelling would have
# to be invented by a future interpreter rather than reached by a user. The cost
# of being wrong is one direction only: a spelling missing from this set is
# allowed, which is today's behaviour, and nothing that works stops working.
_ENTRY_POINTS_WITH_NO_FILE = frozenset(("-", "-c", ""))


def _entry_point_has_no_file():
    """Was this program written down anywhere at all?

    False for an EMPTY `sys.argv`, which is neither of the two answers. An
    embedded interpreter can leave it empty, nothing in this tree does, and a new
    refusal has no business firing on a shape nobody has observed.

    Trivially defeated by assigning to `sys.argv[0]`, and that is not a gap this
    can close: the module docstring says the guard is against an ACCIDENT, and
    code that rewrites its own argv to get past a wall is not one.
    """
    import sys as _sys

    return bool(_sys.argv) and _sys.argv[0] in _ENTRY_POINTS_WITH_NO_FILE


def _under_live_overlay(text):
    """Is this path inside the OPERATOR'S overlay, rather than a scratch root?

    The distinction `_watched_roots()` draws and `_OVERLAY_PREFIXES` flattens
    away. It has to come back for the refusal above, or the new rule would fire
    on the very remedy the old one recommends: `_refusal_message` tells a probe
    to point HEADING_OS_DATA at a scratch directory, and a probe that obeys must
    not then be refused for obeying. MEASURED 2026-09-22 on the live journal, 244
    of the 1,108 file-less-entry-point writes went to a pytest `tmp_path` root,
    and every one of them is a run doing exactly the right thing.

    Structural, never `HEADING_OS_DATA`, for the reason
    `_structural_overlay_root` gives at length: the environment is the one thing
    the code being judged can change. Re-resolved per call rather than cached at
    `arm()` time, which costs two syscalls on a branch that MEASURED 2026-09-22
    carries 0.42% of overlay writes, and keeps `arm()`, `disarm()` and the
    module-copy handover out of this change entirely.
    """
    root = _structural_overlay_root()
    return root is not None and text.startswith(f"{root}{os.sep}")


# The tracked set, loaded ONCE and only when first needed. `None` means "not
# asked yet"; an empty frozenset would be indistinguishable from "git said
# nothing", which must not read as "nothing is tracked, refuse everything".
_TRACKED = None


def _tracked_in(repo):
    """`git ls-files` for one repo, absolute paths, or None when it cannot be asked."""
    import subprocess as _sp

    try:
        result = _sp.run(
            ["git", "ls-files", "-z"],
            capture_output=True, cwd=str(repo), timeout=30, check=True,
        )
    except Exception:  # noqa: BLE001 - no git, not a repo, timeout: all mean "unknown"
        return None
    # Joined as strings, not `repo / part`: git prints normalised relative
    # paths, so both spellings give the same set, and building one Path per
    # tracked file cost about 40 ms on every PreToolUse call that writes guard
    # state. MEASURED 2026-09-28: 97 ms -> 54 ms for both repos, sets equal.
    prefix = f"{repo}{os.sep}"
    return frozenset(
        prefix + part.decode("utf-8", "surrogateescape")
        for part in result.stdout.split(b"\0") if part
    )


def tracked_workspace_files(refresh=False):
    """Every path git tracks in BOTH repos, absolute. Cached per process.

    BOTH, and the second one is not a nicety. The engine's `git ls-files` covers
    2126 files; the private DATA overlay is a SEPARATE repository tracking 525
    Python files of its own, and some of them legitimately write the overlay:
    `build/runbook/admin/provision/provision_exec.py`,  # leak-guard: ok (measurement: the addresses as they were on the day measured)
    `build/runbook/admin/provision/identity_check.py`, and the per-letter  # leak-guard: ok (measurement: the addresses as they were on the day measured)
    renderers under `outputs/documents/`. Measured 2026-08-31,  # leak-guard: ok (measurement: the addresses as they were on the day measured)
    before this function asked the second repo: every one of them would have been
    refused as "untracked", which is the operator's own committed tooling being
    blocked by a guard that simply asked the wrong repository. A guard that
    refuses legitimate work is a guard that gets switched off, after which
    nothing guards anything.

    Those three are ILLUSTRATIONS and nothing compares against them. The
    decision is membership in the union below, so a tool that moves keeps
    writing and a name in this paragraph that rots costs a reader's time and
    nothing else. It did rot: the first two sat at a retired address here for a
    week after the provisioning tools moved, and MEASURED 2026-09-21 the guard
    permitted the real tool at its real address throughout, because it had never
    been asked about the name. Read this list as an example of the KIND of file
    that belongs, never as the set that is allowed.

    Loaded LAZILY, on the first attempted write into the overlay, not at
    `arm()`. Measured the same day: `git ls-files` over 2126 files costs under
    10 ms, but most processes never write the overlay at all, so most should
    never pay even that.

    Returns None when NEITHER repo could be read, and a None tracked set makes
    GUARD mode ALLOW and record rather than refuse. That direction is deliberate:
    a guard that cannot tell tracked from untracked must not refuse the
    operator's own tools because git was unavailable. A partial answer (one repo
    readable, the other not) is returned as the partial union rather than None,
    because refusing to answer would disarm the half that did work.
    """
    global _TRACKED
    if _TRACKED is not None and not refresh:
        return _TRACKED

    repos = [Path(__file__).resolve().parents[2]]
    overlay = _structural_overlay_root()
    if overlay is not None and (overlay / ".git").exists():
        repos.append(overlay)

    union, any_answer = set(), False
    for repo in repos:
        found = _tracked_in(repo)
        if found is None:
            continue
        any_answer = True
        union |= found
    if not any_answer:
        return None
    _TRACKED = frozenset(union)
    return _TRACKED


def _caller_is_tracked(caller):
    """Is the file in a 'path:line:function' frame string one git tracks?

    Splits from the RIGHT twice, because a filename can contain a colon while
    the line number and function name cannot.
    """
    tracked = tracked_workspace_files()
    if tracked is None:
        return True          # unknown, so do not refuse; see the docstring above
    path = caller.rsplit(":", 2)[0]
    return os.path.abspath(path) in tracked


# The operations that may still be aimed at a repository that is not ours,
# because the bytes travel INWARD and nothing in the other repository changes.
#
# DERIVED, on 2026-09-23, by reading the five tools that reach a foreign tree
# without writing one, and recording the argv each of them actually runs:
#
#     ["gh", "repo", "clone", "<org>/<repo>", "<dest>"]   three of the five
#     ["git", "pull"]                     cwd=<foreign>   two of the five
#     ["git", "pull", "--ff-only"]        cwd=<foreign>   one of the five
#     ["git", "log", "--since=...", ...]  cwd=<foreign>   one of the five
#
# `fetch` is here because it is `pull` without the merge and the operator's
# sentence is about what LEAVES; the rest are read-only plumbing observed in the
# banned tools' own read steps (`git diff --cached --quiet` before a commit,
# `git ls-files -z` before a copy) plus the few that cannot mutate a repository
# under any flag.
#
# AN ALLOW LIST AND NOT A DENY LIST, against the usual rule that an enumeration
# is a list of holes, because here the holes point the safe way. A verb missing
# from this set is a FALSE REFUSAL: loud, immediate, and lifted in one line.
# A verb missing from a deny list is a silent permission to change somebody
# else's repository, which is the thing being stopped.
_INWARD_SUBCOMMANDS = frozenset((
    "clone", "pull", "fetch", "log", "diff", "ls-files", "ls-remote", "show",
    "status", "cat-file", "rev-parse", "rev-list", "shortlog", "describe",
))

# Flags that take a SEPARATE value, so the value is not mistaken for the verb.
# `git -C <path> commit` is the shape that matters: read naively, `<path>` is
# the first bare word and `commit` is never examined at all.
_FLAGS_TAKING_A_VALUE = frozenset((
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--repo", "-R",
))


def _leading_subcommands(argv):
    """The command's verb, and for `gh` the noun before it. At most two words.

    git's grammar is `git [global flags] <verb> ...` and gh's is
    `gh [flags] <noun> <verb> ...`, so the words that say what a command DOES
    all sit before the first argument. Everything after the verb is skipped,
    which is what keeps `git commit -m "clone the repo"` from reading as a
    clone.
    """
    words, skip_next = [], False
    for token in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if token.startswith("-"):
            skip_next = token in _FLAGS_TAKING_A_VALUE
            continue
        words.append(token)
        if len(words) == 2:
            break
    return words


def _spawn_is_inward(argv):
    """Does this command only READ the repository it is aimed at?

    True only when the VERB is in `_INWARD_SUBCOMMANDS`, and the verb is the
    first leading word for `git` and the second for `gh`, whose grammar puts a
    noun first. A command that is neither has no such word and is therefore
    refused, which is deliberate: `cp`, `mv`, `rsync`, `tee` and `sed -i` are
    the shapes that change a tree without a verb anybody recognises.

    WHICH WORD IT IS matters, and reading "either of the first two" was the
    first draft. `git commit -m clone` puts the word `clone` second, so a rule
    that accepted either would have been talked past by a one-word commit
    message. The program decides how many words to skip, and nothing else does.
    """
    words = _leading_subcommands(argv)
    if not words:
        return False
    program = os.path.basename(argv[0]).lower()
    if program in ("gh", "gh.exe"):
        return len(words) > 1 and words[1] in _INWARD_SUBCOMMANDS
    return words[0] in _INWARD_SUBCOMMANDS


def _spawn_argv(cmd):
    """A command as a list of strings, or None when it cannot be read as one.

    None is the honest answer for a SHELL STRING, which is `Popen(..., shell=
    True)` or a bare string command: its words are the shell's to split and its
    redirections are invisible here. Nothing in this tree spawns that way, and
    a shape nobody has observed must not be the one a new refusal fires on. It
    is named in the docstring of `_refuse_foreign_spawn` rather than guessed at.
    """
    if isinstance(cmd, (list, tuple)):
        out = []
        for part in cmd:
            try:
                text = os.fspath(part)
            except TypeError:
                return None
            out.append(os.fsdecode(text) if isinstance(text, bytes) else text)
        return out
    return None


def _refuse_foreign_spawn(cmd, kwargs):
    """Refuse a child process that would CHANGE a repository that is not ours.

    The third layer, and the one that reaches the server. The other two refuse
    in-process Python primitives, so `git add`, `git commit` and `git push` walk
    straight past them: MEASURED 2026-09-23, three of the six tools the operator
    stopped do exactly that, each with `cwd` set to the other repository, and a
    commit sitting in somebody else's tree is carried away by whoever pushes
    that repository next. A ban that does not close this is half done in the
    expensive half.

    A spawn is AIMED at a foreign tree when its `cwd` is inside one or when any
    argument names a path inside one. It is then refused unless
    `_spawn_is_inward` recognises it as a read.

    WHAT THIS CANNOT SEE, named rather than implied, because a refusal has to be
    honest about its own reach:

    * A SHELL STRING (`shell=True`, or a command passed as one string). The
      words are the shell's to split; `_spawn_argv` returns None and this
      returns without refusing. Nothing in this tree spawns that way.
    * An argv ASSEMBLED AT RUN TIME from pieces that are not paths, so that no
      single argument contains the foreign prefix, with `cwd` left unset.
    * A path arriving through the child's ENVIRONMENT rather than its argv, and
      a `cwd` supplied POSITIONALLY rather than by keyword. `Popen` takes `cwd`
      ninth; every call in this tree passes it by name.
    * A RELATIVE path the child resolves itself against an inherited cwd.
    * A child that spawns its own grandchild. The wrappers live in THIS
      interpreter and do not travel.
    * `git config`, `git remote` and the other verbs that write `.git/` rather
      than tracked content are not in the inward set, so they are refused. That
      is over-refusal, not a hole, and it lifts with the switch.
    """
    if not _FOREIGN_PREFIXES:
        return
    argv = _spawn_argv(cmd)
    if argv is None or not argv:
        return
    aimed = []
    cwd = kwargs.get("cwd")
    if cwd is not None:
        try:
            text = os.fspath(cwd)
        except TypeError:
            text = None
        if text is not None:
            if isinstance(text, bytes):
                text = os.fsdecode(text)
            # A prefix ends in a separator, so a `cwd` naming the tree ROOT
            # carries none and would miss. Compared with one appended.
            if any(p in f"{text}{os.sep}" for p in _FOREIGN_PREFIXES):
                aimed.append(f"its working directory {text}")
    for token in argv:
        if any(p in f"{token}{os.sep}" for p in _FOREIGN_PREFIXES):
            aimed.append(f"its argument {token}")
            break
    if not aimed or _spawn_is_inward(argv):
        return
    raise OverlayWriteRefused(
        f"{argv[0]} was about to run against a repository that is not this "
        f"workspace's, named by {aimed[0]}, and "
        f"{' '.join(_leading_subcommands(argv)) or 'this command'} is not one of"
        " the operations that only read it. Cloning, pulling and fetching stay"
        " permitted, because those carry bytes INWARD and change nothing there."
        " Operator instruction 2026-09-23: nothing in another repository is to"
        f" change. Set {ENV_MODE}=off for that one command if you mean it, and"
        " say out loud which tree you are changing and why."
    )


def _foreign_refusal_message(text, verb):
    """What a refusal into somebody else's repository says.

    A different sentence from every branch of `_refusal_message`, because every
    one of those explains a judgement about the CALLER: whether the code is
    committed, whether it has a file at all, whether this is a test run. None of
    that applies here and repeating any of it would send the reader to commit
    something. The reason is the DESTINATION, and it is the same whoever asks.
    """
    import sys as _sys

    entry = "<unknown entry point>"
    if _sys.argv and _sys.argv[0]:
        entry = _sys.argv[0]
    elif _sys.argv:
        entry = "<the interpreter's standard input>"
    _, caller = _caller_frames()
    what = (f"{entry} tried to {verb} {text}, which is inside a repository that "
            "is not this workspace's.")
    if caller:
        what += f" Innermost workspace frame: {caller}."
    return what + (
        " This workspace is two repositories, the engine clone and the"
        " operator's own overlay beside it, and every other repository in that"
        " directory belongs to somebody else: another person's overlay, or a"
        " tree published from here rather than edited here. Operator"
        " instruction 2026-09-23: nothing in them is to change."
        f" If you genuinely mean to write there, set {ENV_MODE}=off for that one"
        " command and say out loud which tree you are writing and why."
    )


def _refusal_message(text, verb, caller=None, entryless=False):
    """What the refusal says, and it does not say who.

    Until the guard could arm outside pytest this message opened "a test tried
    to write", which was true of every caller that could reach it. It is not any
    more: a `.pth` arms plain scripts too, and the guard knows only that THIS
    process attempted the write. Naming a test would be the shape
    `.claude/rules/scope-claims.md` exists to stop, and it has already cost one
    investigation in this repository: a complaint reading "a test wrote" sent an
    agent hunting a test that had written nothing.

    So it reports the entry point and the innermost workspace frame, both of
    which the interpreter actually knows, and offers the remedy that fits which
    of the three kinds of caller this is.

    `entryless` is the third, added 2026-09-22 with the rule in
    `_refuse_overlay_path`: there is no calling FILE at all, because the program
    arrived on standard input or in `-c`. It takes precedence over the mode
    branches, since the mode is still GUARD and GUARD's sentence is false here.
    """
    import sys as _sys

    # A bare `python` reading a pipe leaves `argv[0]` an EMPTY STRING, and an
    # empty string dropped into "X tried to write" produces a sentence that
    # starts with a space and names nobody. Rendered, not printed raw.
    if not _sys.argv:
        entry = "<unknown entry point>"
    elif _sys.argv[0] == "":
        entry = "<the interpreter's standard input>"
    else:
        entry = _sys.argv[0]
    under_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST"))
    if caller is None:
        _, caller = _caller_frames()

    # WHO, then WHY, then the REMEDY, and the three are decided separately.
    #
    # They used to be one if-chain with the pytest branch first, and that chain
    # was wrong: under pytest a GUARD refusal reported the generic
    # HEADING_OS_DATA remedy and never said the actual reason, which is that the
    # calling file is not tracked. Found by this rule's own test, 2026-08-31.
    # Mode decides the REASON; being under pytest decides only the prefix and
    # which remedy is useful.
    who = ""
    if under_pytest:
        who = os.environ.get("PYTEST_CURRENT_TEST", "").split(" (")[0] + ": "

    what = f"{entry} tried to {verb} the operator's live data at {text}."
    if caller:
        what += f" Innermost workspace frame: {caller}."

    if entryless:
        # A DIFFERENT REASON, so a different sentence. The GUARD wording below
        # says "that file is not tracked by git", which is not merely unhelpful
        # here but false: there is no file. A refusal that names a cause the
        # reader cannot act on sends them looking for something to commit.
        how = {
            "-": "on standard input (`python - <<'PY'`, or a pipe)",
            "-c": "on the command line (`python -c`)",
            "": "on standard input (a bare `python` reading a pipe or a prompt)",
        }.get(_sys.argv[0] if _sys.argv else None, "with no file behind it")
        why = (
            f" This program reached the interpreter {how}, so it is not a file"
            " anywhere: it cannot be committed, cannot be reviewed, and will not"
            " exist once this process ends. Every tool that writes the"
            " operator's data in this workspace is a committed file."
        )
        remedy = (
            " Save this code to a file and commit it if it is operator tooling;"
            " point HEADING_OS_DATA at a scratch directory if it is a probe;"
            f" set {ENV_MODE}=off for this one command if it is neither and you"
            " mean it."
        )
    elif _MODE == MODE_GUARD:
        why = (
            " That file is not tracked by git, so this guard treats it as a probe,"
            " a harness or a scratch experiment rather than operator tooling."
            " `scripts/overlay-writer-census.py` lists every file in this"
            " checkout that can write the overlay and whether git tracks it."
            #
            # A COUNT USED TO SIT HERE and it had rotted. It read "every one of
            # the 300 files across both repositories", which came from a census
            # run in the main clone; the same command answers 114 over 561 files
            # in a worktree, where the sibling overlay repo does not exist to be
            # asked. MEASURED 2026-09-22. A number frozen into a runtime message
            # is a measurement nobody re-takes, so this points at the command
            # that answers for the checkout the reader is actually standing in.
        )
        remedy = (
            " If this code should be able to write the operator's data, commit it;"
            " if it is an experiment, point HEADING_OS_DATA at a scratch directory."
        )
    elif under_pytest:
        why = ""
        remedy = (
            " Point HEADING_OS_DATA at a tmp_path before anything that writes,"
            " and pass it to any child process too."
        )
    else:
        why = " This is not a test run."
        remedy = (
            f" If the write is legitimate operator work, set {ENV_MODE}=off for"
            f" this command; if it is a probe, a harness or an experiment, point"
            f" HEADING_OS_DATA at a scratch directory."
        )
    return who + what + why + remedy


def _install_overlay_write_guard():
    """Wrap the write primitives. Returns a callable that puts them back."""
    global _RECORD_SINK
    import builtins
    import io

    real_open = builtins.open
    real_replace, real_rename = os.replace, os.rename
    real_remove, real_unlink = os.remove, os.unlink
    # MEASURED 2026-08-31: the guard wrapped the file primitives and NOT the
    # directory ones, so a test reaching `write_text` failed loudly while one
    # reaching `mkdir` or `touch` planted a stray directory in the operator's
    # real private data in total silence. `git status` does not show an empty
    # directory either, so nothing downstream would have shown it. An audit of
    # the 31 test-reachable modules that resolve the data root at import time
    # found 17 of them bite through exactly this gap.
    real_mkdir, real_makedirs, real_rmdir = os.mkdir, os.makedirs, os.rmdir
    # `os.open`, separately from `builtins.open`. MEASURED the same day, by
    # driving the guard by hand: with the three directory calls wrapped and this
    # one not, `Path.touch()` was still ALLOWED and left a real file in the
    # operator's overlay. `Path.touch` does not go through `builtins.open` at
    # all - it calls `os.open` with O_CREAT directly. Wrapping the pretty name
    # and missing the primitive under it is how a guard reads complete and is
    # not. Only creating flags are refused, so an ordinary read still works.
    real_os_open = os.open
    # `sqlite3.connect` opens its file in C and never reaches `os.open`, so it
    # walked straight past every wrapper above. MEASURED 2026-08-31 by driving
    # the guard by hand: it created a real database in the operator's overlay and
    # reported ALLOWED. Two of the 35 modules that resolved the data root at
    # import time reached it when this was measured (`scripts/sentinel.py`,
    # read-write, and `.claude/hooks/memory-inject.py`, read-only; the second was
    # retired on 2026-09-01, which narrows the caller set and not the defect).
    import sqlite3 as _sqlite3

    real_sqlite_connect = _sqlite3.connect

    import subprocess as _subprocess

    real_sp_popen = _subprocess.Popen

    # The RECORD sink, closing over the UNWRAPPED `open` and the UNWRAPPED
    # `os.makedirs`. Both matter: the sink writes into the overlay's
    # `_record_dir_rel()`, which the wrapped versions would report, and a sink that runs
    # through the primitives it reports on is one refactor away from recursing.
    # Line-buffered append, opened per write: a long-lived handle would have to
    # be closed by something, and there is no shutdown hook in a plain script.
    # That same property is what lets the rotation below be a bare rename with
    # no coordination: no writer holds the file open between records.
    def _record_sink(line):
        target = record_log_path()
        if target is None:
            return
        if not target.parent.is_dir():
            real_makedirs(target.parent, exist_ok=True)
        # No handler: `_rotate_record_log` owns the promise that it never raises,
        # so the append below runs whether or not the segment turned.
        _rotate_record_log(target, real_rename)
        with real_open(target, "a", encoding="utf-8") as fh:
            fh.write(line)

    _RECORD_SINK = _record_sink

    def guarded_open(file, mode="r", *args, **kwargs):
        if _WRITE_MODE_CHARS & set(mode):
            _refuse_overlay_path(file, "write")
        return real_open(file, mode, *args, **kwargs)

    def guarded_replace(src, dst, *args, **kwargs):
        _refuse_overlay_path(dst, "replace")
        return real_replace(src, dst, *args, **kwargs)

    def guarded_rename(src, dst, *args, **kwargs):
        _refuse_overlay_path(dst, "rename onto")
        return real_rename(src, dst, *args, **kwargs)

    # `dir_fd` is keyword-only on all five primitives that take it, so reading it
    # out of `kwargs` sees every call. Passing it on is what makes a relative
    # name checkable; without it `shutil.rmtree` empties a directory and only the
    # closing `rmdir` is ever refused.
    def guarded_remove(path, *args, **kwargs):
        _refuse_overlay_path(path, "delete", dir_fd=kwargs.get("dir_fd"))
        return real_remove(path, *args, **kwargs)

    def guarded_unlink(path, *args, **kwargs):
        _refuse_overlay_path(path, "delete", dir_fd=kwargs.get("dir_fd"))
        return real_unlink(path, *args, **kwargs)

    def guarded_mkdir(path, *args, **kwargs):
        # Only refuse a call that would actually CREATE something. `Path.mkdir(
        # exist_ok=True)` still reaches `os.mkdir` and lets the resulting
        # FileExistsError through its own handler, so refusing unconditionally
        # rejected five tests that were creating nothing at all. Over-friction
        # is how a guard gets switched off, after which nothing guards the real
        # thing - so the test is "would this bring a new path into existence?",
        # not "does this call look like a write?".
        # Resolve the descriptor BEFORE the existence test. `os.path.exists`
        # takes no `dir_fd`, so a relative name would be tested against the
        # process cwd, which is a different directory entirely.
        probe = _resolve_dir_fd_path(path, kwargs.get("dir_fd"))
        if probe is _DIR_FD_UNRESOLVED or not os.path.exists(probe):
            _refuse_overlay_path(path, "create a directory in",
                                 dir_fd=kwargs.get("dir_fd"))
        return real_mkdir(path, *args, **kwargs)

    def guarded_makedirs(name, *args, **kwargs):
        if not os.path.exists(name):
            _refuse_overlay_path(name, "create a directory tree in")
        return real_makedirs(name, *args, **kwargs)

    def guarded_rmdir(path, *args, **kwargs):
        _refuse_overlay_path(path, "remove a directory from",
                             dir_fd=kwargs.get("dir_fd"))
        return real_rmdir(path, *args, **kwargs)

    _CREATING_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & _CREATING_FLAGS:
            _refuse_overlay_path(path, "open for writing",
                                 dir_fd=kwargs.get("dir_fd"))
        return real_os_open(path, flags, *args, **kwargs)

    def guarded_sqlite_connect(database, *args, **kwargs):
        # A READ-ONLY connection is allowed, and that is not a softening. It
        # creates nothing and writes nothing, and this workspace opens databases
        # exactly that way on purpose: `scripts/utils/sqlite_uri.read_only_uri()`
        # is the one sanctioned spelling (`?mode=ro`, `uri=True`), used by the
        # cookie readers and the CodeGraph symbol source, and it was how the
        # retired `.claude/hooks/memory-inject.py` read the operator's memory
        # index. Refusing it would be the over-friction that gets a guard
        # switched off. Everything else can create or write, so it is refused.
        target = database
        read_only = False
        if kwargs.get("uri") and isinstance(database, str):
            from urllib.parse import parse_qs, urlparse
            parsed = urlparse(database)
            target = parsed.path or database
            read_only = parse_qs(parsed.query).get("mode", [""])[0] == "ro"
        if not read_only and target not in (":memory:", ""):
            _refuse_overlay_path(target, "open a database for writing in")
        return real_sqlite_connect(database, *args, **kwargs)

    # `io.open` and `builtins.open` are the same object, and pathlib reaches the
    # one on `io`. Both names are rebound or `Path.write_text` walks straight
    # past the guard.
    builtins.open = guarded_open
    io.open = guarded_open
    os.replace, os.rename = guarded_replace, guarded_rename
    os.remove, os.unlink = guarded_remove, guarded_unlink
    # `Path.mkdir` and `Path.touch` reach `os.mkdir` and `os.open`; the latter is
    # already covered through `builtins.open`/`io.open` above, so wrapping the
    # three directory calls closes the pair.
    os.mkdir, os.makedirs, os.rmdir = guarded_mkdir, guarded_makedirs, guarded_rmdir
    def _child_reaches_live_overlay(kwargs) -> bool:
        """True when the child could resolve the operator's real overlay.

        The env it will actually get is `os.environ` unless `env=` overrides it.
        A child with `HEADING_OS_DATA` pointing somewhere else is safe; one with
        it absent, or pointing inside the live tree, is a suspect.
        """
        env = kwargs.get("env")
        pinned = (os.environ if env is None else env).get("HEADING_OS_DATA")
        if not pinned:
            return True
        return any(prefix.rstrip(os.sep) in pinned for prefix in _OVERLAY_PREFIXES)

    def _record_spawn(cmd, kwargs):
        global _CHILD_SPAWN_COUNT, _CHILD_SPAWN_UNATTRIBUTED
        try:
            if not _child_reaches_live_overlay(kwargs):
                return
            _CHILD_SPAWN_COUNT += 1
            head = " ".join(str(a) for a in cmd)[:120] if isinstance(cmd, (list, tuple)) \
                else str(cmd)[:120]
            nodeid = os.environ.get("PYTEST_CURRENT_TEST", UNKNOWN_TEST).split(" (")[0]
            # Attribution BEFORE the example cap, and that ordering is the fix.
            # `_CHILD_SPAWNS` fills at 200 and then records nothing, so under
            # `-n auto` the report described the first 200 spawns of the
            # controller and none of the workers' ten thousand.
            entry = _CHILD_SPAWNS_BY_TEST.get(nodeid)
            if entry is not None:
                entry[0] += 1
            elif len(_CHILD_SPAWNS_BY_TEST) < _CHILD_SPAWN_BY_TEST_CAP:
                _CHILD_SPAWNS_BY_TEST[nodeid] = [1, head]
            else:
                _CHILD_SPAWN_UNATTRIBUTED += 1
            # The cap bounds the EXAMPLES kept, never the count above.
            if len(_CHILD_SPAWNS) >= _CHILD_SPAWN_CAP:
                return
            _CHILD_SPAWNS.append((nodeid, head))
        except Exception:  # noqa: BLE001 - a diagnostic must never break a run
            return

    # ONLY `Popen` is wrapped. `subprocess.run`, `call`, `check_call` and
    # `check_output` all construct a `Popen`, so wrapping `run` as well recorded
    # every `run` TWICE: measured, four spawns produced five records. Wrap the
    # primitive, not the convenience wrapper over it. This is the same shape as
    # `Path.touch` reaching `os.open` rather than `builtins.open`.
    class _GuardedPopen(real_sp_popen):
        def __init__(self, *args, **kwargs):
            if args:
                # REFUSAL FIRST, and OUTSIDE `_record_spawn`. That function
                # swallows every exception on purpose, because a diagnostic must
                # never break a run; a refusal raised inside it would be
                # swallowed too and the wall would be a wall that never says no.
                # This is the only line in this class that can raise, and it
                # raises BEFORE the child exists rather than after.
                _refuse_foreign_spawn(args[0], kwargs)
                _record_spawn(args[0], kwargs)
            super().__init__(*args, **kwargs)

    os.open = guarded_os_open
    _sqlite3.connect = guarded_sqlite_connect
    _subprocess.Popen = _GuardedPopen

    # ------------------------------------------------------------
    # Keep the stdlib's capability detection true
    # ------------------------------------------------------------
    #
    # `os.supports_dir_fd`, `os.supports_fd` and `os.supports_follow_symlinks`
    # are sets of FUNCTION OBJECTS, and the standard library tests membership by
    # identity to decide which algorithm to use. `shutil` does it at import:
    #
    #     _use_fd_functions = ({os.open, os.stat, os.unlink, os.rmdir}
    #                          <= os.supports_dir_fd and ...)
    #
    # A wrapper is a different object, so replacing `os.open` silently removes it
    # from every one of those sets and any module importing AFTER the wrap sees a
    # platform that has lost a capability it actually has.
    #
    # MEASURED 2026-08-31, the day this armed by default:
    # `shutil._use_fd_functions` was True unguarded and False guarded, so
    # `shutil.rmtree` took its legacy walk instead of the file-descriptor one.
    # `tests/test_a_radar_that_watched_three_of_fourteen_layers.py::
    # test_an_unreadable_directory_is_removed_not_raised_over` failed with
    # `OSError: Directory not empty`, and passed with the guard off. A guard that
    # changes which algorithm the standard library runs is doing far more than it
    # was asked to.
    #
    # The wrappers forward `*args, **kwargs` verbatim, so each one genuinely
    # supports whatever its original did. Registering them says something true.
    _CAPABILITY_SETS = (
        os.supports_dir_fd, os.supports_fd,
        os.supports_follow_symlinks, os.supports_effective_ids,
    )
    _WRAPPED_PAIRS = (
        (real_open, guarded_open), (real_replace, guarded_replace),
        (real_rename, guarded_rename), (real_remove, guarded_remove),
        (real_unlink, guarded_unlink), (real_mkdir, guarded_mkdir),
        (real_makedirs, guarded_makedirs), (real_rmdir, guarded_rmdir),
        (real_os_open, guarded_os_open),
    )
    # Registering the wrappers keeps `shutil._use_fd_functions` True, which is
    # the whole reason `rmtree` reaches the guard through relative names plus a
    # `dir_fd`. That is only safe while a descriptor can be resolved back to a
    # directory, so probe it ONCE, here, rather than trusting the platform.
    #
    # If the probe fails, do not register: `shutil` then falls back to its
    # full-path walk, which every wrapper above sees directly. Either the
    # descriptors are readable or the descriptor algorithm is off. Both are safe,
    # and neither refuses a delete outside the overlay, which is what an
    # unconditional refusal would have done to every `rmtree` in the suite.
    global _DIR_FD_READABLE
    _probe_fd = None
    try:
        _probe_fd = real_os_open(os.getcwd(), os.O_RDONLY)
        _DIR_FD_READABLE = os.path.isdir(os.readlink(f"/proc/self/fd/{_probe_fd}"))
    except OSError:
        _DIR_FD_READABLE = False
    finally:
        if _probe_fd is not None:
            with contextlib.suppress(OSError):
                os.close(_probe_fd)

    _registered = []
    if _DIR_FD_READABLE:
        for capability in _CAPABILITY_SETS:
            for original, wrapper in _WRAPPED_PAIRS:
                if original in capability:
                    capability.add(wrapper)
                    _registered.append((capability, wrapper))

    def restore():
        builtins.open = real_open
        io.open = real_open
        os.replace, os.rename = real_replace, real_rename
        os.remove, os.unlink = real_remove, real_unlink
        os.mkdir, os.makedirs, os.rmdir = real_mkdir, real_makedirs, real_rmdir
        os.open = real_os_open
        _sqlite3.connect = real_sqlite_connect
        _subprocess.Popen = real_sp_popen
        # Leaving the wrappers registered would leave dead objects advertising a
        # capability, and a later module could pick one out of the set.
        for capability, wrapper in _registered:
            capability.discard(wrapper)
        _registered.clear()

    return restore


_RESTORE_WRITE_GUARD = None


ENV_MODE = "HEADING_OS_OVERLAY_GUARD"


def resolve_mode(explicit=None, default=MODE_REFUSE):
    """Which mode `arm()` will use, and why. Pure, so a test can ask it.

    An explicit argument wins, then `HEADING_OS_OVERLAY_GUARD`, then `default`.

    An UNRECOGNISED value falls back to `default` rather than being honoured or
    silently ignored. A typo in the variable must never be the thing that softens
    a guard: `HEADING_OS_OVERLAY_GUARD=recrod` reading as RECORD would allow every
    write and log none, and reading as OFF would allow every write and say
    nothing at all. Both are worse than either real mode.

    `default` is REFUSE for a direct caller, and the `.pth` that arms every
    process passes GUARD. Two different defaults on purpose: an explicit
    `arm()` in code is almost always a test harness, and the process-wide arming
    is the always-on control.
    """
    if explicit is not None:
        return explicit if explicit in _MODES else default
    asked = os.environ.get(ENV_MODE)
    return asked if asked in _MODES else default


def arm_process_wide():
    """The entry point the `.pth` calls at interpreter startup.

    GUARD by default, so the control is on without anyone remembering to turn it
    on, and `HEADING_OS_OVERLAY_GUARD=off` is the one-command way past it. No
    snapshot, because walking 11,000 files costs half a second and an arbitrary
    process has no end-of-run moment to diff it against.

    STRUCTURAL ROOT ONLY, and that is not a simplification. MEASURED 2026-08-31:
    62 tests went red the first time this was armed by default. `_overlay_root()`
    does `from scripts.utils.paths import ...`, which binds the name `scripts` in
    `sys.modules` to the ENGINE's package during `site.py`, before any user code
    runs. After that, `python -m scripts.anything` from a directory with its own
    `scripts/` package resolves against the engine instead, and the skill-creator
    tests that do exactly that failed with `No module named
    scripts.improve_description`.

    An always-on startup hook must not decide what `scripts` means for the rest
    of the interpreter. `_structural_overlay_root()` needs no import at all: it
    walks up from `__file__`. The session-scoped second root is what
    `_overlay_root()` is for, and only a bounded run has any use for it, so
    `tests/conftest.py` still gets it by calling `arm()` directly.
    """
    arm(resolve_mode(default=MODE_GUARD), structural_only=True)


# Written onto the installed `builtins.open` wrapper, so the marker travels with
# the PROCESS state rather than with any one module copy. An attribute on the
# live primitive is the only place every copy of this module can agree to look.
_OWNER_ATTR = "_heading_os_overlay_guard_owner"

# The namespace whose wrappers this copy displaced when it took over, so
# `disarm()` can hand back instead of leaving the process unguarded.
_DISPLACED_OWNER = None


def _primitive_slots():
    """Every (module, attribute) pair the wrappers rebind.

    Derived from one place so a snapshot and `restore()` cannot drift apart. A
    name added to the wrappers and forgotten here would make a hand-back
    incomplete, and nothing checks it since 2026-09-21: add a wrapper's slot
    here in the same edit.
    """
    import builtins
    import io
    import sqlite3
    import subprocess
    return (
        (builtins, "open"), (io, "open"),
        (os, "replace"), (os, "rename"), (os, "remove"), (os, "unlink"),
        (os, "mkdir"), (os, "makedirs"), (os, "rmdir"), (os, "open"),
        (sqlite3, "connect"), (subprocess, "Popen"),
    )


def _snapshot_primitives():
    return tuple((mod, attr, getattr(mod, attr)) for mod, attr in _primitive_slots())


def _apply_primitives(snapshot):
    for mod, attr, value in snapshot:
        setattr(mod, attr, value)


def _mark_owner(namespace):
    """Record which module copy's wrappers are the live ones."""
    import builtins
    try:
        builtins.open.__dict__[_OWNER_ATTR] = namespace
    except AttributeError:  # a builtin with no __dict__: nothing installed
        return


def _installed_owner():
    """The globals of the copy whose wrappers are live, or None if unwrapped."""
    import builtins
    return getattr(builtins.open, _OWNER_ATTR, None)


def _carry_spawn_count(extra: int) -> None:
    """Add a displaced copy's reachable-child count to this one's.

    A function rather than an inline `+=` inside `arm()`, and that is not
    style. An augmented assignment to a module global inside a function makes
    the name LOCAL for that whole function, so `arm()` raised UnboundLocalError
    on its first read and every caller of the guard silently stopped arming.
    MEASURED 2026-09-03: nine tests went red, including the one that drives a
    real refused write, and the guard reported nothing at all.
    """
    global _CHILD_SPAWN_COUNT
    _CHILD_SPAWN_COUNT += extra


def top_spawners(limit=10, source=None):
    """`(nodeid, example command, count)` for the busiest spawners, most first.

    `source` defaults to this process's own map. The controller of a sharded
    run passes the aggregate it built from the workers' reports, because its
    own map holds nothing but the execnet bootstrap of the workers themselves.

    Ties break on nodeid so the same run twice prints the same order; without
    it a reader diffing two reports sees churn that means nothing.
    """
    if source is None:
        source = _CHILD_SPAWNS_BY_TEST
    ranked = sorted(source.items(), key=lambda kv: (-kv[1][0], kv[0]))
    return [(nodeid, entry[1], entry[0]) for nodeid, entry in ranked[:limit]]


def spawner_digests(source=None) -> set:
    """One short digest per DISTINCT spawning nodeid in this process.

    A set of digests rather than a count, and that is the whole point. Counts
    from several processes can only be ADDED, and addition assumes no nodeid is
    seen twice: true for real nodeids under xdist's default `load` scheduling,
    false for `--dist each`, and false for `UNKNOWN_TEST`, which every worker
    produces for its own session-level spawns. A union has no such assumption
    and needs no wire budget to speak of: 8 bytes per distinct test against the
    ~80 a nodeid costs, so the whole map fits inside the slice budget the
    attribution rows already spend.

    blake2b truncated to 8 bytes. At the cap of 2000 distinct tests per process
    the chance of one collision across the whole run is about 1e-13, which is
    below the rate at which the count changes for real reasons.
    """
    import hashlib
    if source is None:
        source = _CHILD_SPAWNS_BY_TEST
    return {hashlib.blake2b(nodeid.encode("utf-8", "replace"),
                            digest_size=8).hexdigest()
            for nodeid in source}


def merge_spawn_attribution(entries, unattributed=0, into=None):
    """Fold `(nodeid, example command, count)` triples into an attribution map.

    Used twice, and both callers matter. `arm()` folds a displaced module
    copy's map into this one, the same handover `_carry_spawn_count` performs
    for the bare total. `tests/conftest.py` folds each xdist worker's report
    into the controller's aggregate.

    THE LIMIT, which is the whole reason this takes counts rather than events:
    what arrives from a worker is already the worker's TOP slice, not its whole
    map, so the aggregate is a lower bound per test and cannot see a test that
    spawned a few children on every one of eight workers. It is built to name
    the heavy spawners, which is what a repair needs, and it will not name the
    long tail. `_CHILD_SPAWN_COUNT` remains the only complete number.
    """
    global _CHILD_SPAWN_UNATTRIBUTED
    target = _CHILD_SPAWNS_BY_TEST if into is None else into
    for nodeid, cmd, count in entries:
        entry = target.get(nodeid)
        if entry is not None:
            entry[0] += count
        elif len(target) < _CHILD_SPAWN_BY_TEST_CAP:
            target[nodeid] = [count, cmd]
        else:
            # Over the cap, whichever map this is. Counted, never silent: an
            # attribution list that quietly drops rows is the report claiming
            # coverage it does not have, which is the defect this whole file
            # is a repair for.
            _CHILD_SPAWN_UNATTRIBUTED += count
    _CHILD_SPAWN_UNATTRIBUTED += unattributed
    return target


def arm(mode=None, snapshot=None, structural_only=False):
    """Install the wrappers, and optionally take the before-snapshot.

    Snapshot FIRST when it is taken at all, so a write made while arming is
    still visible as a change; then the prefixes, then the wrappers. A clone
    with no overlay on disk gets an empty root set and arms nothing, which is
    the public-clone and CI case.

    `mode` defaults to whatever `resolve_mode()` decides, which is REFUSE unless
    the environment asks otherwise. Callers that must not be softened by an
    environment variable pass a mode explicitly; `tests/conftest.py` does.

    `snapshot` defaults to True for REFUSE and RECORD and False for GUARD, and
    the reason is a measurement. Walking the overlay costs about half a second
    for roughly 11,000 files: 0.53 s against 0.01 s for a bare interpreter,
    measured 2026-08-31. That is nothing beside a four-minute test suite, and it
    is unacceptable on every `python` invocation in the workspace, which is what
    GUARD mode is for. The snapshot also has no consumer there: it exists to be
    diffed at the END of a bounded run, and an arbitrary script has no such
    moment. The wrappers, which are the half that refuses, cost nothing to
    install.
    """
    global _WATCH_BEFORE, _WATCH_BEFORE_AT, _OVERLAY_PREFIXES, _RESTORE_WRITE_GUARD
    global _MODE, _DISPLACED_OWNER, _FOREIGN_PREFIXES
    _MODE = resolve_mode(mode)
    if _MODE == MODE_OFF:
        # Install NOTHING. Not "install and allow": a wrapped primitive that
        # always returns is still a wrapper on every `open()` in the process, and
        # the escape hatch has to cost nothing or it is not an escape.
        #
        # `disarm()` rather than a bare return. MEASURED 2026-08-31: returning
        # here left `_OVERLAY_PREFIXES` and the wrappers from an EARLIER `arm()`
        # in place, and `_refuse_overlay_path` raised unconditionally once past
        # the RECORD and GUARD branches. So OFF behaved as REFUSE, and the one
        # documented escape an operator has when the guard refuses their work
        # wrongly became a total block instead. Reachable whenever the `.pth`
        # arms at startup and anything later exports the variable and re-arms.
        disarm()
        _MODE = MODE_OFF        # `disarm()` resets the mode; OFF is the answer
        # BOUND, stated rather than papered over: this turns off the layer THIS
        # copy installed. It does not reach a layer owned by a different copy of
        # this module, and deliberately so. Reaching across was tried and it
        # broke the case that armed a throwaway copy OFF and required the live
        # session to stay guarded. That case went in the 2026-09-21 split; the
        # bound above did not, and it is the code below that keeps it.
        #
        # The operator's escape hatch does not need it. MEASURED 2026-08-31:
        # `HEADING_OS_OVERLAY_GUARD=off .venv/bin/python` gives 0 wrapper layers
        # and loads no copy of this module at all, because the `.pth` resolves
        # the same variable before it arms. The escape works at its source; only
        # a mid-process change of the variable reaches this line, and that is not
        # the escape-hatch path.
        return
    if snapshot is None:
        snapshot = _MODE != MODE_GUARD
    if snapshot:
        _WATCH_BEFORE_AT = time.time()
        _WATCH_BEFORE = _watch_snapshot()
    if structural_only:
        # No `_overlay_root()`, so no `scripts` import. See `arm_process_wide()`
        # for the 62 tests that measured why.
        structural = _structural_overlay_root()
        roots = {} if structural is None else {_LIVE_OVERLAY_LABEL: structural}
    else:
        roots = _watched_roots()
    # Resolved here rather than at import, so a tree that appears or is removed
    # between two runs is seen, and so that a copy of this module under a
    # throwaway engine answers about ITS OWN siblings and never about the real
    # workspace's. `structural_only` does not reach it: it selects which of the
    # two questions in `_watched_roots()` is asked, and both are about where
    # this workspace writes, while a foreign tree is nobody's data root.
    foreign = _foreign_prefixes()
    if not roots and not foreign:
        return
    _FOREIGN_PREFIXES = foreign
    _OVERLAY_PREFIXES = tuple(f"{root}{os.sep}" for root in roots.values())
    # Install once per PROCESS, not once per module copy.
    #
    # MEASURED 2026-08-31: `builtins.open layers: 2` and a `Popen` MRO carrying
    # `_GuardedPopen` twice, in a plain `.venv/bin/python`. CPython's `site.py`
    # processes a venv's site-packages TWICE (`site.venv()` then `site.main()`),
    # and `known_paths` de-duplicates only sys.path ENTRIES, not `.pth`
    # execution. So the `.pth` body ran twice, built two module objects, and
    # `sys.modules` kept only the second. The install-once check below is a
    # module global, so copy #2 never saw copy #1's install, and copy #1 is
    # unreachable through `sys.modules` for the life of the interpreter: its
    # `restore()` can never be called by anything. Under pytest a third layer
    # arrived from `tests/conftest.py`.
    #
    # HANDOVER, not delegation, and not a bare skip. Each of the three is a
    # different bug and only one is right:
    #
    # * A bare skip leaves the process on the `.pth`'s GUARD mode while the
    #   caller believes it armed REFUSE. That is a silent SOFTENING.
    # * Delegating (pushing this copy's mode and prefixes into the owner's
    #   globals, leaving the owner's wrappers live) was tried and FAILED LOUDLY:
    #   4 tests went red because two module copies have two distinct
    #   `OverlayWriteRefused` CLASSES, so the wrappers raised the owner's class
    #   and `pytest.raises(cf.OverlayWriteRefused)` matched nothing. Every other
    #   name shared across a copy boundary carries the same hazard.
    # * Handover keeps ONE layer AND puts it in the copy that arms last, which is
    #   the copy reachable through `sys.modules` and therefore the one whose
    #   `restore()` and whose exception class the rest of the tree can see.
    #
    # And the handover HANDS BACK on `disarm()`. Without that, a test that arms a
    # throwaway copy takes the live session guard away and its own `disarm()`
    # leaves the process with NO guard at all for every later test. MEASURED:
    # the 4 tests that went red were all green in isolation and red in a combined
    # run, which is the signature of exactly this. `disarm()` must return the
    # process to the state before `arm()`, not to bare primitives.
    owner = _installed_owner()
    if owner is not None and owner is not globals():
        # Snapshot EVERYTHING FIRST, while the owner's wrappers are still bound
        # and its globals still hold its real settings. Taking the snapshot after
        # `restore()` captures the bare primitives instead, and then the
        # hand-back returns a process with no guard at all: MEASURED, 9 tests in
        # `test_a_guard_that_armed_under_pytest_and_nowhere_else.py` each left
        # the live session unguarded for every test that followed.
        #
        # Identity is why this is a snapshot and not a re-install: freshly
        # built wrappers would not be the objects captured, and nothing checks
        # identity across a hand-back since 2026-09-21, so preserve it by hand.
        _DISPLACED_OWNER = (owner, owner.get("_OVERLAY_PREFIXES", ()),
                            owner.get("_MODE", MODE_REFUSE),
                            _snapshot_primitives(),
                            owner.get("_RESTORE_WRITE_GUARD"),
                            _installed_owner())
        restore = owner.get("_RESTORE_WRITE_GUARD")
        if restore is not None:
            restore()
            owner["_RESTORE_WRITE_GUARD"] = None
        owner["_OVERLAY_PREFIXES"] = ()
        # Spawns recorded before the handover are still suspects, so carry them.
        _CHILD_SPAWNS.extend(owner.get("_CHILD_SPAWNS") or ())
        _carry_spawn_count(owner.get("_CHILD_SPAWN_COUNT") or 0)
        merge_spawn_attribution(
            [(nodeid, entry[1], entry[0]) for nodeid, entry
             in (owner.get("_CHILD_SPAWNS_BY_TEST") or {}).items()],
            unattributed=owner.get("_CHILD_SPAWN_UNATTRIBUTED") or 0)
    # Install ONCE per process. A second `arm()` refreshes the mode, the prefixes
    # and the snapshot, and deliberately does NOT wrap again. Two layers of
    # wrappers is not a cosmetic problem: `restore()` unwinds exactly one, so the
    # inner layer would survive `disarm()` and keep refusing writes for the rest
    # of the process, after the code that installed it believed it was gone.
    # This is reachable now that a `.pth` can arm at interpreter startup and
    # `tests/conftest.py` then arms again at `pytest_sessionstart`.
    #
    # The `_installed_owner()` handover above is what actually enforces this
    # across module copies; this check alone could not, because it is a module
    # global and copy #2 cannot see copy #1's.
    if _RESTORE_WRITE_GUARD is None:
        _RESTORE_WRITE_GUARD = _install_overlay_write_guard()
    _mark_owner(globals())


def disarm():
    """Put the primitives back. A no-op when `arm()` installed nothing."""
    global _RESTORE_WRITE_GUARD, _OVERLAY_PREFIXES, _RECORD_SINK, _MODE
    global _DISPLACED_OWNER, _FOREIGN_PREFIXES
    if _RESTORE_WRITE_GUARD is not None:
        _RESTORE_WRITE_GUARD()
        _RESTORE_WRITE_GUARD = None
        if _DISPLACED_OWNER is not None:
            # Hand back. This copy took the wrappers from another copy, so
            # "put the primitives back" means back to THAT copy's guard, not to
            # bare primitives. Otherwise one throwaway `arm()`/`disarm()` pair
            # leaves the whole process unguarded, silently, for good.
            prior, prefixes, mode, snapshot, restore, marker = _DISPLACED_OWNER
            _DISPLACED_OWNER = None
            prior["_OVERLAY_PREFIXES"] = prefixes
            prior["_MODE"] = mode
            prior["_RESTORE_WRITE_GUARD"] = restore
            _apply_primitives(snapshot)
            # The marker lives on the wrapper object, and the snapshot restored
            # that object, so re-stamp it or the next `arm()` sees an unowned
            # process and stacks a second layer.
            if marker is not None:
                _mark_owner(marker)
    # NOTHING here reaches into another module copy. A copy that installed
    # nothing disarms nothing. MEASURED: an earlier draft did reach out, and
    # `test_off_mode_installs_nothing_at_all` (which calls `arm(MODE_OFF)` on a
    # FRESH copy that had never armed) silently tore down the live session
    # guard, taking 4 later tests with it in a combined run while every one of
    # them passed alone. Turning the process-wide guard off is `arm(MODE_OFF)`'s
    # job, where the operator actually asked for it.
    _OVERLAY_PREFIXES = ()
    # Cleared beside the overlay prefixes, for the reason the line above them
    # exists: this copy installed no wrappers any more, so leaving either tuple
    # set would make an uninstalled copy read as armed. The handover snapshot
    # deliberately does NOT carry this one. A displaced copy's foreign prefixes
    # are never consulted by the live copy's wrappers, which read their own, and
    # on hand-back the prior copy still holds the tuple it computed for its own
    # tree; clearing it there would be the one way to hand back a process with
    # the foreign layer gone.
    _FOREIGN_PREFIXES = ()
    # The sink closes over the primitives `restore()` just put back, so leaving
    # it bound would let a later `_refuse_overlay_path` log through a guard that
    # is no longer installed.
    _RECORD_SINK = None
    _MODE = MODE_REFUSE


def watch_complaints(before, after):
    """Pure diff of two `_watch_snapshot()` results. Public so a test can drive it.

    A directory present in `before` and absent from `after` is itself reported:
    a run that removed the whole archive must not read as a clean pass.

    The wording says the overlay CHANGED, never that "a test wrote" it, and the
    difference is not pedantry. This is a whole-session before/after diff: it
    knows the tree moved between two instants and it knows nothing whatever
    about who moved it. On 2026-08-31 a run of this suite reported four rewritten
    files under docs/ and templates/; the cause was a second agent editing those
    documents and running the doc regenerator at 05:00:47, inside the window.
    The earlier wording had already cost one investigation that day: a complaint
    reading "a test wrote" sent an agent hunting a test that had written nothing,
    and it took an audit hook to establish the negative.

    So the message reports the observation, and the reader draws the inference.
    The in-process guard above is the half that CAN name a culprit, because it
    refuses at the moment of the write and the traceback carries the test.
    """
    complaints = []
    for label, (directory, snapshot) in before.items():
        if label not in after:
            complaints.append(f"{label} at {directory} disappeared during the run")
            continue
        now = after[label][1]
        added = sorted(set(now) - set(snapshot))
        removed = sorted(set(snapshot) - set(now))
        resized = sorted(
            n for n in set(snapshot) & set(now)
            if snapshot[n] is not None and snapshot[n] != now[n]
        )
        for what, names in (("appeared", added), ("vanished", removed), ("changed", resized)):
            if names:
                complaints.append(
                    f"{len(names)} file(s) {what} under the operator's live "
                    f"{label} at {directory} between the start and the end of "
                    f"the run: {names[:5]}"
                )
    return complaints
