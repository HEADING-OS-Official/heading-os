"""Severity-tiered alert router (R14).

One entry point - ``alert(severity, summary, ...)`` - that fans a notice out to
the right channels by severity, with graceful degradation so a failed channel
never escalates into a crash. The two channels:

- **Telegram** push to the CEO's alert channel (best-effort; for outages that
  must reach off-machine). Sent via the dedicated notifications bot
  (scripts/utils/telegram_notify.py) - never to Saved Messages/"me"/"self".
- **Log** - always, tagged with the current ``[trace_id]``. The always-on floor.

Routing:

    critical -> Telegram + log   (daemon down / permanent fail)
    warning  ->            log   (transient, recoverable), at WARNING level
    info     ->            log   at INFO level

There is no Action Queue card channel. One existed, wired through an `init()`
only the bridge daemon ever called, so after that daemon left (HOS-7,
2026-09-21) every caller got log-only warnings; it was removed on 2026-10-16.

A Telegram send failure (no session, offline, missing client) degrades to
log-only and NEVER raises. ``alert`` itself never raises.

CEO-only: alerts route to the CEO's Telegram, so this module is CEO-only during
the spine prove-out (private routing rule in config/routing-map.yaml).

Usage::

    from scripts.utils import alert
    fired = alert.alert("critical", "daemon sentinel silent 6m", source="watchdog")
    # fired == {"telegram": True, "log": True}
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from scripts.utils import telegram_notify
from scripts.utils import tracing
from scripts.utils.paths import load_env
from scripts.utils.workspace import get_workspace_root

logger = logging.getLogger("x31c.alert")

SEVERITIES = ("critical", "warning", "info")


def _telegram_target(workspace_root: Path) -> str:
    """Read daemon.alert.telegram_target from merged config; else
    ODIN_CADENCE_TELEGRAM_TARGET (the shared alerts channel every other
    notification script already uses); else "" (unconfigured, no send).

    Never defaults to "me"/Saved Messages - confirmed live defect, fixed
    2026-07-17: this function used to return "me" here with no config
    override ever set, so critical alerts were silently landing in Saved
    Messages instead of reaching the CEO's phone.

    `.env` is loaded first, from the same root, because the variable lives
    there and `telegram_notify.notify()` loads it only inside the send, after
    this read. Without it the first critical alert of a fresh process got an
    empty target and was dropped (2026-10-16, `daemon-watchdog.py --once`).
    """
    try:
        load_env(workspace_root)
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("alert: .env not loaded before the target read: %s", exc)
    try:
        from scripts.record_readers.config import load_config

        cfg = load_config(workspace_root)
        target = cfg.get("daemon", {}).get("alert", {}).get("telegram_target")
        if isinstance(target, str) and target.strip():
            return target.strip()
    except Exception as exc:  # noqa: BLE001 - config read is best-effort; default below
        logger.debug("alert: telegram_target config read failed: %s", exc)
    return os.environ.get("ODIN_CADENCE_TELEGRAM_TARGET", "")


def _send_telegram(workspace_root: Path, message: str) -> bool:
    """Push a message to Telegram via the dedicated notifications bot.

    Returns True on a clean send, False on any failure (missing token,
    unresolvable target, transport/API error). NEVER raises.

    "Never raises" is enforced HERE, not delegated to `telegram_notify`. This
    function held no handler at all, so any exception out of `notify` - and a
    transport error is the failure mode this docstring names first - propagated
    through `alert()` and into the caller, against three separate promises: this
    line, the module docstring's "degrades to log-only and NEVER raises", and
    `alert()`'s own "Never raises". MEASURED 2026-08-30 with `notify` raising
    `OSError("network unreachable")`: `alert("critical", "daemon down")` raised
    it instead of returning the documented degradation. The callers are
    watchdogs, so the alert fires exactly when the network is already broken -
    the one moment the contract has to hold.
    """
    target = _telegram_target(workspace_root)
    try:
        return telegram_notify.notify(target, message)
    except Exception as exc:  # noqa: BLE001 - a channel failure must not stop an alert
        logger.warning("alert: telegram send failed: %s", exc)
        return False


def alert(severity: str, summary: str, detail: str = "", *, source: str = "") -> dict:
    """Route a notice to channels by severity. Never raises.

    Args:
        severity: one of "critical", "warning", "info". An unknown value is
            treated as "warning" (logged at WARNING level).
        summary: one-line headline (the Telegram subject line).
        detail: optional longer context (the Telegram body).
        source: optional origin label (e.g. "watchdog", "executor").

    Returns:
        dict naming which channels fired, e.g. ``{"telegram": True, "log": True}``.
        ``telegram`` is False when it was not attempted or failed; ``log`` is
        always True.
    """
    sev = severity if severity in SEVERITIES else "warning"
    tid = tracing.get() or "-"
    fired = {"telegram": False, "log": False}

    # Log is the always-on floor. The factory in trace_filter stamps trace_id
    # on the record; we also embed it in the text so a plain handler still shows
    # it, matching the convention for direct-append surfaces (trace-id.md).
    level = {"critical": logging.ERROR, "warning": logging.WARNING}.get(sev, logging.INFO)
    msg = f"[{tid}] alert/{sev}"
    if source:
        msg += f" ({source})"
    msg += f": {summary}"
    if detail:
        msg += f" - {detail}"
    logger.log(level, msg)
    fired["log"] = True

    if sev != "critical":
        return fired  # warning and info are log-only

    workspace_root = get_workspace_root()
    tele_msg = f"HEADING OS alert ({source})" if source else "HEADING OS alert"
    tele_msg += f": {summary}"
    if detail:
        tele_msg += f"\n{detail}"
    fired["telegram"] = _send_telegram(workspace_root, tele_msg)

    return fired
