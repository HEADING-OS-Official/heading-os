#!/usr/bin/env python3
"""Send one file to the operator's own Telegram sink, as a document.

Thin CLI over `telegram_notify.notify_file`, so the file goes through the same
self-only allowlist as every text notification and through its refusals: a
relative path, a non-file, anything over the Bot API's 50 MB upload limit, and
a record whose frontmatter forbids it leaving the machine.

The target is ODIN_CADENCE_TELEGRAM_TARGET, read from the environment after
`.env` is loaded. This script never prints the target or the token; the module's own log
lines go to stderr.

Usage:
    .venv/bin/python scripts/notify-file.py /abs/path/report.html
    .venv/bin/python scripts/notify-file.py /abs/path/report.html --caption "weekly report"

Exit 0 when the file was sent, 1 otherwise.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.paths import load_env  # noqa: E402
from scripts.utils import telegram_notify  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send one file to the operator's own Telegram sink.")
    parser.add_argument("path", help="absolute path of the file to send")
    parser.add_argument("--caption", default="", help="plain-text caption (cut to 1024 characters)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    load_env()
    target = os.environ.get("ODIN_CADENCE_TELEGRAM_TARGET", "").strip()

    if telegram_notify.notify_file(target, args.path, args.caption):
        print(f"sent {Path(args.path).name}")
        return 0
    print("not sent: see the log line above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
