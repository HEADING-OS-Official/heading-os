#!/usr/bin/env python3
"""Retire memory file(s) from ALL stores (canonical + native) so the delete sticks.

Usage:
    python scripts/retire-memory.py NAME [NAME ...] [--merged-into SURVIVOR]
    python scripts/retire-memory.py --trace-only [--merged-into SURVIVOR] [NAME ...]

Every run appends one row, `{ts, merged, retired}`, to `consolidation-trace.jsonl`
under the registry name `dream-reports`: `retired` lists the names whose file was
actually removed, `merged` the survivors given with `--merged-into`. The trace is
written here, by a tracked file, because the overlay write guard refuses a write
from code with no file behind it, which is what the `python - <<'PY'` step in
`/dream` used to be (MEASURED 2026-10-28: refused with OverlayWriteRefused).
`--trace-only` records without deleting anything, for a `/dream` pass that
retired nothing.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.memory_stores import retire_memory
from scripts.utils.colors import GREEN, GRAY, RED, RESET
from scripts.utils.data_path import data_path
from scripts.record_readers._jsonl import append_jsonl


def main():
    ap = argparse.ArgumentParser(description="Retire memory files from all stores")
    ap.add_argument("names", nargs="*", help="memory file name(s), e.g. feedback_foo.md")
    ap.add_argument("--merged-into", action="append", default=[], metavar="SURVIVOR",
                    help="survivor file of a merge; recorded in the trace (repeatable)")
    ap.add_argument("--trace-only", action="store_true",
                    help="append the trace row and retire nothing")
    args = ap.parse_args()
    if not args.names and not args.trace_only:
        ap.error("name at least one memory file, or pass --trace-only")
    exit_code = 0
    retired = list(args.names) if args.trace_only else []
    for name in ([] if args.trace_only else args.names):
        removed, failed = retire_memory(name)
        if failed:
            # A delete that did not stick is not a retirement. Reported here and
            # in the exit code, because the operator's next step is to fix the
            # permission and run it again, not to assume the file is gone.
            print(f"{RED}NOT retired{RESET} {name}: {len(removed)} store(s) cleared, "
                  f"{len(failed)} refused")
            for path, why in failed:
                print(f"    {path}: {why}")
            exit_code = 1
        elif removed:
            retired.append(name)
            print(f"{GREEN}retired{RESET} {name}: {len(removed)} store(s)")
        else:
            print(f"{GRAY}not found{RESET} {name}")
    trace = data_path("dream-reports") / "consolidation-trace.jsonl"
    try:
        append_jsonl(trace, {"ts": time.time(), "merged": args.merged_into, "retired": retired})
    except OSError as exc:
        print(f"{RED}trace NOT written{RESET} {trace}: {exc}")
        return 1
    print(f"{GRAY}trace{RESET} {trace}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
