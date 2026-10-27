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


__all__ = ["DaemonFiles", "daemon_files", "last_job_ok"]
