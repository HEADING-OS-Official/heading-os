#!/usr/bin/env python3
"""Scheduled task/agent install for Sentinel.

Centralizes the launchd (macOS) and systemd-user (Linux) logic for `scripts/setup.py`, its one caller.

The workspace-sync schedule was retired in 2026-06: the destructive
orphan-delete engine is gone, code-down is a plain `git pull`, data-up is
`push-all.py`, and first-run record recovery is `import-legacy-records.py`.

Until 2026-09-25 this module also carried an uninstall API
(`uninstall_sync_schedule`, `uninstall_sentinel_schedule` and a private
teardown per platform) for the executive offboarding and revocation tools.
Those tools were removed with the executive model that day (HOS-130), which
left the uninstallers with no caller, so they went too.

Public API:
    install_sentinel_schedule(slug, workspace_dir, target_platform=None)

Returns True on verified success, False otherwise. A Windows host is not a
target: it gets the unsupported-platform notice; run the workspace under WSL2.
"""

import getpass
import os
import platform as platform_mod
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils.colors import GREEN, RED, RESET, YELLOW


def _ok(msg: str) -> None:
    print(f"  {GREEN}[ok]{RESET} {msg}")


def _warn(msg: str) -> None:
    print(f"  {YELLOW}[warn]{RESET} {msg}")


def _fail(msg: str) -> None:
    print(f"  {RED}[fail]{RESET} {msg}")


def _run(cmd, check: bool = False) -> subprocess.CompletedProcess:
    """Run a subprocess command. Always captures output, never raises.

    A command that is not on PATH raises FileNotFoundError out of
    `subprocess.run` before any return value exists (measured 2026-08-26).
    Callers all read `.returncode`, so the missing binary is reported the way an
    ordinary failure is: exit 127, the shell's own code for "command not found".
    """
    try:
        return subprocess.run(cmd, check=check, capture_output=True, text=True)
    except (OSError, IndexError) as exc:
        # IndexError is what an empty command list produces inside subprocess,
        # and the docstring's promise has to cover that too.
        return subprocess.CompletedProcess(
            cmd, 127, stdout="",
            stderr=f"{cmd[0] if cmd else '(empty command)'}: {exc}")


def _resolve_python() -> str | None:
    """Return absolute path to a working Python 3.11+ interpreter.

    Prefers sys.executable (the interpreter running us) since that is already
    verified. Falls back to shutil.which on candidate names, rejecting the
    Microsoft Store stub on Windows.
    """
    if sys.executable and Path(sys.executable).exists():
        return sys.executable
    for candidate in ("python3", "python", "py"):
        path = shutil.which(candidate)
        if not path:
            continue
        if "WindowsApps" in path:
            continue
        return path
    return None


def _resolve_python3_mac() -> str:
    """Resolve an absolute path to python3 for a macOS launchd plist.

    launchd agents do NOT inherit the user's shell PATH. Homebrew on Apple
    Silicon installs to /opt/homebrew/bin/python3; Intel Macs use
    /usr/local/bin/python3; system Python 3 lives at /usr/bin/python3 only
    when Xcode Command Line Tools are installed.
    """
    path = shutil.which("python3")
    if path and Path(path).exists():
        return path
    for fallback in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"):
        if Path(fallback).exists():
            return fallback
    # Last-resort: return the bare name and let launchd fail with a useful error.
    return "/usr/local/bin/python3"


def _ensure_logs_dir(workspace_dir: Path) -> Path:
    """The directory the installed unit's output is appended to, created.

    It is the data repository's `runtime:sentinel/logs`. It was
    `<workspace>/.sync/logs/` for every workspace until 2026-09-25, when HOS-5
    section 7 took every artifact out of the engine checkout (operator decision
    that day: "old way should go out and do not stay in the workspace"). A
    second branch kept `.sync/logs/` for a workspace other than this checkout,
    an executive's; it went with the executive model the same day (HOS-130).
    Resolved at install time: the path is baked into the unit.
    """
    from scripts.utils.data_path import data_write_dir

    return data_write_dir("runtime:sentinel/logs")


# =============================================================================
# macOS - launchd
# =============================================================================


def _install_launchd_agent(
    *,
    label: str,
    workspace_dir: Path,
    script_rel_path: str,
    script_args: list,
    start_interval: int,
) -> bool:
    """Install a macOS launchd user agent with verification."""
    logs_dir = _ensure_logs_dir(workspace_dir)

    python3_path = _resolve_python3_mac()
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    script_abs = workspace_dir / script_rel_path
    log_stem = label.replace(".", "-")

    args_xml = ""
    if script_args:
        args_xml = "\n        " + "\n        ".join(
            f"<string>{a}</string>" for a in script_args
        )

    plist_content = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{label}</string>
            <key>ProgramArguments</key>
            <array>
                <string>{python3_path}</string>
                <string>{script_abs}</string>{args_xml}
            </array>
            <key>WorkingDirectory</key>
            <string>{workspace_dir}</string>
            <key>StartInterval</key>
            <integer>{start_interval}</integer>
            <key>StandardOutPath</key>
            <string>{logs_dir}/{log_stem}-stdout.log</string>
            <key>StandardErrorPath</key>
            <string>{logs_dir}/{log_stem}-stderr.log</string>
            <key>ProcessType</key>
            <string>Background</string>
            <key>LowPriorityIO</key>
            <true/>
            <key>Nice</key>
            <integer>5</integer>
        </dict>
        </plist>
    """)

    plist_path.write_text(plist_content, encoding="utf-8")

    # Idempotency: unload any existing instance before reloading.
    _run(["launchctl", "unload", str(plist_path)])

    uid = os.getuid() if hasattr(os, "getuid") else None
    loaded = False
    if uid is not None:
        result = _run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)])
        loaded = result.returncode == 0
    if not loaded:
        result = _run(["launchctl", "load", str(plist_path)])
        loaded = result.returncode == 0

    if not loaded:
        _fail(f"launchctl could not load {label}. Plist at: {plist_path}")
        return False

    verify = _run(["launchctl", "list", label])
    if verify.returncode == 0:
        _ok(f"macOS launchd: {label} (every {start_interval}s) - loaded and verified")
        return True
    _warn(f"launchctl loaded but 'list' cannot find {label}. Agent may still run.")
    return False


# =============================================================================
# Linux - systemd user timer
# =============================================================================


def _install_systemd_user_timer(
    *,
    unit_name: str,
    workspace_dir: Path,
    script_rel_path: str,
    script_args: list,
    cadence_min: int,
    description: str,
) -> bool:
    """Install a systemd user .service + .timer pair under ~/.config/systemd/user/.

    Idempotent: re-enables existing unit. Returns True on verified success.
    Writes logs to `_ensure_logs_dir(workspace_dir)/<unit_name>.log`.
    """
    py = _resolve_python()
    if not py:
        _fail(f"Cannot install timer {unit_name}: no Python 3 interpreter found.")
        return False

    if not shutil.which("systemctl"):
        _fail("systemctl not found. systemd user units require systemd >= 226.")
        return False

    user_units_dir = Path.home() / ".config" / "systemd" / "user"
    user_units_dir.mkdir(parents=True, exist_ok=True)

    log_path = _ensure_logs_dir(workspace_dir) / f"{unit_name}.log"

    args_str = " ".join(script_args) if script_args else ""
    script_abs = workspace_dir / script_rel_path
    exec_start = f'{py} {script_abs}'
    if args_str:
        exec_start = f'{exec_start} {args_str}'

    service_unit = user_units_dir / f"{unit_name}.service"
    service_content = textwrap.dedent(f"""\
        [Unit]
        Description={description}
        After=network.target

        [Service]
        Type=oneshot
        WorkingDirectory={workspace_dir}
        ExecStart={exec_start}
        StandardOutput=append:{log_path}
        StandardError=append:{log_path}
        Environment=PYTHONUNBUFFERED=1
    """)
    service_unit.write_text(service_content, encoding="utf-8")

    timer_unit = user_units_dir / f"{unit_name}.timer"
    timer_content = textwrap.dedent(f"""\
        [Unit]
        Description=Timer for {description}

        [Timer]
        OnBootSec=2min
        OnUnitActiveSec={cadence_min}min
        Persistent=true
        Unit={unit_name}.service

        [Install]
        WantedBy=timers.target
    """)
    timer_unit.write_text(timer_content, encoding="utf-8")

    _run(["systemctl", "--user", "daemon-reload"])
    result = _run(["systemctl", "--user", "enable", "--now", f"{unit_name}.timer"])
    if result.returncode != 0:
        _fail(f"systemctl enable failed: {result.stderr.strip() or result.stdout.strip()}")
        return False

    verify = _run(["systemctl", "--user", "is-active", f"{unit_name}.timer"])
    if verify.returncode != 0:
        # The success line below used to print anyway, one line under this
        # warning, so an install that verified INACTIVE still ended in a green
        # "enabled" and returned True. `enable --now` is meant to leave the
        # timer running; if it is not running, nothing fires until the next
        # boot, and the installer's own check is what said so. Reproduced
        # 2026-08-26 with is-active stubbed to report "inactive".
        _fail(f"timer enabled but is-active reports: "
              f"{verify.stdout.strip() or verify.stderr.strip() or 'no output'}; "
              f"it will not fire until this is resolved")
        return False

    if not _ensure_linger():
        return False

    _ok(f"systemd user timer: {unit_name}.timer (every {cadence_min}m) — enabled and active")
    return True


def _linger_enabled(user: str) -> bool:
    result = _run(["loginctl", "show-user", user, "-p", "Linger", "--value"])
    return result.returncode == 0 and result.stdout.strip() == "yes"


def _ensure_linger() -> bool:
    """Enable linger for the current user, so the timer fires after an unattended reboot.

    Without it a user timer stays silent until somebody logs in; the installer
    used to print a hint and return True anyway. Checked after the attempt, and
    a failure is reported with the command to run.
    """
    user = getpass.getuser()
    if _linger_enabled(user):
        return True
    _run(["loginctl", "enable-linger", user])
    if _linger_enabled(user):
        _ok(f"loginctl: linger enabled for {user}")
        return True
    _fail(f"linger is not enabled for {user}: the timer will not run after a "
          f"reboot until someone logs in. Run: loginctl enable-linger {user}")
    return False


# =============================================================================
# Public API
# =============================================================================


def install_sentinel_schedule(
    slug: str,
    workspace_dir: Path,
    target_platform: str = None,
) -> bool:
    """Install the 15-minute Sentinel check scheduled task for this workspace."""
    target_platform = (target_platform or platform_mod.system()).lower()
    sentinel_script = workspace_dir / "scripts" / "sentinel.py"
    if not sentinel_script.exists():
        _warn(f"sentinel.py not found at {sentinel_script}; skipping Sentinel scheduling.")
        return False
    if target_platform == "darwin":
        return _install_launchd_agent(
            label=f"io.headingos.sentinel.{slug}",
            workspace_dir=workspace_dir,
            script_rel_path="scripts/sentinel.py",
            script_args=["--check"],
            start_interval=900,
        )
    if target_platform == "linux":
        return _install_systemd_user_timer(
            unit_name=f"heading-sentinel-{slug}",
            workspace_dir=workspace_dir,
            script_rel_path="scripts/sentinel.py",
            script_args=["--check"],
            cadence_min=15,
            description=f"Sentinel comms monitor ({slug})",
        )
    _warn(f"Unsupported platform: {target_platform}. Add manually to crontab:")
    print(f"         */15 * * * * cd {workspace_dir} && python3 scripts/sentinel.py --check")
    return False
