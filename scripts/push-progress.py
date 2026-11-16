#!/usr/bin/env python3
"""Print where the current or last commit-and-push run is (HOS-346).

Reads the record scripts/push-all.py writes (see scripts/utils/push_progress.py)
and prints the same one-line segment the Claude Code status line shows. Read
only; always exits 0.

Usage:
    python scripts/push-progress.py          # the segment, or "no push recorded"
    python scripts/push-progress.py --json   # the record, or null
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils import push_progress  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Show the progress of the current or last push-all run.")
    ap.add_argument("--json", action="store_true", help="print the raw record, or null")
    args = ap.parse_args()
    record = push_progress.read()
    if args.json:
        print(json.dumps(record, indent=2))
        return 0
    if record is None:
        print("no push recorded")
        return 0
    print(push_progress.render(record, time.time(), always=True) or "no push recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
