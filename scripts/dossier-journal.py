#!/usr/bin/env python3
"""Post or flush [dossier] records, the task journal in Lific.

The records themselves are owned by `scripts/utils/dossier.py`. This is the
door for what no other script writes: the `close` record a yard posts beside
its closing comment, a `relink` or `unlink` correction, the /prime flush.

Usage:
  .venv/bin/python scripts/dossier-journal.py post --issue HOS-9001 --event close --summary-file f --path <data-root-relative path>
  .venv/bin/python scripts/dossier-journal.py flush [--report-rejected]
  .venv/bin/python scripts/dossier-journal.py count

Exit codes: 0 always for post and count (a record that cannot be posted is
queued, never lost); flush exits 1 when records are still waiting.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils import checkpoint_paths as CP
from scripts.utils import dossier


def main() -> int:
    # LIFIC_BOT_USER lives in the checkout's .env; a detached flusher starts without it.
    dossier.load_engine_env()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("post", help="post one record to each named issue")
    p.add_argument("--issue", action="append", required=True)
    p.add_argument("--event", choices=dossier.EVENTS, required=True)
    p.add_argument("--kind", default="")
    p.add_argument("--summary-file", help="summary lines, one per line; - for stdin")
    p.add_argument("--path", required=True, help="data-root-relative path of the file")
    fl = sub.add_parser("flush", help="send what the outbox holds")
    fl.add_argument("--report-rejected", action="store_true",
                    help="add `rejected_new` (by reason) and `corrupt_new`, since the last report, "
                         "`systemic`, `stuck` and `dropped_new`, carried tasks dropped (/prime)")
    fl.add_argument("--deadline", type=float, metavar="SECONDS",
                    help="start no new send after this many seconds; the lock is released as usual")
    sub.add_parser("count", help="how many records wait in the outbox")
    args = parser.parse_args()
    if args.cmd == "post":
        bad = [i for i in args.issue if not dossier.TASK_RE.fullmatch(i)]
        if bad:
            # Lific answers a malformed id with BadRequest on every call; never queue one.
            print(f"dossier-journal: not an issue id: {', '.join(bad)}; nothing queued",
                  file=sys.stderr)
            return 2
        lines = []
        if args.summary_file:
            src = (sys.stdin.read() if args.summary_file == "-"
                   else Path(args.summary_file).read_text(encoding="utf-8"))
            lines = src.splitlines()[:3]
        print(json.dumps(dossier.post(args.issue, args.event, args.kind, CP.session_id(),
                                      lines, args.path), ensure_ascii=False))
        return 0
    if args.cmd == "flush":
        result = dossier.flush(deadline=args.deadline)
        if args.report_rejected:
            result["rejected_new"] = dossier.rejected_unseen()
            result["corrupt_new"] = dossier.corrupt_unseen()
            result["systemic"] = dossier.systemic_state()
            result["stuck"] = dossier.stuck_records()
            result["dropped_new"] = dossier.dropped_unseen()
        print(json.dumps(result))
        return 1 if result["left"] else 0
    print(dossier.outbox_count())
    return 0


if __name__ == "__main__":
    sys.exit(main())
