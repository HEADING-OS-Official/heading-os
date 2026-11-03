#!/usr/bin/env python3
"""The one helper every install step that needs a person uses (HOS-312).

A step that needs the executive (a secret typed into a window, a sign-in in a
browser) never blocks: it starts what the person needs as a detached process
and raises `WaitingForPerson`, whose message is the sentence Claude relays.
`scripts/heading-install.py` turns it into the `waiting-for-person` status
line; the next run finds the window or the sign-in still open, or ended.

Each detached process leaves two files in <target>/.heading-install (mode 700):
  person-<name>.log  its stdout and stderr, mode 600. It may hold a sign-in
                     address and a one-time code; it never holds a secret,
                     because no secret passes through a detached process's
                     output (the secret window prints only names).
  person-<name>.pid  the pid on line 1 and its start time on line 2 (field 22
                     of /proc/<pid>/stat), mode 600, so a reused pid is never
                     taken for the process. A finished child not yet reaped
                     (state Z) counts as not running.

A step calls `clear` when it finds the person's work done and when it reports
a sign-in that ended without success. A sign-in that succeeded and whose step
is later skipped leaves its pid file behind; `ended` counts a pid file 60
minutes or older as absent and removes it, so it ages out on its own.

Standard library only: heading-install.py imports this at start-up, before the
engine's environment exists.
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
import time
from pathlib import Path

PID_MAX_AGE_S = 60 * 60
_URL = re.compile(r"https://[^\s\"'<>]+")


class WaitingForPerson(Exception):
    """A window or a browser page is open for the person; the message is the `say` text.

    Deliberately not a StepFailed, OSError or RuntimeError, so no existing
    handler of a step failure catches it.
    """


def has_display() -> bool:
    """A Linux desktop (WSL with WSLg included); every other system is False."""
    return platform.system() == "Linux" and bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def state_dir(ctx) -> Path:
    """<target>/.heading-install, the folder of the state file; created mode 700 when missing."""
    d = ctx.target / ".heading-install"
    if not d.is_dir():
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    return d


def _pid_file(ctx, name: str) -> Path:
    return state_dir(ctx) / f"person-{name}.pid"


def _log_file(ctx, name: str) -> Path:
    return state_dir(ctx) / f"person-{name}.log"


def _open_private(path: Path) -> int:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # an existing file keeps its old mode under O_CREAT
    return fd


def start_detached(ctx, name: str, argv: list[str], *, env: dict, cwd: Path) -> None:
    """Start `argv` in its own session, its output in the log, its pid recorded."""
    log = _log_file(ctx, name)
    log_fd = _open_private(log)
    try:
        proc = subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL, stdout=log_fd,
                                stderr=subprocess.STDOUT, env=env, cwd=str(cwd))
    except BaseException:
        log.unlink(missing_ok=True)  # nothing started, so no log is left behind
        raise
    finally:
        os.close(log_fd)
    pid_file = _pid_file(ctx, name)
    tmp = pid_file.with_name(pid_file.name + ".tmp")
    try:
        fd = _open_private(tmp)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{proc.pid}\n{_start_time(proc.pid) or ''}\n")
        os.replace(tmp, pid_file)
    except BaseException:
        # A child no pid file records could never be found again: stop it, leave nothing behind.
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Said, not swallowed: the cleanup and the original error still follow.
            print(f"  note: process {proc.pid} did not end within 5 s of being killed")
        tmp.unlink(missing_ok=True)
        log.unlink(missing_ok=True)
        raise


def _stat_fields(pid: int) -> list[str]:
    """The stat line of the pid from field 3 on, or [] when it cannot be read.

    Parsed after the LAST `)`, so a command name holding spaces or brackets
    cannot shift the fields.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return stat[stat.rfind(")") + 1:].split()


def _start_time(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat (start time, clock ticks since boot), or None."""
    fields = _stat_fields(pid)
    return fields[19] if len(fields) > 19 else None


def _recorded(ctx, name: str) -> tuple[int, str] | None:
    try:
        lines = _pid_file(ctx, name).read_text(encoding="utf-8").splitlines()
        return int(lines[0]), lines[1]
    except (OSError, ValueError, IndexError):
        return None


def running(ctx, name: str) -> bool:
    """The recorded pid exists and started at the recorded time; removes nothing.

    The start time tells a reused pid from the process that was started.
    """
    rec = _recorded(ctx, name)
    if rec is None or not rec[1]:
        return False
    pid, started = rec
    fields = _stat_fields(pid)
    return len(fields) > 19 and fields[0] != "Z" and fields[19] == started


def ended(ctx, name: str) -> bool:
    """A pid file under 60 minutes old whose process is not running.

    A pid file 60 minutes or older counts as absent and is removed.
    """
    pid_file = _pid_file(ctx, name)
    try:
        age = time.time() - pid_file.stat().st_mtime
    except OSError:
        return False
    if age >= PID_MAX_AGE_S:
        pid_file.unlink(missing_ok=True)
        return False
    return not running(ctx, name)


def clear(ctx, name: str) -> None:
    """Remove the pid file and the log of `name`."""
    _pid_file(ctx, name).unlink(missing_ok=True)
    _log_file(ctx, name).unlink(missing_ok=True)


def log_tail(ctx, name: str, lines: int = 40) -> str:
    """The last lines of the log, for finding an address or a code; "" when there is none."""
    try:
        text = _log_file(ctx, name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def first_url(text: str) -> str | None:
    m = _URL.search(text or "")
    return m.group(0) if m else None
