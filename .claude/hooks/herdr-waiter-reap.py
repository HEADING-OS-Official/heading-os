#!/usr/bin/env python3
"""herdr-waiter-reap.py - PostToolUse on Bash: stop the waiters of a removed workspace.

A `herdr agent wait` started in the background outlived its pane by about 20
hours after the pane went away with its yard (MEASURED 2026-10-30, HOS-269).
`scripts/herdr-wait.py` exits on its own once its pane is gone; this hook makes
a yard's deletion stop them at once, and only them.

It fires on a Bash command carrying a `herdr worktree remove --workspace W`
link, read by `_yard_deletion_requests` in `.claude/hooks/_dispatch.py` (loaded
by path, never copied). It then asks `herdr workspace list`: only when W is no
longer listed, so the removal succeeded, does it stop the waiters whose record
in `runtime:herdr-waiters/state` names W. Each is identified by PID, start time
and command line; one that no longer matches is a stale record, deleted with
nothing killed. SIGTERM to that PID alone, never a group or a name pattern;
one shared wait of up to 5 seconds, then SIGKILL for any still alive.

A hook never fails the tool call: every error goes to stderr and it exits 0.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Same boot block as `.claude/hooks/yard-watch.py`: the engine root derived
# from THIS file, so a yard and the main clone each use their own tree.
_BOOT = Path(__file__).resolve()
ENGINE_ROOT = _BOOT.parent.parent.parent
for _candidate in [_BOOT.parent, *_BOOT.parents]:
    if (_candidate / "scripts" / "utils" / "clone_guard.py").is_file():
        ENGINE_ROOT = _candidate
        break
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

DISPATCH = _BOOT.parent / "_dispatch.py"
GRACE_S = 5.0


def _load_dispatch():
    spec = importlib.util.spec_from_file_location("heading_dispatch_reap", DISPATCH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {DISPATCH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _removed_workspaces(command: str, cwd: Path) -> list[str]:
    dispatch = _load_dispatch()
    found = []
    for form, _targets, closes in dispatch._yard_deletion_requests(command, cwd):
        if form == "herdr worktree remove" and closes and closes not in found:
            found.append(closes)
    return found


def _listed_workspaces(HA) -> set[str]:
    payload = HA._run(["workspace", "list"], HA.LIST_TIMEOUT)
    result = payload.get("result")
    rows = result.get("workspaces") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise HA.HerdrUnavailable("workspace list carried no workspaces array")
    return {row.get("workspace_id") for row in rows if isinstance(row, dict)}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reap(workspaces: set[str], HA) -> None:
    """SIGTERM every matching waiter of ALL removed workspaces, then ONE wait.

    One grace per workspace made seven removals in one command take 7 x 5 s,
    and the 30 s hook timeout cut the hook off with two waiters still alive
    (reproduced 2026-10-30 with SIGTERM-ignoring stand-ins).
    """
    signalled = []
    for record in HA.waiter_records():
        # A non-string value would raise TypeError on the set lookup and stop
        # the whole pass; such a record names no workspace, so it is skipped.
        if not isinstance(record.get("workspace"), str) \
                or record["workspace"] not in workspaces:
            continue
        if HA.stop_waiter(record):
            signalled.append(record)
    deadline = time.monotonic() + GRACE_S
    while time.monotonic() < deadline and any(_alive(r["pid"]) for r in signalled):
        time.sleep(0.1)
    for record in signalled:
        # Re-checked against the record before SIGKILL: the PID may have exited
        # and been reused inside the grace window.
        if HA._proc_identity(record["pid"]) == (record["start_ticks"], record["cmdline"]):
            # It may exit between the check and the signal, which is the goal.
            with contextlib.suppress(ProcessLookupError):
                os.kill(record["pid"], signal.SIGKILL)
            how = "SIGKILL after 5s"
        else:
            how = "SIGTERM"
        HA.unregister_waiter(Path(record["_path"]))
        print(f"[herdr-waiter-reap] stopped waiter pid {record['pid']} on "
              f"{record.get('pane')} ({how}): workspace "
              f"{record.get('workspace')} was removed")


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if payload.get("tool_name") != "Bash":
            return 0
        command = (payload.get("tool_input") or {}).get("command") or ""
        if "herdr" not in command:
            return 0
        cwd = Path(payload.get("cwd") or os.getcwd())
        workspaces = _removed_workspaces(command, cwd)
        if not workspaces:
            return 0
        from scripts.utils import herdr_agent as HA
        try:
            listed = _listed_workspaces(HA)
        except HA.HerdrUnavailable as exc:
            print(f"[herdr-waiter-reap] herdr could not be asked, no waiter "
                  f"stopped for {', '.join(workspaces)}: {exc}", file=sys.stderr)
            return 0
        removed = set()
        for workspace in workspaces:
            if workspace in listed:
                print(f"[herdr-waiter-reap] workspace {workspace} is still listed "
                      f"by herdr, so its removal did not happen; no waiter "
                      f"stopped", file=sys.stderr)
                continue
            removed.add(workspace)
        if removed:
            reap(removed, HA)
    except Exception as exc:  # a PostToolUse hook must never fail the tool call
        print(f"[herdr-waiter-reap] error, nothing further done: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
