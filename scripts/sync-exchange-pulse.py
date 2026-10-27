#!/usr/bin/env python3
"""Sync-Exchange Pulse — liveness check for the sync-exchange daemon.

Called by /prime's parallel health-check helper.

When the systemd-user unit `sync-exchange-daemon.service` is installed, systemd
owns the daemon and this script only REPORTS the unit's state; it never starts
anything (HOS-168). Starting the unit is HELM's:
`systemctl --user start sync-exchange-daemon.service`.

Without the unit (no systemd, or never installed) the old behaviour stands: a
dead daemon is spawned detached, under the pinned `.venv/bin/python`.

Output policy:
    - Daemon alive: single OK line with pid + last sync timing
    - Unit installed and not active: error line naming the unit state, exit 1
    - No unit, daemon dead + auto-spawn ok: line announcing detached start
    - No unit, daemon dead + auto-spawn failed: error line with manual command

Usage:
    python scripts/sync-exchange-pulse.py
"""
from __future__ import annotations

import io
import subprocess
import sys
from datetime import datetime
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE))

from scripts.utils.data_path import DataPathError  # noqa: E402
from scripts.utils.pid_liveness import pid_runs_program  # noqa: E402
from scripts.utils.sync_exchange_paths import daemon_files, last_job_ok  # noqa: E402
from scripts.utils.clone_guard import require_main_clone
from scripts.utils.venv_guard import venv_python  # noqa: E402

UNIT = "sync-exchange-daemon.service"

# The daemon's pid and log, bound in `main()` from the same place the daemon
# writes them (`scripts/utils/sync_exchange_paths.py`), never in the engine checkout.
PID_FILE: Path
LOG_FILE: Path


def _daemon_alive() -> tuple[bool, int | None]:
    """Return (alive, pid), using the SHARED liveness helper.

    The body here was a verbatim copy of the daemon's, carrying the same defect:
    PermissionError read as dead, so a daemon owned by another user looked
    stopped and this script spawned a duplicate beside it.
    """
    if not PID_FILE.exists():
        return False, None
    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return False, None
    return (pid_runs_program(pid, "sync-exchange-daemon"), pid if pid > 0 else None)


def _resolve_pythonw() -> Path | None:
    """Locate pythonw.exe (Windows) or python (POSIX) for detached spawn.

    Strategy: take sys.executable, swap python.exe -> pythonw.exe on Windows so
    the spawned daemon has no console window. On POSIX the pinned
    `.venv/bin/python`, never sys.executable: /prime runs this under whatever
    `python` resolved, which is not the interpreter the daemon's dependencies
    are pinned in.
    """
    exe = Path(sys.executable)
    if sys.platform == "win32":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.exists():
            return pythonw
        # Fallback to python.exe; CREATE_NO_WINDOW + cmd start /B already suppresses console.
        return exe if exe.exists() else None
    py = venv_python()
    return py if py.exists() else None


def _unit_state() -> dict[str, str] | None:
    """The unit's systemd properties, or None when no unit file is loaded.

    Read-only: `systemctl --user show` changes nothing. None also covers a
    machine with no systemctl, or a user manager that did not answer.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", "-p",
             "LoadState,ActiveState,SubState,MainPID", UNIT],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    props = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    if props.get("LoadState") != "loaded":
        return None
    return props


# How long to give a freshly spawned daemon to fall over before calling it
# started. Only paid on the path where the daemon was down, so a healthy pulse
# never waits.
STARTUP_SETTLE_SECONDS = 2.0


def _spawn_detached_daemon() -> int | None:
    """Spawn the sync-exchange daemon in a fully detached process. Returns sentinel.

    Returns -1 on Windows (success — daemon PID lands in `PID_FILE`, the data
    repository's `runtime:sync-exchange/tmp/daemon.pid`),
    actual PID on POSIX, None on spawn failure OR on a POSIX child that exited
    within `STARTUP_SETTLE_SECONDS`.

    The survival check covers POSIX only. On Windows the Popen is the `cmd`
    shell, not the daemon, so its exit says nothing about the daemon; that lane
    still reports on the spawn alone.
    """
    py = _resolve_pythonw()
    daemon = WORKSPACE / "scripts" / "sync-exchange-daemon.py"
    if py is None or not daemon.exists():
        return None
    try:
        if sys.platform == "win32":
            # Use `cmd /c start /B ""` so the daemon survives parent shell exit.
            # Same subprocess pattern as the other pulse probes — proven reliable under Git Bash.
            cmd = [
                "cmd.exe", "/c", "start", "/B", "",
                str(py), str(daemon), "daemon",
            ]
            subprocess.Popen(
                cmd,
                cwd=str(WORKSPACE),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                close_fds=True,
            )
            return -1
        proc = subprocess.Popen(
            [str(py), str(daemon), "daemon"],
            cwd=str(WORKSPACE),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        # Popen hands back a pid the instant the fork lands, which says nothing
        # about survival. A daemon that dies on startup (apscheduler missing
        # from the interpreter it was spawned with, an import error, or its own
        # "another instance is starting" exit 1 when two health checks race)
        # still yielded a pid, so the pulse printed "started pid N" and returned
        # 0 forever. Wait a moment: if `wait` RETURNS, the child is already
        # dead, and only a timeout means it is still running.
        try:
            proc.wait(timeout=STARTUP_SETTLE_SECONDS)
        except subprocess.TimeoutExpired:
            return proc.pid
        return None
    except Exception:
        return None


def _last_job_ok() -> str | None:
    """The most recent 'job-ok sync-exchange' as a friendly relative time like
    '12m ago', or None if not found. The parsing, rotated backups included, is
    `sync_exchange_paths.last_job_ok`, shared with the ops radar.
    """
    last_ts = last_job_ok(LOG_FILE)
    if last_ts is None:
        return None
    delta = datetime.now().astimezone() - last_ts
    mins = int(delta.total_seconds() / 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    h, m = divmod(mins, 60)
    if h < 24:
        return f"{h}h{m}m ago"
    d = h // 24
    return f"{d}d ago"


def main():
    # Force UTF-8 on stdout so emoji and non-ASCII log lines never crash the
    # print. Done here, not at import, so importing this module has no side effect.
    require_main_clone(__file__)
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    global PID_FILE, LOG_FILE
    try:
        files = daemon_files(create=False)
    except DataPathError as exc:
        print(f"🔄 Sync-Exchange: ❌ no data repository on this tree ({exc}); "
              f"the daemon keeps its pid and log there, so nothing was started.")
        return 2
    PID_FILE, LOG_FILE = files.pid, files.log
    unit = _unit_state()
    if unit is not None:
        if unit.get("ActiveState") != "active":
            print(f"🔄 Sync-Exchange: ❌ {UNIT} is {unit.get('ActiveState', '?')}"
                  f" ({unit.get('SubState', '?')}). Nothing was started; from HELM: "
                  f"systemctl --user start {UNIT}")
            return 1
        alive, pid = True, unit.get("MainPID")
    else:
        alive, pid = _daemon_alive()
    if not alive:
        new_pid = _spawn_detached_daemon()
        if new_pid is None:
            print(
                "🔄 Sync-Exchange: ❌ daemon NOT RUNNING and auto-start failed "
                "(it did not spawn, or it exited straight after starting). "
                "Run manually: python scripts/sync-exchange-daemon.py daemon"
            )
            # Non-zero. This is the liveness check /prime's health helper calls,
            # and exiting 0 here reported a healthy sync pipeline while the
            # daemon was down -- indefinitely, because nothing else looks.
            return 1
        tag = f"pid {new_pid}" if new_pid > 0 else "detached"
        print(f"🔄 Sync-Exchange: daemon was NOT RUNNING — started {tag}")
        return 0

    last_ok = _last_job_ok()
    if last_ok:
        print(f"🔄 Sync-Exchange: ✅ daemon up pid={pid}, last sync {last_ok}")
    else:
        print(f"🔄 Sync-Exchange: ✅ daemon up pid={pid}, no sync logged yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
