#!/usr/bin/env python3
"""Run /prime's read-only health checks in parallel and emit aggregated output.

Replaces the previous serial chain of subprocess invocations the /prime skill
executed. The checks are defined in the CHECKS registry and rendered in
DISPLAY_ORDER; those two objects are the only statement of the live set. The
checks run concurrently on a ThreadPoolExecutor bounded to 8 workers, and the
output blocks are emitted in the fixed order /prime expects.

A single failing health check never blocks the others: the script captures
the exception, reports it inline in that check's block, and continues. Exit
code is always 0 from this helper (per-check failure is informational only).

Usage:
    python scripts/prime-health-parallel.py
    python scripts/prime-health-parallel.py --json    # machine-readable
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

# Workspace import bootstrap (per development-standards.md)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.checkpoint_paths import transcript_dir  # noqa: E402
from scripts.utils.venv_guard import venv_python  # noqa: E402
from scripts.utils.dossier import LIFIC_TIMEOUT  # noqa: E402
from scripts.utils.dossier import relink_command as dossier_relink  # noqa: E402
from scripts.utils.workspace import (  # noqa: E402
    get_default_tz,
    get_workspace_root,
)

#: What makes a file a dream-shadow report, on either side of the move. The
#: suffix is self-delimiting, so no path component is needed to separate these
#: from the rest of the `("trace", "report")` kind.
DREAM_REPORT_SUFFIX = "_dream-shadow_report.md"


# ============================================================
# Configuration
# ============================================================

# Fixed display order for /prime output blocks. Threads run concurrently;
# results are rendered serially in this order after all threads finish.
DISPLAY_ORDER = [
    "hooks_armed",
    "crm_health",
    "knowledge_health",
    "memory_health",
    "hindsight_health",
    "email_intel_status",
    "active_threads_archive_scan",
    "sync_exchange_health",
    "odin_cadence",
    "ops_radar",
    "reminders_due",
    "dream_shadow",
    "updates",
    "lific_board",
    "doc_versions_pending",
]

# Section banner for each block (matches /prime numbering for legibility)
SECTION_BANNERS = {
    "hooks_armed": "### 2.4 Session Hooks",
    "crm_health": "### 2.5 Relationship Radar",
    "knowledge_health": "### 2.7 Knowledge Base Health",
    "memory_health": "### 2.9 Memory Health",
    "hindsight_health": "### 2.9a Hindsight Memory Server",
    "email_intel_status": "### 2.10 Email Intelligence Status",
    "active_threads_archive_scan": "### 2.11 Active Threads -- archive scan",
    "sync_exchange_health": "### 2.13 Sync-Exchange Daemon",
    "odin_cadence": "### 2.14 Odin Cadence",
    "ops_radar": "### 2.15 Ops-Radar",
    "reminders_due": "### 2.16 Durable Reminders",
    "dream_shadow": "### 2.17 Dream-Shadow",
    "updates": "### 2.18 Component Updates",
    "lific_board": "### 2.19 Lific Board",
    "doc_versions_pending": "### 2.20 Document Version Registry",
}

# Per-check timeout (seconds). Real budget for /prime parallel block.
CHECK_TIMEOUT = 60


# ============================================================
# Health Check Implementations
# ============================================================

def _platform_settings_template(workspace_root: Path) -> Path | None:
    """The settings template this OS installs, chosen exactly as setup-platform.sh does.

    Change both together: a clone that arms the wrong template arms the wrong
    set of hooks and says nothing.
    """
    settings_dir = workspace_root / ".claude"
    if sys.platform.startswith("win") or sys.platform == "cygwin":
        names = ["settings.local.windows.json"]
    elif sys.platform == "darwin":
        names = ["settings.local.macos.json", "settings.local.linux.json"]
    else:
        names = ["settings.local.linux.json"]
    for name in names:
        candidate = settings_dir / name
        if candidate.is_file():
            return candidate
    return None


def run_hooks_armed(workspace_root: Path) -> dict[str, Any]:
    """Does the live settings file register every session hook the template does?

    `.claude/settings.local.json` is gitignored, and it is the ONLY place the
    session hooks are registered. The tracked `.claude/settings.json` registers
    exactly one. MEASURED 2026-09-20 by comparing the two files: a clone where
    `scripts/setup-platform.sh` has never run arms 2 hooks of 19, and among the
    17 absent is `_dispatch.py`, the single entry point for eleven PreToolUse
    walls including the release gate and the secret scanner.

    Nothing reported that state, and the step that fixes it was named in no
    document a person setting the workspace up would read. So a workspace could
    run for its whole life with its walls down and every surface calling it
    healthy.

    Renders nothing when armed: a boot line the operator reads every day and can
    never act on is noise. A short clone gets the list at the top of the brief.
    """
    script = workspace_root / "scripts" / "merge-platform-settings.py"
    template = _platform_settings_template(workspace_root)
    if not script.exists() or template is None:
        # Neither is optional in a clone of this repository, so their absence is
        # reported rather than skipped: a check that quietly opts out over a
        # missing file is the shape this whole finding was.
        missing = "merge-platform-settings.py" if not script.exists() \
            else f"a settings template for {sys.platform}"
        return {"status": "missing",
                "output": f"cannot check whether session hooks are armed: "
                          f"{missing} is absent",
                "omit_if_empty": False}
    proc = subprocess.run(
        [sys.executable, str(script), str(template),
         str(workspace_root / ".claude" / "settings.local.json"), "--check"],
        cwd=str(workspace_root),
        capture_output=True,
        text=True,
        timeout=CHECK_TIMEOUT,
    )
    if proc.returncode == 0:
        return {"status": "ok", "output": "", "omit_if_empty": True}
    return {
        "status": "error",
        "output": proc.stdout.strip() or f"--check exited {proc.returncode}",
        "stderr": proc.stderr.strip(),
        "omit_if_empty": False,
    }


def _run_health_script(workspace_root: Path, name: str) -> dict[str, Any]:
    """Invoke scripts/<name> with the pinned interpreter and capture its stdout."""
    script = workspace_root / "scripts" / name
    if not script.exists():
        return {"status": "missing", "output": f"{name} not found at {script}"}
    proc = subprocess.run(
        [str(venv_python()), str(script)],
        cwd=str(workspace_root),
        capture_output=True,
        text=True,
        timeout=CHECK_TIMEOUT,
    )
    return {
        "status": "ok" if proc.returncode == 0 else "error",
        "exit_code": proc.returncode,
        "output": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def run_crm_health(workspace_root: Path) -> dict[str, Any]:
    return _run_health_script(workspace_root, "crm-health.py")


def run_knowledge_health(workspace_root: Path) -> dict[str, Any]:
    return _run_health_script(workspace_root, "knowledge-health.py")


def memory_dir_for(workspace_root: Path) -> Path | None:
    """The native memory store for this workspace, or None off POSIX.

    The slug comes from its one owner,
    `scripts/utils/checkpoint_paths.transcript_dir`, never a local copy, and
    there is no fallback search: a directory found by a looser match is one the
    harness did not write (a local copy diverged, MEASURED 2026-09-05).
    """
    directory = transcript_dir(workspace_root)
    return None if directory is None else directory / "memory"


def run_memory_health(workspace_root: Path) -> dict[str, Any]:
    """Scan the persistent memory directory and report file/line counts.

    Inlined (no subprocess) - reads from the Claude Code memory dir under the
    user's ~/.claude project tree.
    """
    memory_dir = memory_dir_for(workspace_root)
    if memory_dir is None:
        # "error", not "missing". `missing` is in NON_FAILURE_STATUSES, so it
        # renders as an ordinary workspace with no memory system - which is the
        # silent green this whole seam exists to stop. Nothing was counted, and
        # the panel says that instead of a number.
        return {
            "status": "error",
            "output": ("Memory: the Claude Code project directory could not be "
                       "resolved on this platform (the slug rule refuses off "
                       "POSIX rather than guessing). No memory files were "
                       "counted; this is not a count of zero."),
        }
    # Objective defect computation is shared with scripts/memory-hygiene.py via
    # scripts/utils/memory_health.compute_memory_defects (dir-parameterized).
    from scripts.utils.memory_health import (
        MEMORY_BUDGET_LINES,
        MEMORY_INDEX_REMEDY,
        MEMORY_INDEX_UTF16_BUDGET,
        compute_memory_defects,
    )

    data = compute_memory_defects(memory_dir)
    if data["status"] == "missing":
        return {
            "status": "missing",
            "output": f"Memory: directory not found ({memory_dir}). Memory system inactive.",
        }

    files_count = data["file_count"]
    lines = data["memory_md_lines"]
    units = data["memory_md_utf16_units"]
    stale = data["stale"]
    orphans = data["orphans"]

    issues = []
    # `compute_memory_defects` has always returned this flag and this panel has
    # always dropped it, so an index over its own printed budget was reported as
    # "All healthy" - with the number that refutes the claim sitting in the same
    # sentence, two words to its left. `scripts/memory-hygiene.py` reads the same
    # field from the same helper and counts it as a defect in five places; the
    # panel the operator sees at EVERY session start was the honest tool's silent
    # twin.
    if data.get("over_budget"):
        issues.append(f"MEMORY.md is over its {MEMORY_BUDGET_LINES}-line budget")
    # The byte axis, and it is reported BEFORE it is breached on purpose. The
    # harness truncates instead of refusing, so a signal that waits for
    # `over_byte_budget` first speaks on the session where the end of the index
    # has already stopped loading. The warn band is the only place a warning can
    # still do anything.
    if data.get("over_byte_budget"):
        issues.append(
            f"MEMORY.md is over the harness's {MEMORY_INDEX_UTF16_BUDGET}-unit "
            f"budget ({units}); its end is being TRUNCATED at load - "
            f"{MEMORY_INDEX_REMEDY}")
    elif data.get("near_byte_budget"):
        issues.append(
            f"MEMORY.md is at {units}/{MEMORY_INDEX_UTF16_BUDGET} UTF-16 units; "
            f"past that the end is silently truncated - {MEMORY_INDEX_REMEDY}")
    if stale:
        issues.append(f"{len(stale)} memory files >45 days old (review recommended)")
    # The index state comes first, because it explains the orphan count rather
    # than adding to it: an absent MEMORY.md makes every fact file unreferenced.
    if not data.get("index_readable", True):
        issues.append(f"MEMORY.md was NOT read ({data.get('index_problem')})")
    # Names the STORE, because since 2026-09-20 that is the only source this
    # count rests on. An orphan has meant "not linked AND not in the store"
    # since 2026-09-11, and on 2026-09-20 the index stopped linking any record
    # at all (MEASURED: 0 link targets beside 440 fact files), so the old
    # sentence, "not linked from MEMORY.md", named the half that decides
    # nothing. The store is the Hindsight bank `main` since 2026-10-04
    # (HOS-198). Short, because this prints at session start.
    if orphans:
        issues.append(f"{len(orphans)} orphan file(s): in neither MEMORY.md nor "
                      f"the Hindsight bank")

    # The budget in the printed line is the constant the flag above is computed
    # from, not a literal beside it. A hardcoded `/200` here would keep printing
    # 200 after someone moved the budget, which is how the number and the verdict
    # came apart in the first place.
    # BOTH axes are on the counts line, not just the one that has a verdict
    # attached today. The line carried `{lines}/200` alone until 2026-09-09 and
    # read as a complete statement of how much room the index has left, while
    # the number that was actually running out appeared nowhere.
    counts = (f"Memory: {files_count} files, {lines}/{MEMORY_BUDGET_LINES} lines, "
              f"{units}/{MEMORY_INDEX_UTF16_BUDGET} units.")
    # COVERAGE, not a finding, and the distinction is why it sits here and not
    # in `issues`. An unanswered bank means the orphan question went unasked;
    # that is not something wrong with memory, so it must not read as a defect
    # on a panel the operator scans in one second at session start. But "All
    # healthy" over a category nobody evaluated is a coverage over-claim
    # (`.claude/rules/scope-claims.md`: name what you left out, because silence
    # about an exclusion reads as coverage). So the sentence carries both.
    # On a bare public clone there is no Hindsight server, so this is the
    # permanent state there and is worded to be read once rather than acted on.
    if not data.get("orphans_evaluated", True):
        counts += " Orphans NOT checked (Hindsight bank silent or under its floor)."
    body = counts + (" Issues: " + "; ".join(issues) if issues else " All healthy.")

    return {
        "status": "ok",
        "output": body,
        "file_count": files_count,
        "memory_md_lines": lines,
        "over_budget": bool(data.get("over_budget")),
        "memory_md_utf16_units": units,
        "over_byte_budget": bool(data.get("over_byte_budget")),
        "near_byte_budget": bool(data.get("near_byte_budget")),
        "stale": stale,
        "orphans": orphans,
        "orphans_evaluated": bool(data.get("orphans_evaluated")),
    }


def run_email_intel_status(workspace_root: Path) -> dict[str, Any]:
    """Read the email-intelligence state.json (registry name `email-intel-state`) and summarise."""
    from scripts.record_readers.sources.inbox import email_state_path

    state_path = email_state_path()
    if not state_path.exists():
        return {
            "status": "ok",
            "output": (
                "Email Intelligence: Never run. Use `/email-intel` to process "
                "yesterday's emails."
            ),
        }

    import datetime

    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # `ValueError`, not `json.JSONDecodeError`. `read_text(encoding="utf-8")`
        # raises `UnicodeDecodeError` on non-UTF-8 bytes, which is a SIBLING of
        # JSONDecodeError under ValueError, so the narrower tuple missed one of
        # the two ways a state file is unreadable. MEASURED 2026-09-01: a
        # state.json of `b"\xff\xfe\x00binary"` raised out of this check, and
        # `run_all._wrap` then reported the whole panel as an exception instead
        # of the named "state.json unreadable" line this branch exists to print.
        return {
            "status": "error",
            "output": f"Email Intelligence: state.json unreadable ({exc}).",
        }

    last_run = data.get("last_run")
    if not last_run:
        return {
            "status": "ok",
            "output": (
                "Email Intelligence: Never run. Use `/email-intel` to process "
                "yesterday's emails."
            ),
        }

    try:
        last = datetime.datetime.fromisoformat(last_run.replace("Z", "+00:00"))
    except ValueError:
        return {
            "status": "ok",
            "output": f"Email Intelligence: last_run={last_run} (unparseable)",
        }

    now = datetime.datetime.now(datetime.timezone.utc)
    if last.tzinfo is None:
        last = last.replace(tzinfo=datetime.timezone.utc)
    hours_ago = (now - last).total_seconds() / 3600

    # Check pending P1 tasks. tasks.md lives with the digests, not beside the state.
    from scripts.utils.data_path import data_path  # noqa: PLC0415
    tasks_path = data_path("email-intel-reports") / "tasks.md"
    p1_open = 0
    tasks_note = ""
    if tasks_path.exists():
        try:
            for line in tasks_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if stripped.startswith("- [ ]") and "P1" in stripped:
                    p1_open += 1
        except OSError as exc:
            # UNKNOWN is not ZERO. A bare `pass` here reported `p1_open = 0`,
            # so an unreadable tasks.md told the operator there were no pending
            # P1 tasks when the count was simply not available -- a silent
            # failure on a priority signal.
            tasks_note = f" tasks.md unreadable ({exc}); P1 count UNKNOWN."
            p1_open = None

    if hours_ago > 20:
        body = (
            f"Email Intelligence: Last run {hours_ago:.1f} hours ago. "
            f"Run `/email-intel` to catch up."
        )
    else:
        body = f"Email Intelligence: Last run {hours_ago:.1f} hours ago. Status: {data.get('last_run_status', 'unknown')}."

    if p1_open:
        body += f" Pending P1 tasks: {p1_open}."
    body += tasks_note

    return {
        # An unreadable tasks.md is a partial answer, not an ok one.
        "status": "ok" if p1_open is not None else "error",
        "output": body,
        "last_run_hours_ago": hours_ago,
        "p1_open": p1_open,
    }


def run_threads_archive_scan(workspace_root: Path) -> dict[str, Any]:
    """Invoke `python scripts/thread.py archive-scan` and capture output."""
    script = workspace_root / "scripts" / "thread.py"
    if not script.exists():
        return {
            "status": "skipped",
            "output": "[threads] archive-scan unavailable - script missing",
        }
    try:
        proc = subprocess.run(
            [str(venv_python()), str(script), "archive-scan"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "skipped",
            "output": "[threads] archive-scan unavailable - timeout",
        }

    if proc.returncode != 0:
        # Suppress panel on failure per /prime contract; emit single inform line.
        return {
            "status": "skipped",
            "output": "[threads] archive-scan unavailable - skipping panel",
            "exit_code": proc.returncode,
            "stderr": proc.stderr.strip(),
        }

    return {
        "status": "ok",
        "output": proc.stdout.strip() or "(no archive candidates)",
    }


def run_sync_exchange_health(workspace_root: Path) -> dict[str, Any]:
    """Invoke scripts/sync-exchange-pulse.py and capture its stdout (includes auto-spawn)."""
    script = workspace_root / "scripts" / "sync-exchange-pulse.py"
    if not script.exists():
        return {"status": "missing", "output": f"sync-exchange-pulse.py not found at {script}"}
    proc = subprocess.run(
        [str(venv_python()), str(script)],
        cwd=str(workspace_root),
        capture_output=True,
        text=True,
        timeout=CHECK_TIMEOUT,
    )
    # "error", not "failed": one failure word across every check, so the
    # renderer and the checks cannot disagree about which one means trouble.
    return {"status": "ok" if proc.returncode == 0 else "error",
            "output": proc.stdout or proc.stderr or "(no output)"}


def run_odin_cadence(workspace_root: Path) -> dict[str, Any]:
    """Run scripts/odin-cadence.py --quiet if present.

    Existence-guarded: where the script is absent the check no-ops and
    `omit_if_empty` drops the section. --quiet prints a one-line nudge ONLY on a
    genuine collect/reflect cadence signal; when up to date it prints nothing,
    so the section is omitted. Read-only (counts, never content) by
    construction of odin-cadence.py.
    """
    script = workspace_root / "scripts" / "odin-cadence.py"
    if not script.exists():
        return {"status": "skipped", "output": "", "omit_if_empty": True}
    proc = subprocess.run(
        [str(venv_python()), str(script), "--quiet"],
        cwd=str(workspace_root),
        capture_output=True,
        text=True,
        timeout=CHECK_TIMEOUT,
    )
    if proc.returncode != 0:
        # A non-zero child exit with EMPTY stdout used to vanish completely.
        # `render_text` honours `omit_if_empty` BEFORE it ever consults
        # `status`, so the banner, the failure and the captured stderr were all
        # dropped, and session boot rendered a clean brief over a check that had
        # crashed. odin-cadence.py only ever `return 0`s, so a non-zero exit is
        # an uncaught exception: the traceback goes to stderr and stdout is
        # empty, which is exactly the shape that disappeared.
        # The error path is a non-empty `output` plus `omit_if_empty: False`.
        return {
            "status": "error",
            "output": f"odin-cadence.py exited {proc.returncode}",
            "stderr": proc.stderr.strip(),
            "omit_if_empty": False,
        }
    return {
        "status": "ok",
        "output": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "omit_if_empty": True,
    }


def run_ops_radar(workspace_root: Path) -> dict[str, Any]:
    """Run scripts/ops-radar.py (default detailed view) if present.

    Existence-guarded: where the script is absent the check no-ops and
    `omit_if_empty` drops the section. The default (no-arg) run renders the
    detailed due-items view and is READ-ONLY (no heal, no state write); it
    respects ack/crunch suppression via assess(). When nothing is due ops-radar prints an "all clear" line; we map
    that to empty output so the section is omitted from the brief.
    """
    script = workspace_root / "scripts" / "ops-radar.py"
    if not script.exists():
        return {"status": "skipped", "output": "", "omit_if_empty": True}
    proc = subprocess.run(
        [str(venv_python()), str(script)],
        cwd=str(workspace_root),
        capture_output=True,
        text=True,
        timeout=CHECK_TIMEOUT,
    )
    if proc.returncode != 0:
        # Same shape as run_odin_cadence above, for the same reason: a crashed
        # child writes its traceback to stderr and leaves stdout empty, and
        # `omit_if_empty` then erased the whole section before `render_text`
        # looked at the status.
        return {
            "status": "error",
            "output": f"ops-radar.py exited {proc.returncode}",
            "stderr": proc.stderr.strip(),
            "omit_if_empty": False,
        }
    out = proc.stdout.strip()
    # "all clear" -> omit the panel; only surface when something is actually due.
    #
    # Tested on the FIRST LINE, not anywhere in the output. `"all clear" in out`
    # searched every line of the detailed view, so a single due item whose
    # summary carried that phrase blanked the entire panel -- the brief then said
    # nothing at all while something was in fact due. render_detailed emits the
    # all-clear sentence as the whole output, and any other run opens with
    # "ops-radar: N item(s) due".
    first_line = out.splitlines()[0] if out else ""
    if first_line.endswith("all clear - nothing due."):
        out = ""
    return {
        "status": "ok",
        "output": out,
        "stderr": proc.stderr.strip(),
        "omit_if_empty": True,
    }


def run_reminders_due(workspace_root: Path) -> dict[str, Any]:
    """Read-only: surface durable reminders that are DUE, as a /prime backstop.

    Due only, deliberately. This check used to also list everything falling
    inside a 7-day lookahead, which meant a reminder the operator had dated for
    a specific day reached them up to a week early, every session until then --
    the exact noise the date was chosen to avoid. A reminder dated D is for D.
    `scripts/reminders-notify.py`, the Telegram path, has always been due-only;
    this brings the /prime backstop in line with it.

    Never mutates the store, never marks fired. ceo-only surface via outputs;
    omit_if_empty keeps the brief clean when nothing is due.
    """
    from datetime import datetime as _dt

    try:
        from scripts.utils import reminders_store as rs
        today = _dt.now(get_default_tz()).date()
        due = rs.due_records(today)
    except Exception as exc:  # noqa: BLE001 - boundary; reported inline
        return {"status": "error", "output": f"reminders check failed: {exc}",
                "omit_if_empty": True}
    lines = [
        f"DUE: {r['message']}" + (f"  -> {r['command']}" if r.get("command") else "")
        for r in due
    ]
    return {"status": "ok", "output": "\n".join(lines), "omit_if_empty": True}


def run_dream_shadow(workspace_root: Path) -> dict[str, Any]:
    """Read the latest dream-shadow report and surface one line when it lists
    merge candidates, nothing otherwise.

    Read-only: never runs scripts/dream-shadow.py itself -- that is the
    nightly timer's job (scripts/install-dream-shadow-timer.sh). This check
    only reads whatever report already exists. Existence-guarded: if no report
    has been written yet (timer not installed / first run pending), the check is
    silently skipped, matching the odin_cadence "renders nothing when empty"
    pattern.

    WHERE is the registry name `dream-reports`, the one the writer uses, read
    with `data_read` because a reader must look at every home the name has.
    Each existing home is globbed one level deep for the report suffix, which
    is what the previous `compat.records_or_legacy` plus `under_legacy(depth=1)`
    selected. MEASURED 2026-09-25 on the operator's overlay: both select the
    same 52 reports and the same newest one. It was `get_outputs_dir()` joined
    to `operations/dream` until that day, when HOS-5 section 6 deleted the
    helper.

    NEWEST BY MTIME, not by name. The old `sorted(...)[-1]` was a name sort over
    one directory, where the date prefix made it chronological. During the
    transition the reports can sit in more than one directory at once and a
    lexical sort across them is not an ordering in time at all. The name is kept
    as the tiebreak, so a single directory of dated reports answers exactly as
    it did before.
    """
    try:
        from scripts.utils.data_path import data_read
        reports = [p for home in data_read("dream-reports") if home.is_dir()
                   for p in home.glob(f"*{DREAM_REPORT_SUFFIX}") if p.is_file()]
        latest = max(reports, key=lambda p: (p.stat().st_mtime, p.name)) if reports else None
    except Exception as exc:  # noqa: BLE001 - boundary; reported inline
        return {"status": "error", "output": f"dream-shadow check failed: {exc}",
                "omit_if_empty": True}
    if latest is None:
        return {"status": "skipped", "output": "", "omit_if_empty": True}

    try:
        text = latest.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        # `ValueError`, for the reason the two handlers above record.
        # `UnicodeDecodeError` is a ValueError, not an OSError, so a report of
        # non-UTF-8 bytes escaped the branch whose message is literally
        # "unreadable". MEASURED 2026-09-01: the check raised, `run_all._wrap`
        # caught it, and the panel reported a generic exception instead of the
        # named line. The report is written by `scripts/dream-shadow.py` on a
        # nightly timer, so a torn write is how it becomes undecodable.
        return {"status": "error", "output": f"dream-shadow report unreadable: {exc}",
                "omit_if_empty": True}

    # Dormancy is deliberately NOT surfaced here: it is informational, it
    # proposes nothing, and on the first runs after reinforcement shipped it
    # lists nearly every aged file. It lives in the report for the operator to
    # read when they want it. Merge candidates DO need a nudge — each one is a
    # decision waiting on them.
    merge_section = re.search(r"## Merge Candidates.*?\n\n(.*?)(?:\n---|\Z)", text, re.DOTALL)
    body = merge_section.group(1) if merge_section else ""

    # A scan that COULD NOT RUN is not a scan that found nothing. When the
    # embedder is unavailable, dream-shadow writes one `- UNAVAILABLE: ...`
    # bullet instead of pair lines; it carries no `<->`, so the count below read
    # 0 and this check returned `status: ok` with empty output and
    # `omit_if_empty`. The embedder could be down every night and session boot
    # would never say a word. Surfaced by its marker, not by a count.
    #
    # What makes this render is `omit_if_empty: False` plus a non-empty output,
    # NOT the status string: `render_text` only consults the status to decide
    # whether to append stderr. So adding "warn" to NON_FAILURE_STATUSES later
    # cannot silently re-hide this line.
    unavailable = re.search(r"^- UNAVAILABLE: *(.*)$", body, re.MULTILINE)
    if unavailable:
        return {
            "status": "warn",
            "output": (f"Dream-shadow: merge scan did not run "
                       f"({unavailable.group(1).strip() or 'no reason recorded'}) — "
                       f"consolidation is not being detected."),
            "omit_if_empty": False,
        }

    merge_n = len(re.findall(r"^- .+<->.+$", body, re.MULTILINE))
    if merge_n == 0:
        return {"status": "ok", "output": "", "omit_if_empty": True}
    return {
        "status": "ok",
        "output": f"Dream-shadow: {merge_n} merge candidates — run `/dream` to review.",
        "omit_if_empty": True,
    }


def _doc_watch_line() -> str:
    """The documentation watch's one line, or "" when its last report holds
    neither a change nor a source that could not be checked.

    Read from `doc-watch-state`, which `update-manager.py docs` writes; the
    sentence itself is `update_docwatch.prime_line`, so the panel and the
    command cannot word it differently. A tree without the name, or without a
    run yet, has nothing to say. An unreadable state file is said.
    """
    from scripts.utils.data_path import UnknownDataPath, data_path  # noqa: PLC0415
    from scripts.utils.update_docwatch import prime_line  # noqa: PLC0415
    try:
        state_file = data_path("doc-watch-state")
    except UnknownDataPath:
        return ""
    if not state_file.exists():
        return ""
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"MCP docs watch: state unreadable ({exc})"
    return prime_line(state) if isinstance(state, dict) else "MCP docs watch: state malformed"


def run_updates(workspace_root: Path) -> dict[str, Any]:
    """Read the update-manager state and surface waiting `notify` updates and any
    failed auto-apply. Read-only: never runs `update-manager check` (the daily
    timer's job). Silent when everything is current or no state exists yet.

    Reads `data_path('update-state')`, the same name the writer
    `scripts/update-manager.py` uses, so reader and writer cannot point at two
    files. `UnknownDataPath` is mapped to the same "skipped" a missing file
    gives: both are the normal condition of a public clone with no data
    repository, not a defect.
    """
    from scripts.utils.data_path import UnknownDataPath, data_path  # noqa: PLC0415
    try:
        state_file = data_path('update-state')
    except UnknownDataPath:
        return {"status": "skipped", "output": "", "omit_if_empty": True}
    except Exception as exc:  # noqa: BLE001 - boundary; reported inline
        return {"status": "error", "output": f"updates check failed: {exc}",
                "omit_if_empty": True}
    docs_line = _doc_watch_line()
    if not state_file.exists():
        return {"status": "ok" if docs_line else "skipped", "output": docs_line,
                "omit_if_empty": True}
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # `ValueError`, for the reason `run_email_intel_status` above records:
        # `UnicodeDecodeError` is a sibling of `json.JSONDecodeError`, not a
        # subclass, so the narrower tuple caught only half of "unreadable".
        return {"status": "error", "output": f"updates state unreadable: {exc}",
                "omit_if_empty": True}

    lines: list[str] = []
    # `.get` with a fallback on every field. The state file is written by a
    # DIFFERENT component (update-manager), so any version skew between writer
    # and reader used to turn one entry missing one of four keys into a KeyError
    # that took the whole updates section down -- hiding every other valid
    # update behind a traceback. The statuses below are update-manager's
    # vocabulary, not this script's check statuses; they are unrelated words
    # that happen to look alike.
    for name, e in state.get("components", {}).items():
        if not isinstance(e, dict):
            lines.append(f"{name}: malformed entry ({type(e).__name__}), skipped")
            continue
        display = e.get("display", name)
        current = e.get("current", "?")
        latest = e.get("latest", "?")
        tier = e.get("tier", "?")
        status = e.get("status")
        if status == "waiting":
            lines.append(
                f"{display} {current}->{latest} "
                f"({tier} - apply: update-manager apply {name})"
            )
        elif status == "failed":
            lines.append(
                f"{display}: auto-apply FAILED (rolled back, "
                f"{e.get('fail_count', 0)}x) - check logs"
            )
        elif status == "observed-stale":
            lines.append(f"{display} {current}->{latest} (observed - self-updates)")
    out = ["Updates waiting:\n  " + "\n  ".join(lines)] if lines else []
    if docs_line:
        out.append(docs_line)
    return {"status": "ok", "output": "\n".join(out), "omit_if_empty": True}


def run_hindsight_health(workspace_root: Path) -> dict[str, Any]:
    """Run `scripts/hindsight-health.py show`: the memory server, seen from here.

    `show` is the read-only half of the hourly check: it evaluates the tunnel, the
    API, the web UI, the local backup copy and the server's own snapshot, prints
    one `PROBLEM` line each, and never notifies and never writes the check's state.
    A healthy server prints `healthy` and exits 0, so the panel is dropped. Any
    problem, a non-zero exit or a timeout is shown, because silence here would
    read as "the memory server is fine" while nothing was asked.
    """
    script = workspace_root / "scripts" / "hindsight-health.py"
    if not script.exists():
        return {"status": "skipped", "output": "", "omit_if_empty": True}
    try:
        proc = subprocess.run(
            [str(venv_python()), str(script), "show"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "omit_if_empty": False,
                "output": f"Hindsight: state UNKNOWN, the check gave no answer in {CHECK_TIMEOUT} s"}
    problems = [ln.removeprefix("PROBLEM ").strip()
                for ln in proc.stdout.splitlines() if ln.startswith("PROBLEM ")]
    if proc.returncode == 0 and not problems:
        return {"status": "ok", "output": "", "omit_if_empty": True}
    if not problems:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"]
        return {"status": "error", "omit_if_empty": False,
                "output": f"Hindsight: state UNKNOWN, hindsight-health.py exited {proc.returncode}: {tail[0]}"}
    return {"status": "warn", "omit_if_empty": False,
            "output": "Hindsight: " + str(len(problems)) + " problem(s)\n"
                      + "\n".join(f"  - {p}" for p in problems)}


def run_lific_board(workspace_root: Path) -> dict[str, Any]:
    """Run scripts/lific-board.py: the tracker board and pending close-requests.

    Existence-guarded like the radar above, and the helper itself exits 0 with
    no output when `lific` is not installed, so a clone without the tracker
    stays silent rather than reporting a failure.

    The helper prints nothing when every project is idle, so `omit_if_empty`
    drops the panel on a quiet day. A NON-ZERO exit is never omitted: the
    helper returns 2 only when `lific` is present and a read failed, which is
    exactly the case where silence would be read as "nothing due".
    """
    script = workspace_root / "scripts" / "lific-board.py"
    if not script.exists():
        return {"status": "skipped", "output": "", "omit_if_empty": True}
    # The outbox flush runs beside the board read, not after it: two full
    # timeouts in series would hold the panel for twice CHECK_TIMEOUT.
    with ThreadPoolExecutor(max_workers=1) as pool:
        outbox = pool.submit(_dossier_outbox, workspace_root)
        try:
            proc = subprocess.run(
                [str(venv_python()), str(script)],
                cwd=str(workspace_root),
                capture_output=True,
                text=True,
                timeout=CHECK_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            proc = None
        # A hung or failing Lific is exactly when records wait: the outbox line still goes out.
        waiting = outbox.result()
    if proc is None:
        return {
            "status": "error",
            "output": f"lific-board.py timed out after {CHECK_TIMEOUT}s"
                      + (f"\n\n{waiting}" if waiting else ""),
            "omit_if_empty": False,
        }
    if proc.returncode != 0:
        failed = f"lific-board.py exited {proc.returncode}"
        return {
            "status": "error",
            "output": failed + (f"\n\n{waiting}" if waiting else ""),
            "stderr": proc.stderr.strip(),
            "omit_if_empty": False,
        }
    output = proc.stdout.strip()
    if waiting:
        output = (output + "\n\n" if output else "") + waiting
    return {
        "status": "ok",
        "output": output,
        "stderr": proc.stderr.strip(),
        "omit_if_empty": True,
    }


def _dossier_outbox(workspace_root: Path) -> str:
    """Flush the task-dossier outbox (HOS-256); a line when records wait or were rejected, else "".

    The outbox holds `[dossier]` records Lific did not accept when they were
    written. A flush failing is said, never hidden: the line is the only place
    a stuck journal shows.
    """
    script = workspace_root / "scripts" / "dossier-journal.py"
    if not script.exists():
        return ""
    try:
        proc = subprocess.run(
            # The flush starts no Lific call after the deadline, so it overruns it by one
            # call at most (LIFIC_TIMEOUT), plus 5 s for start-up and the lock release:
            # it ends before this call would be killed holding the lock.
            [str(venv_python()), str(script), "flush", "--report-rejected",
             "--deadline", str(CHECK_TIMEOUT - LIFIC_TIMEOUT - 5)],
            cwd=str(workspace_root), capture_output=True, text=True, timeout=CHECK_TIMEOUT,
        )
        # Exit 1 is flush's own "records still wait", with its JSON on stdout.
        if proc.returncode not in (0, 1) or not proc.stdout.strip():
            return (f"Dossier outbox: state unknown "
                    f"(dossier-journal.py flush exited {proc.returncode})")
        result = json.loads(proc.stdout)
        left = result.get("left", 0)
        rejected = result.get("rejected_new") or {}
        corrupt = result.get("corrupt_new", 0)
        systemic = result.get("systemic") or {}
        stuck = result.get("stuck") or {}
        dropped = result.get("dropped_new") or []
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired) as exc:
        return f"Dossier outbox: state unknown ({type(exc).__name__})"
    lines = []
    if left and left < 0:
        lines.append("Dossier outbox: unreadable (scripts/dossier-journal.py count)")
    elif left:
        lines.append(f"Dossier outbox: {left} record(s) still waiting for Lific "
                     f"(scripts/dossier-journal.py flush)")
    if isinstance(rejected, dict) and rejected:
        # Every reason, not only `missing`: a refusal or a closed issue is news too.
        named = ", ".join(f"{n} {kind}" for kind, n in sorted(rejected.items()))
        lines.append(f"Dossier outbox: set aside since last shown: {named} "
                     f"(dossier-outbox.rejected.jsonl)")
    if corrupt:
        lines.append(f"Dossier outbox: {corrupt} unreadable line(s) moved to "
                     f"dossier-outbox.corrupt.jsonl")
    runs = systemic.get("count", 0) if isinstance(systemic, dict) else 0
    if isinstance(runs, int) and runs >= 2:
        # Lific-wide, never one record's fault: nothing is set aside for it.
        lines.append(f"Dossier: Lific refused every record in the last {runs} flushes: "
                     f"{systemic.get('first', '')}")
    stuck_n = stuck.get("count", 0) if isinstance(stuck, dict) else 0
    if isinstance(stuck_n, int) and stuck_n:
        # Never set aside for this: a person decides what a stuck record is.
        named = "; ".join(f"{issue} {error}" for issue, error in stuck.get("named", [])[:3])
        lines.append(f"Dossier: {stuck_n} record(s) stuck: {named}")
    for note in dropped if isinstance(dropped, list) else []:
        # Shown once (HOS-259): the prompt file that dropped it keeps the same line.
        if isinstance(note, dict) and note.get("issue") and note.get("path"):
            lines.append(f"Dossier: carried {note['issue']} dropped: not named in "
                         f"{note['path']}; relink with "
                         f"{dossier_relink(str(note['issue']), str(note['path']))}")
    return "\n".join(lines)


#: The one line this check ever prints, with `{n}` filled in. Named here rather
#: than only inline so the exact wording has one place to change.
_DOC_VERSIONS_PENDING_LINE = "Версии документов: ждут выбора {n} (doc-versions.py pending)"
_DOC_VERSIONS_NEEDS_ATTENTION = "Версии документов: реестр требует внимания (doc-versions.py pending)"


def run_doc_versions_pending(workspace_root: Path) -> dict[str, Any]:
    """Run scripts/doc-versions.py pending --count: how many document version
    families still await a human decision.

    Existence-guarded and rendered like the Lific board above: silent (via
    `omit_if_empty`) when the script is absent on a bare public clone, and
    silent again when the count is 0, per the ruling that this line only
    appears when N > 0.

    A registry `doc-versions.py` cannot replay, whether `RegistryCorrupt` or
    any other failure, makes that CLI exit non-zero with a Python traceback on
    its own stderr (`cmd_pending` reads and replays the registry with no
    try/except of its own, same as `show`, `reject`, `remove-member` and
    `summary`). That traceback is never surfaced here: a non-zero exit or an
    unparsable count both become one fixed line saying the registry needs
    attention, so a damaged registry degrades this panel instead of breaking
    `/prime`.
    """
    script = workspace_root / "scripts" / "doc-versions.py"
    if not script.exists():
        return {"status": "skipped", "output": "", "omit_if_empty": True}
    # the pinned interpreter, never sys.executable or a bare `python` (another
    # interpreter would run the CLI without the pinned dependencies)
    venv_python = workspace_root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        return {"status": "error", "output": _DOC_VERSIONS_NEEDS_ATTENTION + ": .venv/bin/python not found",
                "omit_if_empty": False}
    proc = subprocess.run(
        [str(venv_python), str(script), "pending", "--count"],
        cwd=str(workspace_root),
        capture_output=True,
        text=True,
        timeout=CHECK_TIMEOUT,
    )
    if proc.returncode == 0:
        try:
            count = int(proc.stdout.strip())
        except ValueError:
            count = None
    else:
        count = None
    if count is None:
        # "error", like run_lific_board's own failure case, so this is counted
        # as a failure everywhere results are aggregated. Never proc.stderr
        # here, and no "stderr" key at all: that is exactly the traceback this
        # check exists to keep out of /prime's output, and render_text() only
        # ever prints a "stderr" key that is actually present.
        return {"status": "error", "output": _DOC_VERSIONS_NEEDS_ATTENTION, "omit_if_empty": False}
    if count <= 0:
        return {"status": "ok", "output": "", "omit_if_empty": True}
    return {
        "status": "ok",
        "output": _DOC_VERSIONS_PENDING_LINE.format(n=count),
        "omit_if_empty": True,
    }


# Map check key -> callable
CHECKS: dict[str, Callable[[Path], dict[str, Any]]] = {
    "hooks_armed": run_hooks_armed,
    "crm_health": run_crm_health,
    "knowledge_health": run_knowledge_health,
    "memory_health": run_memory_health,
    "hindsight_health": run_hindsight_health,
    "email_intel_status": run_email_intel_status,
    "active_threads_archive_scan": run_threads_archive_scan,
    "sync_exchange_health": run_sync_exchange_health,
    "odin_cadence": run_odin_cadence,
    "ops_radar": run_ops_radar,
    "reminders_due": run_reminders_due,
    "dream_shadow": run_dream_shadow,
    "updates": run_updates,
    "lific_board": run_lific_board,
    "doc_versions_pending": run_doc_versions_pending,
}


# ============================================================
# Aggregation & Output
# ============================================================

def run_all(workspace_root: Path) -> dict[str, dict[str, Any]]:
    """Dispatch all checks to a ThreadPoolExecutor and collect results."""
    results: dict[str, dict[str, Any]] = {}

    def _wrap(key: str) -> tuple[str, dict[str, Any]]:
        fn = CHECKS[key]
        t0 = time.perf_counter()
        try:
            res = fn(workspace_root)
        except subprocess.TimeoutExpired as exc:
            res = {
                "status": "error",
                "output": f"timeout after {exc.timeout}s",
            }
        except Exception as exc:  # noqa: BLE001 - boundary; reported inline
            # The traceback goes to STDERR, not into the result dict. `--json`
            # prints the dict, and a formatted traceback carries absolute paths
            # -- home directory, username, workspace layout -- into output that
            # is designed to be pasted elsewhere. Text mode never rendered it
            # anyway.
            print(traceback.format_exc(), file=sys.stderr)
            res = {
                "status": "error",
                "output": f"{type(exc).__name__}: {exc}",
            }
        res.setdefault("elapsed_ms", round((time.perf_counter() - t0) * 1000, 1))
        return key, res

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_wrap, k) for k in CHECKS]
        for fut in as_completed(futures):
            key, res = fut.result()
            results[key] = res

    return results


# The statuses that are NOT a failure. Everything else is, including a value
# added later that nobody thought to list here.
NON_FAILURE_STATUSES = frozenset({"ok", "skipped", "missing"})


def render_text(results: dict[str, dict[str, Any]]) -> str:
    """Format aggregated results in the order /prime expects."""
    lines: list[str] = []
    for key in DISPLAY_ORDER:
        banner = SECTION_BANNERS[key]
        res = results.get(key, {"status": "missing", "output": "(no result)"})
        body = res.get("output", "").rstrip()
        # Optional sections (e.g. odin_cadence) render nothing when empty -- no
        # banner, no "(no output)" line.
        #
        # A FAILING check is never omitted, whatever it asks for. `omit_if_empty`
        # is a quiet-when-healthy switch, and read on its own it also silenced
        # two checks whose child had crashed with empty stdout. Both are fixed at
        # their source above; this second gate is what stops the next check added
        # here from reintroducing the same disappearance. It changes nothing for
        # a check that already reports its failure in `output`.
        if not body and res.get("omit_if_empty") \
                and res.get("status") in NON_FAILURE_STATUSES:
            continue
        lines.append(banner)
        if not body:
            body = "(no output)"
        lines.append(body)
        # Anything that is NOT a known-good status is a failure, so its stderr
        # is shown. Deriving the failure set from the good one means a NEW
        # status string cannot hide a diagnostic by accident (a `"failed"`
        # status once dropped two daemons' stderr, fixed 2026-08-30).
        if res.get("status") not in NON_FAILURE_STATUSES:
            stderr = res.get("stderr", "").strip()
            if stderr:
                lines.append(f"[stderr] {stderr}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ============================================================
# CLI Entry Point
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run /prime's read-only health checks in parallel.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the formatted text block.",
    )
    args = parser.parse_args()

    workspace_root = get_workspace_root()
    t0 = time.perf_counter()
    results = run_all(workspace_root)
    elapsed = round((time.perf_counter() - t0) * 1000, 1)

    if args.json:
        print(json.dumps(
            {"elapsed_ms": elapsed, "results": results},
            indent=2,
            default=str,
        ))
    else:
        print(render_text(results))

    # Always exit 0 - per-check failures are reported inline, not propagated.
    return 0


if __name__ == "__main__":
    sys.exit(main())
