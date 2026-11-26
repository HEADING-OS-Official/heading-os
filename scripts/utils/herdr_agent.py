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

`submit_command()` takes a pane and one of exactly two module constants,
`/compact` and `/clear`, and refuses every other string; it is what the hooks
call. Anything able to inject arbitrary text into a live agent session is a
capability no hook should grow by accident, and a closed allowlist of literals
removes the question rather than answering it. `/clear` joined `/compact` on
2026-09-27, operator decision, for `/clear-handover`: the Stop hook sends it
only after the handoff is verified on disk. Sending `/clear` this way is the
same mechanism as `/compact` and was NOT yet run live when it was added.

`send_text()` is the one free-text send, and it exists for exactly one caller:
`scripts/herdr-brief.py`. Under herdr-core the M030.1 relay types its two words
with `type_text()` instead (HOS-385); under the TUI it still goes through
`send_text`. Both callers carry their own text checks, the HELM-only guard and
the transcript delivery check. `send_text` moved here on 2026-11-18 so that one
seam serves both Herdr versions; no hook calls either.

TWO HERDR VERSIONS (2026-11-18). The Herdr TUI (`herdr`, panes such as
`w37:p1`, a session carries `HERDR_PANE_ID`) and herdr-core (`hc`, terminals
such as `t4`, a session carries `HC_TERMINAL_ID`) can both be installed; a
session runs under exactly one. Every function given a pane id picks its backend
from the id's form (`backend_for`); under the TUI each runs the code it ran
before herdr-core support was added. Under herdr-core: `hc get --json` gives the
status (`status.state`, `error` read as `blocked`) and the folders (`cwd` is the
launch folder, `foreground.cwd` the current one); `hc send` sends text, and
`submit_command` types its two commands with `hc keys`, because `hc send` tags
text as `<hc from="tN">` and Claude Code then reads a command as a message
(MEASURED 2026-11-19); the M030.1 relay types its two texts with `hc keys`
through `type_text` for the same reason (MEASURED 2026-11-19, HOS-385), and
still writes its relay journal; the release gate's enveloped-word path
(`_relayed_hc_word`) is no longer used by the relay but stays in place;
`hc read --lines 0` reads the screen; `hc label` carries the countdown. `hc` reports no
Claude session id, so nothing here can match a herdr-core terminal by one.
`agents()`, which takes no pane id and runs from hooks in every session, asks
hc only under herdr-core (`HC_TERMINAL_ID`) or when herdr is absent: whether an
hc call starts a herdr-core server that is not running was not measured, and a
hook must never make a live process appear.

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
HC_BIN = "hc"

# A Herdr TUI pane id; any other non-empty id is a herdr-core terminal address
# (`t4`) or name.
_TUI_PANE = re.compile(r"^w[0-9A-Za-z]+:p[0-9]+$")

# `agent list` walks every pane; `prompt` is a single socket call. The label
# calls sit inside a hook's grace period and must never eat it, so they get the
# tightest budget of the three.
LIST_TIMEOUT = 10
PROMPT_TIMEOUT = 10
LABEL_TIMEOUT = 2
# `hc send` itself waits up to 60 s for a person's unfinished draft
# (`hc --skill`), so its budget sits above that.
CORE_SEND_TIMEOUT = 90
GIT_TIMEOUT = 10

# What `type_text` can type with `hc keys`: an uppercase letter types lowercase
# and `_` is refused by hc (MEASURED 2026-11-19, HOS-385).
TYPEABLE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789 ,-")

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


def backend_for(pane_id: str) -> str:
    """`tui` for a Herdr TUI pane id (`w37:p1`), `core` for any other id (`t4`, a name)."""
    return "tui" if _TUI_PANE.match(pane_id or "") else "core"


def _core_exec(args: list[str], timeout: float,
               timeout_note: str = "") -> subprocess.CompletedProcess:
    """Run one `hc` subcommand; HerdrUnavailable when it cannot run or does not answer.

    The command line is never quoted in a message: for `send` it carries the
    whole text sent. List arguments only, never `shell=True`.
    """
    if shutil.which(HC_BIN) is None:
        raise HerdrUnavailable(f"{HC_BIN} is not on PATH")
    try:
        return subprocess.run([HC_BIN, *args], capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise HerdrUnavailable(
            f"{HC_BIN} {args[0]} timed out after {timeout:g}s{timeout_note}") from exc
    except OSError as exc:
        raise HerdrUnavailable(f"{HC_BIN} could not be executed: {exc}") from exc


def _core_error(proc: subprocess.CompletedProcess) -> dict:
    """The `{"error": {...}}` object hc prints on a failure, from either stream."""
    for stream in (proc.stdout, proc.stderr):
        try:
            payload = json.loads(stream)
        except ValueError:
            continue
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            return error
    return {}


def _core_json(args: list[str], timeout: float, *, gone: str | None = None,
               timeout_note: str = "", allow_empty: bool = False) -> dict:
    """`hc <args>` parsed as one JSON object.

    MEASURED 2026-11-18 on hc 0.1.21: `hc get t999 --json` exits 3 with
    `{"error":{"kind":"not_found",...}}`. With `gone` set, that answer raises
    PaneGone(gone); every other failure raises HerdrUnavailable. `allow_empty`
    accepts an exit 0 with no output as `{}` (the shape of `hc label` and
    `hc send` output was not measured, because neither may run from a yard).
    """
    proc = _core_exec(args, timeout, timeout_note)
    if proc.returncode != 0:
        error = _core_error(proc)
        if gone is not None and proc.returncode == 3 and error.get("kind") == "not_found":
            raise PaneGone(gone)
        detail = error.get("message") or (proc.stderr or proc.stdout or "").strip()
        first = (detail.splitlines() or ["no output"])[0][:200]
        raise HerdrUnavailable(f"{HC_BIN} {args[0]} exited {proc.returncode}: {first}")
    if allow_empty and not proc.stdout.strip():
        return {}
    try:
        payload = json.loads(proc.stdout)
    except ValueError as exc:
        raise HerdrUnavailable(f"{HC_BIN} {args[0]} returned unparseable output") from exc
    if not isinstance(payload, dict):
        raise HerdrUnavailable(
            f"{HC_BIN} {args[0]} returned {type(payload).__name__}, not an object")
    return payload


def _hc_get(terminal: str, timeout: float = 10) -> dict:
    """`hc get <terminal> --json`. PaneGone when hc answers not_found."""
    return _core_json(["get", terminal, "--json"], timeout, gone=terminal)


def _core_status(record: dict) -> dict:
    status = record.get("status")
    return status if isinstance(status, dict) else {}


# MEASURED 2026-11-20 (HELM, hc 0.1.21): with the turn ended and one Claude Code background shell alive, hc get reported state "working", msg "1 shell still running" for 35 s. The plural form is NOT measured (HOS-392).
_SHELLS_ONLY_MSG = re.compile(r"^\d+ shells? still running$")


def _core_state(status: dict) -> str | None:
    """The herdr-core `state` of one status object, as this module reads it.

    `error` reads as `blocked`. `working` whose `msg` fully matches
    `_SHELLS_ONLY_MSG` reads as `idle`: the turn has ended and only background
    shells are alive, so Claude's input line is free. Any other msg (empty,
    absent, not a str, a tool call still in flight, a subagent or monitor)
    leaves `working` as it is. Never raises.
    """
    state = status.get("state")
    if state == "error":
        return "blocked"
    if state == "working":
        msg = status.get("msg")
        if isinstance(msg, str) and _SHELLS_ONLY_MSG.fullmatch(msg):
            return "idle"
    return state


def _core_record(terminal: dict) -> dict:
    """One `hc ls` terminal as the record shape `agents()` returns for herdr-core.

    `agent_status` is `_core_state` of the status (so `blocked` for `error`
    and `idle` for a shell-only `working`), `unknown` when the status is null.
    """
    address = terminal.get("address")
    if not isinstance(address, str) or not address:
        raise HerdrUnavailable("hc ls carried a terminal with no address")
    record = _hc_get(address)
    status = record.get("status")
    app = _core_status(record).get("app")
    foreground = record.get("foreground")
    return {
        "pane_id": address,
        "agent": "claude" if app == "claude-code" else terminal.get("agent"),
        "cwd": record.get("cwd"),
        "foreground_cwd": foreground.get("cwd") if isinstance(foreground, dict) else None,
        "agent_status": _core_state(_core_status(record)) if status is not None else "unknown",
    }


def _core_agents() -> list[dict]:
    """Every live herdr-core terminal, mapped by `_core_record`."""
    payload = _core_json(["ls", "--json"], LIST_TIMEOUT)
    terminals = payload.get("terminals")
    if not isinstance(terminals, list) or not all(isinstance(t, dict) for t in terminals):
        raise HerdrUnavailable("hc ls carried no terminals array")
    return [_core_record(t) for t in terminals]


def agents() -> list[dict]:
    """Every agent the Herdr TUI hosts, then every herdr-core terminal.

    The TUI records come first, exactly as before herdr-core support, errors
    included, whenever `herdr` is on PATH. The herdr-core records follow when
    `hc` is on PATH AND this process runs under herdr-core (`HC_TERMINAL_ID`
    set) or `herdr` is not on PATH; a failing hc call prints one warning line
    to stderr and adds none, and never hides a TUI error. Neither on PATH raises.

    Why the narrowing: hooks call this on every turn of a TUI session, and it is
    not known whether `hc ls` starts a herdr-core server when none runs (not
    measured; `hc --skill` says only that `hc server status` never starts one
    on Windows). A hook must never make a live process appear, so a TUI session
    never asks hc here. Functions handed a herdr-core pane id still call hc:
    their caller is already aiming at a live hc terminal.
    """
    have_tui = shutil.which(HERDR_BIN) is not None
    have_core = shutil.which(HC_BIN) is not None
    if not have_tui and not have_core:
        raise HerdrUnavailable(f"neither {HERDR_BIN} nor {HC_BIN} is on PATH")
    found = _tui_agents() if have_tui else []
    under_core = bool(os.environ.get("HC_TERMINAL_ID", "").strip())
    if have_core and (under_core or not have_tui):
        try:
            found = found + _core_agents()
        except (HerdrUnavailable, PaneGone) as exc:
            print(f"herdr_agent: herdr-core terminals not listed: {exc}", file=sys.stderr)
    return found


def _tui_agents() -> list[dict]:
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

    Under herdr-core no record carries a session id, so after both TUI steps
    the terminal herdr-core put in the environment, `HC_TERMINAL_ID`, is
    accepted (`terminal env`) only when `hc get` shows `claude-code` on it and
    its launch or current folder IS this checkout. Only the TUI's own list is
    read for the session-id step: herdr-core has nothing to match there.
    When the `herdr` binary is present but its server is not running, the TUI
    list raises; whenever the TUI list cannot be read (server not running, a
    timeout, unreadable output) under herdr-core (no `HERDR_PANE_ID`, a
    `HC_TERMINAL_ID`), that is not a failure of this lookup, so it falls
    through to the terminal step (server not running MEASURED 2026-11-19,
    HOS-383).
    """
    if not session_id:
        return None, "no session id"
    if shutil.which(HERDR_BIN) is not None:
        try:
            found = _tui_agents()
        except HerdrUnavailable:
            # Under herdr-core the herdr 0.9.5 binary can stay on PATH with its
            # server down: `herdr agent list` then exits 1 with
            # server_not_running before the HC_TERMINAL_ID step below is
            # reached (MEASURED 2026-11-19 in HELM, terminal t1r, HOS-383).
            # Only there is the TUI list optional; with HERDR_PANE_ID set, or
            # with no HC_TERMINAL_ID, the failure stays the caller's.
            if (os.environ.get("HERDR_PANE_ID", "").strip()
                    or not os.environ.get("HC_TERMINAL_ID", "").strip()):
                raise
            found = []
    elif shutil.which(HC_BIN) is not None:
        found = []
    else:
        raise HerdrUnavailable(f"neither {HERDR_BIN} nor {HC_BIN} is on PATH")
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
        terminal = os.environ.get("HC_TERMINAL_ID", "").strip()
        if terminal:
            return _resolve_core_terminal(terminal)
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


def _resolve_core_terminal(terminal: str) -> tuple[str | None, str]:
    """`HC_TERMINAL_ID` accepted only for claude-code in exactly this checkout."""
    try:
        record = _hc_get(terminal)
    except PaneGone:
        return None, f"not in herdr-core (HC_TERMINAL_ID {terminal} names no terminal)"
    app = _core_status(record).get("app")
    if app != "claude-code":
        return None, f"terminal {terminal} runs {app!r}, not claude-code"
    foreground = record.get("foreground")
    for cwd in (record.get("cwd"),
                foreground.get("cwd") if isinstance(foreground, dict) else None):
        if isinstance(cwd, str) and cwd and Path(cwd).resolve() == _CHECKOUT:
            return terminal, "terminal env"
    return None, (f"terminal {terminal} runs claude-code in another directory, "
                  f"not {_CHECKOUT}")


def check_core_terminal(terminal: str) -> tuple[str | None, str]:
    """`(terminal, how)` when herdr-core shows claude-code on `terminal` in this checkout.

    The same checks as the `terminal env` step of `resolve_pane_how`, for a
    caller holding a terminal id from elsewhere, such as a daemon in the main
    clone checking a terminal the SessionStart hook recorded before Claude
    showed on it (HOS-391). Raises as `_hc_get` does.
    """
    return _resolve_core_terminal(terminal)


def is_claude_terminal(pane: str) -> bool:
    """Whether herdr-core shows claude-code on `pane`; False when it cannot tell."""
    try:
        return _core_status(_hc_get(pane)).get("app") == "claude-code"
    except (HerdrUnavailable, PaneGone):
        return False


def send_text(pane: str, text: str) -> dict:
    """Send `text` and Enter to `pane`. The one free-text send; see the module docstring.

    TUI: `herdr agent prompt <pane> <text>`, PROMPT_TIMEOUT. herdr-core:
    `hc send <pane> <text>`, CORE_SEND_TIMEOUT, never `--if` (it accepts only
    `idle` or `working`, and a finished Claude turn reads `done`) and never
    `--force` (hc refuses a `blocked` terminal, which is the safe answer).
    Returns the parsed JSON answer, `{"output": ...}` when it is not JSON.
    Raises HerdrUnavailable on a missing binary, a non-zero exit or a timeout,
    and the caller does not retry: a timed-out send may still have arrived.
    """
    if not pane:
        raise HerdrUnavailable("send_text needs a pane id")
    note = "; the text may still have arrived; do not resend"
    if backend_for(pane) == "tui":
        if shutil.which(HERDR_BIN) is None:
            raise HerdrUnavailable(f"{HERDR_BIN} is not on PATH")
        try:
            proc = subprocess.run([HERDR_BIN, "agent", "prompt", pane, text],
                                  capture_output=True, text=True,
                                  timeout=PROMPT_TIMEOUT, check=False)
        except subprocess.TimeoutExpired as exc:
            raise HerdrUnavailable(f"{HERDR_BIN} agent prompt did not answer within "
                                   f"{PROMPT_TIMEOUT}s{note}") from exc
        except OSError as exc:
            raise HerdrUnavailable(f"{HERDR_BIN} could not be executed: {exc}") from exc
        name = f"{HERDR_BIN} agent prompt"
    else:
        proc = _core_exec(["send", pane, text, "--json"], CORE_SEND_TIMEOUT, note)
        name = f"{HC_BIN} send"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise HerdrUnavailable(f"{name} exited {proc.returncode}: "
                               f"{(detail.splitlines() or ['no output'])[0][:200]}")
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        out = proc.stdout.strip()
        return {"output": out} if out else {}
    return payload if isinstance(payload, dict) else {"output": payload}


def type_text(pane: str, text: str) -> dict:
    """Type `text` and Enter into a herdr-core `pane` with `hc keys`, untagged; the M030.1 relay's send (HOS-385)."""
    if backend_for(pane) != "core":
        raise HerdrUnavailable("type_text is herdr-core only")
    for ch in text:
        if ch not in TYPEABLE_CHARS:
            raise ValueError(f"type_text cannot type {ch!r} with hc keys; nothing typed")
    keys = ["space" if ch == " " else ch for ch in text]
    return _core_json(["keys", pane, *keys, "enter", "--json"], PROMPT_TIMEOUT,
                      timeout_note="; some keys may have been typed; do not resend",
                      allow_empty=True)


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

    herdr-core: the command is TYPED with `hc keys <pane> / c o m p a c t enter
    --json`, never sent with `hc send`, which tags its text as `<hc from="tN">`
    so that Claude Code reads the command as a message (MEASURED 2026-11-19 on
    hc 0.1.21: `hc send` gave a user record `<hc from="t1">/compact</hc>` and no
    compaction; `hc keys` gave a `compact_boundary` with trigger `manual`).
    `hc keys` waits for no draft and refuses no status, so this checks first:
    the pane must be `idle`, `done` or `working` (a typed line queues while
    Claude works, as a TUI prompt does; that Claude Code queues a TYPED line
    is NOT measured) and its input line empty, or HerdrUnavailable is raised
    with nothing typed. Every failure of those two reads arrives as
    HerdrUnavailable too, because that is the one exception callers catch.
    Returns the `hc keys --json` object (`{"status": ..., "turn": ...}`
    measured), `{}` when it prints nothing.
    """
    if command not in ALLOWED_COMMANDS:
        raise ValueError(f"refused: {command!r} is not one of {sorted(ALLOWED_COMMANDS)}")
    if not pane_id:
        raise HerdrUnavailable("submit_command needs a pane id")
    if backend_for(pane_id) == "core":
        try:
            status = pane_status(pane_id)
            if status not in ("idle", "done", "working"):
                raise HerdrUnavailable(f"pane {pane_id} is {status}; nothing typed")
            if read_input_line(pane_id) != "":
                raise HerdrUnavailable(
                    "text typed but not sent in the input line; nothing typed")
        except HerdrUnavailable:
            raise
        except PaneGone as exc:
            raise HerdrUnavailable(f"pane {pane_id} is gone; nothing typed") from exc
        except Exception as exc:  # noqa: BLE001 - re-raised as the one type callers catch
            raise HerdrUnavailable(f"{type(exc).__name__}: {exc}; nothing typed") from exc
        return _core_json(["keys", pane_id, *command, "enter", "--json"], PROMPT_TIMEOUT,
                          timeout_note="; some keys may have been typed; do not resend",
                          allow_empty=True)
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
    if backend_for(pane_id) == "core":
        # herdr-core label: data on the terminal (`hc label`); nothing documented
        # shows it as a pane title.
        return _core_json(["label", pane_id, f"hos_wait={label_text(text)}", "--json"],
                          LABEL_TIMEOUT, allow_empty=True)
    return _run(["agent", "rename", pane_id, label_text(text)], LABEL_TIMEOUT)


def clear_label(pane_id: str) -> dict:
    """Restore the agent's own label. A frozen countdown is worse than none."""
    if not pane_id:
        raise HerdrUnavailable("clear_label needs a pane id")
    if backend_for(pane_id) == "core":
        return _core_json(["label", pane_id, "hos_wait=", "--json"], LABEL_TIMEOUT,
                          allow_empty=True)
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

    herdr-core (`backend_for`): `hc get <pane> --json`; not_found raises
    PaneGone, every other failure HerdrUnavailable; `status` null answers None;
    the state is read by `_core_state` (`error` reads as `blocked`, a
    `working` whose msg says only that background shells are still running
    reads as `idle`); then the same PANE_STATUSES filter.
    """
    if backend_for(pane) == "core":
        record = _hc_get(pane, timeout)
        if record.get("status") is None:
            return None
        state = _core_state(_core_status(record))
        return state if isinstance(state, str) and state in PANE_STATUSES else None
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


def screen_text(pane: str) -> str:
    """The visible screen of `pane` as text. RuntimeError when the read exits non-zero."""
    if backend_for(pane) == "core":
        argv, name = [HC_BIN, "read", pane, "--lines", "0"], f"{HC_BIN} read"
    else:
        argv = [HERDR_BIN, "pane", "read", pane, "--source", "visible", "--lines", "12"]
        name = "herdr pane read"
    proc = subprocess.run(  # nosec B603 - list argv, no shell
        argv, capture_output=True, text=True, timeout=INPUT_READ_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"{name} exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def read_input_line(pane: str) -> str:
    """What is typed but not sent in `pane`: the text after the last `❯` line, stripped.

    MEASURED 2026-10-31 by HELM: with the operator typing unsent, the visible snapshot
    showed `❯ Prob`; with nothing typed the line is a bare `❯`. NOT measured:
    whether Claude Code's greyed placeholder suggestion appears on that line. If
    it does, this reads non-empty and the dispatch is merely withheld, the safe
    direction. No `❯` line at all raises, so the caller does not dispatch.

    herdr-core: `hc read <pane> --lines 0`, the visible screen as text, through
    the same detector. NOT measured against a real Claude screen under hc.
    """
    lines = [line for line in screen_text(pane).splitlines() if line.startswith(PROMPT_GLYPH)]
    if not lines:
        raise RuntimeError(f"no {PROMPT_GLYPH} input line in the visible snapshot of {pane}")
    return lines[-1][len(PROMPT_GLYPH):].strip()


# ============================================================
# The checkout of a pane (moved here from scripts/herdr-brief.py, 2026-11-18)
# ============================================================


def workspaces() -> list:
    """`herdr workspace list`, parsed. Empty list on any failure."""
    try:
        payload = _run(["workspace", "list"], LIST_TIMEOUT)
    except HerdrUnavailable:
        return []
    result = payload.get("result") or {}
    found = result.get("workspaces") if isinstance(result, dict) else None
    return found if isinstance(found, list) else []


def agent_cwd(pane: str) -> Path | None:
    """The working directory of the agent in a TUI `pane`, asked of herdr.

    The fallback for a workspace with no `worktree` block. MEASURED 2026-09-06:
    a yard whose herdr workspace had died was re-attached with
    `herdr workspace create --cwd <checkout>`, which produces a workspace herdr
    reports WITHOUT a worktree block. The agent's own cwd is then the better
    answer: the transcript slug derives from the path the agent runs in. A wrong
    cwd sends the delivery check to the wrong transcript directory, so the
    failure is a loud false alarm, never a silent success.
    """
    try:
        payload = _run(["agent", "get", pane], LIST_TIMEOUT)
    except HerdrUnavailable:
        return None
    result = payload.get("result")
    agent = result.get("agent") if isinstance(result, dict) else None
    cwd = agent.get("cwd") if isinstance(agent, dict) else None
    return Path(cwd) if isinstance(cwd, str) and cwd else None


def pane_checkout(pane: str, workspace_list: list | None = None) -> Path | None:
    """The checkout `pane` works in, None when it cannot be told.

    TUI: the `checkout_path` of the herdr workspace owning the pane (from
    `workspace_list`, else `workspaces()`), falling back to `agent_cwd`.
    herdr-core: `foreground.cwd` from `hc get`, then `git rev-parse
    --show-toplevel` there; None when the field is absent or git fails.
    """
    if backend_for(pane) == "core":
        try:
            foreground = _hc_get(pane).get("foreground")
        except (HerdrUnavailable, PaneGone):
            return None
        cwd = foreground.get("cwd") if isinstance(foreground, dict) else None
        if not isinstance(cwd, str) or not cwd:
            return None
        try:
            proc = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                                  capture_output=True, text=True, timeout=GIT_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            return None
        top = proc.stdout.strip()
        return Path(top) if proc.returncode == 0 and top else None
    workspace_id = pane.split(":", 1)[0]
    for entry in workspaces() if workspace_list is None else workspace_list:
        if entry.get("workspace_id") != workspace_id:
            continue
        worktree = entry.get("worktree") or {}
        path = worktree.get("checkout_path")
        if path:
            return Path(path)
        return agent_cwd(pane)
    return None


# ============================================================
# The Telegram bridge's seam (HOS-408): both versions from a daemon, keys, the input lock
# ============================================================

#: The only keys `press_keys` sends: an option digit, Enter, Escape and the
#: arrows, which is all a Claude Code menu or permission dialog needs. Which
#: key selects an option in Claude Code's menus: NOT MEASURED (HOS-408).
PRESSABLE_KEYS = frozenset({*"123456789", "enter", "esc", "up", "down"})


def core_socket() -> Path:
    """Where the herdr-core server listens: `HC_TERMINAL_SOCKET`, else the per-user default.

    The default `/run/user/<uid>/herdr-core/default.sock` was MEASURED
    2026-11-19 by `stat` (release-gate design, registry name `plans`).
    """
    raw = os.environ.get("HC_TERMINAL_SOCKET", "").strip()
    if raw:
        return Path(raw)
    run = os.environ.get("XDG_RUNTIME_DIR", "").strip() or f"/run/user/{os.getuid()}"
    return Path(run) / "herdr-core" / "default.sock"


def all_sessions() -> tuple[list[dict], set[str]]:
    """`(records, unreadable)`: every agent of both Herdr versions, for a daemon.

    `agents()` asks herdr-core only under `HC_TERMINAL_ID`, because hooks call
    it every turn and must never be the process that starts a server; a daemon
    under systemd has no `HC_TERMINAL_ID`, so it would never see a herdr-core
    session. Here herdr-core is asked only when its socket already exists, so
    this cannot start a server either (whether `hc ls` would start one: NOT
    MEASURED). Each record carries `backend`, `tui` or `core`.

    `unreadable` names each backend whose list could not be read this time. A
    caller must not read a missing session of an unreadable backend as gone:
    the TUI's `herdr agent list` exits 1 with `server_not_running` when only
    herdr-core runs (MEASURED 2026-11-19, HOS-383), and that is "no TUI
    sessions", not a failure, so a down TUI server is not reported unreadable.
    """
    found: list[dict] = []
    unreadable: set[str] = set()
    if shutil.which(HERDR_BIN) is not None:
        try:
            found += [{**a, "backend": "tui"} for a in _tui_agents()]
        except HerdrUnavailable as exc:
            if "server_not_running" not in str(exc):
                unreadable.add("tui")
                print(f"herdr_agent: TUI agents not listed: {exc}", file=sys.stderr)
    if shutil.which(HC_BIN) is not None and core_socket().is_socket():
        try:
            found += [{**r, "backend": "core"} for r in _core_agents()]
        except (HerdrUnavailable, PaneGone) as exc:
            unreadable.add("core")
            print(f"herdr_agent: herdr-core terminals not listed: {exc}", file=sys.stderr)
    return found, unreadable


def press_keys(pane: str, keys: list[str]) -> dict:
    """Press named keys in `pane`: `herdr pane send-keys` (TUI) or `hc keys` (herdr-core).

    Every key must be in PRESSABLE_KEYS, checked before Herdr is reached; the
    Telegram bridge's buttons are the one caller. Raises HerdrUnavailable as
    `send_text` does; a timed-out press may still have landed.
    """
    if not pane:
        raise HerdrUnavailable("press_keys needs a pane id")
    if not keys or any(k not in PRESSABLE_KEYS for k in keys):
        raise ValueError(f"press_keys refuses {keys!r}; nothing pressed")
    note = "; a key may still have been pressed; do not resend"
    if backend_for(pane) == "core":
        return _core_json(["keys", pane, *keys, "--json"], PROMPT_TIMEOUT,
                          timeout_note=note, allow_empty=True)
    if shutil.which(HERDR_BIN) is None:
        raise HerdrUnavailable(f"{HERDR_BIN} is not on PATH")
    try:
        proc = subprocess.run([HERDR_BIN, "pane", "send-keys", pane, *keys],
                              capture_output=True, text=True, timeout=PROMPT_TIMEOUT,
                              check=False)
    except subprocess.TimeoutExpired as exc:
        raise HerdrUnavailable(f"{HERDR_BIN} pane send-keys did not answer within "
                               f"{PROMPT_TIMEOUT}s{note}") from exc
    except OSError as exc:
        raise HerdrUnavailable(f"{HERDR_BIN} could not be executed: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise HerdrUnavailable(f"{HERDR_BIN} pane send-keys exited {proc.returncode}: "
                               f"{(detail or ['no output'])[0][:200]}")
    return {}


def input_lock_dir() -> Path:
    """The per-user runtime directory every typist shares: `$XDG_RUNTIME_DIR/heading-os/input-locks`.

    Neutral on purpose (HOS-408 review): the relay, the brief sender and the
    bridge each open it, and none depends on another's
    folder, so the bridge being absent never blocks the others. The directory is
    tmpfs and per user; a lock needs to outlive nothing.
    """
    run = os.environ.get("XDG_RUNTIME_DIR", "").strip() or f"/run/user/{os.getuid()}"
    return Path(run) / "heading-os" / "input-locks"


class input_lock:  # noqa: N801 - used as `with input_lock(pane) as held:`
    """A non-blocking exclusive `flock` per pane, held across the last ready check and the send.

    `held` is False when another typist holds it or the lock file cannot be
    opened: fail closed, the caller does not type. It coordinates only the
    workspace's own typists; nothing can lock the operator's keyboard, so the
    idle and empty-input check right before each send stays what protects his
    typing.
    """

    def __init__(self, pane: str):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", pane or "")
        self.path = input_lock_dir() / f"{safe or 'none'}.lock"
        self.fd = None

    def __enter__(self) -> bool:
        import fcntl
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            print(f"herdr_agent: input lock {self.path} unavailable: {exc}", file=sys.stderr)
            return False
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            return False
        return True

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
