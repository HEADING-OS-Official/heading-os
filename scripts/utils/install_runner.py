"""The service manager an installer step talks to, shared by the installer modules.

Standard library only, like scripts/heading-install.py, which imports it before
the engine's environment exists. Two things live here so that each installer
module does them one way (HOS-306 wrote them, HOS-307 review round 3 shared them):

- An injected runner. A proof run hands a step a stub `systemctl`; its
  directory then leads the PATH of every child, so the service layer's
  `systemctl`, `loginctl` and `launchctl` come from the stub and never from the
  machine's own user manager. A stub that does not bring all of them is refused,
  because the missing one would be found on the real PATH.
- A settings file newer than the running process. Installing a unit
  (`daemon-reload`, `enable --now`) does not restart a process already running,
  so a step that repaired the settings must restart it to apply them.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import time
from pathlib import Path


def runner_env(env: dict[str, str], injected: str | None) -> dict[str, str]:
    """`env` with the injected runner's directory first on PATH; unchanged when nothing is injected."""
    if injected:
        env["PATH"] = f"{Path(injected).parent}{os.pathsep}{env.get('PATH', '')}"
    return env


def runner_problem(injected: str | None) -> str | None:
    """Why an injected runner cannot stand in for the service manager, or None when it can."""
    if not injected:
        return None
    needed = ("launchctl",) if platform.system() == "Darwin" else ("systemctl", "loginctl")
    missing = [n for n in needed if not os.access(Path(injected).parent / n, os.X_OK)]
    if Path(injected).name != "systemctl" or missing:
        return (f"the injected service manager {injected} must be named systemctl and have "
                f"{', '.join(needed)} beside it; missing: {', '.join(missing) or 'its name'}")
    return None


def service_pid(systemctl: str, unit: str, launchd_label: str, env: dict[str, str]) -> int | None:
    """The running service's process id, from the service manager; None when nothing runs."""
    if platform.system() == "Darwin":  # UNTESTED on a Mac
        out = subprocess.run(["launchctl", "list", launchd_label], capture_output=True, text=True,
                             env=env).stdout
        m = re.search(r'"PID"\s*=\s*(\d+)', out)
    else:
        out = subprocess.run([systemctl, "--user", "show", unit, "-p", "MainPID", "--value"],
                             capture_output=True, text=True, env=env).stdout
        m = re.fullmatch(r"\s*(\d+)\s*", out)
    pid = int(m.group(1)) if m else 0
    return pid or None


def _elapsed(etime: str) -> int | None:
    """Seconds from `ps -o etime=` ([[dd-]hh:]mm:ss), the one form Linux and macOS both print."""
    m = re.fullmatch(r"\s*(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)\s*", etime)
    if not m:
        return None
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def changed_since_start(path: Path, pid: int | None) -> bool:
    """True when `path` changed after process `pid` started, so it runs on old settings.

    The start is `now - elapsed`, and `ps` rounds elapsed down to the second,
    so the estimate is never earlier than the real start: a process started
    after the last write never reads as stale. A write within the process's
    first second can be missed.
    """
    if pid is None:
        return False  # nothing runs; whether it should is the caller's question
    out = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], capture_output=True, text=True).stdout
    elapsed = _elapsed(out)
    if elapsed is None:
        return True  # cannot tell when it started: treat as stale, a restart is harmless
    return path.stat().st_mtime > time.time() - elapsed
