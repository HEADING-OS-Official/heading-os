#!/usr/bin/env python3
"""
herdr_agent.py - the narrow seam between this workspace and the HERDR terminal
manager.

It exists for one reason: Claude Code has NO in-process entry point for
`/compact`. In the 2.1.235 binary `compactConversation` is internal and
`requestCompact` is transcript-file garbage collection, neither reachable from a
hook. So the Stop hook (`.claude/hooks/checkpoint-offer.py`), which compacts or
clears a session once its handoff is saved, has to ask from OUTSIDE, by
submitting the literal text `/compact` or `/clear` to the terminal that hosts the
session. The harness then parses it exactly as it would parse the same characters
typed by hand.

That path is proven rather than assumed. Measured 2026-08-19 in a live session
(the session id is deliberately not recorded here: it identifies the operator's
machine and this file ships in the public engine, while the command and its
outcome are the whole of the evidence):

    herdr agent prompt w37:p1 "/compact"
    -> {"type": "agent_prompted", ... "agent_status": "working"}

and the transcript then gained one `compact_boundary` carrying
`trigger: "manual"`, `preTokens: 324190`, `postTokens: 10929`. A prompt submitted
while `agent_status` is `working` is QUEUED and runs when the current turn ends,
which is why the caller must submit and then let its turn finish. A hook that
blocks after submitting deadlocks its own request.

Self-identification needs no hardcoded pane. `herdr agent list` returns, per
agent, an `agent_session` object shaped `{"kind": "id", "value": "<uuid>"}`
beside `pane_id`, and that uuid is the Claude session id a hook already holds.

WHAT THIS MODULE DELIBERATELY IS NOT: a "send text to a terminal" utility.
`submit_command()` takes a pane and one of exactly two module constants,
`/compact` and `/clear`, and refuses every other string. Anything able to inject
arbitrary text into a live agent session is a capability this workspace should
not grow by accident, and a closed allowlist of literals removes the question
rather than answering it. `/clear` joined `/compact` on 2026-09-27, operator
decision, for `/clear-handover`: the Stop hook sends it only after the handoff
is verified on disk. Sending `/clear` this way is the same mechanism as
`/compact` and was NOT yet run live when it was added.

WHY THE CONFIRMATION OPTIONS ARE UNUSED: `herdr agent prompt` accepts `--wait`,
`--until <STATUS>` and `--timeout <MS>`. The seam uses none of them and infers
the queue from the returned `agent_status`. `--wait` without `--timeout` waits
indefinitely, which is disqualifying inside a Stop hook with a 90-second budget,
and `--wait` with a timeout buys a confirmation the caller cannot act on anyway -
by then it has already decided to let the turn end. Do not "improve" this.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# The checkout this module runs from, the tree the env fallback below compares a
# pane's cwd against: derived from this file, never from a constant.
_CHECKOUT = Path(__file__).resolve().parents[2]

# The only strings this module is allowed to submit. Callers name one of these
# constants; any other text is refused before HERDR is reached.
COMPACT_COMMAND = "/compact"
CLEAR_COMMAND = "/clear"
ALLOWED_COMMANDS = frozenset({COMPACT_COMMAND, CLEAR_COMMAND})

HERDR_BIN = "herdr"

# `agent list` walks every pane; `prompt` is a single socket call. The label
# calls sit inside a hook's grace period and must never eat it, so they get the
# tightest budget of the three.
LIST_TIMEOUT = 10
PROMPT_TIMEOUT = 10
LABEL_TIMEOUT = 2

# The longest name `herdr agent rename` accepts; see `label_text`.
LABEL_MAX_CHARS = 32


class HerdrUnavailable(RuntimeError):
    """HERDR could not be reached, or answered with something unparseable.

    Raised for the binary being absent, a timeout, a non-zero exit, and JSON that
    does not parse. Deliberately NOT raised when the session simply is not hosted
    by HERDR - that is an ordinary outcome and `resolve_pane` returns None for it.
    The distinction matters to every caller: "not hosted" is a fact worth
    reporting, "could not tell" is not the same fact, and conflating them is the
    defect `.claude/rules/scope-claims.md` exists to prevent.
    """


def _run(args: list[str], timeout: int) -> dict:
    """Run one herdr subcommand and return its parsed payload.

    List arguments only, never `shell=True`, per the global security policy.
    """
    if shutil.which(HERDR_BIN) is None:
        raise HerdrUnavailable(f"{HERDR_BIN} is not on PATH")
    try:
        proc = subprocess.run(
            [HERDR_BIN, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise HerdrUnavailable(
            f"{HERDR_BIN} {' '.join(args)} timed out after {timeout}s"
        ) from exc
    except OSError as exc:
        raise HerdrUnavailable(f"{HERDR_BIN} could not be executed: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        first = detail[0] if detail else "no output"
        raise HerdrUnavailable(
            f"{HERDR_BIN} {' '.join(args)} exited {proc.returncode}: {first}"
        )
    try:
        payload = json.loads(proc.stdout)
    except ValueError as exc:
        raise HerdrUnavailable(
            f"{HERDR_BIN} {' '.join(args)} returned unparseable output"
        ) from exc
    # `json.loads` answers with any JSON value; every caller expects a dict, so a
    # non-object answer raises HerdrUnavailable here rather than AttributeError
    # past every handler (2026-08-19).
    if not isinstance(payload, dict):
        raise HerdrUnavailable(
            f"{HERDR_BIN} {' '.join(args)} returned "
            f"{type(payload).__name__}, not an object"
        )
    return payload


def agents() -> list[dict]:
    """Every agent HERDR currently hosts, as raw records.

    Every shape this does not expect leaves as HerdrUnavailable, which is the
    one exception the callers handle. `_run` already converts UNPARSEABLE output
    and, since 2026-08-24, output that parses to something other than an object;
    the gap this closes is the layer below that - a `result` or an `agents` entry
    of the wrong type, which a future HERDR release can introduce without
    warning. That used to leave `payload.get` raising AttributeError straight
    through `_herdr_status` and out of the Stop hook, so a third-party format
    change cost the session its whole checkpoint system: no offer, no save, no
    countdown, exit 1, no output.

    A malformed ENTRY raises rather than being skipped. Dropping it would answer
    "HERDR does not host this session" when the truth is "the lookup could not be
    trusted", and `resolve_pane` exists to keep those two apart.
    """
    payload = _run(["agent", "list"], LIST_TIMEOUT)
    result = payload.get("result")
    if result is None:
        result = {}
    if not isinstance(result, dict):
        raise HerdrUnavailable(
            f"agent list result is {type(result).__name__}, not an object"
        )
    found = result.get("agents")
    if not isinstance(found, list):
        raise HerdrUnavailable("agent list carried no agents array")
    if not all(isinstance(agent, dict) for agent in found):
        raise HerdrUnavailable("agent list carried a malformed agent record")
    return found


def resolve_pane(session_id: str) -> str | None:
    """The pane hosting this Claude session, or None when HERDR does not host it.

    None is a normal answer, not a failure. Callers degrade to whatever they do
    without HERDR; they must not report it as an error, and they must not report
    it as "not hosted" when the lookup itself failed - that case raises
    HerdrUnavailable instead, and the two are different sentences to an operator.

    A record that MATCHES this session and then carries an unusable `pane_id`
    is the second kind, not the first. It used to be the first: the match fell
    through to `return None`, which every caller reads as "HERDR does not host
    this session". `_herdr_status` (removed 2026-10-14 with the driven
    compaction) then PERSISTED `compact_host="not-hosted"` and short-circuited on that cached value
    for the rest of the session, so one malformed record from a HERDR release
    would have turned driven compaction off and told the operator it had never
    been available. `agents()` already raises on a malformed agent record for
    exactly this reason; this is the same judgement one field further in.
    """
    return resolve_pane_how(session_id)[0]


def resolve_pane_how(session_id: str) -> tuple[str | None, str]:
    """`(pane, how)`: the pane as `resolve_pane` answers it, and how it was found.

    `how` is `session id` or `pane env` when a pane was found, otherwise a
    sentence saying why none was. Raises HerdrUnavailable as `resolve_pane` does.

    The session id is tried first. After `/login` mid-session Claude Code goes on
    under a new session id while herdr keeps the old one for the same pane
    (MEASURED 2026-10-24; after an ordinary /clear herdr follows the new id), so
    no record matches and the session read as not hosted (HOS-258). The fallback
    is the pane herdr itself put in this process's environment, `HERDR_PANE_ID`,
    accepted only when herdr lists a claude agent on exactly that pane whose cwd
    is this checkout.
    """
    if not session_id:
        return None, "no session id"
    found = agents()
    for agent in found:
        session = agent.get("agent_session")
        # `or {}` alone covered a missing key and left a present-but-wrong one
        # crashing, which is the same defect `agents()` above just closed one
        # level up. A record that does not describe a session is not this
        # session, so skipping it is the honest reading here.
        if not isinstance(session, dict):
            continue
        if session.get("kind") == "id" and session.get("value") == session_id:
            pane = agent.get("pane_id")
            if not isinstance(pane, str) or not pane:
                raise HerdrUnavailable(
                    f"the agent record matching this session carries pane_id "
                    f"{type(pane).__name__}, not a usable pane id"
                )
            return pane, "session id"
    env_pane = os.environ.get("HERDR_PANE_ID", "").strip()
    if not env_pane:
        return None, "not in HERDR (no record has this session id, HERDR_PANE_ID unset)"
    record = next((a for a in found if a.get("pane_id") == env_pane), None)
    if record is None:
        return None, f"not in HERDR (no record has this session id, none on pane {env_pane})"
    if record.get("agent") != "claude":
        return None, (f"stale session: no record has this session id, and pane "
                      f"{env_pane} runs {record.get('agent')!r}, not claude")
    for key in ("cwd", "foreground_cwd"):
        cwd = record.get(key)
        if isinstance(cwd, str) and cwd and Path(cwd).resolve() == _CHECKOUT:
            return env_pane, "pane env"
    return None, (f"stale session: no record has this session id, and pane "
                  f"{env_pane} runs claude in another directory, not {_CHECKOUT}")


def submit_command(pane_id: str, command: str) -> dict:
    """Submit `/compact` or `/clear` to one pane. Returns HERDR's payload.

    `command` must be one of `ALLOWED_COMMANDS`, compared exactly; anything else
    raises ValueError before HERDR is called. ValueError and not
    HerdrUnavailable, because a refused string is a caller defect, not HERDR
    being unreachable, and the callers' HerdrUnavailable handlers must not
    absorb it.

    The submission is asynchronous by nature: if the agent is `working`, the
    prompt queues and runs when that turn ends. The returned `agent_status` says
    which of the two happened; nothing here waits for the result.
    """
    if command not in ALLOWED_COMMANDS:
        raise ValueError(f"refused: {command!r} is not one of {sorted(ALLOWED_COMMANDS)}")
    if not pane_id:
        raise HerdrUnavailable("submit_command needs a pane id")
    return _run(["agent", "prompt", pane_id, command], PROMPT_TIMEOUT)


def label_text(text: str) -> str:
    """`text` brought inside the grammar `herdr agent rename` accepts.

    MEASURED 2026-10-24 on herdr 0.9.5: the name must start with a lowercase
    letter, hold only lowercase letters, digits, '-' and '_', and be 1 to 32
    characters; anything else exits 1 with `invalid_agent_name`. The countdown
    label "waiting for operator - 60s -> auto-continue" was refused on every
    Stop, so it was never shown (HOS-257). Lowercased, every run of other
    characters becomes one '-', leading non-letters are dropped, and the result
    is cut to 32.
    """
    name = re.sub(r"[^a-z0-9_-]+", "-", text.lower())
    name = re.sub(r"^[^a-z]+", "", name)[:LABEL_MAX_CHARS].rstrip("-_")
    return name or "agent"


def set_label(pane_id: str, text: str) -> dict:
    """Set the agent's display label - the countdown surface during a wait.

    Chosen over writing to /dev/tty, which would fight Claude Code's own TUI for
    the same lines, and over hook stdout, which the harness shows only after the
    hook returns - by which time the wait it described is over. The text passes
    through `label_text` first, so no caller can hand herdr a name it refuses.
    """
    if not pane_id:
        raise HerdrUnavailable("set_label needs a pane id")
    return _run(["agent", "rename", pane_id, label_text(text)], LABEL_TIMEOUT)


def clear_label(pane_id: str) -> dict:
    """Restore the agent's own label. A frozen countdown is worse than none."""
    if not pane_id:
        raise HerdrUnavailable("clear_label needs a pane id")
    return _run(["agent", "rename", pane_id, "--clear"], LABEL_TIMEOUT)


# ============================================================
# Waiting on a pane without outliving it (HOS-269)
# ============================================================
#
# `herdr agent wait <pane> --until <status>` does not exit when its pane goes
# away: its own help says "Without --timeout, waits indefinitely", and a waiter
# started from a HELM session was still running about 20 hours after its pane
# was removed with its yard (MEASURED 2026-10-30). The replacement below polls
# `herdr pane get` instead, so no long-lived herdr child exists and the wait
# ends on its own when the pane disappears. `scripts/herdr-wait.py` is the CLI;
# `.claude/hooks/herdr-waiter-reap.py` stops the waiters of a removed workspace.

#: The `agent_status` values `herdr pane get` reports (MEASURED 2026-10-30).
PANE_STATUSES = frozenset({"idle", "working", "blocked", "done", "unknown"})

PANE_GET_TIMEOUT = 10


class PaneGone(Exception):
    """herdr answered that the pane does not exist (`pane_not_found`)."""


def pane_status(pane: str, timeout: float = PANE_GET_TIMEOUT) -> str | None:
    """The pane's `agent_status`, None when herdr answered without a usable one.

    Not through `_run`: `_run` raises the same HerdrUnavailable for every
    non-zero exit and drops stdout, and the one answer that matters here, "this
    pane does not exist", arrives as exit 1 with a JSON error on stderr:
    `{"error":{"code":"pane_not_found",...}}` (MEASURED 2026-10-30). That code
    raises PaneGone; every other failure raises HerdrUnavailable, so an
    unreachable herdr is never reported as a gone pane.

    `herdr pane get` and not `herdr agent get`: on a missing pane the latter
    answers `agent_not_found` (MEASURED 2026-10-30), which a live pane with no
    agent would answer too.

    `timeout` lets a caller with a deadline cap the call at the time it has
    left; it never exceeds PANE_GET_TIMEOUT in practice because the caller
    passes the smaller of the two.
    """
    args = [HERDR_BIN, "pane", "get", pane]
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise HerdrUnavailable(
            f"{' '.join(args)} timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise HerdrUnavailable(f"{HERDR_BIN} could not be executed: {exc}") from exc
    if proc.returncode != 0:
        # The error JSON arrives on STDERR, stdout empty (MEASURED 2026-10-30:
        # 0 bytes on stdout, 90 on stderr for a missing pane). Both streams are
        # read so a herdr that moves it to stdout still reads as gone.
        for stream in (proc.stdout, proc.stderr):
            try:
                payload = json.loads(stream)
            except ValueError:
                continue
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict) and error.get("code") == "pane_not_found":
                raise PaneGone(pane)
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        first = detail[0] if detail else "no output"
        raise HerdrUnavailable(f"{' '.join(args)} exited {proc.returncode}: {first}")
    try:
        payload = json.loads(proc.stdout)
    except ValueError as exc:
        raise HerdrUnavailable(f"{' '.join(args)} returned unparseable output") from exc
    result = payload.get("result") if isinstance(payload, dict) else None
    record = result.get("pane") if isinstance(result, dict) else None
    if not isinstance(record, dict):
        raise HerdrUnavailable(f"{' '.join(args)} carried no pane object")
    status = record.get("agent_status")
    # A list or object here made the membership test raise TypeError and the
    # waiter died with a traceback (reproduced 2026-10-30); any non-string or
    # unknown value is "herdr answered, no usable status".
    return status if isinstance(status, str) and status in PANE_STATUSES else None


def wait_for_pane(pane: str, states, ceiling_s: float, poll_s: float = 5,
                  unavailable_limit_s: float = 600) -> tuple[int, str]:
    """Poll `pane` until it reaches one of `states`. Returns (exit code, line).

    0 reached, 3 pane gone, 4 ceiling hit, 5 herdr unavailable for
    `unavailable_limit_s` seconds in a row without one good answer. An answer
    with no usable status counts as good: herdr answered, the status is simply
    not one of `states` yet.

    The deadline is checked BEFORE each poll and each poll's subprocess timeout
    is capped at the time left, so no poll starts after the ceiling and none
    runs past it. Checked after the poll, `--ceiling 10` let a poll at 10 s
    report `reached`, and a hanging poll overran a 3 s ceiling to 9 s
    (reproduced 2026-10-30 against a stand-in herdr).
    """
    wanted = set(states)
    start = time.monotonic()
    deadline = start + ceiling_s
    last_good = start
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 4, f"ceiling hit: {pane} after {int(time.monotonic() - start)}s"
        try:
            status = pane_status(pane, timeout=min(PANE_GET_TIMEOUT, remaining))
        except PaneGone:
            return 3, f"target gone: {pane}"
        except HerdrUnavailable as exc:
            # Kept polling, by design: a herdr hiccup is not a verdict. The
            # error is carried into the exit line if it persists.
            if time.monotonic() - last_good >= unavailable_limit_s:
                return 5, (f"herdr unavailable: {pane} for "
                           f"{int(unavailable_limit_s)}s: {exc}")
        else:
            last_good = time.monotonic()
            if status in wanted:
                return 0, f"reached: {pane} {status}"
        time.sleep(max(0.0, min(poll_s, deadline - time.monotonic())))


def _proc_identity(pid: int) -> tuple[str, str] | None:
    """(start_ticks, cmdline) of a live PID, None when it is gone.

    `start_ticks` is field 22 of `/proc/<pid>/stat`; it is read after the last
    `)` because field 2, the command name, may itself hold spaces or parens.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    fields = stat.rsplit(")", 1)[-1].split()
    # fields[0] is field 3 (state), so field 22 is fields[19].
    if len(fields) < 20:
        return None
    return fields[19], raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def _waiter_dir_for_write() -> Path:
    from scripts.utils.data_path import data_write_dir
    return data_write_dir("runtime:herdr-waiters/state")


def _waiter_dir_for_read() -> Path:
    from scripts.utils.data_path import data_path
    return data_path("runtime:herdr-waiters/state")


def register_waiter(pane: str) -> Path:
    """Write this process's record, atomically, and return its path."""
    pid = os.getpid()
    identity = _proc_identity(pid)
    if identity is None:
        raise RuntimeError(f"cannot read /proc/{pid} for this process")
    record = {
        "pid": pid,
        "start_ticks": identity[0],
        "cmdline": identity[1],
        "pane": pane,
        "workspace": pane.split(":", 1)[0],
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    target = _waiter_dir_for_write() / f"{pid}.json"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def unregister_waiter(path: Path) -> None:
    """Remove a record; an already-absent one is the ordinary outcome."""
    path.unlink(missing_ok=True)


def _record_is_live(record: dict) -> bool:
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    identity = _proc_identity(pid)
    # The command line must also be a herdr-wait.py one, so a record naming
    # some other process of this user can never get it signalled.
    return (identity is not None
            and identity == (record.get("start_ticks"), record.get("cmdline"))
            and "herdr-wait.py" in identity[1])


def waiter_records() -> list[dict]:
    """Every live waiter's record; a stale or unreadable one is deleted.

    Stale means the PID is dead or its start time or command line no longer
    match: the waiter was SIGKILLed and skipped its cleanup, or the PID was
    reused. Each returned dict carries `_path`, its record file.
    """
    store = _waiter_dir_for_read()
    if not store.is_dir():
        return []
    live = []
    for path in sorted(store.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[herdr_agent] dropping unreadable waiter record {path.name}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            unregister_waiter(path)
            continue
        if not isinstance(record, dict) or not _record_is_live(record):
            unregister_waiter(path)
            continue
        record["_path"] = str(path)
        live.append(record)
    return live


def stop_waiter(record: dict) -> bool:
    """SIGTERM the one PID a record names, only if it is still that waiter.

    Returns True when the signal was sent. A record that no longer matches its
    process is stale: it is deleted and nothing is killed. Never a process
    group, never a name pattern.
    """
    if not _record_is_live(record):
        unregister_waiter(Path(record["_path"]))
        return False
    try:
        os.kill(record["pid"], signal.SIGTERM)
    except ProcessLookupError:
        unregister_waiter(Path(record["_path"]))
        return False
    return True


# ============================================================
# The unsent input line of a pane (HOS-277, moved here by HOS-296)
# ============================================================
#
# Used by the relay (`scripts/herdr-relay.py`): it must never type into a pane
# where the operator has text not sent yet.

INPUT_READ_TIMEOUT = 10
PROMPT_GLYPH = "❯"             # the Claude Code input line starts with it


def read_input_line(pane: str) -> str:
    """What is typed but not sent in `pane`: the text after the last `❯` line, stripped.

    MEASURED 2026-10-31 by HELM: with the operator typing unsent, the visible snapshot
    showed `❯ Prob`; with nothing typed the line is a bare `❯`. NOT measured:
    whether Claude Code's greyed placeholder suggestion appears on that line. If
    it does, this reads non-empty and the dispatch is merely withheld, the safe
    direction. No `❯` line at all raises, so the caller does not dispatch.
    """
    proc = subprocess.run(  # nosec B603 - list argv, no shell
        [HERDR_BIN, "pane", "read", pane, "--source", "visible", "--lines", "12"],
        capture_output=True, text=True, timeout=INPUT_READ_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"herdr pane read exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    lines = [line for line in proc.stdout.splitlines() if line.startswith(PROMPT_GLYPH)]
    if not lines:
        raise RuntimeError(f"no {PROMPT_GLYPH} input line in the visible snapshot of {pane}")
    return lines[-1][len(PROMPT_GLYPH):].strip()
