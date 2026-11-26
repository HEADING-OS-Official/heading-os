#!/usr/bin/env python3
"""Send one Telegram alert when a HEADING OS user unit fails (HOS-396).

Started by systemd through `OnFailure=unit-failure-alert@%n.service`, which
every service template but hindsight-pulse (it runs on another host) carries,
with the failed unit's name as the argument.
It reads the unit's result and its last journal lines, adds the update state
when the unit is `update-manager.service`, and sends one critical alert through
`scripts/utils/alert.py`, deduplicated per unit in `alerts.json` under the
runtime store `runtime:unit-failure-alert/state`:

- a unit with no entry is alerted;
- a oneshot unit is alerted again once its journal shows a successful run
  (`Finished <unit>`) since the last alert, which is a new failure;
- a daemon (any other `Type`) is alerted at most once per 60 minutes, so a
  crash loop sends one message an hour;
- any unit still failing 24 hours after its last alert is alerted again, the
  detail starting `still failing since <first_alert>`.

The state is written only after Telegram accepted the message. A failed send
leaves it unchanged, prints `NOT SENT: <unit>` to stderr and exits 1, so the
next failure tries again and this unit itself shows in `systemctl --user
--failed`. Its own template carries no `OnFailure=`, so a failing alert run
never starts another one.

Usage:
    .venv/bin/python scripts/unit-failure-alert.py example-job.service
    .venv/bin/python scripts/unit-failure-alert.py --dry-run example-job.service

`--dry-run` prints the exact message, applies the same dedup decision and
writes the state as if the send succeeded; it never calls `alert()`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Workspace import bootstrap (per development-standards.md)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.checkpoint_paths import file_lock  # noqa: E402
from scripts.utils.data_path import data_path, data_write_dir  # noqa: E402
from scripts.utils.secret_patterns import redact  # noqa: E402

SOURCE = "unit-failure-alert"
STATE_FILE = "alerts.json"
TIMEOUT_S = 10
JOURNAL_LINES = 5
MAX_ATTEMPTS = 3  # MAX_AUTO_RETRIES of scripts/utils/update_apply.py
DAEMON_REPEAT = timedelta(minutes=60)
STILL_FAILING = timedelta(hours=24)
UPDATE_UNIT = "update-manager.service"
# A systemd unit name; refuses anything an argument list could misread as an option.
UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@:._\\-]*$")


# ============================================================
# Reading the unit
# ============================================================

def _run(cmd: list[str]) -> tuple[str | None, str]:
    """(stdout, "") on exit 0, else (None, reason). Never raises."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, f"no answer in {TIMEOUT_S} s"
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return None, f"exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]}"
    return proc.stdout, ""


def read_unit(unit: str) -> dict:
    """`Result`, `ExecMainStatus` and `Type` of `unit`, or `{"error": <reason>}`."""
    out, err = _run(["systemctl", "--user", "show", unit,
                     "-p", "Result", "-p", "ExecMainStatus", "-p", "Type"])
    if out is None:
        return {"error": err}
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def journal_tail(unit: str, n: int = JOURNAL_LINES) -> list[str] | str:
    """The last `n` journal lines of `unit`, redacted, or the reason they could not be read."""
    out, err = _run(["journalctl", "--user", "-u", unit, "-n", str(n), "--no-pager", "-o", "cat"])
    if out is None:
        return err
    return [redact(line) for line in out.splitlines() if line.strip()]


def finished_since(unit: str, since: datetime) -> bool:
    """True when the journal holds a `Finished <unit>` line since `since`; a failed read is False."""
    out, _err = _run(["journalctl", "--user", "-u", unit, "--since", f"@{int(since.timestamp())}",
                      "--no-pager", "-o", "cat"])
    if out is None:
        return False
    return any(line.startswith(f"Finished {unit}") for line in out.splitlines())


def update_lines(state: dict) -> list[str]:
    """One line per failed component, or per component when every one is `unknown`."""
    comps = state.get("components") or {}
    lines = []
    for name, e in comps.items():
        if e.get("status") != "failed":
            continue
        cur, latest, fails = e.get("current", ""), e.get("latest", ""), e.get("fail_count", 0)
        if fails >= MAX_ATTEMPTS:
            lines.append(f"{name}: stopped retrying after {MAX_ATTEMPTS} attempts, "
                         f"waits for a new version ({cur} -> {latest})")
        else:
            lines.append(f"{name}: apply failed ({cur} -> {latest}), attempt {fails}/{MAX_ATTEMPTS}")
    if comps and all(e.get("status") == "unknown" for e in comps.values()):
        for name, e in comps.items():
            if not e.get("latest"):
                lines.append(f"{name}: upstream not reached")
            if not e.get("current"):
                lines.append(f"{name}: installed version not read")
    return lines


def read_update_lines() -> list[str] | str:
    """`update_lines` over the `update-state` record, read-only, or the reason it could not be read."""
    try:
        path = data_path("update-state")
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        # DataPathError is a ValueError, as is JSONDecodeError.
        return f"{type(exc).__name__}: {exc}"
    if not isinstance(state, dict):
        return "the state is not a JSON object"
    return update_lines(state)


# ============================================================
# Dedup
# ============================================================

def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: object) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def decide(unit: str, unit_type: str, entry: dict | None, now: datetime) -> tuple[str, bool, bool]:
    """(`"send"` or `"suppress"`, new_failure, still_failing) for one unit's dedup entry."""
    last = _parse((entry or {}).get("last_alert"))
    if entry is None or last is None:
        return "send", True, False
    if unit_type == "oneshot" and finished_since(unit, last):
        return "send", True, False
    if now - last >= STILL_FAILING:
        return "send", False, True
    if unit_type != "oneshot" and now - last >= DAEMON_REPEAT:
        return "send", False, False
    return "suppress", False, False


def load_alerts(path: Path) -> dict:
    """The dedup state, or `{}` when absent or unreadable (unreadable is said on stderr)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"{SOURCE}: ignoring unreadable {path.name}: {exc}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def save_alert(path: Path, unit: str, entry: dict) -> None:
    """Record `entry` for `unit`: re-read the state, set the one key, write it atomically.

    Two alert units for two different units can fail together; each read the
    state before sending, so writing back that copy would drop the other's
    entry. The read-modify-write runs under `file_lock` on a sidecar
    `alerts.json.lock` (the state itself is replaced by `os.replace`), and the
    temporary file carries the process id. The dedup decision for one unit
    needs no lock: systemd never runs the same `unit-failure-alert@<unit>`
    instance twice at once.
    """
    with file_lock(path.with_name(path.name + ".lock"), label=SOURCE):
        data = load_alerts(path)
        data[unit] = entry
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)


# ============================================================
# Message
# ============================================================

def build_message(unit: str, info: dict, journal: list[str] | str,
                  updates: list[str] | str | None, still_since: str | None) -> tuple[str, str]:
    """(summary, detail) for `alert()`."""
    if "error" in info:
        summary = f"{unit} failed (not readable)"
        head = f"(not readable: {info['error']})"
    else:
        summary = f"{unit} failed ({info.get('Result') or 'unknown'})"
        head = f"type {info.get('Type') or 'unknown'}, exit status {info.get('ExecMainStatus') or 'unknown'}"
    lines = [f"still failing since {still_since}"] if still_since else []
    lines.append(head)
    if updates is not None:
        lines.append("updates:")
        lines += [f"(not readable: {updates})"] if isinstance(updates, str) else (updates or ["(no failed component)"])
    lines.append("last journal lines:")
    lines += [f"(not readable: {journal})"] if isinstance(journal, str) else (journal or ["(none)"])
    # The whole detail, not only the journal lines: a systemctl error or an
    # update-state value is redacted too before it leaves for Telegram.
    return summary, redact("\n".join(lines))


# ============================================================
# Main
# ============================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("unit", help="the failed unit, e.g. example-job.service")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the message and record the state; never send")
    args = parser.parse_args(argv)
    unit = args.unit
    if not UNIT_RE.match(unit):
        print(f"{SOURCE}: not a unit name: {unit!r}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    info = read_unit(unit)
    state_path = data_write_dir("runtime:unit-failure-alert/state") / STATE_FILE
    alerts = load_alerts(state_path)
    entry = alerts.get(unit) if isinstance(alerts.get(unit), dict) else None
    verdict, new_failure, still = decide(unit, info.get("Type", ""), entry, now)
    if verdict == "suppress":
        print(f"suppressed: {unit} alerted at {entry['last_alert']}")
        return 0

    updates = read_update_lines() if unit == UPDATE_UNIT else None
    summary, detail = build_message(unit, info, journal_tail(unit), updates,
                                    entry.get("first_alert") if still and entry else None)
    if args.dry_run:
        print(f"HEADING OS alert ({SOURCE}): {summary}\n{detail}")
        sent = True
    else:
        from scripts.utils.alert import alert
        sent = alert("critical", summary, detail, source=SOURCE).get("telegram", False)
    if not sent:
        print(f"NOT SENT: {unit}", file=sys.stderr)
        return 1
    stamp = _iso(now)
    first = stamp if new_failure or not entry else entry.get("first_alert", stamp)
    save_alert(state_path, unit, {"first_alert": first, "last_alert": stamp})
    return 0


if __name__ == "__main__":
    sys.exit(main())
