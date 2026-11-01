#!/usr/bin/env python3
"""herdr-wait.py - wait for a herdr pane to reach a status, and never outlive it.

The replacement for `herdr agent wait <pane> --until <status>`, which waits
indefinitely and does not exit when its pane is removed: one started from a
HELM session was still running about 20 hours after its pane went away with
its yard (MEASURED 2026-10-30, HOS-269). `.claude/hooks/_dispatch.py` refuses
the raw form and names this one.

    .venv/bin/python scripts/herdr-wait.py w9Z:p1 --until blocked
    .venv/bin/python scripts/herdr-wait.py w9Z:p1 --until idle --until done --ceiling 3600

It polls `herdr pane get` every 5 seconds; no long-lived herdr child exists, so
killing this process leaves nothing behind. First stdout line and exit code:

    reached: <pane> <status>                      0
    target gone: <pane>                           3
    ceiling hit: <pane> after <N>s                4   (default ceiling 43200 s)
    herdr unavailable: <pane> for 600s: <error>   5
    usage error (argparse)                        2

While it runs it keeps one record, `<pid>.json`, in the runtime store
`runtime:herdr-waiters/state`, removed on exit and on SIGTERM (exit 143). The
PostToolUse hook `.claude/hooks/herdr-waiter-reap.py` reads those records to
stop exactly the waiters of a workspace that `herdr worktree remove` deleted.
A record left by SIGKILL is swept by the next reader, here at start or there.
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils import herdr_agent as HA  # noqa: E402

DEFAULT_CEILING_S = 43200
POLL_S = 5
UNAVAILABLE_LIMIT_S = 600


def _on_sigterm(signum, frame):
    # SystemExit unwinds through main's `finally`, which removes the record.
    raise SystemExit(143)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wait for a herdr pane to reach a status; exit when the pane is gone.")
    parser.add_argument("pane", help="pane id, e.g. w9Z:p1")
    parser.add_argument("--until", action="append", required=True,
                        choices=sorted(HA.PANE_STATUSES), dest="states",
                        help="status to wait for; repeat for several")
    parser.add_argument("--ceiling", type=int, default=DEFAULT_CEILING_S,
                        help=f"give up after this many seconds (default {DEFAULT_CEILING_S})")
    args = parser.parse_args(argv)
    if args.ceiling <= 0:
        parser.error("--ceiling must be a positive number of seconds")

    signal.signal(signal.SIGTERM, _on_sigterm)
    record = None
    try:
        try:
            HA.waiter_records()      # sweeps records SIGKILL left behind
            record = HA.register_waiter(args.pane)
        except (OSError, RuntimeError, ValueError) as exc:
            # The wait itself does not depend on the record; only the reaper
            # does, so say it loudly and keep waiting.
            print(f"[herdr-wait] no waiter record written, the reaper cannot "
                  f"stop this waiter: {type(exc).__name__}: {exc}", file=sys.stderr)
        code, line = HA.wait_for_pane(args.pane, args.states, args.ceiling,
                                      poll_s=POLL_S,
                                      unavailable_limit_s=UNAVAILABLE_LIMIT_S)
        print(line, flush=True)
        return code
    finally:
        if record is not None:
            HA.unregister_waiter(record)


if __name__ == "__main__":
    sys.exit(main())
