"""Per-daemon liveness heartbeat (R14).

Each daemon emits a *liveness* beat on a fast tick (~60s, decoupled from its
work cadence) into ``heartbeats_dir()/<daemon_name>.json`` in the data repository.
One file per daemon means the beating daemons (sync-exchange, sentinel,
any other) never race on a shared file, and the watchdog
(``scripts/daemon-watchdog.py``) sees each daemon independently. The write goes
through ``scripts/utils/atomic.atomic_write_text`` at 0o644.

Fields written:
- ``daemon``: the daemon name (also the filename stem)
- ``pid``: process id
- ``version``: a COPY of ``config_loaded_version``, not a build version; a
  difference between two daemons' values is not a code skew.
- ``config_loaded_version``: version of the merged config in memory, or "unversioned"
- ``uptime_s``: seconds since this process imported the module (per-process boot ts)
- ``last_heartbeat``: ISO-8601 UTC of this write
- ``trace_id``: the process-tree trace ID (``scripts.utils.tracing.get()``), or None

Never raises: a write failure logs a warning so the scheduler keeps ticking and
only the one beat is lost.

Usage::

    from scripts.utils import daemon_heartbeat
    daemon_heartbeat.beat("sync-exchange", config_version="3")
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from scripts.utils import tracing
from scripts.utils.atomic import atomic_write_text

# Per-process boot timestamp, captured once at import. uptime_s is measured
# against this so a fresh import means a fresh uptime baseline.
_BOOT_TS = time.time()

def heartbeats_dir(*, create: bool = False) -> Path:
    """Where every daemon's heartbeat lives: the data repository's
    `runtime:daemon-heartbeat/tmp`, overwritten every beat and ignored by its git.

    It was `<engine>/.daemon-state/heartbeats/` until 2026-09-25, when HOS-5
    section 7 took every artifact out of the engine checkout (operator decision
    that day: "old way should go out and do not stay in the workspace"). Raises
    `DataPathError` on a tree with no data repository. `create=True` is the
    writer's call; a reader asks with the default and creates nothing.
    """
    from scripts.utils.data_path import data_path, data_write_dir

    if create:
        return data_write_dir("runtime:daemon-heartbeat/tmp")
    return data_path("runtime:daemon-heartbeat/tmp")


def beat(daemon_name: str, *, config_version: str | None = None) -> None:
    """Write a per-daemon liveness heartbeat. Never raises.

    Args:
        daemon_name: the daemon's name; also the filename stem
            (``heartbeats_dir()/<daemon_name>.json``).
        config_version: version of the merged config currently in memory, if
            the caller tracks one. Defaults to "unversioned". It is written to
            BOTH ``config_loaded_version`` and ``version``; see the module
            docstring for why ``version`` is not a build version.
    """
    try:
        path = heartbeats_dir(create=True) / f"{daemon_name}.json"
        payload = {
            "daemon": daemon_name,
            "pid": os.getpid(),
            "version": config_version if config_version is not None else "unversioned",
            "config_loaded_version": config_version if config_version is not None else "unversioned",
            "uptime_s": int(time.time() - _BOOT_TS),
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "trace_id": tracing.get(),
        }
        atomic_write_text(path, json.dumps(payload, indent=2) + "\n", mode=0o644)
    except Exception as e:  # noqa: BLE001 - the docstring's promise is total
        # `except OSError` did not deliver "Never raises". The try block also
        # calls the data-root resolver and `tracing.get()`, and neither is
        # confined to OSError: a workspace that will not resolve (no marker, an
        # unresolvable `~` in WORKSPACE_ROOT) raises RuntimeError straight
        # through this handler and into the caller's scheduler tick. Callers
        # rely on the total promise (the team-bot daemon piggybacks this beat
        # on the job whose only purpose is the healthchecks ping), so a
        # liveness-telemetry fault became a monitoring outage. Losing one beat
        # is the correct cost; taking the scheduler with it is not.
        logging.warning("daemon heartbeat write failed for %s: %s", daemon_name, e)
