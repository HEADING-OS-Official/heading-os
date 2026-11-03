#!/usr/bin/env python3
"""Mail, calendar and time zone for `scripts/heading-install.py` (HOS-309).

Slice 9 of the approved deployment design (registry name `plans`,
2026-11-01-heading-os-ideal-deployment-design, sections 2 items 5, 6 and 8,
5 item 9, and 7), the Exchange half. Google mail and calendar are connected by
scripts/utils/install_google.py (HOS-324), through the executive's own Google
sign-in app; with `--mail google` the two Exchange steps below only say so.

The installer's `# ---- HOS-309` section registers four steps, in this order:
  timezone       HEADING_OS_TZ in the engine's .env, the zone the installer's
                 intake recorded in `ctx.tz` (HOS-312)
  mail-choice    HEADING_OS_MAIL=exchange|google|none in .env, from `--mail`
  mail-account   with exchange, EXCHANGE_EMAIL, EXCHANGE_SERVER and
                 EXCHANGE_PASSWORD set in .env. The step never reads a value:
                 with a screen it opens the heading-secrets.py window for the
                 missing names and stops with waiting-for-person (HOS-312); a
                 window closed without saving opens again on the next run.
                 Without a screen, or when the window could not open, the stop
                 names the heading-secrets.py command per name, typed by the
                 executive into a hidden terminal prompt.
  mail-services  with exchange, the `sentinel` and `sync-exchange-daemon`
                 service families installed and running, then one first sync

Each check reads the real state, so a rerun repairs what is broken and skips
what is in place. Proof runs set `ctx.host` (scripts/utils/install_herdr_cliproxy.py
`Host`) to a scratch home and a stub systemctl, and point the engine at a
scratch copy; every child goes through the injected runner. No secret is read
into a message, printed, logged or passed on a command line: only names.

Standard library only, like heading-install.py, which imports this module
before the engine's environment exists. Linux is the tested path; macOS
(launchd through the service layer) is written and UNTESTED.
"""
from __future__ import annotations

import os
import platform
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.doctor import EXCHANGE_SETTINGS  # noqa: E402 - the three names doctor checks, never retyped
from scripts.utils import install_answers  # noqa: E402 - the one reader of the install's answers
from scripts.utils import install_person as person  # noqa: E402 - the person-needed stop (HOS-312)
from scripts.utils.env_file import upsert_env_value  # noqa: E402 - the one .env writer
from scripts.utils.install_herdr_cliproxy import (  # noqa: E402
    InstallError, _env, _injected, _run, host,
)
from scripts.utils.install_answers import MAIL_CHOICES, MAIL_ENV, TZ_ENV, valid_zone  # noqa: E402 - moved there (HOS-312)
from scripts.utils.install_runner import runner_problem  # noqa: E402
from scripts.utils.paths import read_env_value  # noqa: E402

FAMILIES = ("sentinel", "sync-exchange-daemon")
LAUNCHD_PREFIX = "com.headingos."  # scripts/utils/schedule.py LAUNCHD_PREFIX
SERVICE_WAIT_S = 30.0


def _env_file(ctx) -> Path:
    return ctx.engine / ".env"


def _value(ctx, name: str) -> str:
    return read_env_value(_env_file(ctx), name, default="") or ""


# ============================================================
# timezone
# ============================================================

def system_zone() -> str | None:
    """The machine's own zone name, or None. Reads only; creates no link.

    Linux: `timedatectl show -p Timezone --value`, then /etc/timezone. macOS:
    the name after `zoneinfo/` in the target of /etc/localtime. The installer's
    intake offers it as the answer to its time zone question (HOS-312).
    """
    candidates: list[str] = []
    if platform.system() == "Darwin":  # UNTESTED: no Mac was available
        try:
            target = os.readlink("/etc/localtime")
        except OSError as exc:
            target = f"(unreadable: {exc})"  # no `zoneinfo/` in it, so no candidate
        if "zoneinfo/" in target:
            candidates.append(target.split("zoneinfo/", 1)[1])
    else:
        # A source that cannot be read adds no candidate; when none is left the
        # intake offers UTC and the executive confirms or names another zone.
        try:
            out = subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"],
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            out = subprocess.CompletedProcess([], 127, stdout="", stderr=str(exc))
        if out.returncode == 0:
            candidates.append(out.stdout.strip())
        try:
            candidates.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
        except (OSError, UnicodeDecodeError) as exc:
            candidates.append(f"(unreadable: {exc})")  # never a valid zone, so it is skipped below
    return next((c for c in candidates if valid_zone(c)), None)


def timezone_ready(ctx) -> bool:
    return install_answers.timezone(ctx.engine, environ=False) == ctx.tz


def install_timezone(ctx) -> None:
    """Writes the zone the intake recorded; heading-install.py runs no step before it has one."""
    upsert_env_value(_env_file(ctx), TZ_ENV, ctx.tz)
    print(f"  Time zone: {ctx.tz}")


# ============================================================
# mail-choice
# ============================================================

def mail_choice_ready(ctx) -> bool:
    return install_answers.mail(ctx.engine, environ=False) == ctx.mail


def install_mail_choice(ctx) -> None:
    if ctx.mail not in MAIL_CHOICES:
        raise InstallError(f"--mail must be one of {', '.join(MAIL_CHOICES)}, not {ctx.mail!r}")
    upsert_env_value(_env_file(ctx), MAIL_ENV, ctx.mail)


# ============================================================
# mail-account
# ============================================================

def missing_account_settings(ctx) -> list[str]:
    return [name for name in EXCHANGE_SETTINGS if not _value(ctx, name)]


def mail_account_ready(ctx) -> bool:
    if ctx.mail == "none":
        print("  Mail not connected; connect it later with heading-install.py --mail exchange (or --mail google)")
        return True
    if ctx.mail == "google":
        print("  Exchange is not used; Google is connected by the steps above")
        return True
    return not missing_account_settings(ctx)


EXCHANGE_WINDOW = "secrets-exchange"
WINDOW_CANCELLED = "Cancelled. Nothing was saved."   # scripts/heading-secrets.py CANCELLED
WINDOW_NO_DISPLAY = "No window can be opened here."  # scripts/heading-secrets.py NO_WINDOW


def _terminal_say(ctx, missing: list[str]) -> str:
    engine = shlex.quote(str(ctx.engine))
    commands = " && ".join(f"cd {engine} && .venv/bin/python scripts/heading-secrets.py set {name}"
                           for name in missing)
    return ("This computer has no screen for a window. In a terminal, run this one line; it asks for "
            f"each value without showing it: {commands} Then tell me you are done.")


def install_mail_account(ctx) -> None:
    """Never reads a value: opens the secret window for what is missing, or names the terminal line."""
    missing = missing_account_settings(ctx)
    if not missing:
        person.clear(ctx, EXCHANGE_WINDOW)
        return
    if not person.has_display():
        raise person.WaitingForPerson(_terminal_say(ctx, missing))
    if person.running(ctx, EXCHANGE_WINDOW):
        raise person.WaitingForPerson("The window that asks for your Exchange details is still open on "
                                      "your screen. Fill it in and press Save.")
    prefix = ""
    if person.ended(ctx, EXCHANGE_WINDOW):
        tail = person.log_tail(ctx, EXCHANGE_WINDOW)
        # Each ended window is reported once, then cleared, so the next run starts afresh.
        if WINDOW_NO_DISPLAY in tail:
            person.clear(ctx, EXCHANGE_WINDOW)
            raise person.WaitingForPerson("The window could not be opened. " + _terminal_say(ctx, missing))
        if WINDOW_CANCELLED not in tail:
            last = (tail.strip().splitlines() or ["it printed nothing"])[-1]
            person.clear(ctx, EXCHANGE_WINDOW)
            raise InstallError(f"the window that asks for your Exchange details ended without saving: {last}")
        prefix = "The window was closed without saving, so it has opened again. "
    env = _env(ctx, host(ctx))
    env["WORKSPACE_ROOT"] = str(ctx.engine)  # the window writes this install's .env, never an inherited one
    person.start_detached(ctx, EXCHANGE_WINDOW,
                          [str(ctx.python), str(ctx.engine / "scripts" / "heading-secrets.py"), "window", *missing],
                          env=env, cwd=ctx.engine)
    raise person.WaitingForPerson(
        prefix + "A window has opened on your screen asking for your Exchange details: " + ", ".join(missing)
        + ". Type them there; the password is never shown, and it never passes through this chat. "
        "Press Save, then tell me you are done.")


# ============================================================
# mail-services
# ============================================================

def _services(ctx, verb: str, capture: bool = True) -> subprocess.CompletedProcess:
    """`scripts/heading-services.py <verb> sentinel sync-exchange-daemon`, on the injected runner."""
    h = host(ctx)
    problem = runner_problem(_injected(h))
    if problem:
        raise InstallError(problem)
    return _run([str(ctx.python), str(ctx.engine / "scripts" / "heading-services.py"), verb, *FAMILIES],
                env=_env(ctx, h), cwd=ctx.engine, capture=capture)


def _good_lines() -> list[str]:
    """The status line each unit prints when installed and running (schedule.service_status).

    The exit code of `status` is not enough: both families are optional, and
    `service_status` reports an optional family healthy in any state.
    """
    if platform.system() == "Darwin":  # UNTESTED: no Mac was available
        return [f"{fam}: {LAUNCHD_PREFIX}{fam} plist=yes loaded=yes" for fam in FAMILIES]
    return [f"{fam}: {fam}.service file=yes enabled=enabled active=active" for fam in FAMILIES]


GOOGLE_NO_SYNC = "  Google mail and calendar have no background sync yet; it ships in a later HOS-324 slice"


def mail_services_ready(ctx) -> bool:
    if ctx.mail == "none":
        return True
    if ctx.mail == "google":
        print(GOOGLE_NO_SYNC)
        return True
    if ctx.dry_run or not ctx.python.exists():
        return False
    out = _services(ctx, "status", capture=True)
    text = (out.stdout or "") + (out.stderr or "")
    return all(line in text for line in _good_lines())


def _log_hint(ctx) -> str:
    if platform.system() == "Darwin":  # UNTESTED: schedule._launchd_logs_dir
        logs = host(ctx).home / "Library" / "Logs" / "HeadingOS"
        return f"; its log is in {logs}" if logs.is_dir() else ""
    return "; its log: journalctl --user -u sync-exchange-daemon.service -n 50"


def install_mail_services(ctx) -> None:
    if ctx.mail == "none":
        return
    if ctx.mail == "google":
        print(GOOGLE_NO_SYNC)
        return
    out = _services(ctx, "install", capture=False)
    if out.returncode != 0:
        raise InstallError("the service layer could not install sentinel and sync-exchange-daemon; "
                           "see the lines above (scripts/heading-services.py install sentinel "
                           "sync-exchange-daemon)")
    deadline = time.monotonic() + SERVICE_WAIT_S
    while not mail_services_ready(ctx):
        if time.monotonic() >= deadline:
            raise InstallError(f"sentinel and sync-exchange-daemon are installed but not both running "
                               f"after {SERVICE_WAIT_S:.0f} seconds: scripts/heading-services.py status "
                               "sentinel sync-exchange-daemon")
        time.sleep(1.0)
    # The first sync. Contract of scripts/sync-exchange.py, read 2026-11-01:
    # with no argument it syncs the calendar and the emails, read only. There is
    # no "once" flag; --create-meeting, --send-invites, --delete and --yes send
    # or delete and are never passed here.
    h = host(ctx)
    problem = runner_problem(_injected(h))
    if problem:
        raise InstallError(problem)
    first = _run([str(ctx.python), str(ctx.engine / "scripts" / "sync-exchange.py")],
                 env=_env(ctx, h), cwd=ctx.engine, capture=False)
    if first.returncode != 0:
        print(f"  The first mail sync exited {first.returncode}; the background sync retries on its "
              f"own schedule{_log_hint(ctx)}")
