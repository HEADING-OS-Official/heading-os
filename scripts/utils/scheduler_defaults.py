#!/usr/bin/env python3
"""Shared APScheduler job defaults, so lateness never silently drops work.

APScheduler's ``misfire_grace_time`` defaults to 1 second. A job whose due
moment passes by more than that is not run late, it is DISCARDED, with a WARNING
in the journal and no other trace. Measured on this workspace over the 24 hours
to 2026-07-30, before this constant existed: a 1-minute heartbeat job lost 1059
of 1440 runs, and a 2-hour Exchange sync job ran twice instead of twelve times,
while systemd reported ``active running`` throughout.

The cause is tick latency, not load. WSL 9P filesystem latency delays the
scheduler's own ticks by seconds, which is enough to exceed a 1 second grace.

The three keys below are ONE decision and are read together:

    misfire_grace_time=None   run the job however late it is; never drop work
                              merely because a tick slipped
    coalesce=True             but collapse a backlog into a single run, so a
                              laptop that slept for eight hours does not fire a
                              2-hour job four times on wake
    max_instances=1           and never overlap a run already in flight

Only the FIRST key changes behaviour. APScheduler's own defaults are already
``coalesce=True`` and ``max_instances=1`` (``schedulers/base.py:910-915``), so
the other two are stated for legibility rather than for effect: ``grace=None``
is safe only BECAUSE runs coalesce and do not overlap, and a reader who sees one
key alone has to know the library's defaults to judge whether the decision is
sound.

Pass this to a scheduler CONSTRUCTOR, not to ``add_job``:

    scheduler = AsyncIOScheduler(timezone=get_default_tz(),
                                 job_defaults=JOB_DEFAULTS)

APScheduler fills each job's unset options from its scheduler's ``job_defaults``
when the job reaches its jobstore (``schedulers/base.py``, ``_real_add_job``), so
EVERY job on that scheduler inherits these, including jobs added later by an
author who never reads this file. That inheritance is the whole point: a
per-job value once sat on one ``add_job`` call while five sibling jobs on the
same scheduler kept the 1 second grace (daemon removed 2026-09-21, HOS-7).

A call site may still override any key explicitly, and an explicit value wins.

This module imports nothing, deliberately, so it is safe to import at module
scope or beside a local optional-dependency import, which is what a daemon that
defers its APScheduler import until it knows it will run needs.

A guard failed any scheduler construction under ``scripts/`` that omitted
``job_defaults``. The 2026-09-21 test-suite split took it, so a scheduler built
without these defaults now silently keeps APScheduler's 1-second misfire grace
and discards late jobs. Pass ``job_defaults=JOB_DEFAULTS`` at construction.
"""

JOB_DEFAULTS = {
    "misfire_grace_time": None,
    "coalesce": True,
    "max_instances": 1,
}
