#!/usr/bin/env python3
"""Append structured trajectory events for /implement runs.

Closes R12 from the 2026-05-27 /scrutinize meta-review. Per Agent-as-a-Judge
(DevAI benchmark, Zhuge et al. 2024), trajectory evaluation lifts agent-
review accuracy from 70% to 90% vs outcome-only. This helper is the
emission side of that pattern. /scrutinize trajectory:<run_id> is the
audit side (see .claude/skills/scrutinize/references/trajectory-evaluation.md).

Four subcommands:

  --new --plan <plan-path>
      Mint a new run_id and create the trajectory JSONL with an opening
      run_start event. Prints the run_id to stdout so /implement can
      capture it and reference it in subsequent --event calls.

      Slug derivation: Path(plan_path).stem with leading YYYY-MM-DD-
      stripped if present. Example:
        <plans>/2026-05-27-r12-trajectory-evaluation.md
        -> slug=r12-trajectory-evaluation
        -> run_id=2026-05-27_134522_r12-trajectory-evaluation

  --event --run-id <id> --type <event-type> (typed flags | --data-*)
      Append one event record to the trajectory JSONL. The payload comes
      from EITHER typed flags OR one --data-* mode; the two are mutually
      exclusive.

      Typed flags (preferred, single call, no temp file): --step, --title,
      --file (repeatable), --status, --notes, --wave, --step-count,
      --parallel/--no-parallel, --successes, --failures, --check,
      --passed/--failed, --detail, --reason, --what-changed, --scope,
      --artefact, --grade, --iteration, --summary, --plan-status. Only set
      flags contribute a key; type-aware defaults fill step_end
      (files_affected=[], status=ok) and run_end (run_id, trajectory_path,
      plan_status=Implemented). Plain string args avoid JSON quoting, so
      this path is cross-platform safe.

      Escape hatch for an arbitrary payload: exactly one of --data-file
      (writes JSON via the Write tool, passes the path), --data-stdin (a
      shell pipe), or --data-json (bash-only / hand-runs only).

  --verify --run-id <id>
      Structurally self-check an existing trajectory: run_start present and
      first, run_end present and last, step_start/step_end pairing,
      wave_start/wave_end pairing, each wave's successes count (an orphan
      wave_end is reconciled against its implicit bracket, not skipped), no
      step outside every bracket in a wave-mode run, and literal
      files_affected paths. Plus a run-level files reconciliation
      (advisory): the current engine working tree is diffed against
      run_start.git_head and any changed file recorded in no step's
      files_affected is flagged; this is meaningful only immediately after the
      run (before any commit / git pull) and degrades to a no-op when git_head
      is "unknown" or git is unavailable. Plus a validation-gate check
      (advisory): a completed run with zero validation_check events is flagged so
      Phase 3 gates are logged as structured events, not only prose. Plus two
      plan-derived advisories (silent when the plan cannot be located): a file
      listed under a plan step's "Files affected" that appears in no step's
      files_affected; and a plan whose Implementation Notes declare more
      deviations than the trajectory carries as events. Plus one ordering
      advisory computed purely from the JSONL, which therefore fires whether or
      not the plan can be located: a deviation emitted before its own step's
      step_start. Prints defects and exits 1 on any
      defect, 0 when clean. Read-only; never mutates the audit record.
      /implement calls this in Phase 5 after run_end (advisory).

  --list-files --run-id <id>
      Print the union of every step's files_affected, one literal path per
      line, so the Phase 3 hidden-character scan reads its file list off the
      record instead of assembling it by hand.

Event types: run_start, step_start, step_end, validation_check,
              evaluation_result, deviation, wave_start, wave_end, run_end.

wave_start.parallel records the PLAN'S declaration that a wave's steps may
interleave, not an observation that they did. Nothing here watches execution,
so a declared-parallel wave whose steps ran back-to-back is normal
(`.claude/rules/scope-claims.md`: say what the method establishes).

Each event record: {timestamp, event_type, step_number, payload}.

Atomic append discipline: the JSONL is shared-state in wave-mode
parallel /implement runs. O_APPEND keeps concurrent writers from overwriting
each other at the offset; `cmd_event`'s `file_lock` is what serialises a whole
record.

The trajectory is a verbatim audit record: never mutate, never sanitize.
Hidden-character checking happens at READ time in the /scrutinize
trajectory lens, emitted at LOW severity (advisory).

All timestamps (in run_id and in event records) are UTC for cross-
machine consistency.

Usage:
  SCRATCH="$(python scripts/data-path.py path --create runtime:scratch/tmp)"
  run_id=$(python scripts/implement-trajectory-log.py --new \\
             --plan "$(python scripts/data-path.py path plans)/2026-05-27-r12-trajectory-evaluation.md")
  python scripts/implement-trajectory-log.py --event \\
    --run-id $run_id --type step_start --data-file "$SCRATCH/event.json"

Exit codes:
  0  ok
  2  bad args (missing required, mutually-exclusive violation)
  3  filesystem error (cannot write, locking timeout)
  4  JSON parse error on supplied data
  5  sequencing violation (step_start opened while another step is open
     outside a parallel wave, or step_end for an unopened step)

Coverage: no tests, by policy (M050, Fix-Note).
"""
from __future__ import annotations

import argparse
import json
import os
import glob
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.checkpoint_paths import file_lock  # noqa: E402
from scripts.utils.colors import GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils.data_path import data_write_dir  # noqa: E402
from scripts.utils.data_path import DataPathError, data_path  # noqa: E402
from scripts.utils.workspace import (  # noqa: E402
    get_data_root,
    get_workspace_root,
)

WORKSPACE_ROOT = get_workspace_root()


def trajectory_dir() -> Path:
    """The registry address `runtime:implement/logs`, resolved at call time,
    never at import, so it follows a data root repointed after import.
    """
    return data_write_dir("runtime:implement/logs")


EVENT_TYPES = {
    "run_start",
    "step_start",
    "step_end",
    "validation_check",
    "evaluation_result",
    "deviation",
    "wave_start",
    "wave_end",
    "run_end",
}

# ============================================================
# Run ID minting
# ============================================================
def derive_slug(plan_path: str) -> str:
    """Path(plan_path).stem with leading YYYY-MM-DD- stripped if present.

    Examples:
      <plans>/2026-05-27-r12-trajectory-evaluation.md -> r12-trajectory-evaluation
      <plans>/refactor-foo.md                          -> refactor-foo
      docs/some-plan-name                            -> some-plan-name
    """
    stem = Path(plan_path).stem
    # Strip YYYY-MM-DD- (10 chars + 1 hyphen = 11 chars) if it matches the pattern
    if len(stem) >= 11 and stem[4] == "-" and stem[7] == "-" and stem[10] == "-":
        date_part = stem[:10]
        if all(c.isdigit() or c == "-" for c in date_part):
            return stem[11:] or "untitled"
    return stem or "untitled"


def mint_run_id(plan_path: str) -> str:
    """Generate run_id = YYYY-MM-DD_HHMMSS_<slug> (UTC).

    All timestamps in trajectory artefacts are UTC for cross-machine consistency.
    A run minted at 16:25 local (UTC+3) and a run minted at 13:25 London (UTC+0)
    should produce identical run_id prefixes so their event records sort coherently.
    """
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d_%H%M%S")
    slug = derive_slug(plan_path)
    return f"{timestamp}_{slug}"


def mint_unique_run_id(plan_path: str, attempts: int = 8) -> str:
    """A run_id no trajectory file already uses, created EXCLUSIVELY.

    One-second resolution is not unique, so two runs of one plan in the same
    second would otherwise share a trajectory. `O_CREAT | O_EXCL` is what makes the check and the create one step: the
    filesystem refuses the second creator rather than this code losing a race
    with it. A suffix is added only on an actual collision, so the ordinary id
    keeps its readable shape.
    """
    base = mint_run_id(plan_path)
    for attempt in range(attempts):
        run_id = base if attempt == 0 else f"{base}-{uuid.uuid4().hex[:6]}"
        path = trajectory_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        os.close(fd)
        return run_id
    raise FileExistsError(
        f"could not mint an unused run_id for {plan_path} after {attempts} attempts")


# ============================================================
# Atomic append
# ============================================================
def _append_jsonl_posix(path: Path, record: dict) -> None:
    """O_APPEND, writing the whole record or raising.

    `os.write()` may write FEWER bytes than given, so the loop writes the
    remainder; a short write must never leave a truncated JSONL line. O_APPEND
    keeps concurrent writers apart at the offset, not within one record.
    """
    line = json.dumps(record, ensure_ascii=False) + "\n"
    payload = memoryview(line.encode("utf-8"))
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        written = 0
        while written < len(payload):
            n = os.write(fd, payload[written:])
            if n <= 0:
                raise OSError(
                    f"short write to {path}: {written} of {len(payload)} bytes "
                    f"and no progress; the record is truncated")
            written += n
    finally:
        os.close(fd)


def append_event(path: Path, record: dict) -> None:
    """Append one JSON record as a JSONL line, atomic under concurrent writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _append_jsonl_posix(path, record)


# ============================================================
# Event records
# ============================================================
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_run_id(run_id: str) -> str:
    """Return run_id, or raise ValueError when it is not a bare file-name part.

    A separator or `..` would file the audit record outside trajectory_dir()
    (MEASURED 2026-08-30); no privilege boundary, but a misfiled record.

    A DENYLIST, not an allowlist: `derive_slug` takes the slug straight off the
    plan's filename stem, so a plan called "wave two.md" mints a run_id with a
    space in it and an allowlist would refuse the run that just minted it. Only
    what can leave the directory, or name nothing, is refused.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError(f"run_id {run_id!r} is empty")
    bad = [c for c in ("/", "\\", "\0", "\n", "\r") if c in run_id]
    if ".." in run_id:
        bad.append("..")
    if run_id in (".", ".."):
        bad.append(run_id)
    if bad:
        raise ValueError(
            f"run_id {run_id!r} may not contain {', '.join(repr(b) for b in bad)}: "
            f"a trajectory id is one file-name part, never a path")
    return run_id


def trajectory_path(run_id: str) -> Path:
    # The id is validated BEFORE the directory is resolved: `trajectory_dir()`
    # creates `runtime:implement/logs` on the way, and a refused id must leave
    # no trace (MEASURED 2026-09-14).
    name = f"_trajectory_{validate_run_id(run_id)}.jsonl"
    return trajectory_dir() / name


# ============================================================
# Typed-flag payload assembly (single-call emission path)
# ============================================================
# Maps each argparse dest to its payload key. List order defines the
# serialization order of flag-supplied keys. The payload dict is equal
# (same keys and values) to the legacy hand-authored --data-file JSON;
# key ORDER may differ when type-aware defaults are appended below, which
# is fine - the /scrutinize trajectory lens reads payload by key.
_FLAG_TO_PAYLOAD_KEY = [
    ("step", "step"),
    ("title", "title"),
    ("files", "files_affected"),
    ("status", "status"),
    ("notes", "notes"),
    ("wave", "wave"),
    ("step_count", "step_count"),
    ("parallel", "parallel"),
    ("successes", "successes"),
    ("failures", "failures"),
    ("check", "check"),
    ("passed", "passed"),
    ("detail", "detail"),
    ("reason", "reason"),
    ("what_changed", "what_changed"),
    ("scope", "scope"),
    ("artefact", "artefact"),
    ("grade", "grade"),
    ("iteration", "iteration"),
    ("summary", "summary"),
    ("plan_status", "plan_status"),
]

# The set of argparse dests that constitute the typed-flag mode. Shared by
# the mutual-exclusion guard in cmd_event and the payload builder.
TYPED_FLAG_DESTS = [dest for dest, _ in _FLAG_TO_PAYLOAD_KEY]


def build_payload_from_flags(event_type: str, args: argparse.Namespace) -> dict:
    """Assemble an event payload from typed flags, type-awarely.

    Only flags whose value is not None contribute a key. Then per-type
    defaults are applied so the emitted record matches the legacy shape:
      - step_end always carries files_affected (default []) and status (ok)
      - run_end auto-fills run_id, trajectory_path, plan_status (Implemented)
    """
    payload: dict[str, Any] = {}
    for dest, key in _FLAG_TO_PAYLOAD_KEY:
        val = getattr(args, dest, None)
        if val is not None:
            payload[key] = val

    if event_type == "step_end":
        payload.setdefault("files_affected", [])
        payload.setdefault("status", "ok")
    elif event_type == "run_end":
        payload.setdefault("run_id", args.run_id)
        payload.setdefault("trajectory_path", str(trajectory_path(args.run_id)))
        payload.setdefault("plan_status", "Implemented")
    return payload


def write_run_start(run_id: str, plan_path: str) -> Path:
    path = trajectory_path(run_id)
    # A NON-EMPTY file is an existing trajectory. `mint_unique_run_id` creates
    # the file empty and exclusively to reserve the id, so plain `exists()`
    # would now refuse the very run it just reserved.
    if path.exists() and path.stat().st_size > 0:
        # Refuse to overwrite an existing trajectory - this would clobber audit history.
        raise FileExistsError(f"trajectory already exists: {path}")
    try:
        import subprocess
        git_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(WORKSPACE_ROOT),
            capture_output=True, text=True, timeout=3,
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        git_head = "unknown"

    record = {
        "timestamp": now_iso(),
        "event_type": "run_start",
        "step_number": 0,
        "payload": {
            "run_id": run_id,
            "plan_path": plan_path,
            "slug": derive_slug(plan_path),
            "workspace_root": str(WORKSPACE_ROOT),
            "git_head": git_head,
        },
    }
    append_event(path, record)
    return path


# ============================================================
# Data ingestion (three input modes - cross-platform safety)
# ============================================================
def load_data(args: argparse.Namespace) -> Any:
    """Load the event data payload from one of three input modes.

    Exactly one of --data-file, --data-stdin, --data-json must be set.
    """
    # Presence, not truthiness: an empty `--data-json ""` is still a supplied mode.
    supplied = sum(1 for v in (args.data_file, args.data_json) if v is not None)
    supplied += 1 if args.data_stdin else 0
    if supplied == 0:
        print(f"{RED}ERROR: one of --data-file, --data-stdin, --data-json is required.{RESET}",
              file=sys.stderr)
        sys.exit(2)
    if supplied > 1:
        print(f"{RED}ERROR: --data-file, --data-stdin, --data-json are mutually exclusive.{RESET}",
              file=sys.stderr)
        sys.exit(2)

    raw: str
    if args.data_file is not None:
        try:
            raw = Path(args.data_file).read_text(encoding="utf-8")
        # UnicodeDecodeError too (a ValueError), so a stray byte exits 3, not 1.
        except (OSError, UnicodeDecodeError) as exc:
            print(f"{RED}ERROR: cannot read --data-file {args.data_file}: {exc}{RESET}",
                  file=sys.stderr)
            sys.exit(3)
    elif args.data_stdin:
        raw = sys.stdin.read()
    else:
        raw = args.data_json

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"{RED}ERROR: data payload is not valid JSON: {exc}{RESET}",
              file=sys.stderr)
        sys.exit(4)


# ============================================================
# Emit-time sequencing state (guard support)
# ============================================================
def _read_events(path: Path) -> list[dict]:
    """Tolerant JSONL read: return event dicts, silently skipping bad lines.

    Used by the emit-time sequencing guard to reconstruct open-step state from
    the trajectory written so far. Malformed lines are ignored here (verify
    surfaces them as defects); the guard only needs the well-formed events.

    "Tolerant" covers the decode too: an undecodable file reads as no events.
    """
    events: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return events
    # split("\n"), NOT splitlines(): with ensure_ascii off a record carries
    # U+2028, U+2029 and U+0085 verbatim, and splitlines() breaks on them
    # (MEASURED 2026-09-01).
    for raw in text.split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _step_sort_key(step: Any) -> tuple:
    """A total ordering over step numbers of ANY type.

    Step numbers reach this set straight off an event payload, and a
    `--data-*` payload is only checked for being a JSON object -- so a string
    or a null step lands here beside the ints. `sorted()` on that mixture
    raises TypeError, which it did inside the branch whose only job is to
    print a clean rejection and exit 5.

    Ints keep their numeric order (10 after 2, which sorting by `repr` alone
    would not give); anything else sorts after them, by `repr`.
    """
    if isinstance(step, int):
        return (0, step, "")
    return (1, 0, repr(step))


def _open_state(events: list[dict]) -> tuple[set, bool]:
    """Reduce a trajectory's events to (open_step_numbers, in_open_parallel_wave).

    Open steps are tracked with an open-stack per step_number (a number reused
    across waves still reconciles). A parallel wave is "open" while a
    wave_start with payload.parallel is True has no matching wave_end; tracked
    by wave number so a non-parallel wave_end never clears a parallel wave.
    """
    open_counts: dict[Any, int] = {}
    parallel_waves_open: set = set()
    for e in events:
        et = e.get("event_type")
        payload = e.get("payload") or {}
        if et == "step_start":
            sn = e.get("step_number")
            open_counts[sn] = open_counts.get(sn, 0) + 1
        elif et == "step_end":
            sn = e.get("step_number")
            if open_counts.get(sn, 0) > 0:
                open_counts[sn] -= 1
        elif et == "wave_start":
            if payload.get("parallel") is True:
                parallel_waves_open.add(payload.get("wave"))
        elif et == "wave_end":
            parallel_waves_open.discard(payload.get("wave"))
    open_steps = {sn for sn, c in open_counts.items() if c > 0}
    return open_steps, bool(parallel_waves_open)


# ============================================================
# CLI
# ============================================================
def cmd_new(args: argparse.Namespace) -> int:
    if not args.plan:
        print(f"{RED}ERROR: --new requires --plan <plan-path>{RESET}", file=sys.stderr)
        return 2
    # Minting is INSIDE the try: it is where a read-only tree, a full disk or
    # exhausted collision attempts are met first, and all of them are exit 3.
    try:
        run_id = mint_unique_run_id(args.plan)
        path = write_run_start(run_id, args.plan)
    except FileExistsError as exc:
        print(f"{RED}ERROR: {exc}{RESET}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"{RED}ERROR: cannot write trajectory: {exc}{RESET}", file=sys.stderr)
        return 3
    # run_id to stdout for capture by /implement; informational line to stderr.
    print(run_id)
    print(f"{GREEN}trajectory: {path}{RESET}", file=sys.stderr)
    return 0


def cmd_event(args: argparse.Namespace) -> int:
    if args.type not in EVENT_TYPES:
        print(f"{RED}ERROR: --type must be one of {sorted(EVENT_TYPES)}{RESET}",
              file=sys.stderr)
        return 2
    path = trajectory_path(args.run_id)
    if not path.exists():
        print(f"{RED}ERROR: trajectory not found: {path}. "
              f"Did you call --new first?{RESET}", file=sys.stderr)
        return 3

    # PRESENCE, not truthiness: `--data-json ""` is a supplied payload and must
    # reach the exit-4 validation, never vanish (MEASURED 2026-08-30).
    any_data_mode = (args.data_file is not None or args.data_stdin
                     or args.data_json is not None)
    typed_present = any(getattr(args, d, None) is not None for d in TYPED_FLAG_DESTS)
    if any_data_mode and typed_present:
        print(f"{RED}ERROR: typed flags and --data-file/--data-stdin/--data-json "
              f"are mutually exclusive.{RESET}", file=sys.stderr)
        return 2

    if any_data_mode:
        payload = load_data(args)
        if not isinstance(payload, dict):
            print(f"{YELLOW}WARN: payload is not a JSON object; wrapping under 'value' key.{RESET}",
                  file=sys.stderr)
            payload = {"value": payload}
    else:
        payload = build_payload_from_flags(args.type, args)
    step_number = payload.get("step", payload.get("step_number"))

    record = {
        "timestamp": now_iso(),
        "event_type": args.type,
        "step_number": step_number,
        "payload": payload,
    }

    # Read the state, judge it, and append as ONE step, under the lock, so two
    # concurrent step_start calls cannot both read "no open step".
    #
    # `file_lock` is the workspace's one exclusive-lock primitive. It is
    # BOUNDED and degrades to running unlocked with a line on stderr rather
    # than blocking, which is the right trade here too: a trajectory log must
    # never wedge the run it is recording.
    lock_path = path.with_name(path.name + ".lock")
    with file_lock(lock_path, label="trajectory-log"):
        # Emit-time sequencing guard: reject a mis-ordered step marker the
        # moment it is emitted (exit 5), so a bad marker cannot land silently.
        # Wave-aware: an open parallel wave suspends the guard (its member
        # steps legitimately interleave). This rejects a single --event call,
        # never the run.
        if args.type in ("step_start", "step_end"):
            open_steps, parallel_open = _open_state(_read_events(path))
            if args.type == "step_start" and open_steps and not parallel_open:
                print(f"{RED}ERROR: sequencing violation: cannot open step "
                      f"{step_number} while step(s) "
                      f"{sorted(open_steps, key=_step_sort_key)} are still "
                      f"open. Emit their step_end first, or open a parallel "
                      f"wave_start for legitimate interleaving.{RESET}",
                      file=sys.stderr)
                return 5
            if args.type == "step_end" and step_number not in open_steps:
                print(f"{RED}ERROR: sequencing violation: step_end for step "
                      f"{step_number} has no open step_start.{RESET}",
                      file=sys.stderr)
                return 5

        try:
            append_event(path, record)
        except OSError as exc:
            print(f"{RED}ERROR: append failed: {exc}{RESET}", file=sys.stderr)
            return 3
    return 0


# ============================================================
# Self-check (--verify)
# ============================================================
# `glob.has_magic` covers `*?[`; the braces are this project's own shorthand,
# which glob does not treat as magic.
_GLOB_CHARS = ("{", "}")


def _is_literal_path(value: object) -> bool:
    """A usable literal file path: a non-blank string with no glob magic."""
    if not isinstance(value, str) or not value.strip():
        return False
    return not glob.has_magic(value) and not any(c in value for c in _GLOB_CHARS)


def is_scannable_path(value: object) -> bool:
    """The ONE rule for "this files_affected entry names a real file".

    One predicate for both readers, `--verify` and `--list-files`: no glob, no
    count token ("+3 more"), no blank.
    """
    s = str(value).strip()
    is_count = s[:1] == "+" and s[1:2].isdigit()
    return _is_literal_path(value) and not is_count

# The plan is the other half of the record, so these read the plan file itself
# rather than trusting the run's own narrative.
_PLAN_FILES_HEADING = "**Files affected:**"
_PLAN_DEVIATIONS_HEADING = "### Deviations from Plan"
_BACKTICKED = re.compile(r"`([^`]+)`")
_NUMBERED_ITEM = re.compile(r"^\d+\.\s")


def resolve_plan_path(plan_path: str) -> Path | None:
    """Locate the plan a run_start names. None when it cannot be found.

    The recorded path is relative and the plans live in the DATA overlay, so a
    bare `WORKSPACE_ROOT / plan_path` misses. Every plan-derived check degrades
    to silence when this returns None - a missing plan is not a trajectory
    defect.

    Candidates are the `plans` registry address and the engine root; a
    refusal to resolve `plans` drops only the overlay candidates.
    """
    raw = Path(plan_path)
    # A RELATIVE recorded path is never resolved against the caller's current
    # directory. `plans/foo.md` used to be tried as-is first, so verifying from  # leak-guard: ok (history: the old cwd lookup)
    # a scratch directory read `<cwd>/plans/foo.md` -- an unrelated or planted
    # file -- and the plan-derived advisories were computed from it. The two
    # roots this workspace actually keeps plans under are the only candidates.
    if raw.is_absolute():
        candidates = [raw]
    else:
        candidates = [WORKSPACE_ROOT / raw]
        try:
            plans = data_path("plans")
        except DataPathError as exc:
            # One candidate refusing must not take the others with it; the
            # plan-derived checks are advisory and degrade to silence on None.
            print(f"{YELLOW}plans could not be resolved ({exc}); looking only "
                  f"at {WORKSPACE_ROOT / raw}{RESET}", file=sys.stderr)
        else:
            candidates = [plans / raw.name, WORKSPACE_ROOT / raw, plans / raw]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def planned_files(plan_text: str) -> set[str]:
    """Every path listed under a `**Files affected:**` block in the plan."""
    out: set[str] = set()
    lines = plan_text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != _PLAN_FILES_HEADING:
            continue
        for follow in lines[i + 1:]:
            stripped = follow.strip()
            if not stripped:
                continue
            if not stripped.startswith("- "):
                break
            for token in _BACKTICKED.findall(stripped):
                token = token.strip()
                if "/" in token or "." in token:
                    out.add(token)
    return out


def declared_deviation_count(plan_text: str) -> int:
    """How many deviations the plan's Implementation Notes claim."""
    lines = plan_text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines)
                     if line.strip() == _PLAN_DEVIATIONS_HEADING)
    except StopIteration:
        return 0
    count = 0
    for line in lines[start + 1:]:
        if line.startswith(("### ", "## ")):
            break
        if _NUMBERED_ITEM.match(line):
            count += 1
    return count


def _tree_prefixes() -> tuple[str, ...]:
    """The prefixes a recorded path may legitimately carry, and no others.

    A run records a path against the engine tree or the data overlay, and it
    may spell either as an absolute root, as that root's directory name, or as
    the word "engine". That is the whole set. Anything else is a different
    file.
    """
    roots = [WORKSPACE_ROOT, get_data_root()]
    out = {"engine"}
    for root in roots:
        out.add(str(root).rstrip("/"))
        out.add(Path(root).name)
    return tuple(sorted(out, key=len, reverse=True))


def _repo_relative(value: str) -> str:
    """`value` with ONE known tree prefix stripped, if it carries one."""
    text = str(value).strip()
    # A leading "./" only. `lstrip("./")` takes a SET of characters, so it ate
    # the leading dot of `.heading-os-data/...` and the prefix never matched.
    while text.startswith("./"):
        text = text[2:]
    for prefix in _tree_prefixes():
        marker = prefix.rstrip("/") + "/"
        if text.startswith(marker):
            return text[len(marker):]
    return text


def _covers(recorded: str, planned: str) -> bool:
    """Whether a recorded path and a planned path name the same file.

    Exact equality after stripping ONE known tree prefix from each side.

    The bound is a NAMED set of prefixes, never any common suffix, which would
    match a different file.
    """
    return _repo_relative(recorded) == _repo_relative(planned)


def _git_changed_files(git_head: str) -> set[str]:
    """Engine working-tree change set since git_head: tracked diff ∪ untracked.

    Returns repo-relative POSIX paths (what git prints and what /implement
    records). Runs in WORKSPACE_ROOT. Returns an empty set on any git failure
    so the run-level reconciliation degrades gracefully to "no defect". Named
    (not inlined) so tests can monkeypatch exactly this seam.
    """
    import subprocess

    # `-z` so git never C-quotes a name; bytes mode plus `errors="replace"`
    # because text mode raises on a non-UTF-8 name and its universal newlines
    # merge names differing only by CR (both MEASURED 2026-08-30).
    changed: set[str] = set()
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", "-z", git_head],
            cwd=str(WORKSPACE_ROOT), capture_output=True, timeout=10,
        )
        if diff.returncode != 0:
            return set()
        changed.update(
            p for p in diff.stdout.decode("utf-8", "replace").split("\0") if p)
        untracked = subprocess.run(
            ["git", "ls-files", "-z", "--others", "--exclude-standard"],
            cwd=str(WORKSPACE_ROOT), capture_output=True, timeout=10,
        )
        if untracked.returncode == 0:
            changed.update(
                p for p in untracked.stdout.decode("utf-8", "replace").split("\0")
                if p)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return set()
    return changed


def verify_trajectory(run_id: str) -> list[str]:
    """Structurally self-check a trajectory JSONL; return defect strings.

    Empty list means clean. The checks are order-tolerant: they assert
    pairing and bracket membership, NOT step-bracket non-overlap, because
    parallel waves legitimately interleave their member steps. The M1
    interleave-ordering judgment stays with /scrutinize.

    Checks: run_start present and first; run_end present and last; every
    step_start paired with a step_end (open-stack per step_number, so a
    number reused across waves still reconciles); every wave_start paired
    with a wave_end; each wave bracket's wave_end.successes equals the
    bracketed count of step_end with status ok/deviation - including an
    ORPHAN wave_end, reconciled against the implicit bracket since the last
    wave boundary; in a run that uses waves at all, no step_end (other than
    the step-0 plan-load marker) sits outside every wave bracket; every
    files_affected entry is a literal path (no glob/shorthand/count token).

    Plus four advisory checks that report without asserting a violation:
    a wave_start whose payload omits step_count/parallel; a timestamp that
    goes backwards (clock or emission skew, not a sequencing fault); a
    deviation for step N emitted before that step's step_start (wave-scoped
    deviations exempt, since a deferred wave never opens a step at all); and
    the validation-gate check below. All four read the JSONL only, so none of
    them depends on the plan file being locatable.

    Plus a run-level files reconciliation (advisory): the current engine
    working tree is diffed against run_start.git_head, and any changed engine
    file absent from every step's files_affected is flagged "(advisory) ...".
    **Scope: the ENGINE tree only.** A run that also writes into the data
    overlay (a plan, a report, an output artifact) has those writes checked by
    nothing here, so a clean reconciliation is not full coverage - `cmd_verify`
    prints that scope explicitly rather than leaving a clean line to imply it.
    Reconciling the overlay too was considered and rejected: it accumulates
    unrelated background churn (daemon outputs, auto-memory, the trajectory
    file itself), which would bury real findings in noise.
    This is meaningful ONLY immediately after the run, against a tree holding
    just this run's changes (before any commit / git pull); re-run later on a
    historical trajectory with a stale-but-valid git_head it will over-flag
    pulled/committed files - expected, and harmless because advisory. It skips
    entirely (no defect) when git_head is "unknown" or any git call fails, so
    verify never becomes environment-fragile. The structural checks above stay
    pure-JSONL and are unaffected by repo state.

    Plus a validation-gate check (advisory): a completed run (run_end present)
    with zero validation_check events is flagged, so Phase 3 gate outcomes are
    logged as structured, machine-auditable events rather than only prose notes.
    """
    path = trajectory_path(run_id)
    defects: list[str] = []
    events: list[dict] = []
    # A verifier that dies on the file it is verifying reports nothing. One
    # invalid byte, or a file that turned unreadable after the caller's
    # existence check, used to leave a traceback where a defect string belongs.
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"trajectory could not be read: {exc}"]
    # split("\n"), for the reason given at the sibling reader `_read_events`;
    # change both readers together.
    for i, raw in enumerate(text.split("\n")):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            defects.append(f"line {i + 1}: malformed JSON ({exc})")
            continue
        # A JSONL line can be valid JSON and not an event: `[]`, `"x"`, `null`
        # all decode, and every `.get()` below then raises AttributeError.
        if not isinstance(obj, dict):
            defects.append(
                f"line {i + 1}: record is a {type(obj).__name__}, not an event object")
            continue
        events.append(obj)
    if not events:
        defects.append("trajectory is empty")
        return defects

    types = [e.get("event_type") for e in events]

    # run_start present, first, and EXACTLY ONE (two racing runs on one id).
    if "run_start" not in types:
        defects.append("run_start event is missing")
    elif types[0] != "run_start":
        defects.append("run_start is not the first event")
    if types.count("run_start") > 1:
        defects.append(
            f"{types.count('run_start')} run_start events in one trajectory "
            f"(two runs wrote to the same run_id)")

    # run_end present and last (a trajectory missing run_end is incomplete).
    if "run_end" not in types:
        defects.append("run_end event is missing (incomplete trajectory)")
    elif types[-1] != "run_end":
        defects.append("run_end is not the last event")

    # step_start / step_end pairing via an open-stack per step number.
    open_steps: dict[Any, int] = {}
    for idx, e in enumerate(events):
        et = e.get("event_type")
        sn = e.get("step_number")
        if et == "step_start":
            open_steps[sn] = open_steps.get(sn, 0) + 1
        elif et == "step_end":
            if open_steps.get(sn, 0) > 0:
                open_steps[sn] -= 1
            else:
                defects.append(
                    f"step_end for step {sn} at position {idx} has no open step_start")
    for sn, count in open_steps.items():
        if count > 0:
            defects.append(
                f"step {sn}: {count} step_start(s) never closed by a step_end")

    # wave_start / wave_end pairing + bracketed successes count.
    #
    # An orphan wave_end's successes claim is still reconciled, against the
    # IMPLICIT bracket since the last wave boundary (or run start). A STACK of
    # open start indices per wave number, so a repeated wave_start is reported.
    wave_starts: dict[Any, list[int]] = {}
    wave_spans: list[tuple[int, int]] = []
    saw_wave_event = False
    last_boundary = 0
    for idx, e in enumerate(events):
        et = e.get("event_type")
        payload = e.get("payload") or {}
        if et == "wave_start":
            saw_wave_event = True
            wave_no = payload.get("wave")
            open_for_wave = wave_starts.setdefault(wave_no, [])
            if open_for_wave:
                defects.append(
                    f"wave_start for wave {wave_no} at position {idx} opens a "
                    f"wave already opened at position {open_for_wave[-1]}")
            open_for_wave.append(idx)
            if "step_count" not in payload or "parallel" not in payload:
                defects.append(
                    f"(advisory) wave_start for wave {payload.get('wave')} at "
                    f"position {idx} omits step_count/parallel; the wave's shape "
                    f"is unrecoverable from the record")
            last_boundary = idx
        elif et == "wave_end":
            saw_wave_event = True
            w = payload.get("wave")
            open_for_wave = wave_starts.get(w) or []
            start_idx = open_for_wave.pop() if open_for_wave else None
            if start_idx is None:
                defects.append(
                    f"wave_end for wave {w} at position {idx} has no matching wave_start")
                bracket_start, implicit = last_boundary, True
            else:
                bracket_start, implicit = start_idx, False
                wave_spans.append((start_idx, idx))
            bracket = events[bracket_start + 1:idx]
            ok_ends = sum(
                1 for b in bracket
                if b.get("event_type") == "step_end"
                and (b.get("payload") or {}).get("status") in ("ok", "deviation")
            )
            declared = payload.get("successes")
            if declared != ok_ends:
                where = "implicit bracket since the last wave boundary" if implicit \
                    else "bracketed"
                defects.append(
                    f"wave {w}: wave_end.successes={declared} but {where} "
                    f"ok/deviation step_end count={ok_ends}")
            last_boundary = idx
    for w, open_indices in wave_starts.items():
        for start_idx in open_indices:
            defects.append(
                f"wave {w}: wave_start at position {start_idx} never closed by a wave_end")
            wave_spans.append((start_idx, len(events)))

    # Steps outside every wave bracket. Only meaningful in a run that uses
    # waves at all - a bare sequential run legitimately has none, and the plan
    # format forbids mixing the two shapes. Step 0 (the plan-load marker) sits
    # outside by design and is exempt.
    if saw_wave_event:
        unbracketed = [
            e.get("step_number")
            for idx, e in enumerate(events)
            if e.get("event_type") == "step_end"
            and e.get("step_number") != 0
            and not any(a < idx < b for a, b in wave_spans)
        ]
        if unbracketed:
            defects.append(
                "wave-mode run has step_end(s) outside every wave bracket: "
                + ", ".join(str(s) for s in unbracketed))

    # Timestamp monotonicity (advisory). Emission is sequential, so a record
    # whose timestamp precedes its predecessor is clock or emission skew, not a
    # sequencing violation - it is reported, never treated as one. Timestamps
    # are ISO-8601 UTC to the second, so they compare lexicographically; an
    # event without one is skipped rather than assumed.
    prev_ts, prev_idx = "", -1
    for idx, e in enumerate(events):
        ts = str(e.get("timestamp") or "")
        if not ts:
            continue
        if prev_ts and ts < prev_ts:
            defects.append(
                f"(advisory) event at position {idx} carries timestamp {ts}, "
                f"earlier than position {prev_idx} ({prev_ts})")
        prev_ts, prev_idx = ts, idx

    # files_affected literal-path check (catches the N1 glob/shorthand defect).
    for idx, e in enumerate(events):
        if e.get("event_type") != "step_end":
            continue
        for entry in (e.get("payload") or {}).get("files_affected") or []:
            s = str(entry)
            if not is_scannable_path(entry):
                defects.append(
                    f"step_end at position {idx}: files_affected entry "
                    f"{s!r} is not a literal path (glob/shorthand/count/blank)")

    # Run-level files reconciliation (advisory). Compare the current engine
    # working tree against run_start.git_head; flag any changed engine file
    # recorded in no step's files_affected. Graceful degrade: skip on git_head
    # "unknown" or any git failure (empty change set). See the docstring for
    # the "meaningful only immediately after the run" precondition.
    git_head = ""
    for e in events:
        if e.get("event_type") == "run_start":
            git_head = str((e.get("payload") or {}).get("git_head") or "")
            break
    if git_head and git_head != "unknown":
        changed = _git_changed_files(git_head)
        if changed:
            recorded: set = set()
            for e in events:
                if e.get("event_type") == "step_end":
                    for entry in (e.get("payload") or {}).get("files_affected") or []:
                        recorded.add(str(entry))
            # `_covers`, not a raw set difference: a recorded path may carry a
            # tree prefix, and both reconciliations use the same rule.
            for path_str in sorted(changed):
                if any(_covers(r, path_str) for r in recorded):
                    continue
                defects.append(
                    f"(advisory) {path_str} was modified in this run but "
                    f"appears in no step's files_affected")

    # Validation-gate logging (advisory; F-6.1 loose end). A completed run
    # (run_end present) that recorded zero validation_check events narrated its
    # Phase 3 gates in prose (step_end notes) instead of structured, machine-
    # auditable events. Surfaces the gap so gate outcomes are deterministically
    # checkable, matching the files-reconciliation advisory style.
    if "run_end" in types and "validation_check" not in types:
        defects.append(
            "(advisory) run has a run_end but zero validation_check events; "
            "Phase 3 gates should emit validation_check events, not only prose notes")

    # Deviation ordering (advisory). A deviation for step N emitted before that
    # step's step_start reads, to anyone consuming the JSONL in order, as a
    # divergence from a step that has not begun. Wave-scoped deviations are
    # exempt by design: a deferred wave never emits a step_start at all.
    seen_starts: set = set()
    for idx, e in enumerate(events):
        et = e.get("event_type")
        sn = e.get("step_number")
        if et == "step_start":
            seen_starts.add(sn)
        elif et == "deviation":
            if (e.get("payload") or {}).get("scope") == "wave":
                continue
            if sn is not None and sn not in seen_starts:
                defects.append(
                    f"(advisory) deviation for step {sn} at position {idx} precedes "
                    f"that step's step_start")

    defects.extend(_plan_reconciliation(events))
    return defects


def _plan_reconciliation(events: list[dict]) -> list[str]:
    """Advisory checks that read the run's plan file. Silent when unavailable.

    Two findings from the 2026-08-09 scrutiny live here. A file the plan lists
    for a step but no step ever records (M1: the run edited the one file its
    step named, and named four others instead). And a run whose plan declares
    more deviations in prose than the trajectory carries as events (M2: two of
    six, one of them inside a step recorded `ok`) - the narrative is what the
    event record exists to be checkable against, so the narrative claiming more
    is the direction that matters.
    """
    defects: list[str] = []
    plan_ref = ""
    for e in events:
        if e.get("event_type") == "run_start":
            plan_ref = str((e.get("payload") or {}).get("plan_path") or "")
            break
    if not plan_ref:
        return defects
    plan_file = resolve_plan_path(plan_ref)
    if plan_file is None:
        return defects
    try:
        plan_text = plan_file.read_text(encoding="utf-8")
    # UnicodeDecodeError too: it is a ValueError, not an OSError.
    except (OSError, UnicodeDecodeError):
        return defects

    recorded: set[str] = set()
    for e in events:
        if e.get("event_type") == "step_end":
            for entry in (e.get("payload") or {}).get("files_affected") or []:
                recorded.add(str(entry))
    for planned in sorted(planned_files(plan_text)):
        if not any(_covers(r, planned) for r in recorded):
            defects.append(
                f"(advisory) {planned} is listed under a plan step's Files affected "
                f"but appears in no step's files_affected")

    declared = declared_deviation_count(plan_text)
    emitted = sum(1 for e in events if e.get("event_type") == "deviation")
    if declared > emitted:
        defects.append(
            f"(advisory) the plan declares {declared} deviation(s) in its "
            f"Implementation Notes but the trajectory carries {emitted} deviation "
            f"event(s); emit the event at the moment of divergence, not at write-up")
    return defects


def cmd_files(args: argparse.Namespace) -> int:
    """Print the union of every step's files_affected, one literal path per line.

    So the Phase 3 hidden-character scan reads its file list off the record
    instead of being assembled by hand. The 2026-08-09 run scanned 11 files for a
    run that touched 22; nothing had escaped, but the evidence covered half the
    surface it claimed to.
    """
    if not args.run_id:
        print(f"{RED}ERROR: --list-files requires --run-id <id>{RESET}", file=sys.stderr)
        return 2
    path = trajectory_path(args.run_id)
    if not path.exists():
        print(f"{RED}ERROR: trajectory not found: {path}.{RESET}", file=sys.stderr)
        return 3
    out: set[str] = set()
    rejected: set[str] = set()
    for e in _read_events(path):
        if e.get("event_type") == "step_end":
            for entry in (e.get("payload") or {}).get("files_affected") or []:
                (out if is_scannable_path(entry) else rejected).add(str(entry))
    for entry in sorted(out):
        print(entry)
    # Named on stderr, never on stdout. The consumer pipes stdout into a scan
    # list, so a glob or a "+3 more" printed there becomes a path the scanner
    # tries to open; dropping them silently would instead shrink the evidence
    # without saying so, which is the same defect wearing the other hat.
    if rejected:
        print(f"{YELLOW}--list-files dropped {len(rejected)} non-literal "
              f"files_affected entr{'y' if len(rejected) == 1 else 'ies'}: "
              f"{', '.join(repr(r) for r in sorted(rejected))}. Run --verify: "
              f"each one is a defect on the record.{RESET}", file=sys.stderr)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    if not args.run_id:
        print(f"{RED}ERROR: --verify requires --run-id <id>{RESET}", file=sys.stderr)
        return 2
    path = trajectory_path(args.run_id)
    if not path.exists():
        print(f"{RED}ERROR: trajectory not found: {path}.{RESET}", file=sys.stderr)
        return 3
    defects = verify_trajectory(args.run_id)
    # State the reconciliation's scope on every run, clean or not. A clean line
    # otherwise reads as "every file this run touched is accounted for", which
    # it is not: the files check covers the engine tree only.
    print(f"{YELLOW}scope: files reconciliation covers the engine tree "
          f"({WORKSPACE_ROOT.name}) only; data-overlay writes are not checked.{RESET}",
          file=sys.stderr)
    if not defects:
        print(f"{GREEN}trajectory clean: {path.name}{RESET}")
        return 0
    print(f"{RED}trajectory has {len(defects)} structural defect(s):{RESET}",
          file=sys.stderr)
    for d in defects:
        print(f"{RED}  - {d}{RESET}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit structured trajectory events for /implement runs.",
    )
    sub = parser.add_mutually_exclusive_group(required=True)
    sub.add_argument("--new", action="store_true",
                     help="Mint a new run_id and write the run_start event.")
    sub.add_argument("--event", action="store_true",
                     help="Append one event to an existing trajectory.")
    sub.add_argument("--verify", action="store_true",
                     help="Structurally self-check an existing trajectory "
                          "(exit 1 on any defect, 0 when clean).")
    sub.add_argument("--list-files", dest="list_files", action="store_true",
                     help="Print the union of every step's files_affected, one "
                          "literal path per line (feeds the Phase 3 scan list).")

    parser.add_argument("--plan", help="Plan file path (required with --new).")
    parser.add_argument("--run-id", help="Existing run_id (required with --event).")
    parser.add_argument("--type", help=f"Event type. One of: {sorted(EVENT_TYPES)}.")
    parser.add_argument("--data-file",
                        help="Path to a JSON file holding the event payload. "
                             "REQUIRED for /implement automated calls (cross-platform).")
    parser.add_argument("--data-stdin", action="store_true",
                        help="Read JSON payload from stdin (pipe-friendly).")
    parser.add_argument("--data-json",
                        help="Inline JSON payload. Bash-only / hand-runs only. "
                             "/implement MUST NOT use this mode.")

    # Typed convenience flags (single-call emission, no temp file). Mutually
    # exclusive with the --data-* modes above. All default None so an unset
    # flag contributes no payload key.
    typed = parser.add_argument_group(
        "typed event flags",
        "Build the payload directly from flags instead of --data-file. "
        "Mutually exclusive with --data-file/--data-stdin/--data-json.")
    typed.add_argument("--step", type=int, help="Step number.")
    typed.add_argument("--title", help="Step or event title.")
    typed.add_argument("--file", dest="files", action="append",
                       help="A literal file path touched by the step (repeatable). "
                            "No globs, brace-shorthand, or count strings.")
    typed.add_argument("--status", help="step_end status: ok | issues | deviation.")
    typed.add_argument("--notes", help="Optional free-text note.")
    typed.add_argument("--wave", type=int, help="Wave number.")
    typed.add_argument("--step-count", type=int, dest="step_count",
                       help="wave_start step count.")
    typed.add_argument("--parallel", action=argparse.BooleanOptionalAction,
                       default=None,
                       help="wave_start: the PLAN'S declaration that this wave's steps "
                            "may interleave (--parallel/--no-parallel). It records what "
                            "was permitted, never what was observed - a declared-parallel "
                            "wave whose steps ran back-to-back is normal and is not a "
                            "defect. The emit-time sequencing guard reads it as a "
                            "permission; do not read it as evidence of concurrency.")
    typed.add_argument("--successes", type=int, help="wave_end successes count.")
    typed.add_argument("--failures", type=int, help="wave_end failures count.")
    typed.add_argument("--check", help="validation_check name.")
    passed_group = typed.add_mutually_exclusive_group()
    passed_group.add_argument("--passed", dest="passed", action="store_const",
                              const=True, default=None,
                              help="validation_check passed (sets passed=true).")
    passed_group.add_argument("--failed", dest="passed", action="store_const",
                              const=False,
                              help="validation_check failed (sets passed=false).")
    typed.add_argument("--detail", help="validation_check one-line detail.")
    typed.add_argument("--reason", help="deviation reason.")
    typed.add_argument("--what-changed", dest="what_changed",
                       help="deviation: what changed vs the plan.")
    typed.add_argument("--scope", help="deviation scope, e.g. 'wave' for a whole-wave deferral.")
    typed.add_argument("--artefact", help="evaluation_result artefact path.")
    typed.add_argument("--grade", help="evaluation_result grade.")
    typed.add_argument("--iteration", type=int, help="evaluation_result iteration.")
    typed.add_argument("--summary", help="run_end one-line summary.")
    typed.add_argument("--plan-status", dest="plan_status",
                       help="run_end plan status (default Implemented).")

    args = parser.parse_args(argv)

    # One place, before dispatch: every run-id command builds its path from this
    # string, and a rejected id is a bad ARGUMENT (exit 2), not a filesystem
    # error and not a traceback.
    if args.run_id is not None:
        try:
            validate_run_id(args.run_id)
        except ValueError as exc:
            print(f"{RED}ERROR: {exc}{RESET}", file=sys.stderr)
            return 2

    if args.new:
        return cmd_new(args)
    if args.event:
        if not args.run_id:
            print(f"{RED}ERROR: --event requires --run-id <id>{RESET}", file=sys.stderr)
            return 2
        if not args.type:
            print(f"{RED}ERROR: --event requires --type <event-type>{RESET}", file=sys.stderr)
            return 2
        return cmd_event(args)
    if args.verify:
        return cmd_verify(args)
    if args.list_files:
        return cmd_files(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
