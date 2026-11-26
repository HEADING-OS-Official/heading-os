#!/usr/bin/env python3
"""Relay one of the two bare M030.1 words into a pane, never into typed text.

Usage:

    .venv/bin/python scripts/herdr-relay.py <yard pane> "commit"
    .venv/bin/python scripts/herdr-relay.py <helm pane> "commit, merge and delete w7F"
    .venv/bin/python scripts/herdr-relay.py <pane> "commit" --timeout 600

`<pane>` is a Herdr TUI pane id (`w37:p1`) or a herdr-core terminal (`t4`);
`herdr_agent.backend_for` picks the version from its form.

The only sanctioned way HELM relays those words (HOS-296). A bare
`herdr agent prompt <pane> <text>` types into the pane whatever
is already in its input line: on 2026-11-01, twice, the relayed merge sentence
joined text the operator was typing in HELM's pane and had not sent.

So this script sends only when BOTH hold, re-checked every 5 seconds:

* the pane's `agent_status` is `idle` or `done` (`herdr_agent.pane_status`;
  under herdr-core a finished Claude turn stays `done`, MEASURED 2026-11-18);
* its input line is empty (`herdr_agent.read_input_line`). An input line that cannot be read counts as NOT
  empty: fail closed.

While either fails it waits, printing one line each time the reason changes,
up to `--timeout` seconds (default 1800). When the input line has been
unreadable for 120 s without a break (HOS-388: on 2026-11-19 one unchanged
reason printed once and the relay then sat silent until the operator typed),
it does three things once per run and keeps waiting: it saves the visible
screen to a `relay-unreadable-<pane>-<UTC stamp>` text file in the checkout
scratch, prints one line saying so, and sends one notice to the operator's own
Telegram sink. It still never types while that line cannot be read. The typed text itself is never
printed. A small window remains between the last check and the send; the
check runs immediately before it.

It refuses any text other than `commit` or `commit, merge and delete <workspace
id>`, so it can never carry a brief; briefs stay `scripts/herdr-brief.py`'s.
After sending, delivery is confirmed in the addressed session's own transcript
by `herdr-brief.py`'s `verify_delivery`, limited to the bytes written after the
send, because the same bare word already sits in older records. Under the TUI it
is limited as well to the transcript of the session herdr reports for the pane;
when herdr reports none, nothing is sent. herdr-core reports no session id, so
there the relay first requires `hc get` to show `claude-code` on the terminal
and a git checkout under its current folder (else exit 5, nothing sent), and
the check reads every transcript of that checkout. Accepted risk: under
herdr-core a second Claude session in the same checkout could satisfy the
delivery check; the workspace runs one session per checkout (HELM and each
yard).

HELM ONLY: a session carrying `HEADING_OS_YARD` is refused before anything
else is read, and `require_main_clone` refuses any other checkout after the
text check.

THE RELAY JOURNAL (HOS-380), herdr-core only. Under herdr-core the relay
types its word with `hc keys` (HOS-385), so it arrives untagged, as under the
TUI; the journal is still written, and the release gate's envelope path stays
for any enveloped word. Immediately before
the send this script appends `{"id", "at", "sender", "target", "text"}` to
`.claude/.relay-journal.jsonl` in HELM's main clone, and right after it
`{"id", "outcome": "sent" | "failed"}`; `check_release_gate` accepts an
enveloped word only against a `sent` entry. No sender (`HC_TERMINAL_ID`), no
target address from `hc get`, or an append that fails or writes short: exit 5,
nothing sent. The delivery token is the plain record under both versions.

Into HELM's OWN pane (the merge sentence), run it in the background and end the
turn: while HELM waits on it in the foreground its pane is `working`, so the
relay would only time out. NOT measured, under either version: the status
reported for a Claude Code pane whose turn ended with a background shell still
running; if it is not `idle` or `done`, this relay times out (exit 6) and sends
nothing.

Exit codes: 0 sent and verified; 2 refused text or not HELM (under herdr-core
also a text holding uppercase or `_`, which `hc keys` cannot type); 3 herdr could not
send; 4 delivery not verified; 5 no checkout or transcript directory for the
pane, no session id (TUI) or not a Claude session in a checkout (herdr-core),
or (herdr-core) no sender, no target address or no journal entry written;
6 timed out waiting; 7 the pane is gone.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.utils import herdr_agent as HA  # noqa: E402
from scripts.utils.clone_guard import main_clone_path, require_main_clone  # noqa: E402
from scripts.utils.colors import GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils import telegram_notify  # noqa: E402
from scripts.utils.data_path import checkout_scratch_dir  # noqa: E402
from scripts.utils.paths import load_env  # noqa: E402

COMMIT_WORD = "commit"
MERGE_SENTENCE = re.compile(r"commit, merge and delete [A-Za-z0-9][A-Za-z0-9_-]*")
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PANE_PASSES = ("idle", "done")
POLL_S = 5
DEFAULT_TIMEOUT_S = 1800
UNREADABLE_NOTICE_S = 120
UNREADABLE_PREFIX = "input line unreadable"
NOTICE_TARGET_VARS = ("CHECKPOINT_TELEGRAM_TARGET", "OPS_RADAR_TELEGRAM_TARGET",
                      "ODIN_CADENCE_TELEGRAM_TARGET")
JOURNAL_REL = Path(".claude") / ".relay-journal.jsonl"


def _load_brief():
    """`scripts/herdr-brief.py` by path: its send and delivery check, not copies."""
    spec = importlib.util.spec_from_file_location("herdr_brief", ROOT / "scripts" / "herdr-brief.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def allowed(text: str) -> bool:
    return text == COMMIT_WORD or MERGE_SENTENCE.fullmatch(text) is not None


def journal_path() -> Path:
    """HELM's relay journal, in the main clone of the relay's OWN checkout, never the cwd."""
    return main_clone_path(ROOT) / JOURNAL_REL


def append_journal(record: dict) -> None:
    """One JSON line by one `os.write` on an O_APPEND file, as `_record_release` does.

    Raises OSError when the file cannot be written or the write comes back
    short; the caller decides what that means.
    """
    blob = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(journal_path(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        written = os.write(fd, blob)
    finally:
        os.close(fd)
    if written != len(blob):
        raise OSError(f"relay journal wrote {written} of {len(blob)} bytes")


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


def notice_unreadable(pane: str) -> None:
    """Snapshot the screen, say so on stdout, and send one self-notification.

    Every failure is reported in red on stderr and the wait goes on.
    """
    snapshot = None
    try:
        text = HA.screen_text(pane)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = checkout_scratch_dir() / f"relay-unreadable-{pane}-{stamp}.txt"
        path.write_text(text, encoding="utf-8")
        snapshot = path
    except Exception as exc:  # noqa: BLE001 - a missed snapshot never stops the wait
        print(f"{RED}herdr-relay: the screen snapshot could not be saved "
              f"({type(exc).__name__}: {exc}).{RESET}", file=sys.stderr)
    print(f"{YELLOW}herdr-relay: still waiting after 120 s: the input line of {pane} "
          f"cannot be seen. Snapshot: {snapshot if snapshot else 'not saved'}.{RESET}",
          flush=True)
    try:
        load_env()
    except Exception as exc:  # noqa: BLE001 - the target may already be in the environment
        print(f"{RED}herdr-relay: .env not loaded for the notice ({exc}).{RESET}",
              file=sys.stderr)
    target = next((os.environ[var].strip() for var in NOTICE_TARGET_VARS
                   if os.environ.get(var, "").strip()), "")
    if not target:
        print(f"{RED}herdr-relay: no Telegram target is set, so no notice was sent.{RESET}",
              file=sys.stderr)
        return
    message = (f"HEADING OS: the relay has waited 2 minutes to type a word into {pane} and "
               f"cannot see its input line. It keeps waiting; a new message in that terminal "
               f"may redraw the screen (not measured).")
    try:
        sent = telegram_notify.notify(target, message)
    except Exception as exc:  # noqa: BLE001 - notify reaches load_env, which can raise
        print(f"{RED}herdr-relay: the Telegram notice failed "
              f"({type(exc).__name__}: {exc}).{RESET}", file=sys.stderr)
        return
    if not sent:
        print(f"{RED}herdr-relay: the Telegram notice was not sent (see the telegram_notify "
              f"log line).{RESET}", file=sys.stderr)


def wait_until_ready(pane: str, timeout_s: float) -> int:
    """0 when ready, 6 on timeout, 7 when the pane is gone.

    Prints a waiting line each time the reason changes. When every reason for
    UNREADABLE_NOTICE_S without a break has been an unreadable input line (the
    category, whatever the exception text), it calls `notice_unreadable` once
    per run: a screen snapshot in the checkout scratch, one stdout line, one
    self-notification. Any other reason resets the count. It never types then.
    """
    deadline = time.monotonic() + timeout_s
    last = None
    unreadable_since = None
    notified = False
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
        now = time.monotonic()
        if reason.startswith(UNREADABLE_PREFIX):
            if unreadable_since is None:
                unreadable_since = now
            if not notified and now - unreadable_since >= UNREADABLE_NOTICE_S:
                notified = True
                notice_unreadable(pane)
        else:
            unreadable_since = None
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
    parser.add_argument("pane", help=("Herdr TUI pane id (e.g. w37:p1) or herdr-core "
                                      "terminal (e.g. t4)"))
    parser.add_argument("text", help="`commit` or `commit, merge and delete <workspace id>`")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help=f"seconds to wait for the pane (default {DEFAULT_TIMEOUT_S})")
    parser.add_argument("--projects-root", default=None,
                        help="where Claude Code writes transcripts (default ~/.claude/projects)")
    args = parser.parse_args()

    if os.environ.get("HEADING_OS_YARD"):
        print(f"{RED}herdr-relay: REFUSED. This session carries HEADING_OS_YARD; only HELM "
              f"relays. Nothing sent.{RESET}", file=sys.stderr)
        return 2

    if not allowed(args.text):
        print(f"{RED}herdr-relay: REFUSED. Only `{COMMIT_WORD}` or `commit, merge and delete "
              f"<workspace id>` is relayed; a brief goes through scripts/herdr-brief.py.{RESET}",
              file=sys.stderr)
        return 2

    require_main_clone(__file__)
    brief = _load_brief()
    core = HA.backend_for(args.pane) == "core"
    if core:
        bad = next((ch for ch in args.text if ch not in HA.TYPEABLE_CHARS), None)
        if bad is not None:
            print(f"{RED}herdr-relay: REFUSED. herdr-core cannot type {bad!r}; nothing "
                  f"sent.{RESET}", file=sys.stderr)
            return 2

    checkout = brief.checkout_for_pane(args.pane, brief.herdr_workspaces())
    if core and (checkout is None or not HA.is_claude_terminal(args.pane)):
        print(f"{RED}herdr-relay: {args.pane} is not a Claude session in a checkout "
              f"(herdr-core). Nothing sent.{RESET}", file=sys.stderr)
        return 5
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

    # Sender and address are checked BEFORE the wait below: a relay that cannot
    # journal refuses at once rather than after up to `--timeout` seconds.
    sender = address = None
    if core:
        sender = os.environ.get("HC_TERMINAL_ID")
        if not sender:
            print(f"{RED}herdr-relay: REFUSED. HC_TERMINAL_ID is not set, so the relay "
                  f"journal cannot name the sender. Nothing sent.{RESET}", file=sys.stderr)
            return 5
        try:
            address = HA._hc_get(args.pane).get("address")
        except (HA.PaneGone, HA.HerdrUnavailable) as exc:
            print(f"{RED}herdr-relay: REFUSED. hc get {args.pane} failed "
                  f"({type(exc).__name__}: {exc}). Nothing sent.{RESET}", file=sys.stderr)
            return 5
        if not isinstance(address, str) or not address:
            print(f"{RED}herdr-relay: REFUSED. hc get {args.pane} carries no address. "
                  f"Nothing sent.{RESET}", file=sys.stderr)
            return 5

    # HOS-408: the last readiness check and the send happen under the pane's
    # shared input lock, which the Telegram bridge and herdr-brief.py take too. Held from here until the process exits, which
    # releases it; a lock another typist holds means wait and check again.
    lock = HA.input_lock(args.pane)
    deadline = time.monotonic() + args.timeout
    while True:
        status = wait_until_ready(args.pane, max(1.0, deadline - time.monotonic()))
        if status != 0:
            return status
        if lock.__enter__():
            try:
                again = not_ready(args.pane)
            except HA.PaneGone:
                print(f"{RED}herdr-relay: pane {args.pane} is gone. Nothing sent.{RESET}",
                      file=sys.stderr)
                return 7
            if again is None:
                break
            lock.__exit__()
        if time.monotonic() >= deadline:
            print(f"{RED}herdr-relay: the pane's input lock or readiness never held together "
                  f"within {int(args.timeout)}s. Nothing sent.{RESET}", file=sys.stderr)
            return 6
        time.sleep(POLL_S)

    entry = None
    if core:
        entry = {"id": uuid.uuid4().hex,
                 "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                 "sender": sender, "target": address, "text": args.text}
        # `hc keys` types the word untagged (HOS-385, MEASURED 2026-11-19): the
        # user record is the plain one, as under the TUI.
        token = '"content":' + json.dumps(args.text)
    else:
        # The user record carries the text as `"content":"<text>"` (MEASURED
        # 2026-11-01 in this yard's own transcript).
        token = f'"content":{json.dumps(args.text)}'
    root = projects_root or target.parent
    # Confirm in the ADDRESSED session's transcript only: another session sharing
    # the checkout can receive the same bare word (HOS-296 review).
    session = None if core else pane_session(args.pane)
    if session is None and not core:
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
    if entry is not None:
        try:
            append_journal(entry)
        except OSError as exc:
            print(f"{RED}herdr-relay: REFUSED. The relay journal could not be written "
                  f"({exc}). Nothing sent.{RESET}", file=sys.stderr)
            return 5
    if core:
        try:
            result = HA.type_text(args.pane, args.text)
        except (HA.HerdrUnavailable, ValueError) as exc:
            print(f"{RED}herdr-relay: {exc}. Nothing is confirmed sent.{RESET}",
                  file=sys.stderr)
            status = 3
        else:
            if result:
                print(json.dumps(result))
            print(f"{GREEN}Sent to {args.pane}.{RESET}")
            status = 0
    else:
        status = brief.send(args.pane, args.text)
    if entry is not None:
        try:
            append_journal({"id": entry["id"],
                            "outcome": "sent" if status == 0 else "failed"})
        except OSError as exc:
            # The send already happened. An entry with no outcome line refuses at
            # the gate, the safe direction; reported, never swallowed.
            print(f"{RED}herdr-relay: the journal outcome line could not be written "
                  f"({exc}); the release gate will refuse this relay.{RESET}",
                  file=sys.stderr)
    if status != 0:
        return status
    return brief.verify_delivery(target, token, root, since=since, session=session)


if __name__ == "__main__":
    sys.exit(main())
