#!/usr/bin/env python3
"""What every YARD of this repository is doing right now, in one table.

The reading, the four states, the evening that produced them and why the stall
threshold is a parameter rather than a measurement are all in
`scripts/utils/yard_status.py`. This file renders what that module returns and
does nothing else, so a Stop hook can ask the same question without paying an
interpreter start on every turn.

Console-first: the table is the product, `--json` is for a caller, and nothing
here starts, stops, restarts or writes to anything. It reads.

Usage:
    python scripts/yard-status.py                 # the table
    python scripts/yard-status.py --json          # for a hook or a script
    python scripts/yard-status.py --attention     # only what needs a human
    python scripts/yard-status.py --quiet-for 45  # a different stall threshold
    python scripts/yard-status.py --strict        # exit 1 when one needs a look
    python scripts/yard-status.py --wait --notify # sleep until a yard changes

`--wait` is how HELM waits for its yards without spending a turn on it, an idea
taken from the supertick plugin (2026-10-25): HELM starts this as a background
Bash call and ends its turn, the loop sweeps every `--interval` seconds with
`sleep` between sweeps, so the wait costs no model tokens, and it exits the
moment a yard reaches a verdict that needs attention and that it did not have
when the wait began. That exit is what wakes HELM. A yard already needing
attention at the start is the BASELINE and does not wake it again, or a wait
started beside a ready yard would return at once, forever. `--max` ends the
wait anyway, as a planned look at what no verdict covers.

`--notify` sends the wake, one message, to the operator's own Telegram sink
named by `YARD_WATCH_TELEGRAM_TARGET`, the variable the Stop hook
`.claude/hooks/yard-watch.py` already reads, and marks the same records as
notified in that hook's state, so its own thirty-minute notice does not repeat
the message. Unset, nothing is sent and the output says so. Like the rest of
this file it reads and reports: it starts, stops and writes to no yard.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.colors import (BOLD, CYAN, GRAY, GREEN, RED, RESET,  # noqa: E402
                                  YELLOW)
from scripts.utils.yard_status import (ACTION, DEFAULT_QUIET_MINUTES,  # noqa: E402
                                       needs_attention, sweep)

_COLOUR = {"dead": RED, "stalled": YELLOW, "ready": GREEN,
           "working": CYAN, "unknown": GRAY}


def render(result: dict, *, quiet_for: float, attention_only: bool) -> str:
    """The table, or the one honest line that replaces it."""
    if result["note"]:
        return f"{RED}{result['note']}{RESET}"

    if not result["yards"]:
        return f"{GRAY}no yards of this repository{RESET}"

    yards = needs_attention(result) if attention_only else result["yards"]
    if not yards:
        return (f"{GRAY}{len(result['yards'])} yard(s), none needing "
                f"attention{RESET}")

    lines = [f"{BOLD}{len(yards)} yard(s){RESET}"]
    for y in yards:
        colour = _COLOUR[y["verdict"]]
        ahead = "?" if y["ahead"] is None else y["ahead"]
        staged = "?" if y["staged"] is None else y["staged"]
        unstaged = "?" if y["unstaged"] is None else y["unstaged"]
        quiet = ("no transcript yet" if y["quiet_minutes"] is None
                 else f"quiet {y['quiet_minutes']:.0f}m")
        lines.append(f"  {colour}{y['verdict']:<8}{RESET} {y['name']:<26} "
                     f"{GRAY}{y['branch'] or '?'}{RESET}")
        lines.append(f"           {GRAY}{ahead} commit(s) ahead, {staged} "
                     f"staged, {unstaged} unstaged, {quiet}{RESET}")
        lines.append(f"           {ACTION[y['verdict']]}")
    lines.append(f"{GRAY}stall threshold {quiet_for:.0f} minutes; it is a "
                 f"parameter, not a measurement{RESET}")
    return "\n".join(lines)


#: The variable and the state file the Stop hook `.claude/hooks/yard-watch.py`
#: owns. Read here, never redefined: a second sink would be a second stream of
#: messages, and a second state file would let the hook repeat every wake.
TARGET_ENV_VAR = "YARD_WATCH_TELEGRAM_TARGET"


def _events(result: dict) -> set[tuple[str, str]]:
    return {(y["name"], y["verdict"]) for y in needs_attention(result)}


def notify_wake(new: list[dict], now: float) -> str:
    """Send one message for `new` and mark it in the hook's state; a status line."""
    from scripts.utils.paths import load_env
    from scripts.utils.telegram_notify import notify

    load_env(Path(__file__).resolve().parent.parent)
    target = os.environ.get(TARGET_ENV_VAR, "").strip()
    if not target:
        return f"notify: {TARGET_ENV_VAR} is not set, nothing was sent"

    lines = [f"  {y['name']} [{y['verdict']}] {ACTION[y['verdict']]}" for y in new]
    body = "\n".join([f"HEADING OS: {len(new)} yard(s) changed and need attention.",
                      *lines])
    try:
        sent = notify(target, body)
    except Exception as exc:  # noqa: BLE001 - notify() promises not to raise; reported if it does
        return f"notify: failed ({type(exc).__name__}: {exc})"
    if not sent:
        return "notify: not sent, see the log line above"

    try:
        from scripts.utils.atomic import atomic_write_text
        from scripts.utils.data_path import data_write_dir

        path = data_write_dir("runtime:yard-watch/state") / "fleet.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {}
        if not isinstance(state, dict):
            state = {}
        seen = [r for r in state.get("seen") or [] if isinstance(r, dict)]
        index = {(r.get("name"), r.get("verdict")): r for r in seen}
        for y in new:
            record = index.get((y["name"], y["verdict"]))
            if record is None:
                record = {"name": y["name"], "verdict": y["verdict"], "since": now}
                seen.append(record)
            record["notified_at"] = now
        state["seen"] = seen
        atomic_write_text(path, json.dumps(state, indent=2, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - the message went out; only the de-duplication is lost
        return (f"notify: sent, but the hook state was not updated "
                f"({type(exc).__name__}: {exc}); the Stop hook may repeat it")
    return "notify: sent"


def wait(*, quiet_for: float, interval: float, max_seconds: float,
         send: bool) -> int:
    """Sleep until a yard newly needs attention, the sweep fails, or `max_seconds`."""
    started = time.monotonic()
    first = sweep(quiet_for=quiet_for)
    if first["note"]:
        print(f"WAKE: the sweep could not be taken: {first['note']}")
        return 2
    baseline = _events(first)
    print(f"yard-status: waiting; {len(first['yards'])} yard(s), "
          f"{len(baseline)} already needing attention, a sweep every "
          f"{interval:g} s, at most {max_seconds:g} s", flush=True)
    while True:
        if time.monotonic() - started >= max_seconds:
            print(f"WAKE: {max_seconds:g} s passed with no change; a planned look")
            print(render(sweep(quiet_for=quiet_for), quiet_for=quiet_for,
                         attention_only=True))
            return 0
        # Never sleep past --max: an uncapped sleep let a planned wake
        # overshoot it by up to one whole interval.
        left = max_seconds - (time.monotonic() - started)
        time.sleep(max(0.0, min(interval, left)))
        result = sweep(quiet_for=quiet_for)
        if result["note"]:
            print(f"WAKE: the sweep could not be taken: {result['note']}")
            return 2
        current = _events(result)
        new = [y for y in needs_attention(result)
               if (y["name"], y["verdict"]) not in baseline]
        # A yard that leaves the attention list and comes back is a new event.
        baseline &= current
        if new:
            print(f"WAKE: {len(new)} yard(s) changed")
            print(render({"yards": new, "note": None}, quiet_for=quiet_for,
                         attention_only=True))
            if send:
                print(notify_wake(new, time.time()))
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true",
                        help="machine output, the same data the table renders")
    parser.add_argument("--attention", action="store_true",
                        help="print only the yards a human should look at")
    parser.add_argument("--quiet-for", type=float,
                        default=DEFAULT_QUIET_MINUTES,
                        help=f"minutes of silence before a yard with nothing "
                             f"written down reads as stalled "
                             f"(default {DEFAULT_QUIET_MINUTES})")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 when any yard needs attention")
    parser.add_argument("--wait", action="store_true",
                        help="sleep until a yard newly needs attention, then "
                             "exit; run it in the background")
    parser.add_argument("--interval", type=float, default=60.0,
                        help="seconds between sweeps while waiting (default 60)")
    parser.add_argument("--max", type=float, default=3600.0, dest="max_seconds",
                        help="seconds before the wait ends anyway (default 3600)")
    parser.add_argument("--notify", action="store_true",
                        help=f"with --wait, send the wake to {TARGET_ENV_VAR}")
    args = parser.parse_args()

    if args.notify and not args.wait:
        parser.error("--notify only applies to --wait")
    if args.wait:
        if args.interval <= 0 or args.max_seconds <= 0:
            parser.error("--interval and --max must be positive")
        return wait(quiet_for=args.quiet_for, interval=args.interval,
                    max_seconds=args.max_seconds, send=args.notify)

    result = sweep(quiet_for=args.quiet_for)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render(result, quiet_for=args.quiet_for,
                     attention_only=args.attention))

    if result["note"]:
        return 2
    if args.strict and needs_attention(result):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
