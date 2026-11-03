#!/usr/bin/env python3
"""Relay one of the two bare M030.1 words into a pane, never into typed text.

Usage:

    .venv/bin/python scripts/herdr-relay.py <yard pane> "commit"
    .venv/bin/python scripts/herdr-relay.py <helm pane> "commit, merge and delete w7F"
    .venv/bin/python scripts/herdr-relay.py <pane> "commit" --timeout 600

The only sanctioned way HELM relays those words (HOS-296). A bare
`herdr agent prompt <pane> <text>` types into the pane whatever is already in
its input line: on 2026-11-01, twice, the relayed merge sentence joined text
the operator was typing in HELM's pane and had not sent.

So this script sends only when BOTH hold, re-checked every 5 seconds:

* the pane's `agent_status` is `idle` or `done` (`herdr_agent.pane_status`);
* its input line is empty (`herdr_agent.read_input_line`). An input line that cannot be read counts as NOT
  empty: fail closed.

While either fails it waits, printing one line each time the reason changes,
up to `--timeout` seconds (default 1800). The typed text itself is never
printed. A small window remains between the last check and the send; the
check runs immediately before it.

It refuses any text other than `commit` or `commit, merge and delete <workspace
id>`, so it can never carry a brief; briefs stay `scripts/herdr-brief.py`'s.
After sending, delivery is confirmed in the addressed session's own transcript
by `herdr-brief.py`'s `verify_delivery`, limited to the bytes written after the
send, because the same bare word already sits in older records, and to the
transcript of the session herdr reports for the pane; when herdr reports none,
nothing is sent.

HELM ONLY: `require_main_clone` refuses elsewhere, after the text check.

Into HELM's OWN pane (the merge sentence), run it in the background and end the
turn: while HELM waits on it in the foreground its pane is `working`, so the
relay would only time out. NOT measured: the `agent_status` herdr reports for a
Claude Code pane whose turn ended with a background shell still running; if it
is not `idle` or `done`, this relay times out (exit 6) and sends nothing.

Exit codes: 0 sent and verified; 2 refused text or not HELM; 3 herdr could not
send; 4 delivery not verified; 5 no checkout or transcript directory for the
pane; 6 timed out waiting; 7 the pane is gone.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.utils import herdr_agent as HA  # noqa: E402
from scripts.utils.clone_guard import require_main_clone  # noqa: E402
from scripts.utils.colors import RED, RESET, YELLOW  # noqa: E402

COMMIT_WORD = "commit"
MERGE_SENTENCE = re.compile(r"commit, merge and delete [A-Za-z0-9][A-Za-z0-9_-]*")
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PANE_PASSES = ("idle", "done")
POLL_S = 5
DEFAULT_TIMEOUT_S = 1800


def _load_brief():
    """`scripts/herdr-brief.py` by path: its send and delivery check, not copies."""
    spec = importlib.util.spec_from_file_location("herdr_brief", ROOT / "scripts" / "herdr-brief.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def allowed(text: str) -> bool:
    return text == COMMIT_WORD or MERGE_SENTENCE.fullmatch(text) is not None


def pane_session(pane: str) -> str | None:
    """The Claude session id herdr reports for `pane`, None when it cannot be told.

    From `herdr agent list`: each agent carries `pane_id` and
    `agent_session: {"kind": "id", "value": <uuid>}`, and the value names the
    session's transcript file (MEASURED 2026-11-01 on this yard's own pane).
    """
    try:
        found = HA.agents()
    except HA.HerdrUnavailable as exc:
        print(f"{RED}herdr-relay: agent list unreadable: {exc}{RESET}", file=sys.stderr)
        return None
    for agent in found:
        if agent.get("pane_id") != pane:
            continue
        session = agent.get("agent_session")
        if isinstance(session, dict) and session.get("kind") == "id":
            value = session.get("value")
            if isinstance(value, str) and SESSION_ID.fullmatch(value):
                return value
        return None
    return None


def not_ready(pane: str) -> str | None:
    """None when the pane may receive the word, else why not. Raises PaneGone."""
    try:
        status = HA.pane_status(pane)
    except HA.HerdrUnavailable as exc:
        return f"pane status unreadable: {exc}"
    if status not in PANE_PASSES:
        return f"pane {pane} is {status}"
    try:
        typed = HA.read_input_line(pane)
    except Exception as exc:  # any failure to read means "not known empty"
        return f"input line unreadable: {type(exc).__name__}: {exc}"
    if typed:
        return "text typed but not sent in the input line"
    return None


def wait_until_ready(pane: str, timeout_s: float) -> int:
    """0 when ready, 6 on timeout, 7 when the pane is gone."""
    deadline = time.monotonic() + timeout_s
    last = None
    while True:
        try:
            reason = not_ready(pane)
        except HA.PaneGone:
            print(f"{RED}herdr-relay: pane {pane} is gone. Nothing sent.{RESET}", file=sys.stderr)
            return 7
        if reason is None:
            return 0
        if reason != last:
            print(f"{YELLOW}waiting: {reason}{RESET}", flush=True)
            last = reason
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"{RED}herdr-relay: still not ready after {int(timeout_s)}s ({reason}). "
                  f"Nothing sent.{RESET}", file=sys.stderr)
            return 6
        time.sleep(min(POLL_S, remaining))


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="herdr-relay.py",
        description="Relay `commit` or `commit, merge and delete <workspace id>` into a pane "
                    "once its agent is idle and its input line is empty.")
    parser.add_argument("pane", help="HERDR pane id, e.g. w37:p1")
    parser.add_argument("text", help="`commit` or `commit, merge and delete <workspace id>`")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help=f"seconds to wait for the pane (default {DEFAULT_TIMEOUT_S})")
    parser.add_argument("--projects-root", default=None,
                        help="where Claude Code writes transcripts (default ~/.claude/projects)")
    args = parser.parse_args()

    if not allowed(args.text):
        print(f"{RED}herdr-relay: REFUSED. Only `{COMMIT_WORD}` or `commit, merge and delete "
              f"<workspace id>` is relayed; a brief goes through scripts/herdr-brief.py.{RESET}",
              file=sys.stderr)
        return 2

    require_main_clone(__file__)
    brief = _load_brief()

    checkout = brief.checkout_for_pane(args.pane, brief.herdr_workspaces())
    if checkout is None:
        print(f"{RED}herdr-relay: REFUSED. herdr reports no workspace owning {args.pane}, "
              f"so delivery cannot be verified.{RESET}", file=sys.stderr)
        return 5
    projects_root = Path(args.projects_root) if args.projects_root else None
    target = brief.target_dir(checkout, projects_root)
    if target is None:
        print(f"{RED}herdr-relay: REFUSED. No transcript directory for {checkout} on this "
              f"platform, so delivery cannot be verified.{RESET}", file=sys.stderr)
        return 5

    status = wait_until_ready(args.pane, args.timeout)
    if status != 0:
        return status

    # The user record carries the text as `"content":"<text>"` (MEASURED
    # 2026-11-01 in this yard's own transcript).
    token = f'"content":{json.dumps(args.text)}'
    root = projects_root or target.parent
    # Confirm in the ADDRESSED session's transcript only: another session sharing
    # the checkout can receive the same bare word (HOS-296 review).
    session = pane_session(args.pane)
    if session is None:
        print(f"{RED}herdr-relay: REFUSED. herdr reports no session id for {args.pane}, so "
              f"delivery could not be confirmed in that session's transcript. Nothing "
              f"sent.{RESET}", file=sys.stderr)
        return 5
    since = brief.transcript_sizes(root)
    # A transcript missing from `since` would be read from byte 0, where an older
    # record with the same word proves nothing: refuse rather than verify falsely.
    unsized = [p for p in brief.transcripts(target) if p not in since]
    if unsized:
        print(f"{RED}herdr-relay: REFUSED. Could not size {len(unsized)} transcript(s) in "
              f"{target.name} before sending, so delivery could not be told from an older "
              f"record. Nothing sent.{RESET}", file=sys.stderr)
        return 5
    status = brief.send(args.pane, args.text)
    if status != 0:
        return status
    return brief.verify_delivery(target, token, root, since=since, session=session)


if __name__ == "__main__":
    sys.exit(main())
