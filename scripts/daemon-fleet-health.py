#!/usr/bin/env python3
"""Health reader for the daemons of THIS workspace.

Reads the per-daemon liveness beats written by `daemon_heartbeat.beat`, which
`sentinel.py`, `sync-exchange-daemon.py` and every other daemon call, and
prints one row per daemon. A daemon that beats on another host, or has never
beaten here, has no row: expected-but-missing is `scripts/daemon-watchdog.py`'s.

Scope: this workspace on this machine only. Unlike `scripts/daemon-watchdog.py`,
this reader routes no alert; it only reads.

Usage:
  python scripts/daemon-fleet-health.py
  python scripts/daemon-fleet-health.py --json       # machine-readable
  python scripts/daemon-fleet-health.py --stale 120  # custom stale threshold
  python scripts/daemon-fleet-health.py --exit-zero  # always exit 0

Exit status: 0 every beat ok, 1 any stale, 2 any in error or no beat at all.
The first line is the verdict, e.g. "Fleet healthy: 3 daemons ok (this machine
only)."

Status conventions:
- ok: beat within --stale seconds
- stale: beat older than --stale seconds (default 120)
- error: beat unreadable, not an object, or without a readable timestamp

A CLI, run by hand.

A fleet report that died on one undecodable or malformed beat is the defect
the handlers below record. Since the 2026-09-21 test-suite split nothing goes
red if it returns, so they rest on review.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.colors import BOLD, CYAN, GRAY, GREEN, RED, RESET, YELLOW
from scripts.utils import daemon_heartbeat
from scripts.utils.data_path import DataPathError
from scripts.utils.clone_guard import require_main_clone

STALE_DEFAULT_S = 120

def _classify_beat(record: dict, stale_threshold_s: int) -> str:
    """Classify ONE per-daemon liveness beat (R14): ok | stale | error.

    Per-daemon beats (``daemon_heartbeat.heartbeats_dir()/<name>.json``, written by
    ``daemon_heartbeat.beat``) carry only liveness, so the one dimension is age.
    A record that fails to parse or has no timestamp is ``error``. Absent
    daemons are NOT counted here (the watchdog owns expected-but-missing); this
    reconciles only over beats that are PRESENT.
    """
    if record.get("status") == "error":
        return "error"
    last_hb = record.get("last_heartbeat")
    if not last_hb:
        return "error"
    # TypeError too. A heartbeat file can hold VALID JSON with a non-string
    # timestamp — `{"last_heartbeat": 123}` — and `fromisoformat` raises
    # TypeError on that, not ValueError. One malformed beat then took down the
    # whole grid, the JSON output and the exit code with a traceback, instead
    # of classifying that one daemon as `error`.
    try:
        dt = datetime.fromisoformat(last_hb)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
    except (TypeError, ValueError):
        return "error"
    return "stale" if age > stale_threshold_s else "ok"


def _collect_daemon_beats() -> list[dict]:
    """Read the per-daemon liveness beats, in dir-listing order. Returns []
    when the heartbeat store is absent.

    The beats are in the data repository since 2026-09-25 (HOS-5 section 7).
    """
    try:
        beats_dir = daemon_heartbeat.heartbeats_dir()
    except DataPathError as e:
        return [{"daemon": "*", "status": "error",
                 "detail": f"no heartbeat store on this tree: {e}"}]
    if not beats_dir.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(beats_dir.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as e:
            # UnicodeError too. A per-daemon beat is written by a live daemon
            # on this host, so a torn write is ordinary, and
            # `read_text(encoding="utf-8")` raises UnicodeDecodeError, a
            # ValueError that neither of the other clauses catches. It killed
            # the loop in `main`, so one corrupt beat took down the whole
            # report.
            out.append({"daemon": path.stem, "status": "error", "detail": f"parse failed: {e}"})
            continue
        # A beat that parses to something other than an object is still a beat
        # this function must DESCRIBE. `rec.get` raised AttributeError on a bare
        # `null`, `[]`, a string or a number, past the same handler.
        if not isinstance(rec, dict):
            out.append({"daemon": path.stem, "status": "error",
                        "detail": f"beat is {type(rec).__name__}, not an object"})
            continue
        rec["daemon"] = rec.get("daemon") or path.stem
        out.append(rec)
    return out


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    return f"{int(seconds / 3600)}h"


def _beat_age(beat: dict) -> str:
    """The beat's age for display, "-" with no timestamp, "?" unreadable."""
    if not beat.get("last_heartbeat"):
        return "-"
    try:
        dt = datetime.fromisoformat(beat["last_heartbeat"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return _format_age((datetime.now(timezone.utc) - dt).total_seconds())
    except (TypeError, ValueError):
        # TypeError too: a beat can hold valid JSON with a non-string
        # timestamp, which `_classify_beat` already reports as `error`.
        return "?"


def _verdict(statuses: list[str]) -> tuple[str, str]:
    """One-line verdict + colour, at the top of the grid so a grep pipeline
    sees the answer without parsing the table. The WORST status wins, and no
    beat at all is broken rather than healthy: this tool exists to say which
    daemons are down, and silence is not evidence that none are.

    "This machine only", because a separate host running its own daemons has no
    beat on this filesystem.
    """
    if not statuses:
        return ("Fleet broken: no daemon heartbeat found (this machine only).", RED)
    counts: dict[str, int] = {}
    for s in statuses:
        counts[s] = counts.get(s, 0) + 1

    def _n(status: str) -> str:
        n = counts.get(status, 0)
        return f"{n} daemon" + ("s" if n != 1 else "")

    if counts.get("error"):
        return (f"Fleet broken: {_n('error')} in error (this machine only).", RED)
    if counts.get("stale"):
        return (f"Fleet drift: {_n('stale')} stale (this machine only).", YELLOW)
    return (f"Fleet healthy: {_n('ok')} ok (this machine only).", GREEN)


def _print_grid(beats: list[dict], stale_threshold_s: int) -> None:
    color_for = {"ok": GREEN, "stale": YELLOW, "error": RED}
    statuses = [_classify_beat(b, stale_threshold_s) for b in beats]
    verdict_text, verdict_color = _verdict(statuses)
    print(f"{BOLD}{verdict_color}{verdict_text}{RESET}")
    print(f"{GRAY}stale threshold {stale_threshold_s}s{RESET}")
    print()
    print(f"  {'DAEMON':<20} {'STATUS':<10} {'AGE':<8}")
    print(f"  {'-' * 20} {'-' * 10} {'-' * 8}")
    counts: dict[str, int] = {}
    for beat, s in zip(beats, statuses, strict=True):
        counts[s] = counts.get(s, 0) + 1
        col = color_for.get(s, RESET)
        print(f"  {str(beat.get('daemon', '?')):<20} {col}{s:<10}{RESET} {_beat_age(beat):<8}")
        detail = beat.get("detail")
        if s == "error" and isinstance(detail, str) and detail.strip():
            print(f"      {GRAY}-> {detail.strip().splitlines()[0][:120]}{RESET}")
    print()
    summary_bits = [f"{color_for.get(k, RESET)}{k}: {v}{RESET}" for k, v in sorted(counts.items())]
    print("  " + "  ".join(summary_bits) if summary_bits else "  no daemons")


def _classify_fleet_exit_code(statuses: list[str]) -> int:
    """Map fleet posture to a Unix-friendly exit code for monitoring.

    - 0: every present beat ok
    - 1: any beat stale
    - 2: any beat in error, or no beat at all

    So `daemon-fleet-health.py || alert` works.
    """
    if not statuses or "error" in statuses:
        return 2
    if "stale" in statuses:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    require_main_clone(__file__)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit a JSON report instead of the grid")
    parser.add_argument("--stale", type=int, default=STALE_DEFAULT_S,
                        help=f"seconds before a heartbeat is considered stale (default {STALE_DEFAULT_S})")
    parser.add_argument("--exit-zero", action="store_true",
                        help="always exit 0 (useful for CEO interactive runs; default returns 0/1/2)")
    args = parser.parse_args(argv)

    beats = _collect_daemon_beats()
    statuses = [_classify_beat(b, args.stale) for b in beats]
    if args.json:
        verdict_text, _ = _verdict(statuses)
        out = {
            "verdict": verdict_text,
            "stale_threshold_s": args.stale,
            "daemons": [{**b, "classified": s} for b, s in zip(beats, statuses, strict=True)],
        }
        print(json.dumps(out, indent=2))
    else:
        _print_grid(beats, args.stale)

    if args.exit_zero:
        return 0
    return _classify_fleet_exit_code(statuses)


if __name__ == "__main__":
    sys.exit(main())
