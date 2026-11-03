"""Where the Exchange sync daemon keeps its pid, start time, stop file, lock and log.

Two scripts need the same five files, `scripts/sync-exchange-daemon.py`, which
writes them, and `scripts/sync-exchange-pulse.py`, which reads them to decide
whether to start the daemon. Each used to spell `<engine>/.sync-exchange/` for
itself; this module is the one answer both ask.

They are in the data repository's runtime store since 2026-09-25, when HOS-5
section 7 took every artifact out of the engine checkout (operator decision that
day: "old way should go out and do not stay in the workspace"):

    runtime:sync-exchange/logs   daemon.log and its rotations (ignored by the
                                 overlay's git, like every sync-exchange log)
    runtime:sync-exchange/tmp    daemon.pid, started_at, stop, daemon.start.lock
                                 (overwritten or removed by the daemon itself)

A tree with no data repository raises `DataPathError` here, and both callers
refuse to start rather than write into the engine checkout.

A DAEMON STARTED BEFORE THIS MOVE KEEPS ITS OLD FILES. Its pid file and its start
lock are at the old address, so neither the pulse nor a new daemon can see it:
the pulse would start a second scheduler beside it. The running daemon has to be
stopped BEFORE the code that reads the new address is deployed, then started.
"""
from __future__ import annotations

import contextlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from scripts.utils.data_path import data_path, data_write_dir


class DaemonFiles(NamedTuple):
    pid: Path
    started_at: Path
    stop: Path
    start_lock: Path
    log: Path


def daemon_files(*, create: bool) -> DaemonFiles:
    """The five files. `create=True` is the daemon's call; a reader creates nothing."""
    if create:
        state = data_write_dir("runtime:sync-exchange/tmp")
        logs = data_write_dir("runtime:sync-exchange/logs")
    else:
        state = data_path("runtime:sync-exchange/tmp")
        logs = data_path("runtime:sync-exchange/logs")
    return DaemonFiles(
        pid=state / "daemon.pid",
        started_at=state / "started_at",
        stop=state / "stop",
        start_lock=state / "daemon.start.lock",
        log=logs / "daemon.log",
    )


# The R12 trace-id convention (2026-06-03) inserts an optional "[<hex>] "
# correlation token between INFO and the message. Match it optionally so both
# pre- and post-R12 log lines parse.
_JOB_OK = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO (?:\[[0-9a-f]+\] )?job-ok sync-exchange"
)


def last_job_ok(log: Path) -> datetime | None:
    """When the daemon last logged a successful Exchange sync, or None.

    AWARE, in the local zone, because the daemon writes `%(asctime)s` in local
    time. One reader for the two callers that ask: the pulse, which prints how
    long ago, and the ops radar, which raises a signal when it is too long ago.
    It lived in the pulse alone until 2026-10-12.

    The rotated backups are read too, newest first. The daemon logs through
    `RotatingFileHandler(maxBytes=1_000_000, backupCount=3)`, which moves all
    history into `daemon.log.1`-`.3` and starts a fresh EMPTY `daemon.log`.
    Reading only the active file therefore reported "no sync logged yet" for a
    daemon that had synced minutes earlier: MEASURED 2026-08-29 with a
    7-minute-old job-ok line in `daemon.log.1` and an empty `daemon.log`.
    """
    last_ts: datetime | None = None
    # Newest first, and stop at the first file that yields a match: a rotated
    # backup can only hold lines OLDER than the file that displaced it.
    for path in [log] + [log.with_name(f"{log.name}.{n}") for n in (1, 2, 3)]:
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = _JOB_OK.match(line)
                    if m:
                        # A line the regex accepted but strptime rejects is a
                        # truncated write, which a rotating log produces at its
                        # tail. Skipping it keeps the previous match.
                        with contextlib.suppress(ValueError):
                            last_ts = datetime.strptime(
                                m.group(1), "%Y-%m-%d %H:%M:%S"
                            ).replace(tzinfo=datetime.now().astimezone().tzinfo)
        except OSError:
            continue
        if last_ts is not None:
            break
    return last_ts


# The two failure records `JobDispatcher.dispatch` writes in
# `scripts/sync-exchange-daemon.py` (read 2026-11-01):
#   "job-fail sync-exchange exit=%d stderr=%s"   the job's stderr tail, may span lines
#   "job-fail sync-exchange (exception)"         followed by a traceback
#   "job-timeout sync-exchange after %ds"
_JOB_FAIL = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \w+ (?:\[[0-9a-f]+\] )?"
    r"(job-fail|job-timeout) sync-exchange(.*)$"
)
_RECORD_START = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ ")
_REASON_MAX = 200


def _failure_reason(kind: str, first: str, continuation: list[str]) -> str:
    """One line a reader can act on, from one failure record.

    The last non-empty line of the record is the one that says what broke: the
    final line of a traceback, or the `[ERROR]`/JSON line `optdeps.require`
    prints for a missing extra, which names the `uv sync --extra` command.
    """
    if kind == "job-timeout":
        return f"timed out {first.strip()}".strip()
    head = first.split("stderr=", 1)[1] if "stderr=" in first else first
    lines = [ln.strip() for ln in [head, *continuation] if ln.strip()]
    reason = lines[-1] if lines else first.strip()
    if reason.startswith("{"):
        with contextlib.suppress(ValueError, TypeError, AttributeError):
            reason = str(json.loads(reason).get("error") or reason)
    if len(reason) > _REASON_MAX:
        reason = reason[: _REASON_MAX - 3] + "..."
    return reason


def last_job_fail(log: Path) -> tuple[datetime, str] | None:
    """The newest failed Exchange sync logged AFTER the last success, and why.

    None when the newest job outcome in the log is a success, or no failure is
    logged. "After" is LOG ORDER, never a timestamp comparison: the log stamps
    whole seconds plus milliseconds the parsers drop, so a job-ok and a later
    job-fail in the same second compare equal by time (HOS-294 review). Same
    files, newest first, and zone as `last_job_ok`. A failure record may run
    over several lines (a stderr tail or a traceback), so the lines after it up
    to the next timestamped record belong to it.
    """
    tz = datetime.now().astimezone().tzinfo
    for path in [log] + [log.with_name(f"{log.name}.{n}") for n in (1, 2, 3)]:
        if not path.exists():
            continue
        # (stamp, kind, rest, continuation); kind "job-ok" marks a success.
        records: list[tuple[str, str, str, list[str]]] = []
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                current: tuple[str, str, str, list[str]] | None = None
                for line in f:
                    m = _JOB_FAIL.match(line)
                    if m:
                        current = (m.group(1), m.group(2), m.group(3), [])
                        records.append(current)
                    elif (ok := _JOB_OK.match(line)):
                        current = None
                        records.append((ok.group(1), "job-ok", "", []))
                    elif _RECORD_START.match(line):
                        current = None
                    elif current is not None:
                        current[3].append(line)
        except OSError:
            continue
        for stamp, kind, first, cont in reversed(records):
            try:
                ts = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
            except ValueError:
                # A truncated write at a rotating log's tail: `last_job_ok`
                # skips it too, so skip it here and keep looking.
                continue
            if kind == "job-ok":
                return None
            return ts, _failure_reason(kind, first, cont)
        # Neither outcome in this file: an older rotation may hold one.
    return None


__all__ = ["DaemonFiles", "daemon_files", "last_job_fail", "last_job_ok"]
