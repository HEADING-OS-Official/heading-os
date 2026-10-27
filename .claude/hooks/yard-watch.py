#!/usr/bin/env python3
"""yard-watch.py - Claude Code Stop hook. HELM notices its own fleet.

Operator instruction, 2026-09-20: *"think about it: maybe there should be a
mechanism where HELM itself periodically goes and checks the state of every
yard it created, instead of waiting for me to tell it to. That will avoid
losing a great deal of time."* (translated)

THE EVENING THAT PRODUCED IT. Five yards were working. One finished its
analysis, wrote a good report into the chat, and stopped: the agent alive, the
branch at ZERO commits ahead, the working tree clean. Everything it had
understood existed only as prose inside a worktree that gets deleted. A second
had finished completely and stood ready to merge. Neither was noticed for over
an hour, and only because the operator asked.

WHAT THIS IS AND IS NOT. It reads `scripts/utils/yard_status.sweep()` and
prints one block naming every yard that is `dead`, `stalled`, `ready` or
`unknown`. It starts nothing, re-tasks nothing, writes to no yard and never
blocks a turn: starting an agent changes what runs on this machine, which a hook
does not get to decide. A `working` fleet prints nothing at all.

WHY STOP. The event fires at the end of every turn with no timer and no
daemon, which is what the operator asked for and what he forbids building
instead (checked 2026-09-20 against the hooks reference of Claude Code 2.1.277).
`systemMessage` is the channel that reaches him without blocking; `decision:
block` would also reach him and is not used here, because an alert that holds
the turn hostage is an action.

HELM ONLY. `.git` being a DIRECTORY is how the main clone is told apart, the
same predicate `check_yards_without_a_session` in `session-start.py` uses for
the same reason: this alert is about the fleet, and firing it in every yard
would print the same list N times.

A REPORTER FAILS QUIET. `.claude/rules/development-standards.md` § 12 has a
control fail closed; this is not a control. An unimportable module, an
unreadable `/proc`, a git that will not answer: each costs one line on stderr
and the turn ends normally. Nothing here is ever swallowed in silence, because a
watcher whose failure looks exactly like a clean fleet is the shape the whole
evening above was made of.

THE COST, AND WHY IT IS THROTTLED. MEASURED 2026-09-20 on this machine, load
average 2.1 to 3.3 over 16 cores with four other yards live: one sweep over 5
worktrees is 119 ms median (min 107, max 136), which is 23.8 ms per yard, plus
45 ms for the interpreter and this file's imports. That is not "a few
milliseconds per yard" and it is paid at the end of EVERY turn, so the sweep is
rate-limited to one per `SWEEP_EVERY_MINUTES` and the skipped turns cost the
45 ms floor alone. The dominant term is three `git` subprocesses per yard, not
the `/proc` walk (0.8 ms per yard, MEASURED 2026-09-06), so the throttle is the
cheap shape available without changing `yard_status` itself.

Tests: none. The checks written for this hook were parked in the 2026-09-21
test-suite split, so the throttle, the sweep and the fleet alert are held by
this docstring and by review.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Derived from THIS file, walking up to the checkout that holds the engine
# modules. Never a constant, never the common git directory, never
# `${CLAUDE_PROJECT_DIR}`: a worktree and the main clone must each arm the
# guard against their own tree (`.claude/rules/craft-silent-failure.md`, and
# the `WORKSPACE_ROOT` trap in `ARCHITECTURE.md` § 8, under `spec-docs` in the
# data repository).
_BOOT = Path(__file__).resolve()
ENGINE_ROOT = _BOOT.parent.parent.parent
for _candidate in [_BOOT.parent, *_BOOT.parents]:
    if (_candidate / "scripts" / "utils" / "clone_guard.py").is_file():
        ENGINE_ROOT = _candidate
        break
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

#: The service row the `runtime:` front door (`data_write_dir`) validates against. Registering it in the
#: LIVE registry (`<data root>/config/data-paths.yaml`, `services:`) is the whole
#: registration; the row in `scripts/runtime-services.example.yaml` only changes
#: what a freshly seeded data root starts with. Until it is registered this hook
#: still sweeps and still prints; only the de-duplication state, and therefore
#: the notification, is unavailable, and it says so on stderr rather than
#: notifying on every turn.
SERVICE = "yard-watch"

#: One sweep per this many minutes, whatever the turn rate. Chosen, not
#: measured: the cost above is real and the loss is bounded by it. The evening
#: this hook exists for cost over an hour, so five minutes of latency on a
#: `ready` verdict is not a cost anyone can feel.
SWEEP_EVERY_MINUTES = 5.0

#: How long a yard must have been asking for attention before the operator's
#: phone is involved. Chosen, not measured, for the same reason
#: `yard_status.DEFAULT_QUIET_MINUTES` is: nothing here establishes how long a
#: verdict may legitimately stand before it is worth an interruption. The honest
#: way to replace it is a sample over real evenings.
NOTIFY_AFTER_MINUTES = 30.0

#: The one variable that arms the notification. Deliberately NOT a fallback
#: chain onto `OPS_RADAR_TELEGRAM_TARGET` or `ODIN_CADENCE_TELEGRAM_TARGET`: an
#: operator who configured those asked for those, and inheriting them would put
#: a new stream of messages on his phone as a side effect of installing a hook.
#: Setting this IS turning it on, and it is unset by default.
TARGET_ENV_VAR = "YARD_WATCH_TELEGRAM_TARGET"


def _warn(message: str) -> None:
    """One line on stderr. Never silent, never fatal, never on stdout.

    stdout on this hook is a JSON document the harness parses, so a diagnostic
    printed there would be read as the hook's verdict rather than as a warning
    about it.
    """
    print(f"yard-watch: {message}", file=sys.stderr)


# ============================================================
# The sweep
# ============================================================

def _load_yard_status():
    """`scripts.utils.yard_status`, or None with a line on stderr.

    Defensive on purpose. The module is newer than this hook and may be absent
    from a branch that carries the hook, and a public clone may carry neither.
    An ImportError here means the hook does nothing at all that turn, which is
    the correct outcome and must not be the invisible one.
    """
    try:
        from scripts.utils import yard_status
    except Exception as exc:                  # noqa: BLE001 - reported, not raised
        _warn(f"scripts/utils/yard_status.py could not be imported "
              f"({type(exc).__name__}: {exc}); no yard was checked this turn")
        return None
    return yard_status


def render(attention: list[dict], action: dict) -> str:
    """The block the operator reads. Names and verdicts, never paths.

    `y["path"]` is deliberately not rendered. It is an absolute path into the
    operator's own machine, this repository is public, and a message is the one
    artifact a future reader copies out whole.
    """
    rows = []
    for yard in attention:
        quiet = yard.get("quiet_minutes")
        tail = f", quiet {quiet:g}m" if isinstance(quiet, (int, float)) else ""
        rows.append(f"  {yard.get('name')} [{yard.get('verdict')}{tail}] "
                    f"{action.get(yard.get('verdict'), '')}".rstrip())
    head = f"{len(attention)} yard(s) need attention:"
    return "\n".join([head, *rows])


def check_fleet(workspace_root: Path):
    """`(message, attention)` for HELM, `(None, [])` anywhere else.

    `message` is what the operator should see this turn, or None when the fleet
    is working and the sweep was taken. `attention` is the yard list behind it,
    handed on so the notifier does not sweep a second time.
    """
    try:
        if not (workspace_root / ".git").is_dir():
            return None, []                   # a YARD, or not a checkout at all
    except OSError:
        return None, []

    module = _load_yard_status()
    if module is None:
        return None, []

    try:
        result = module.sweep(workspace_root)
    except Exception as exc:                  # noqa: BLE001 - a reporter fails quiet
        _warn(f"the sweep failed ({type(exc).__name__}: {exc}); no yard was "
              f"checked this turn")
        return None, []

    try:
        attention = module.needs_attention(result)
        note = result.get("note")
    except Exception as exc:                  # noqa: BLE001 - same reason
        _warn(f"the sweep returned an unreadable result "
              f"({type(exc).__name__}: {exc})")
        return None, []

    parts = []
    if note:
        # Printed whenever it is set. Silence about a sweep that did not happen
        # reads as a clean result, which is the one reading this hook must never
        # produce.
        parts.append(f"YARD SWEEP INCOMPLETE: {note}.")
    if attention:
        parts.append(render(attention, getattr(module, "ACTION", {})))
    if not parts:
        return None, attention
    return "\n".join(parts), attention


# ============================================================
# The state, and the notification it de-duplicates
# ============================================================

def state_path():
    """The de-duplication file under the DATA root, or None with a warning.

    Under `data_write_dir("runtime:yard-watch/state")`, never in the engine tree and never in `/tmp`, which
    is emptied on every boot and shared by every yard on this machine.
    """
    try:
        from scripts.utils.data_path import data_write_dir
        return data_write_dir("runtime:yard-watch/state") / "fleet.json"
    except Exception as exc:                  # noqa: BLE001 - reported, not raised
        _warn(f"no de-duplication state ({type(exc).__name__}: {exc}); the "
              f"sweep still runs and nothing is notified. Register `{SERVICE}` "
              f"in the `services:` block of <data root>/config/data-paths.yaml.")
        return None


def read_state(path):
    """The stored state, or an empty one. A corrupt file is not a fatal event."""
    if path is None:
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:                  # noqa: BLE001 - reported, not raised
        _warn(f"unreadable state ({type(exc).__name__}: {exc}); starting fresh")
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_state(path, state) -> None:
    """Atomically, or one line on stderr. Losing this costs a duplicate alert."""
    if path is None:
        return
    try:
        from scripts.utils.atomic import atomic_write_text
        atomic_write_text(path, json.dumps(state, indent=2, sort_keys=True))
    except Exception as exc:                  # noqa: BLE001 - reported, not raised
        _warn(f"state not written ({type(exc).__name__}: {exc}); the next turn "
              f"may repeat an alert")


def _records(state):
    got = state.get("seen")
    return [r for r in got if isinstance(r, dict)] if isinstance(got, list) else []


def age_records(attention: list[dict], state: dict, now: float) -> list[dict]:
    """Carry each yard's clock forward, keyed on (name, verdict).

    On the VERDICT as well as the name, so a yard moving from `stalled` to
    `ready` restarts its clock and earns its own notification. A yard that
    leaves the attention list drops out entirely, so returning to it later is a
    new event rather than one silenced forever by a `notified_at` nobody can
    see.
    """
    previous = {(r.get("name"), r.get("verdict")): r for r in _records(state)}
    fresh = []
    for yard in attention:
        key = (yard.get("name"), yard.get("verdict"))
        old = previous.get(key)
        since = old.get("since") if isinstance(old, dict) else None
        notified = old.get("notified_at") if isinstance(old, dict) else None
        fresh.append({
            "name": key[0],
            "verdict": key[1],
            "since": since if isinstance(since, (int, float)) else now,
            "notified_at": notified if isinstance(notified, (int, float)) else None,
        })
    return fresh


def due_for_notice(records: list[dict], now: float,
                   after_minutes: float = NOTIFY_AFTER_MINUTES) -> list[dict]:
    """Records past the threshold that have never been notified."""
    cutoff = after_minutes * 60.0
    return [r for r in records
            if r.get("notified_at") is None and (now - r["since"]) >= cutoff]


def notify(records: list[dict], now: float) -> bool:
    """One message to the operator's OWN sink, or nothing. Returns whether sent.

    `.claude/rules/lethal-trifecta.md` exempts a self-notification from the
    outbound send gate, because a message that can only arrive at the person who
    already holds the data is the machine telling him what he already owns. That
    exemption is mechanical, not a promise: `telegram_notify.own_targets()`
    resolves the allowlist from the environment and REFUSES anything else, which
    is why `TARGET_ENV_VAR` had to be added to that module's own set rather than
    handed in from here.

    OFF by default. No target configured means no message and no warning: an
    unset variable is the operator not having asked for this, not a fault.

    `.env` is loaded before the read, because that is where the variable is set
    and nothing else in this hook loads it (2026-10-16).
    """
    try:
        from scripts.utils.paths import load_env

        load_env(ENGINE_ROOT)
    except Exception as exc:                  # noqa: BLE001 - reported, not raised
        _warn(f".env not loaded ({type(exc).__name__}: {exc})")
    target = os.environ.get(TARGET_ENV_VAR, "").strip()
    if not target:
        return False

    lines = [f"  {r['name']} [{r['verdict']}] waiting "
             f"{int((now - r['since']) / 60)}m" for r in records]
    body = "\n".join([f"HEADING OS: {len(records)} yard(s) need attention.",
                      *lines])

    try:
        from scripts.utils.telegram_notify import notify as send
    except Exception as exc:                  # noqa: BLE001 - reported, not raised
        _warn(f"notifier unavailable ({type(exc).__name__}: {exc})")
        return False

    try:
        return bool(send(target, body))
    except Exception as exc:                  # noqa: BLE001 - a reporter fails quiet
        _warn(f"notification failed ({type(exc).__name__}: {exc})")
        return False


# ============================================================
# The turn
# ============================================================

def due_for_sweep(state: dict, now: float,
                  every_minutes: float = SWEEP_EVERY_MINUTES) -> bool:
    """Whether the throttle allows a sweep this turn.

    An unreadable or absent `sweep_at` sweeps. Failing toward the work is right
    here: the cost of one extra sweep is 119 ms and the cost of skipping every
    one of them is the evening in the docstring.
    """
    last = state.get("sweep_at")
    if not isinstance(last, (int, float)):
        return True
    return (now - last) >= every_minutes * 60.0


def main(root=None, now=None) -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:                         # noqa: BLE001 - never hold the turn
        return 0
    if not isinstance(payload, dict):
        # Valid JSON that is not an object still reaches `.get` and raises an
        # uncaught AttributeError. Swept across every stdin hook on 2026-08-23;
        # this one carries the guard from birth.
        return 0
    if payload.get("stop_hook_active"):
        # Another Stop hook is already holding this turn. Nothing here is urgent
        # enough to pile onto a blocked one, and the next turn sweeps anyway.
        return 0

    workspace_root = Path(root) if root is not None else ENGINE_ROOT
    moment = float(now) if now is not None else time.time()

    try:
        if not (workspace_root / ".git").is_dir():
            return 0                          # a YARD: cheapest gate, taken first
    except OSError:
        return 0

    path = state_path()
    state = read_state(path)
    if not due_for_sweep(state, moment):
        return 0

    message, attention = check_fleet(workspace_root)

    state["sweep_at"] = moment
    records = age_records(attention, state, moment)
    due = due_for_notice(records, moment)
    if due and notify(due, moment):
        for record in due:
            record["notified_at"] = moment
    state["seen"] = records
    write_state(path, state)

    if message:
        print(json.dumps({"systemMessage": message}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
