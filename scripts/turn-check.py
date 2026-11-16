#!/usr/bin/env python3
"""turn-check.py - the fast check a turn should not end without.

Answers one question about the uncommitted working tree: did the Python edits
made so far break anything a cheap check can see? It is deliberately NOT the
full suite. The full suite is `scripts/run-tests.py` and takes minutes; this
takes seconds, because something that runs at the end of every turn only helps
if nobody is tempted to skip it.

Whose edits. `git` knows a file changed, never who changed it, and this
workspace runs more than one session against one checkout. Pass
`--session-transcript` (the Stop hook passes the path Claude Code gives it) and
the changed set is narrowed to the files THIS session wrote, with the drop
count reported rather than swallowed. Without it the scope is the whole working
tree, which is right for a hand run from a terminal that belongs to no session.
Measured 2026-08-12: without this narrowing the hook blocked a turn over a
deliberately-red TDD test a parallel session had written a minute earlier.

Why it exists. On 2026-08-09 a one-line constant rename in
`scripts/wizard-verify-key.py` broke four tests, and that was discovered only
because a full suite happened to be run by hand later in the session. Nothing
was watching the end of a turn. Anthropic's own Claude Code guidance puts a
verification hook first for exactly this reason.

Three lanes, cheapest first, each bounded:

  compile  every changed .py through py_compile (milliseconds)
  import   every changed LIBRARY module imported in one subprocess. Restricted
           to `scripts/utils/`, `scripts/record_readers/`, `scripts/inbox_pulse/`
           and `scripts/updaters/` on purpose: a top-level CLI script may call
           `ensure_venv()` at module scope and re-exec the interpreter, which is
           not something a hook should trigger.
  tests    the test files that name the changed modules, by stem. A changed
           `scripts/wizard-verify-key.py` maps to `tests/test_wizard_verify_key.py`
           (hyphens normalise to underscores, which is the mapping that would
           have caught the rename above). Files under `tests/contract/` are
           matched, then skipped and counted: a frozen contract is red between
           the approval commit and the implementation, on purpose.

Every lane that spawns a child runs it under `PYTHON` below, never under
`sys.executable`. MEASURED 2026-08-31: run as the documented `python
scripts/turn-check.py`, `python` is `/usr/bin/python` 3.12 on this machine while
the project pins `.venv` 3.11, so the tests lane collected under the SYSTEM
interpreter and a Playwright test errored on a `~/.local` copy of the SDK that
the pinned environment does not use. The failure was the interpreter, not the
code, and the same run under `.venv/bin/python` was clean. `sys.executable` is
whatever launched this script; it is not the environment the suite is pinned to.

One budget, for the whole run. `--timeout` is the wall-clock ceiling on
EVERYTHING below, not a per-lane cap, and it defaults to a number DERIVED from
the `Stop` timeout the settings files register for this checker's hook. Per-lane
caps added up past the harness kill (2026-09-02), and a hook killed by the
harness has its output discarded, which reads exactly like a clean tree.

Usage:
    .venv/bin/python scripts/turn-check.py         # human output, exit 1 on failure
    .venv/bin/python scripts/turn-check.py --json  # machine output for the Stop hook
    .venv/bin/python scripts/turn-check.py --no-cache    # ignore the pass cache
    .venv/bin/python scripts/turn-check.py --timeout 300 # widen the whole-run budget

Exit codes: 0 clean or nothing to check, 1 a lane failed, 2 bad arguments.

No test covers this module: a lane that silently matches zero files and a
budget that exceeds the harness kill both present as a clean turn, so change
either by reading, never by trying it once.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.checkpoint_paths import state_root  # noqa: E402
from scripts.utils.colors import GRAY, GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils.session_scope import (current_transcript,  # noqa: E402
                                         narrow_with_scope)
from scripts.utils.venv_guard import venv_python  # noqa: E402
from scripts.utils.workspace import get_workspace_root  # noqa: E402

# The interpreter every child lane runs under. Falls back to `sys.executable`
# only when the venv is genuinely absent (a fresh clone before `uv sync`), which
# is the one case where refusing would be worse than reporting under whatever is
# available. `.claude/hooks/turn-check.py` already resolves the same path for the
# automatic run; this makes the by-hand invocation behave identically.
_VENV = venv_python()
PYTHON = str(_VENV) if _VENV.exists() else sys.executable

ROOT = get_workspace_root()
STATE_PATH = state_root(ROOT) / "turn-check.json"

# Packages safe to import without side effects. A module outside these is still
# compiled and still gets its tests run; it is only spared the import probe.
IMPORT_SAFE_PREFIXES = (
    "scripts/utils/",
    "scripts/record_readers/",
    "scripts/inbox_pulse/",
    "scripts/updaters/",
)

WATCHED_PREFIXES = ("scripts/", "tests/", ".claude/hooks/")

# A frozen Canopus contract is written RED at step 3 and stays red until the
# implementation lands at step 6. That is the method, not a defect. Running it at
# the end of every turn in between leaves the operator two bad choices: watch the
# hook block on a test whose failure IS the plan, or learn to ignore the hook.
# The second is worse and is the one that happens. Skipped by prefix, and counted
# out loud, because a narrowed check that prints like a complete one is the
# defect this script was already fixed for once (.claude/rules/scope-claims.md).
CONTRACT_PREFIX = "tests/contract/"

# ============================================================
# The budget, and where it comes from
# ============================================================
#
# The whole run is bounded by ONE number. These are the per-lane ceilings, and
# each child actually receives `min(its ceiling, what is left of the budget)`,
# so a ceiling can only ever make a lane FINISH EARLIER than the budget - it can
# never spend past it. That distinction is the fix: sequential lanes with
# independent caps add up, and the sum was five times the hook's registration.
GIT_TIMEOUT = 20
IMPORT_TIMEOUT = 60

# What the harness charges to the hook that is NOT this process: the wrapper's
# own interpreter start (the registration runs `python3 -c` with a `runpy`
# preamble), reading the payload, spawning this checker, and then formatting and
# printing the block message after it returns. The harness clock starts before
# python does. `.claude/hooks/turn-check.py` also kills this child ten seconds
# before its own budget expires so that an honest "did not finish" survives, and
# that ten seconds lives inside this reserve too.
HOOK_RESERVE_SECONDS = 30

# The budget when no registration can be read at all: a public clone with no
# `.claude/settings.local*.json`, or a hand run from a tree that has one. There
# is no harness to be killed by in that case, so this is a comfort number rather
# than a ceiling, and it is the value this module carried as its only budget
# until 2026-09-02.
FALLBACK_BUDGET_SECONDS = 120

# A derived budget never goes below this. A registration small enough to push it
# lower is a misconfiguration, and the honest response is to run the lanes and
# report "did not finish" rather than to skip them silently.
MIN_BUDGET_SECONDS = 30

# The clock, behind a module attribute so a test can bound wall time without
# spending any, and so nothing here rebinds `time.monotonic` for the whole
# interpreter. Monotonic, not `time.time`: an NTP step mid-run must not hand a
# lane a negative budget or an hour of one.
_now = time.monotonic

# When this run must be finished, on `_now`'s scale. `None` outside a `run`.
#
# Module state and not a parameter, deliberately: it is set exactly once per
# process, and threading a `deadline` argument through three helpers and four
# lanes buys nothing. `run`
# restores the previous value on the way out, so a later direct call to a lane
# is not silently capped by a budget that expired minutes ago.
_DEADLINE: "float | None" = None


def registered_hook_timeout(root: "Path | None" = None) -> "int | None":
    """The smallest `Stop` timeout any settings file registers for this checker.

    The number is DATA, not a property of this file. It lives in
    `.claude/settings.local*.json` - one live file plus three tracked per-OS
    templates - and a constant here would be a copy that describes whichever of
    them was current when it was written. That is exactly how this defect
    happened: three files each carried a number, each defensible alone.

    The SMALLEST, because the templates are meant to agree and the conservative
    reading of a disagreement is the one that gets killed first. `None` means no
    registration was readable, which is a different answer from a small one and
    must not be collapsed into a default by the caller silently.
    """
    root = ROOT if root is None else Path(root)
    found: list[int] = []
    for path in sorted((root / ".claude").glob("settings.local*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            continue
        for group in hooks.get("Stop") or []:
            if not isinstance(group, dict):
                continue
            for entry in group.get("hooks") or []:
                if not isinstance(entry, dict):
                    continue
                command = entry.get("command")
                if not isinstance(command, str) or "turn-check.py" not in command:
                    continue
                timeout = entry.get("timeout")
                if isinstance(timeout, int) and not isinstance(timeout, bool):
                    found.append(timeout)
    return min(found) if found else None


def budget_seconds(root: "Path | None" = None) -> int:
    """Wall seconds this run may spend, derived from the registration."""
    registered = registered_hook_timeout(root)
    if registered is None:
        return FALLBACK_BUDGET_SECONDS
    return max(MIN_BUDGET_SECONDS, registered - HOOK_RESERVE_SECONDS)


def _left() -> "float | None":
    """Seconds remaining, or None when this run is not on a deadline."""
    return None if _DEADLINE is None else _DEADLINE - _now()


def _cap(ceiling: float) -> float:
    """A child's timeout: its own ceiling, or what is left, whichever is less."""
    left = _left()
    if left is None:
        return float(ceiling)
    return max(0.0, min(float(ceiling), left))


def _spent() -> bool:
    left = _left()
    return left is not None and left <= 0


# Matched-file count at which the lane switches to `-n auto`.
#
# DERIVED from two measurements, not chosen. xdist costs about 7s to start its
# workers (a 2-test file: 0.04s serial, 7.41s parallel), and it repays that
# somewhere below 40 files (40 files: 25.95s serial, 18.90s parallel). 20 sits
# clear of the crossover in both directions, so an ordinary turn stays serial
# and exact while a fix campaign gets the parallelism it needs to finish inside
# the hook's budget at all.
PARALLEL_FILE_THRESHOLD = 20

# Pytest's exit code for "no tests were collected": none of the matched files holds a test. Reported, not failed.
NO_TESTS_COLLECTED = 5

# xdist's own wording when two workers collected different suites. Matched as a
# literal on purpose: the retry it gates must fire for THIS cause and nothing
# else, so a substring loose enough to catch a second kind of failure would be
# a bug, not a convenience. See the retry in `lane_tests` for the measurement.
_COLLECTION_RACE = "Different tests were collected between"

# A module naming its own fast contract, for when the stem rule finds nothing.
# Repeatable, because six paths do not fit on one line.
#
# Anchored at column 0 with no leading whitespace allowed, so that an INDENTED
# example of the syntax - inside a docstring explaining the convention, which is
# exactly where one lives - is prose and not a declaration.
DECLARED_TESTS_RE = re.compile(r"^Tests:[ \t]*(.+)$", re.M)


def _rel(path: Path) -> str:
    """Repo-relative display path, falling back to the absolute one.

    `Path.relative_to` RAISES for anything outside the root, and this module
    promises never to raise: a check that crashes is worse than no check, since
    the hook then silently passes the turn.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _git(args: list[str]) -> list[str]:
    """Paths from a git command in the engine tree. Any failure yields no paths.

    Every caller passes `-z`, and it is not cosmetic. `core.quotePath` defaults
    to on, so both `diff --name-only` and `ls-files` C-quote a path holding any
    byte outside printable ASCII: a Cyrillic filename arrives as
    `"scripts/\\320\\261.py"`, which `ROOT / r` then resolves to a file that does
    not exist, and the edit is dropped without a word. This tool says it checks
    "the edits made in this turn", so a silent drop is a false coverage claim
    (`.claude/rules/scope-claims.md`). NUL separation also survives a path
    holding a newline, which `splitlines()` would cut in two.

    The flag is written at each call site rather than injected here, so that a
    guard can read it off the source.

    Paths are NOT stripped: with `-z` the bytes between separators are the exact
    name, and a leading or trailing space is part of it.

    Nor is the output read through subprocess text mode. Text mode turns on
    universal newlines and rewrites every CR byte to LF, with no `newline=` knob
    to switch it off, so `-z` fixes the quoting and leaves this untouched.
    MEASURED 2026-08-30: `docs/x\\r\\ny.md` and `docs/x\\ny.md` are two tracked
    files that `text=True` returns as one name. `ROOT / r` then resolves to
    nothing, `is_file()` is False, and the edit is dropped without a word - the
    same false coverage claim the paragraph above exists to prevent.
    """
    cap = _cap(GIT_TIMEOUT)
    if cap <= 0:
        # No time left to spend, so do not spend the fork either. The caller
        # sees the same empty list a failed git gives it, and `run` notices the
        # blown deadline and says so rather than reporting an empty changed set.
        return []
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(ROOT), capture_output=True, timeout=cap,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    decoded = out.stdout.decode("utf-8", "surrogateescape")
    return [path for path in decoded.split("\0") if path]


def changed_python_files() -> list[Path]:
    """Uncommitted .py edits in the watched trees, as existing absolute paths.

    Working tree only, never `origin/main..HEAD`: a turn check is about what is
    on disk right now, and committed work has already passed the commit gates.
    """
    rel = set(_git(["diff", "--name-only", "-z", "HEAD"]))
    rel |= set(_git(["ls-files", "-z", "--others", "--exclude-standard"]))
    out = []
    for r in sorted(rel):
        if not r.endswith(".py") or not r.startswith(WATCHED_PREFIXES):
            continue
        p = ROOT / r
        if p.is_file():
            out.append(p)
    return out


def deleted_python_files() -> list[str]:
    """Watched `.py` paths git reports as changed but that are gone from disk.

    Returned as repo-relative STRINGS, because there is no file to read. They
    feed the fingerprint only: a deletion is not something a compile or import
    lane can run against, but it absolutely changes what the surviving code
    does. Without it, a turn whose only change is `rm scripts/foo.py` left the
    changed-set hash byte-identical to the last pass, so the very turn that
    broke every importer of `foo` reported `cached`.
    """
    gone = []
    for r in sorted(set(_git(["diff", "--name-only", "-z", "HEAD"]))):
        if not r.endswith(".py") or not r.startswith(WATCHED_PREFIXES):
            continue
        if not (ROOT / r).is_file():
            gone.append(r)
    return gone


def fingerprint(paths: list[Path], deleted: "list[str] | tuple[str, ...]" = ()) -> str:
    """Content hash of the changed set, so an unchanged tree is checked once.

    Content, not mtime: a file rewritten with identical bytes is not a new
    thing to check, and an editor that touches mtime on save would otherwise
    re-run the whole lane set for nothing.

    `deleted` carries the paths that no longer exist. They have no bytes to
    hash, so their NAME goes in behind a marker; that is enough to make the
    hash move when a file disappears, which is the whole point.
    """
    h = hashlib.sha256()
    for p in paths:
        h.update(_rel(p).encode("utf-8"))
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
    for r in sorted(deleted):
        h.update(r.encode("utf-8"))
        h.update(b"<deleted>")
    return h.hexdigest()


def read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    # UnicodeDecodeError is the gap between the two named here. The decode runs
    # inside `read_text`, so `json.loads` is never reached, and it is a
    # ValueError -- a sibling of `json.JSONDecodeError`, not a subclass of
    # OSError. A torn state file therefore took this whole lane down from
    # inside the Stop hook, where a raise is not a failed check but a failed
    # turn.
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def write_state(data: dict) -> None:
    """Record the last passing fingerprint. A failed write costs nothing but a
    repeated check, so it is never allowed to fail the run."""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(STATE_PATH)
    except OSError:
        pass


def lane_compile(paths: list[Path]) -> list[str]:
    """Syntax. Cheapest possible signal, and it catches the whole class of edit
    that leaves a file unparseable."""
    import py_compile

    failures = []
    for p in paths:
        try:
            py_compile.compile(str(p), doraise=True, cfile=str(p) + ".turncheck.pyc")
        except py_compile.PyCompileError as e:
            failures.append(f"{_rel(p)}: {e.msg.strip().splitlines()[-1]}")
        except OSError as e:
            failures.append(f"{_rel(p)}: {e}")
        finally:
            Path(str(p) + ".turncheck.pyc").unlink(missing_ok=True)
    return failures


def module_name(path: Path) -> str | None:
    """Dotted module name for an importable library path, else None."""
    rel = _rel(path).replace("\\", "/")
    if not rel.startswith(IMPORT_SAFE_PREFIXES) or rel.endswith("__init__.py"):
        return None
    return rel[: -len(".py")].replace("/", ".")


def lane_import(paths: list[Path]) -> list[str]:
    """Import every changed library module in ONE subprocess.

    One subprocess, not one per module: interpreter startup dominates, and a
    single failing import names itself in the traceback anyway.
    """
    modules = [m for m in (module_name(p) for p in paths) if m]
    if not modules:
        return []
    probe = "import importlib\n" + "\n".join(
        f"importlib.import_module({m!r})" for m in modules
    )
    cap = _cap(IMPORT_TIMEOUT)
    if cap <= 0:
        return [f"import probe never ran over {len(modules)} module(s): the "
                f"run budget was already spent"]
    try:
        out = subprocess.run(
            [PYTHON, "-c", probe],
            cwd=str(ROOT), capture_output=True, text=True,
            errors="replace", timeout=cap,
        )
    except subprocess.TimeoutExpired:
        return [f"import probe timed out over {len(modules)} module(s)"]
    except OSError as e:
        return [f"import probe could not run: {e}"]
    if out.returncode != 0:
        tail = (out.stderr or "").strip().splitlines()[-6:]
        return ["\n".join(tail) or f"import probe exited {out.returncode}"]
    return []


def _declared_paths(path: Path) -> list[str]:
    """The repo-relative test paths a module names in its own docstring.

    Unresolved and unfiltered; `declared_tests` keeps the ones that exist.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[str] = []
    for line in DECLARED_TESTS_RE.findall(text):
        for token in line.replace(",", " ").split():
            token = token.strip("`'\"")
            if token.startswith("tests/") and token.endswith(".py"):
                found.append(token)
    return found


def declared_tests(path: Path) -> list[Path]:
    """The declared test files that actually exist, resolved under the root."""
    return [ROOT / rel for rel in _declared_paths(path) if (ROOT / rel).is_file()]


def matching_tests(paths: list[Path]) -> list[Path]:
    """Test files that name the changed modules, plus changed test files, plus
    the ones a module declares for itself.

    Stem matching with hyphens normalised: `wizard-verify-key.py` and
    `test_wizard_verify_key.py` only line up once `-` becomes `_`, and that
    pair is the exact miss this script was written for.

    The stem rule finds tests NAMED after a module and nothing else, so a module
    whose tests are named after the behaviour they pin matches nothing at all.
    `scripts/checkpoint-paths.py` was that case: fifteen test files exercise it,
    the stem `checkpoint_paths` matched none, and editing it ran zero tests under
    a lane that printed `clean`. A module closes that by naming its own fast
    contract in its docstring:

        Tests: tests/test_a.py, tests/test_b.py

    Additive, and the author picks the members. Matching by content instead was
    measured and rejected - the fifteen files that merely mention
    `checkpoint-paths` cost 60.6s, which is the end-of-turn wait this lane exists
    to avoid. A declaration that points at a file which is not there is dropped
    here in silence, never honoured.
    """
    tests_dir = ROOT / "tests"
    if not tests_dir.is_dir():
        return []
    # A LIST per basename, not one path. Keyed by `p.name`, two test files with
    # the same name in different subdirectories collided and one was silently
    # dropped from ever being matched -- so a module whose only matching test
    # was the loser ran ZERO tests under a lane that printed `clean` (measured
    # 2026-08-24). Kept for the first test subdirectory anybody adds back.
    all_tests: dict[str, list[Path]] = {}
    for tp in tests_dir.rglob("test_*.py"):
        all_tests.setdefault(tp.name, []).append(tp)
    picked: set[Path] = set()
    for p in paths:
        rel = _rel(p).replace("\\", "/")
        if rel.startswith("tests/"):
            picked.add(p)
            continue
        picked.update(declared_tests(p))
        stem = p.stem.replace("-", "_")
        for name, tps in all_tests.items():
            body = name[len("test_"): -len(".py")]
            if body == stem or body.startswith(stem + "_"):
                picked.update(tps)
    return sorted(picked)


def is_contract(path: Path) -> bool:
    """A file under the frozen-contract tree, whose red state is the plan."""
    return _rel(path).replace("\\", "/").startswith(CONTRACT_PREFIX)


def lane_tests(paths: list[Path],
               timeout: int) -> tuple[list[str], int, int, int, int]:
    """Run the matched tests.

    Returns (failures, test files run, contract files skipped, files that collected nothing, files left unmeasured).

    A run that never finished reports `tests_run=0` and counts its files as
    unmeasured. Until 2026-08-29 both non-completion paths, the wall-clock
    timeout and the `OSError`, returned `tests_run=len(targets)` and a plain
    failure. MEASURED that day by forcing `TimeoutExpired`: the result carried
    `"tests_run": 2` when pytest had been killed and zero tests had run, and the
    Stop hook rendered "a failure here is almost always real" over it. A number
    for work that did not happen is worse than no number.

    The lane runs every matched test. The `-m "not slow"` filter and its deselection count were removed 2026-11-16 (HOS-364) because the `slow` marker was dropped from pyproject.toml in aae0db09 and, under --strict-markers, the filter made pytest refuse to run.
    """
    picked = matching_tests(paths)
    targets = [t for t in picked if not is_contract(t)]
    skipped = len(picked) - len(targets)
    if not targets:
        return [], 0, skipped, 0, 0
    parallel = len(targets) >= PARALLEL_FILE_THRESHOLD
    args = [
        PYTHON, "-m", "pytest", "-q", "-p", "no:randomly",
        "--no-header", "-x",
        # PARALLEL above PARALLEL_FILE_THRESHOLD only. Serial did not fit the
        # hook budget at campaign size (2026-08-31: 40 files serial 25.95s,
        # parallel 18.90s), while xdist worker start-up costs about seven
        # seconds (a 2-test file: 0.04s serial, 7.41s parallel), so the common
        # small turn stays serial. `-n auto` is safe by precedent: run-tests.py
        # runs the whole suite that way.
        *(["-n", "auto"] if parallel else []),
        *[str(t) for t in targets],
    ]
    try:
        # `_cap` and not `timeout`. This is the lane the defect was measured in:
        # three children could each be handed the FULL cap, and the git and
        # import lanes had already spent their own before any of them started.
        # Asking for what is LEFT is what makes the whole run fit one number.
        out = subprocess.run(
            args, cwd=str(ROOT), capture_output=True, text=True,
            errors="replace", timeout=_cap(timeout)
        )
        # ONE retry, on ONE error string, and only under `-n auto`. xdist workers
        # collect a moment apart, so a test file landing in between aborts the
        # lane with "Different tests were collected" though nothing failed
        # (2026-09-01: serial collection stable, parallel grew while agents
        # wrote tests). Retrying only this string keeps a red meaningful: a
        # corpus that collects nondeterministically (such as a fixture baking
        # the clock into its test id) mismatches on BOTH attempts and is still
        # reported.
        if out.returncode != 0 and parallel and _COLLECTION_RACE in (
                (out.stdout or "") + (out.stderr or "")):
            out = subprocess.run(
                args, cwd=str(ROOT), capture_output=True, text=True,
                errors="replace", timeout=_cap(timeout)
            )
    except subprocess.TimeoutExpired:
        return [
            f"the matched tests did not finish in {timeout}s "
            f"({len(targets)} file(s)); run them yourself or raise --timeout"
        ], 0, skipped, 0, len(targets)
    except OSError as e:
        return [f"pytest could not run: {e}"], 0, skipped, 0, len(targets)
    body = (out.stdout or "") + (out.stderr or "")
    # Exit 5 means no matched file collected a test. That is reported as
    # `collected_nothing`, not as a failure, because a silent exclusion reads as
    # coverage.
    if out.returncode == NO_TESTS_COLLECTED:
        return [], len(targets), skipped, len(targets), 0
    if out.returncode != 0:
        tail = [ln for ln in body.strip().splitlines() if ln.strip()][-12:]
        return (["\n".join(tail) or f"pytest exited {out.returncode}"],
                len(targets), skipped, 0, 0)
    return [], len(targets), skipped, 0, 0


def _out_of_budget(budget: int, lane: str, files: int, foreign: int,
                   scope_known: bool) -> dict:
    """The result for a run the wall clock stopped before `lane` could start.

    Reported as a FAILURE rather than as an idle pass, and that is the whole
    point of the branch. When the budget ran out during the git scan the changed
    set came back empty, and an empty changed set is `status: "idle"` with the
    reason "no uncommitted Python edits" - byte-for-byte what a genuinely clean
    tree returns. `.claude/rules/scope-claims.md` obligation 3: when the
    evidence is unavailable, say the state is unknown; failing toward silence is
    the one direction that is never allowed.

    `unmeasured` carries the number of files this run knows about, which is what
    that field means everywhere else. It is 0 when the budget died inside the
    git scan, because at that point the count itself was never established - so
    the sentence below states the exclusion in words rather than leaning on a
    count it does not have.
    """
    return {
        "status": "fail", "lane": lane,
        "failures": [f"the {budget}s run budget was spent before the {lane} "
                     f"lane could start, so nothing was measured; re-run with "
                     f"a longer --timeout"],
        "files": files, "tests_run": 0, "skipped_foreign": foreign,
        "scope_unknown": not scope_known, "skipped_contract": 0,
        "collected_nothing": 0, "unmeasured": files,
    }


def run(timeout: int, use_cache: bool, transcript=None) -> dict:
    """Run the lanes and return a result dict. Never raises.

    `timeout` is the wall-clock budget for the WHOLE run, not for the test lane
    alone. Every child is handed what is LEFT of it, so the sequential lanes
    below cannot each spend one.

    The deadline is armed here and restored on the way out, including on an
    early return, so nothing outside a run is capped by a budget that has since
    expired.
    """
    global _DEADLINE
    previous = _DEADLINE
    _DEADLINE = _now() + timeout
    try:
        return _run_lanes(timeout, use_cache, transcript)
    finally:
        _DEADLINE = previous


def _run_lanes(timeout: int, use_cache: bool, transcript) -> dict:
    paths = changed_python_files()
    # `scope_known` is False when the write set could not be established at all:
    # no transcript, an absent or unreadable one, or ANY ONE unreadable subagent
    # sidecar (a session with a hundred of them has a hundred ways to get here).
    # The narrowing then keeps every candidate and reports zero drops, which is
    # indistinguishable from a clean scope that dropped nothing, so the flag
    # travels in the result and the Stop hook names the widening out loud.
    # Without it, MEASURED 2026-08-31 over a malformed transcript, the hook told
    # the operator a break was in "the uncommitted Python edits in this turn"
    # with no exclusion line, over a file another session had written.
    paths, foreign, scope_known = narrow_with_scope(paths, transcript)
    deleted = deleted_python_files()
    if _spent():
        return _out_of_budget(timeout, "scan", len(paths), foreign, scope_known)
    if not paths:
        reason = "no uncommitted Python edits"
        if foreign:
            # Same disjunction as `_foreign_note`, and for the same reason: the
            # scope establishes "no write recorded by this session", which is
            # another session's file OR a Bash edit made here.
            reason = (f"no uncommitted Python edits by this session "
                      f"({foreign} by another session, or edited here "
                      f"through Bash)")
        if deleted:
            reason += f" ({len(deleted)} deleted, nothing left to run against)"
        return {"status": "idle", "reason": reason, "files": 0,
                "skipped_foreign": foreign, "deleted": len(deleted)}

    fp = fingerprint(paths, deleted)
    if use_cache and read_state().get("last_pass") == fp:
        return {"status": "cached", "reason": "unchanged since the last pass",
                "files": len(paths), "skipped_foreign": foreign}

    failures = lane_compile(paths)
    lane = "compile"
    if not failures:
        failures, lane = lane_import(paths), "import"
    tests_run = 0
    skipped_contract = 0
    collected_nothing = 0
    unmeasured = 0
    if not failures:
        if _spent():
            return _out_of_budget(timeout, "tests", len(paths), foreign,
                                  scope_known)
        (failures, tests_run, skipped_contract,
         collected_nothing, unmeasured) = lane_tests(paths, timeout)
        lane = "tests"

    if failures:
        return {"status": "fail", "lane": lane, "failures": failures,
                "files": len(paths), "tests_run": tests_run,
                "skipped_foreign": foreign,
                "scope_unknown": not scope_known,
                "skipped_contract": skipped_contract,
                "collected_nothing": collected_nothing,
                "unmeasured": unmeasured}

    write_state({"last_pass": fp, "files": len(paths), "tests_run": tests_run})
    return {"status": "pass", "files": len(paths), "tests_run": tests_run,
            "skipped_foreign": foreign, "scope_unknown": not scope_known,
            "skipped_contract": skipped_contract,
            "collected_nothing": collected_nothing,
            "unmeasured": unmeasured}


def _foreign_note(result: dict) -> str:
    """What the scope left out, named. Silence here is how a narrowed check
    starts reading as a complete one.

    The sentence is a disjunction because that is all the method establishes.
    `session_scope.files_written` reads this session's transcript AND its
    subagent sidecars for `Write`/`Edit`/`MultiEdit`/`NotebookEdit` calls, so a
    dropped file is one carrying no such call: another session's work, OR an
    edit this session made through `Bash`, which records a command and never a
    path. Until 2026-08-30 this said flatly "written by another session" while
    the scope read only the parent transcript, and that day it labelled 37 files
    written by this session's own subagents as a stranger's."""
    count = result.get("skipped_foreign") or 0
    if not count:
        return ""
    return (f" {GRAY}[{count} changed file(s) written by another session, or "
            f"edited here through Bash, not checked]{RESET}")


def _contract_note(result: dict) -> str:
    """The frozen contracts this run declined to judge. Same reason as above:
    the operator has to be able to see that the number of tests run is smaller
    than the number of tests matched."""
    count = result.get("skipped_contract") or 0
    if not count:
        return ""
    return (f" {GRAY}[{count} frozen-contract file(s) not run: red by design "
            f"until the slice implements them]{RESET}")


def _empty_note(result: dict) -> str:
    """The matched files that collected no test at all. Pytest answers exit 5 for this, which the lane once read as a failure; not failing it must not mean saying nothing about it, or a test_*.py that holds no test reads as a passing lane. Every matched file is counted, because exit 5 means none of them collected a test."""
    count = result.get("collected_nothing") or 0
    if not count:
        return ""
    return (f" {GRAY}[{count} matched file(s) collected no tests: "
            f"nothing ran, and nothing failed]{RESET}")


def _unmeasured_note(result: dict) -> str:
    """The files the lane never got an answer about.

    A run killed by the wall clock, or one where pytest could not start, judged
    nothing. Counting those files as run was a claim about work that did not
    happen, which `.claude/rules/scope-claims.md` calls the defect that gets
    trusted and quoted back later."""
    count = result.get("unmeasured") or 0
    if not count:
        return ""
    return (f" {GRAY}[{count} matched file(s) left unmeasured: the lane did not "
            f"finish, so nothing about them is known]{RESET}")


def _notes(result: dict) -> str:
    return (_foreign_note(result) + _contract_note(result)
            + _empty_note(result) + _unmeasured_note(result))


def render(result: dict) -> str:
    status = result["status"]
    if status in ("idle", "cached"):
        return f"{GRAY}turn-check: {result['reason']}{RESET}" + _notes(result)
    if status == "pass":
        return (f"{GREEN}turn-check: clean{RESET} "
                f"{GRAY}({result['files']} changed file(s), "
                f"{result['tests_run']} test file(s)){RESET}" + _notes(result))
    # "failed" is a verdict about the code. A lane that never finished reached
    # no verdict, so it does not get that word.
    verb = "did not finish" if result.get("unmeasured") else "failed"
    head = f"{RED}turn-check: {result['lane']} lane {verb}{RESET}" + _notes(result)
    return head + "\n" + "\n".join(result["failures"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Fast check over uncommitted Python edits."
    )
    parser.add_argument("--json", action="store_true",
                        help="Emit the result as JSON instead of prose.")
    parser.add_argument("--no-cache", action="store_true",
                        help="Re-run even if this exact tree already passed.")
    parser.add_argument("--timeout", type=int, default=None,
                        help="Wall-clock budget for the WHOLE run, seconds. "
                             "Default is derived from the Stop timeout the "
                             "settings files register for this checker's hook, "
                             f"less a {HOOK_RESERVE_SECONDS}s wrapper reserve.")
    parser.add_argument("--session-transcript", default=None,
                        help="Session transcript; narrows the check to files this "
                             "session wrote. Omitted means the whole working tree.")
    args = parser.parse_args(argv)

    if args.timeout is not None and args.timeout <= 0:
        print(f"{YELLOW}--timeout must be positive{RESET}", file=sys.stderr)
        return 2

    # An explicit value is the operator's word and is honoured as given: a hand
    # run from a terminal has no harness to be killed by, and the "did not
    # finish" message this tool prints tells its reader to come back with
    # `--timeout 300`. Only the DEFAULT is derived.
    timeout = args.timeout if args.timeout is not None else budget_seconds()

    result = run(timeout=timeout, use_cache=not args.no_cache,
                 transcript=args.session_transcript or current_transcript())
    print(json.dumps(result, ensure_ascii=False) if args.json else render(result))
    return 1 if result["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
