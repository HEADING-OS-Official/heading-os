"""Daemon liveness watchdog core (R14).

Classifies each daemon's per-daemon liveness beat
(``daemon_heartbeat.heartbeats_dir()/<name>.json``, written by ``daemon_heartbeat.beat``)
against its configured cadence + grace, and routes a severity-tiered alert on a
missed beat with per-(daemon) dedup so a sustained outage does not spam.

This is the importable core (snake_case), wrapped by the
``scripts/daemon-watchdog.py`` CLI, its only caller, which the
``daemon-watchdog.timer`` systemd-user timer runs every two minutes.

Since HOS-397 the same pass also watches the user manager, read-only: every
enabled user service that is not ``oneshot`` must be active/running, and every
enabled user timer must have fired within twice its shortest recurring period
plus 30 minutes. ``daemon.watchdog.ignore_units`` drops named services.
Neither is suppressed by a missing heartbeat config; both share the dedup
state file under the keys ``service:<unit>``, ``timer:<unit>``,
``systemd:read`` and ``_meta``. Its own death is the one thing this timer
cannot see; ``_meta.last_pass_ts`` is the hook for a check from outside.

Console-first: it only reads files (heartbeats + a tiny dedup state file) and
the read-only systemd queries named above.

CEO-only: alerts route to the CEO's Telegram via ``scripts/utils/alert.py``
(CEO-only during the spine prove-out).

Usage::

    from scripts import watchdog_core
    report = watchdog_core.check_once(workspace_root)
    # report == {"verdict": "ok"|"down", "daemons": [...], "services": [...],
    #            "timers": [...], "alerts_fired": N, ...}
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.utils import daemon_heartbeat, install_answers
from scripts.utils.data_path import DataPathError, data_write_dir
from scripts.utils.timeparse import parse_iso

logger = logging.getLogger("x31c.watchdog")

# Defaults when config carries no per-daemon cadence.
DEFAULT_EXPECTED_S = 60
DEFAULT_GRACE_S = 120
DEFAULT_REALERT_MIN = 30

# The full fleet the spine knows about. This is the fallback expected set when a
# host declares no `daemon.watchdog.expect` scope. The fleet is split across
# hosts (see load_expected): the team bot and sentinel were migrated to the service
# host on 2026-05-23, and sync-exchange is the laptop's. A daemon in the *host's
# expected set* with no heartbeat file resolves to "missing" (a genuine down
# state), not silence.
#
# A name kept here after its daemon is gone resolves "missing" on every pass
# (the bridge left 2026-09-28, HOS-168). Keep it in step with the unit templates
# under scripts/templates/systemd/ by hand: no test checks it since the
# 2026-09-21 test-suite split.
EXPECTED_DAEMONS = ("sync-exchange", "sentinel")

# The beats and this module's dedup state live in the data repository's runtime
# store (HOS-5 section 7, 2026-09-25), never in the engine checkout.
WATCHDOG_STATE_NAME = "watchdog-state.json"


# ============================================================
# Time + formatting helpers
# ============================================================

def _now() -> datetime:
    return datetime.now(timezone.utc)


def format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    return f"{int(seconds / 3600)}h"


# ============================================================
# Config cadence
# ============================================================

def _read_config(workspace_root: Path) -> dict:
    """The merged daemon config, or {} when it cannot be read (best-effort)."""
    try:
        from scripts.record_readers.config import load_config

        return load_config(workspace_root) or {}
    except Exception as exc:  # noqa: BLE001 - config read is best-effort; defaults apply
        logger.debug("watchdog: config read failed: %s", exc)
        return {}


def load_expected(workspace_root: Path, cfg: dict | None = None) -> tuple[str, ...]:
    """Return the daemons *this host's* watchdog should check.

    Reads ``daemon.watchdog.expect`` (a list of daemon names) from the merged
    daemon config. The fleet is split across hosts since the 2026-05-23 service-host
    migration: the team bot and sentinel are supervised on the service host. A watchdog only sees heartbeat files on its
    own filesystem, so each host scopes itself to the daemons that actually beat
    there - otherwise the off-host daemons resolve "missing" and fire false
    criticals.

    When ``expect`` is present and non-empty, it is the authoritative scope.
    Absent or empty falls back to ``EXPECTED_DAEMONS`` (the full fleet) for
    back-compat with a single-host deployment. Config read is best-effort;
    ``cfg`` is the config already read for this pass, else it is read here.

    Then the install's mail answer, read through ``install_answers.mail``
    (HOS-312): with ``none`` (connect later) or ``google``, ``sync-exchange``
    and ``sentinel`` are dropped, because the installer turns neither on and
    they would otherwise resolve "missing" forever. With ``exchange``, or with
    the answer unset (HELM's own machine, whose .env predates the intake),
    nothing is dropped. With any mail answer at all (an install the intake
    made), only those two mail daemons are kept, because no install turns any
    other daemon's family on. A ``daemon.watchdog.expect`` list from config is
    narrowed the same way on such an install.
    """
    if cfg is None:
        cfg = _read_config(workspace_root)
    names: tuple[str, ...] = EXPECTED_DAEMONS
    try:
        raw = cfg.get("daemon", {}).get("watchdog", {}).get("expect")
        if isinstance(raw, list):
            scoped = tuple(str(x).strip() for x in raw if isinstance(x, str) and str(x).strip())
            if scoped:
                names = scoped
    except Exception as exc:  # noqa: BLE001 - malformed config is best-effort; default below
        logger.debug("watchdog: expected-daemons config read failed: %s", exc)
    answer = install_answers.mail(workspace_root)
    if answer in ("none", "google"):
        names = tuple(n for n in names if n not in ("sync-exchange", "sentinel"))
    if answer is not None:
        names = tuple(n for n in names if n in ("sync-exchange", "sentinel"))
    return names


def load_cadence(workspace_root: Path, cfg: dict | None = None) -> dict[str, tuple[int, int]]:
    """Return ``{daemon: (expected_s, grace_s)}`` for this host's expected set.

    Scoped to ``load_expected(workspace_root)`` so a host only watches the
    daemons that beat on its own filesystem. Each expected daemon takes its
    ``daemon.watchdog.cadence.<name>`` entry, or the defaults when absent.
    Config read is best-effort: a missing/unreadable config yields all-default
    cadence over the expected set. ``cfg`` is the config already read for this
    pass, else it is read here.
    """
    if cfg is None:
        cfg = _read_config(workspace_root)
    expected = load_expected(workspace_root, cfg)
    # Every level is type-checked, not just `daemon`: one YAML typo turning a
    # mapping into a scalar must fall back to defaults, never raise out of
    # `check_once` and silence the watchdog itself.
    raw: dict = {}
    node = cfg
    for key in ("daemon", "watchdog", "cadence"):
        if node is None:
            break
        if not isinstance(node, dict):
            logger.warning(
                "watchdog: config path daemon.watchdog.cadence is malformed at "
                "%r (%s, expected a mapping); using default cadence for every "
                "daemon", key, type(node).__name__)
            node = None
            break
        node = node.get(key)
    if isinstance(node, dict):
        raw = node
    elif node is not None:
        logger.warning(
            "watchdog: daemon.watchdog.cadence is a %s, not a mapping; using "
            "default cadence for every daemon", type(node).__name__)
    def _seconds(entry: dict, key: str, default: int, name: str) -> int:
        """One cadence number, or the default.

        A malformed value (`~`, a non-numeric string) falls back to the default
        rather than raising out of `check_once` and silencing the watchdog. A
        bool is rejected too: bool subclasses int, so `expected: true` would
        become a one-second cadence that fabricates an outage.
        """
        if isinstance(entry.get(key), bool):
            logger.warning(
                "watchdog: cadence.%s.%s is %r, not a number; using %ds",
                name, key, entry.get(key), default)
            return default
        try:
            return int(entry[key])
        except KeyError:
            return default
        except (TypeError, ValueError):
            logger.warning(
                "watchdog: cadence.%s.%s is %r, not a number; using %ds",
                name, key, entry.get(key), default)
            return default

    out: dict[str, tuple[int, int]] = {}
    for name in expected:
        entry = raw.get(name) if isinstance(raw, dict) else None
        if isinstance(entry, dict):
            expected_s = _seconds(entry, "expected", DEFAULT_EXPECTED_S, name)
            grace = _seconds(entry, "grace", DEFAULT_GRACE_S, name)
        else:
            expected_s, grace = DEFAULT_EXPECTED_S, DEFAULT_GRACE_S
        out[name] = (expected_s, grace)
    return out


def _realert_minutes(workspace_root: Path, cfg: dict | None = None) -> int:
    if cfg is None:
        cfg = _read_config(workspace_root)
    try:
        v = cfg.get("daemon", {}).get("watchdog", {}).get("realert_minutes")
        # `not isinstance(v, bool)` is load-bearing: bool subclasses int, and
        # YAML reads `realert_minutes: yes` / `true` as True, which passed the
        # numeric guard and returned 1. A one-MINUTE re-alert window turns a
        # sustained outage into a critical alert every minute, which is the
        # exact spam the dedup model exists to prevent.
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return int(v)
    except Exception as exc:  # noqa: BLE001 - malformed config is best-effort; default below
        logger.debug("watchdog: realert-minutes config read failed: %s", exc)
    return DEFAULT_REALERT_MIN


# ============================================================
# Heartbeat read + classify
# ============================================================

def _read_beat(workspace_root: Path, name: str) -> dict | None:
    """Read a per-daemon liveness beat, or None when there is none to read.

    `workspace_root` still scopes the config reads beside this; the beat itself
    is in the data repository, and a tree with none has no beats.
    """
    try:
        path = daemon_heartbeat.heartbeats_dir() / f"{name}.json"
    except DataPathError as exc:
        logger.debug("watchdog: no heartbeat store on this tree: %s", exc)
        return None
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        # UnicodeError too: undecodable bytes raise UnicodeDecodeError (a
        # ValueError, neither OSError nor JSONDecodeError), and one corrupt beat
        # file must read as `missing`, not stop the whole fleet's classification.
        return None
    if not isinstance(data, dict):
        # Valid JSON, wrong shape: an array, string or number would raise in
        # `_age_seconds` and stop the whole fleet's classification. It is
        # `missing`, as `classify` documents.
        logger.warning("watchdog: %s holds a %s, not an object; treating the "
                       "beat as missing", path, type(data).__name__)
        return None
    return data


def _age_seconds(record: dict | None, now: datetime) -> float | None:
    # `isinstance`, not truthiness. `classify` promises `missing` for "a file
    # with no parseable timestamp", and a non-empty non-dict has none; the old
    # falsy test let `[]` and `{}` through correctly and passed `[1, 2]` and
    # `"x"` straight into `.get`. `_read_beat` now filters those too, so this is
    # the second line of defence for a caller that builds a record itself.
    if not isinstance(record, dict) or not record:
        return None
    dt = parse_iso(record.get("last_heartbeat"))
    if dt is None:
        return None
    return (now - dt).total_seconds()


def classify(record: dict | None, threshold_s: float, now: datetime) -> str:
    """Return ``ok`` | ``silent`` | ``missing`` for one daemon's beat.

    - ``missing``: no heartbeat file, or a file with no parseable timestamp.
    - ``silent``: a beat older than ``threshold_s``.
    - ``ok``: a beat within ``threshold_s``.
    """
    if record is None:
        return "missing"
    age = _age_seconds(record, now)
    if age is None:
        return "missing"
    return "silent" if age > threshold_s else "ok"


# ============================================================
# Dedup state
# ============================================================

def _load_state(state_path: Path | None) -> dict:
    if state_path is None or not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        # UnicodeError for the reason `_read_beat` carries it (MEASURED
        # 2026-09-01: one 0xff byte in this state file raised out of
        # `check_once`). Losing the dedup state is the correct degradation: the
        # next pass re-alerts rather than staying quiet.
        return {}


def _save_state(state_path: Path | None, state: dict) -> bool:
    """Atomically persist the dedup state. Best-effort: never raises.

    True when the state was written, False when the write failed (HOS-339).
    """
    if state_path is None:
        return False
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(state_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(state, indent=2) + "\n")
            os.replace(tmp, state_path)
            return True
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        logger.warning("watchdog state write failed: %s", e)
        return False


# ============================================================
# Services and timers (HOS-397)
# ============================================================
#
# Read-only toward systemd: `systemctl --user show`, `list-unit-files` and
# `systemd-analyze calendar`, nothing that starts, stops or reloads a unit.
# Every call runs with LC_ALL=C TZ=UTC so timestamps read
# `Mon 2026-11-16 07:43:05 UTC` (MEASURED 2026-11-20; `--timestamp=unix` left
# `LastTriggerUSec` as local text, so it is not used).

SYSTEMD_TIMEOUT_S = 10
BOOT_GRACE_S = 30 * 60
RESUME_GAP_S = 10 * 60
RESUME_GRACE_S = 30 * 60
TIMER_SLACK_S = 30 * 60
CALENDAR_ITERATIONS = 8
_SYSTEMD_TS_FMT = "%a %Y-%m-%d %H:%M:%S UTC"
_ELAPSE_RE = re.compile(r"^\s*(Next elapse|Iteration #\d+): (.+ UTC)$", re.MULTILINE)
_CAL_GROUP_RE = re.compile(r"\{ OnCalendar=(.*?) ; next_elapse=[^}]*\}")
_MONO_GROUP_RE = re.compile(r"\{ (\w+)=(.*?) ; next_elapse=[^}]*\}")
_SPAN_TOKEN_RE = re.compile(r"^(\d+(?:\.\d+)?)([A-Za-z]+)$")
_SPAN_UNITS = {
    "us": 1e-6, "ms": 1e-3, "s": 1.0, "min": 60.0, "h": 3600.0,
    "d": 86400.0, "w": 7 * 86400.0, "M": 30.44 * 86400.0,
    "month": 30.44 * 86400.0, "y": 365.25 * 86400.0,
}
# Recurring monotonic triggers; every other kind (OnBootUSec, OnStartupUSec,
# OnActiveUSec) fires once and does not define a period.
_RECURRING_MONO = ("OnUnitActiveUSec", "OnUnitInactiveUSec")
_SERVICE_PROPS = ("Type", "LoadState", "ActiveState", "SubState", "NRestarts")
_TIMER_PROPS = ("LoadState", "TimersCalendar", "TimersMonotonic",
                "LastTriggerUSec", "ActiveEnterTimestamp")


class SystemdUnreadable(Exception):
    """A systemctl or systemd-analyze call exited non-zero or timed out."""

    def __init__(self, command: str, error: str):
        super().__init__(f"{command}: {error}")
        self.command = command
        self.error = error


class SystemdReader:
    """The live, read-only reader of the user manager."""

    def _run(self, args: list[str]) -> str:
        command = " ".join(args)
        env = dict(os.environ, LC_ALL="C", TZ="UTC")
        try:
            proc = subprocess.run(args, capture_output=True, text=True, env=env,
                                  timeout=SYSTEMD_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            raise SystemdUnreadable(command, f"timed out after {SYSTEMD_TIMEOUT_S}s") from None
        except OSError as exc:
            raise SystemdUnreadable(command, str(exc)) from exc
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise SystemdUnreadable(command, f"exit {proc.returncode}: "
                                    f"{err[0] if err else 'no output'}")
        return proc.stdout

    def enabled(self, kind: str) -> list[str]:
        out = self._run(["systemctl", "--user", "list-unit-files",
                         f"--type={kind}", "--state=enabled", "--no-legend"])
        return [line.split()[0] for line in out.splitlines() if line.strip()]

    def show(self, unit: str, props: tuple[str, ...]) -> dict[str, str]:
        """``show -p``; an empty ``unit`` asks the manager itself. A property
        printed once per trigger (TimersCalendar, TimersMonotonic) is joined."""
        args = ["systemctl", "--user", "show", "-p", ",".join(props)]
        if unit:
            args.append(unit)
        out: dict[str, str] = {}
        for line in self._run(args).splitlines():
            key, sep, value = line.partition("=")
            if not sep:
                continue
            out[key] = f"{out[key]} {value}" if key in out else value
        return out

    def calendar(self, expr: str, n: int) -> list[datetime]:
        out = self._run(["systemd-analyze", "calendar", f"--iterations={n}", expr])
        return [d for d in (parse_systemd_ts(m.group(2)) for m in _ELAPSE_RE.finditer(out))
                if d is not None]


def parse_systemd_ts(value: str | None) -> datetime | None:
    """A ``LC_ALL=C TZ=UTC`` systemd timestamp, or None for never.

    Raises ValueError on a non-empty value in another shape, so a format drift
    is reported rather than read as "never fired".
    """
    value = (value or "").strip()
    if not value or value == "n/a":
        return None
    return datetime.strptime(value, _SYSTEMD_TS_FMT).replace(tzinfo=timezone.utc)


def parse_span(text: str) -> float:
    """A systemd time span (``2min``, ``1d 12h``) in seconds.

    Raises ValueError carrying the token when a unit is not known.
    """
    total = 0.0
    tokens = text.split()
    if not tokens:
        raise ValueError(text)
    for token in tokens:
        m = _SPAN_TOKEN_RE.match(token)
        if not m or m.group(2) not in _SPAN_UNITS:
            raise ValueError(token)
        total += float(m.group(1)) * _SPAN_UNITS[m.group(2)]
    return total


def parse_timer_triggers(cal: str, mono: str) -> tuple[list[str], list[tuple[str, str]]]:
    """The ``OnCalendar`` expressions and the ``(kind, span)`` monotonic pairs."""
    return (_CAL_GROUP_RE.findall(cal or ""), _MONO_GROUP_RE.findall(mono or ""))


def timer_period(calendars: dict[str, list[datetime] | str], cal_exprs: list[str],
                 mono: list[tuple[str, str]]) -> tuple[float | None, str]:
    """The smallest recurring trigger period, or ``(None, why)`` when none.

    ``calendars`` maps an expression to its elapses, or to an error string
    when ``systemd-analyze calendar`` failed for it. A calendar period is the
    largest gap between consecutive elapses, so a weekly timer reads 7 d and
    a weekdays-only one its weekend gap.
    """
    periods: list[tuple[float, str]] = []
    for expr in cal_exprs:
        elapses = calendars.get(expr)
        if isinstance(elapses, str):
            return None, f"calendar {expr!r}: {elapses}"
        if not elapses or len(elapses) < 2:
            continue
        gap = max((b - a).total_seconds() for a, b in zip(elapses, elapses[1:], strict=False))
        periods.append((gap, "OnCalendar"))
    for kind, span in mono:
        if kind not in _RECURRING_MONO:
            continue
        try:
            periods.append((parse_span(span), kind))
        except ValueError as exc:
            return None, f"{kind}: unknown span token {exc}"
    if not periods:
        return None, "no recurring trigger"
    period, kind = min(periods)
    return period, f"period {format_age(period)} ({kind})"


def _ignored_units(cfg: dict) -> set[str]:
    try:
        raw = cfg.get("daemon", {}).get("watchdog", {}).get("ignore_units")
    except AttributeError as exc:
        logger.warning("watchdog: daemon.watchdog is malformed (%s); no unit ignored", exc)
        return set()
    if not isinstance(raw, list):
        return set()
    names = {str(x).strip() for x in raw if isinstance(x, str) and x.strip()}
    return names | {f"{n}.service" for n in names if "." not in n}


def _read_systemd(reader: SystemdReader, cfg: dict) -> dict:
    """Every systemd read of one pass, before any classification.

    Raises SystemdUnreadable on the first failed systemctl call; a failed
    ``systemd-analyze calendar`` is kept as that expression's error instead.
    """
    ignored = _ignored_units(cfg)
    manager = reader.show("", ("UserspaceTimestamp",))
    services = {}
    for unit in reader.enabled("service"):
        if "@." in unit or unit in ignored:
            continue  # a template has no runtime state; an ignored unit is the operator's word
        props = reader.show(unit, _SERVICE_PROPS)
        if props.get("Type") == "oneshot":
            continue
        services[unit] = props
    timers = {}
    calendars: dict[str, list[datetime] | str] = {}
    for unit in reader.enabled("timer"):
        if "@." in unit:
            continue
        props = reader.show(unit, _TIMER_PROPS)
        timers[unit] = props
        for expr in parse_timer_triggers(props.get("TimersCalendar", ""), "")[0]:
            if expr in calendars:
                continue
            try:
                calendars[expr] = reader.calendar(expr, CALENDAR_ITERATIONS)
            except SystemdUnreadable as exc:
                calendars[expr] = exc.error
    return {"manager": manager, "services": services, "timers": timers,
            "calendars": calendars}


class _Counters:
    def __init__(self) -> None:
        self.fired = 0
        self.undelivered = 0


def _dedup(state: dict, key: str, bad: bool, summary: str, detail: str,
           recovery: str, now: datetime, realert_window: timedelta,
           alert_fn, counters: _Counters) -> bool:
    """The heartbeat path's dedup model for one state key; True when an alert fired."""
    prev = state.get(key) if isinstance(state.get(key), dict) else {}
    if bad:
        last_alert = parse_iso(prev.get("last_alert_ts"))
        if (prev.get("state") != "down" or last_alert is None
                or (now - last_alert) >= realert_window):
            delivered = _delivered(alert_fn("critical", summary, detail, source="watchdog"))
            counters.fired += 1
            if not delivered:
                counters.undelivered += 1
            state[key] = {"state": "down", "last_alert_ts": now.isoformat()}
            return True
        state[key] = {"state": "down", "last_alert_ts": prev.get("last_alert_ts")}
        return False
    fired = False
    if prev.get("state") == "down":
        # `info` goes to the log only, by design; not counted as undelivered.
        alert_fn("info", recovery, f"recovered at {now.isoformat()}", source="watchdog")
        counters.fired += 1
        fired = True
    state[key] = {"state": "ok"}
    return fired


def _classify_services(read: dict, state: dict, now: datetime, in_boot_grace: bool,
                       realert_window: timedelta, alert_fn, counters: _Counters) -> list[dict]:
    rows = []
    for unit, props in sorted(read["services"].items()):
        active, sub = props.get("ActiveState", ""), props.get("SubState", "")
        try:
            restarts: int | None = int(props.get("NRestarts", ""))
        except ValueError:
            restarts = None
        detail = f"{active or '?'}/{sub or '?'}"
        if props.get("LoadState") not in (None, "loaded"):
            detail = f"{detail}, load {props.get('LoadState')}"
        down = not (active == "active" and sub == "running")
        key = f"service:{unit}"
        fired = False
        if down and in_boot_grace:
            status = "grace"
            state.setdefault(key, {"state": "grace"})  # listed if systemd turns unreadable
        else:
            status = "down" if down else "ok"
            fired = _dedup(state, key, down, f"service {unit} down ({detail})",
                           f"restarts {restarts}; watchdog at {now.isoformat()}",
                           f"service {unit} running again", now, realert_window,
                           alert_fn, counters)
        rows.append({"unit": unit, "status": status, "restarts": restarts,
                     "detail": detail, "fired": fired})
    return rows


def _classify_timers(read: dict, state: dict, now: datetime, in_grace: bool,
                     realert_window: timedelta, alert_fn, counters: _Counters) -> list[dict]:
    rows = []
    for unit, props in sorted(read["timers"].items()):
        row = {"unit": unit, "status": "not-checked", "last_trigger": None,
               "age_s": None, "threshold_s": None, "detail": "", "fired": False}
        rows.append(row)
        key = f"timer:{unit}"
        # Every timer keeps a key, so an unreadable pass still lists it.
        state.setdefault(key, {"state": "ok"})
        if props.get("LoadState") not in (None, "loaded"):
            row["detail"] = f"load {props.get('LoadState')}"
            continue
        cal_exprs, mono = parse_timer_triggers(props.get("TimersCalendar", ""),
                                               props.get("TimersMonotonic", ""))
        period, why = timer_period(read["calendars"], cal_exprs, mono)
        row["detail"] = why
        if period is None:
            continue
        threshold = int(2 * period + TIMER_SLACK_S)
        row["threshold_s"] = threshold
        try:
            last = parse_systemd_ts(props.get("LastTriggerUSec"))
            since = last or parse_systemd_ts(props.get("ActiveEnterTimestamp"))
        except ValueError as exc:
            row["detail"] = f"{why}; unparseable timestamp: {exc}"
            continue
        row["last_trigger"] = last.isoformat() if last else None
        if since is None:
            # Enabled, never fired and never activated: it is not running.
            stale, age = True, None
            row["detail"] = f"{why}; never fired, never activated"
        else:
            age = (now - since).total_seconds()
            stale = age > threshold
            row["age_s"] = int(age)
            if last is None:
                row["detail"] = f"{why}; never fired, measured from activation"
        if stale and in_grace:
            row["status"] = "grace"
            continue
        row["status"] = "stale" if stale else "ok"
        when = f"last fired {format_age(age)} ago" if last and age is not None else "never fired"
        row["fired"] = _dedup(state, key, stale, f"timer {unit} stale, {when}",
                              f"threshold {threshold}s; watchdog at {now.isoformat()}",
                              f"timer {unit} firing again", now, realert_window,
                              alert_fn, counters)
    return rows


def _resume_since(meta: dict, now: datetime) -> datetime | None:
    """The resume-grace start after this pass, or None outside a resume grace.

    A pass more than RESUME_GAP_S after the previous one means the machine
    slept; timers then get RESUME_GRACE_S to catch up. A first pass (no meta)
    sets none.
    """
    last_pass = parse_iso(meta.get("last_pass_ts"))
    since = parse_iso(meta.get("resume_since"))
    if last_pass is not None and (now - last_pass).total_seconds() > RESUME_GAP_S:
        since = now
    if since is not None and (now - since).total_seconds() >= RESUME_GRACE_S:
        since = None
    return since


def _check_systemd(reader: SystemdReader, cfg: dict, state: dict, now: datetime,
                   in_resume: bool, realert_window: timedelta, alert_fn,
                   counters: _Counters) -> tuple[list[dict], list[dict]]:
    """Services and timers for one pass: ``(service_rows, timer_rows)``."""
    try:
        read = _read_systemd(reader, cfg)
    except SystemdUnreadable as exc:
        prev_keys = sorted(k for k in state if k.startswith(("service:", "timer:")))
        _dedup(state, "systemd:read", True,
               f"watchdog cannot read systemd: {exc.command} {exc.error}",
               f"watchdog at {now.isoformat()}", "watchdog reads systemd again",
               now, realert_window, alert_fn, counters)
        services, timers = [], []
        for k in prev_keys:
            kind, unit = k.split(":", 1)
            if kind == "service":
                services.append({"unit": unit, "status": "unknown", "restarts": None,
                                 "detail": "systemd unreadable", "fired": False})
            else:
                timers.append({"unit": unit, "status": "unknown", "last_trigger": None,
                               "age_s": None, "threshold_s": None,
                               "detail": "systemd unreadable", "fired": False})
        if not prev_keys:
            services.append({"unit": "systemd", "status": "unknown", "restarts": None,
                             "detail": f"{exc.command}: {exc.error}", "fired": False})
        return services, timers
    _dedup(state, "systemd:read", False, "", "", "watchdog reads systemd again",
           now, realert_window, alert_fn, counters)
    try:
        boot = parse_systemd_ts(read["manager"].get("UserspaceTimestamp"))
    except ValueError as exc:
        logger.warning("watchdog: UserspaceTimestamp unparseable (%s); no boot grace", exc)
        boot = None
    in_boot = boot is not None and (now - boot).total_seconds() < BOOT_GRACE_S
    services = _classify_services(read, state, now, in_boot, realert_window,
                                  alert_fn, counters)
    timers = _classify_timers(read, state, now, in_boot or in_resume, realert_window,
                              alert_fn, counters)
    # Dedup memory of a unit no longer enabled would read `unknown` on the
    # next unreadable pass; it is this module's own cache, not a record.
    current = ({f"service:{u}" for u in read["services"]}
               | {f"timer:{u}" for u in read["timers"]})
    for k in [k for k in state if k.startswith(("service:", "timer:")) and k not in current]:
        del state[k]
    return services, timers


# ============================================================
# One pass
# ============================================================

def _default_alert():
    from scripts.utils import alert

    return alert.alert


def _delivered(result) -> bool:
    """True when an alert reached a channel a human reads.

    `alert()` returns `{"telegram": bool, "log": bool}`. `log` is
    always True and is NOT a channel for this purpose - a log line in a file
    nobody is watching is what "the watchdog fired and nobody came" looks like.

    An injected `alert_fn` may return anything, including None. An unrecognised
    return is treated as DELIVERED rather than undelivered, so a test double or
    a third-party callable cannot inflate the undelivered count into a false
    alarm about the alerting path itself. The claim this function makes is
    narrow on purpose: it reports what `alert()` reported, and nothing when
    `alert()` was not what ran.
    """
    if not isinstance(result, dict):
        return True
    return any(v for k, v in result.items() if k != "log")


def check_once(
    workspace_root: Path,
    *,
    now: datetime | None = None,
    alert_fn=None,
    state_path: Path | None = None,
    cadence: dict[str, tuple[int, int]] | None = None,
    realert_min: int | None = None,
    threshold_override: float | None = None,
    systemd: SystemdReader | None = None,
) -> dict:
    """Run one watchdog pass: classify each configured daemon, fire deduped
    alerts on a missed beat, persist dedup state, and return a report.

    Dedup model (Design Decisions 6-7):
    - a daemon going ok -> down fires one critical alert immediately;
    - while down, it re-alerts only after ``realert_min`` minutes;
    - a daemon going down -> ok fires one ``info`` "resumed" alert.

    Then every enabled user service that is not oneshot (``down`` unless
    active/running) and every enabled user timer (``stale`` past twice its
    period plus 30 min), with a boot grace and, for timers, a resume grace
    (HOS-397). ``threshold_override`` applies to heartbeat rows only.

    Args are injectable so tests drive the clock, the alert sink, the state
    file, the cadence and the systemd reader (``systemd``; None reads the live
    user manager) without touching the live workspace or Telegram.
    """
    now = now or _now()
    if alert_fn is None:
        alert_fn = _default_alert()
    if state_path is None:
        try:
            state_path = (data_write_dir("runtime:daemon-watchdog/state")
                          / WATCHDOG_STATE_NAME)
        except DataPathError as exc:
            # No data repository: no dedup memory, so every pass alerts afresh.
            # That is the loud direction, and it is said.
            logger.warning("watchdog: no dedup state on this tree (%s); every "
                           "pass alerts afresh", exc)
            state_path = None
    # One config read per pass, shared by the helpers below, unconditional so
    # `ignore_units` applies when a caller injects the cadence (HOS-397).
    cfg = _read_config(workspace_root)
    if cadence is None:
        cadence = load_cadence(workspace_root, cfg)
    if realert_min is None:
        realert_min = _realert_minutes(workspace_root, cfg)
    realert_window = timedelta(minutes=realert_min)

    state = _load_state(state_path)
    # HOS-339: a daemon with no first beat yet may be reported `starting` only
    # when the dedup state can be trusted: a fresh install (no file yet) or a
    # file that parsed as an object. No state path or an unreadable file keeps
    # the loud direction above, where every missing daemon alerts.
    if state_path is None:
        trusted = False
    elif not state_path.exists():
        trusted = True
    else:
        try:
            trusted = isinstance(json.loads(state_path.read_text(encoding="utf-8")), dict)
        except (OSError, UnicodeError, ValueError) as exc:
            logger.warning("watchdog: dedup state unreadable (%s); no daemon is "
                           "given a start-up grace this pass", type(exc).__name__)
            trusted = False
    daemons: list[dict] = []
    # `alerts_fired` counts alerts RAISED. `alerts_undelivered` counts the ones
    # that reached no channel beyond the log, which is the number that decides
    # whether a human learned about a dead daemon. `alert()` has always returned
    # `{"telegram": bool, "card": bool, "log": bool}` naming what actually
    # fired, and both call sites below discarded it - so "3 alert(s) fired"
    # could mean three alerts that reached nobody, and the grid printed the word
    # "fired" over it. That is the `.claude/rules/scope-claims.md` shape: the
    # method counted attempts, the sentence asserted delivery.
    alerts_fired = 0
    alerts_undelivered = 0

    for name, (expected, grace) in cadence.items():
        threshold = threshold_override if threshold_override is not None else (expected + grace)
        record = _read_beat(workspace_root, name)
        status = classify(record, threshold, now)
        age = _age_seconds(record, now)
        prev = state.get(name) if isinstance(state.get(name), dict) else {}
        prev_state = prev.get("state", "ok")
        last_alert = parse_iso(prev.get("last_alert_ts"))
        fired = False
        starting_since = None
        if status == "missing" and trusted:
            if name not in state:
                # Started, first beat not yet written (HOS-339): no alert yet.
                starting_since = now
            elif prev_state == "starting":
                since = parse_iso(prev.get("since"))
                if since is not None and (now - since).total_seconds() <= threshold:
                    starting_since = since

        if starting_since is not None:
            status = "starting"
            state[name] = {"state": "starting", "since": starting_since.isoformat()}
        elif status in ("silent", "missing"):
            should_alert = (
                prev_state != "down"
                or last_alert is None
                or (now - last_alert) >= realert_window
            )
            if should_alert:
                if status == "missing":
                    summary = f"daemon {name} missing - no heartbeat"
                else:
                    summary = f"daemon {name} silent {format_age(age or 0)}"
                detail = (
                    f"expected a liveness beat within {threshold}s; "
                    f"watchdog at {now.isoformat()}"
                )
                delivered = _delivered(
                    alert_fn("critical", summary, detail, source="watchdog"))
                fired = True
                alerts_fired += 1
                if not delivered:
                    alerts_undelivered += 1
                state[name] = {
                    "state": "down",
                    "condition": status,
                    "last_alert_ts": now.isoformat(),
                }
            else:
                # Still down, within the re-alert window: keep the prior stamp.
                state[name] = {
                    "state": "down",
                    "condition": status,
                    "last_alert_ts": prev.get("last_alert_ts"),
                }
        else:  # ok
            if prev_state == "down":
                # Recovery is `info`, which `alert()` routes to the log ONLY by
                # design. It is deliberately not counted as undelivered: a
                # severity that has no other channel cannot fail to use one.
                alert_fn(
                    "info",
                    f"daemon {name} heartbeat resumed",
                    f"recovered at {now.isoformat()}",
                    source="watchdog",
                )
                fired = True
                alerts_fired += 1
            state[name] = {"state": "ok"}

        daemons.append({
            "daemon": name,
            "status": status,
            "age_s": int(age) if isinstance(age, (int, float)) else None,
            "threshold_s": threshold,
            "fired": fired,
        })

    # Services and timers (HOS-397). The meta is read before this pass writes it.
    meta = state.get("_meta") if isinstance(state.get("_meta"), dict) else {}
    resume_since = _resume_since(meta, now)
    services: list[dict] = []
    timers: list[dict] = []
    systemd_note = None
    if systemd is None and not sys.platform.startswith("linux"):
        systemd_note = f"services and timers: not checked on {sys.platform}"
    else:
        counters = _Counters()
        services, timers = _check_systemd(
            systemd or SystemdReader(), cfg, state, now, resume_since is not None,
            realert_window, alert_fn, counters)
        alerts_fired += counters.fired
        alerts_undelivered += counters.undelivered
    state["_meta"] = {"last_pass_ts": now.isoformat()}
    if resume_since is not None:
        state["_meta"]["resume_since"] = resume_since.isoformat()

    if not _save_state(state_path, state):
        # HOS-339: `starting` rests on the saved state; a failed save would
        # restart its clock every pass, so each such daemon alerts as missing.
        for d in daemons:
            if d["status"] != "starting":
                continue
            delivered = _delivered(alert_fn(
                "critical", f"daemon {d['daemon']} missing - no heartbeat",
                f"expected a liveness beat within {d['threshold_s']}s; "
                f"watchdog at {now.isoformat()}",
                source="watchdog"))
            d["status"], d["fired"] = "missing", True
            alerts_fired += 1
            if not delivered:
                alerts_undelivered += 1
    healthy = ("ok", "starting", "grace", "not-checked")
    rows = daemons + services + timers
    verdict = "down" if any(r["status"] not in healthy for r in rows) else "ok"
    report = {
        "verdict": verdict,
        "checked_at": now.isoformat(),
        "alerts_fired": alerts_fired,
        "alerts_undelivered": alerts_undelivered,
        "daemons": daemons,
        "services": services,
        "timers": timers,
    }
    if systemd_note:
        report["systemd_note"] = systemd_note
    return report
