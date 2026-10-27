#!/usr/bin/env python3
"""The checkpoint system's autonomy: the unattended switch, the wait, the stop conditions.

One per-session switch. `unattended` (`session_unattended`, else
`CLAUDE_HANDOFF_UNATTENDED`, else on in a yard through `HEADING_OS_YARD=1`)
decides whether a pause hands the turn back at all. It is the operator's:
nothing here lowers it.

An unattended stretch ends on one of three recorded signals: the assistant's
done marker (`checkpoint-paths.py --done`), `IDLE_CONTINUATION_LIMIT`
continued turns with no tool call, or the continuation ceiling.

Freeing the context is /second-wind's job (operator decision 2026-10-14): the
Stop hook `.claude/hooks/checkpoint-offer.py` verifies its handoff and sends
`/clear` or `/compact` through HERDR. The `auto` switch and the HERDR-driven
compaction that lived here until then were removed with it. The Stop hook
drives this module and the CLI `scripts/checkpoint-paths.py` flips its
switch. Incident history behind the choices here: `code-history.md` under the
registry name `skill-design-references` (skill `checkpoint`).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from scripts.utils import checkpoint_paths as CP
from scripts.utils import herdr_agent as HA


# ============================================================
# The switches
# ============================================================

def unattended_mode(state: dict | None = None) -> bool:
    """May this session continue on its own at a pause? Session flag, then env, then yard.

    A yard is unattended by default (operator decision 2026-10-14): its agent is
    started by the bootstrap with HEADING_OS_YARD=1
    (`scripts/herdr/heading-os-yard/yard-bootstrap.sh`). HELM is on since
    2026-10-18 through `CLAUDE_HANDOFF_UNATTENDED=1` in its local settings.
    """
    if state:
        flag = state.get("session_unattended")
        if flag is not None:
            return bool(flag)
    if os.environ.get("CLAUDE_HANDOFF_UNATTENDED") is not None:
        return CP.env_bool("CLAUDE_HANDOFF_UNATTENDED", False)
    return os.environ.get("HEADING_OS_YARD", "").strip() == "1"


def config() -> dict:
    """The two menu points: `soft` is the first menu, `hard` the last call.

    Two showings and no repeat in between (operator decision 2026-10-14: 52 of
    73 menus were followed by more work). A `soft >= hard` pair falls back to
    the defaults.
    """
    soft = CP.env_int("CLAUDE_HANDOFF_SOFT_THRESHOLD", 45)
    hard = CP.env_int("CLAUDE_HANDOFF_HARD_THRESHOLD", 55)
    if soft >= hard:
        soft, hard = 45, 55
    return {"soft": soft, "hard": hard}


def threshold_bucket(used: float, cfg: dict) -> int:
    """The menu point `used` has reached: `hard`, `soft`, or 0 below both."""
    if used >= cfg["hard"]:
        return cfg["hard"]
    if used >= cfg["soft"]:
        return cfg["soft"]
    return 0


# The keys that describe ONE unattended stretch, as opposed to the switch.
# Cleared together, always, so an ended stretch and one never started look alike.
_WINDOW_KEYS = (
    "unattended_continuations",
    "unattended_done_at",
    "unattended_done_note",
    "unattended_paused_at",
    "unattended_stop_reason",
    "unattended_tool_baseline",
    "unattended_idle_streak",
)


def clear_unattended_window(state: dict) -> dict:
    """Start a new stretch: forget how the previous one ended, keep the switch.

    Called when the mode is raised and when the operator starts a new turn.
    Mutates and returns `state`; the caller writes it.
    """
    for key in _WINDOW_KEYS:
        state.pop(key, None)
    return state


def raise_unattended(state: dict) -> dict:
    """Turn the mode ON in `state` and start a new stretch."""
    state["session_unattended"] = True
    state["session_unattended_at"] = CP.utc_now().isoformat()
    state.pop("unattended_turn_id", None)
    clear_unattended_window(state)
    return state


def lower_unattended(state: dict) -> dict:
    """Turn the mode OFF in `state`."""
    state["session_unattended"] = False
    state["session_unattended_at"] = CP.utc_now().isoformat()
    return state


def mark_unattended_done(state: dict, note: str) -> dict:
    """Record that the plan is finished, so the Stop hook stops continuing.

    The explicit end-of-work signal. It ends the stretch, not the switch, and
    the operator's next instruction clears it.
    """
    state["unattended_done_at"] = CP.utc_now().isoformat()
    state["unattended_done_note"] = (note or "").strip() or "no note given"
    return state


# The standing instruction for a run nobody is watching. Anthropic's paragraph,
# verbatim except "the user" -> "the operator". Source: the Opus 5.5 prompting
# guide, section "Unattended agentic runs", adopted on the operator's decision of
# 2026-09-27. ONE copy for both unattended paths: the Stop hook sends it in a
# continuation, and scripts/night-repair.py appends it to its system prompt.
UNATTENDED_STANDING_INSTRUCTION = (
    "A standing instruction from the operator, the person you are working for. "
    "It is about how your turns end. A message with no tool call in it ends your "
    "turn, and the work stops there until you are asked to continue. The operator "
    "has seen you end turns in four ways while work they asked for was still "
    "owed, and does not want any of them. One: a long summary of what was done "
    "that closes by announcing the next step and has no tool call, so the next "
    "thing never starts. Two: an offer to carry on with something unless the "
    "operator would prefer otherwise, which stops to wait for an answer the "
    "operator was not going to give. Three: a list of decisions for the operator "
    "when, by your own account, none of them blocks the rest of the work. Four: "
    "deciding that this is a good place to report, because the turn has been long "
    "or a milestone is done. Status notes are welcome, and so are your "
    "recommendations on open decisions, but put them in the same message as your "
    "next tool call and carry on with whatever does not depend on the operator's "
    "answer. If you notice yourself inviting the operator to redirect you or "
    "offering to wait, delete it and do the next thing. The stops the operator "
    "does want are the ones where nothing can move without them, or where the "
    "thing blocking you is deliberately protected from you. This does not "
    "override the need for confirmation on risky or destructive actions."
)


# ============================================================
# The CLI switches (`scripts/checkpoint-paths.py`)
# ============================================================

def _session_state_path() -> tuple[Path, str]:
    slug = CP.safe_slug(CP.session_id())
    return CP.state_path(CP.project_root(), slug), slug


def unattended_switch(value: str) -> int:
    """Decide whether a pause hands the turn back, for THIS session, or report it."""
    path, slug = _session_state_path()
    state = CP.read_json(path)

    if value == "status":
        chosen = state.get("session_unattended")
        source = "this session" if chosen is not None else "the environment"
        on = unattended_mode(state)
        cfg = config()
        print(f"unattended={'on' if on else 'off'} (set by {source})")
        print(f"menu at {cfg['soft']}%, last call at {cfg['hard']}%")
        if on:
            print(
                f"at the threshold: wait {wait_seconds()}s, continue on silence · "
                f"continuations {int(state.get('unattended_continuations') or 0)}"
            )
        if state.get("unattended_done_at"):
            # `--unattended off` does not clear the window keys, so this can
            # report last night's DONE while the mode reads off. Never wrong.
            note = state.get("unattended_done_note") or "no note given"
            print(f"DONE: {note}")
            print(f"declared at: {state['unattended_done_at']}")
        if state.get("unattended_paused_at"):
            why = state.get("unattended_stop_reason") or "no reason recorded"
            print(f"PAUSED: {why}")
            print(f"paused at: {state['unattended_paused_at']}")
            print("The switch is still on. Your next instruction resumes it.")
        return 0

    try:
        # Inside the lock: `lower_unattended` pops keys, which a merge cannot express.
        with CP.locked_state(path) as state:
            if value == "on":
                raise_unattended(state)
            else:
                lower_unattended(state)
    except OSError as exc:
        print(f"checkpoint-paths: could not write the switch: {exc}", file=sys.stderr)
        return 1

    if value == "on":
        print(f"unattended=on for this session ({slug}).")
        print(f"At every pause: wait {wait_seconds()}s for you, then continue without asking.")
        print("Type anything inside that window and the turn goes back to you.")
        print("At the menu: wait the same time, then run /second-wind by itself.")
        print("Turn it off: python scripts/checkpoint-paths.py --unattended off")
    else:
        print(f"unattended=off for this session ({slug}). A pause waits for you again.")
    return 0


def done_marker(note: str) -> int:
    """Declare the plan finished, so the unattended stretch stops at the pause.

    The Stop hook reads state, never prose. Recorded even with the mode off.
    """
    path, slug = _session_state_path()
    state = CP.read_json(path)

    if not unattended_mode(state):
        print(f"note: unattended is off for this session ({slug}); marker recorded anyway.")

    try:
        # Marked on the current file inside the lock: the statusline writes it every render.
        with CP.locked_state(path) as state:
            mark_unattended_done(state, note)
            state["unattended_last_stop"] = f"the plan is finished: {note}"
    except OSError as exc:
        print(f"checkpoint-paths: could not write the marker: {exc}", file=sys.stderr)
        return 1

    print("done recorded. Stretch ended, bar reads `unattended paused`; "
          "the switch stays on and your next instruction resumes it.")
    print(CP.unattended_line(CP.read_json(path)))
    print("Put that line, verbatim, in your final report and in the closing Lific comment.")
    return 0


# ============================================================
# The Stop hook's budget and the grace period
# ============================================================

# The Stop timeout this hook is registered with. Claude Code DISCARDS the output
# of a hook that outruns it, so the wait is bounded against the hook's own clock
# (MEASURED 2026-08-20: 92.0 s end to end against a 90 s registration, with HERDR
# answering just inside each of its timeouts and the wait at its ceiling).
HOOK_TIMEOUT_SECONDS = CP.env_int(
    "CLAUDE_HANDOFF_HOOK_TIMEOUT", 90, minimum=10, maximum=600
)

# What must still fit after the wait: a label overrun, `clear_label`, the state
# write and the print. Measured worst case about 4.1 s.
POST_WAIT_RESERVE_SECONDS = 8

# The configurable ceiling on the grace period. An upper limit on what may be
# configured, not a proof that the hook fits; `_effective_wait` is that proof.
UNATTENDED_WAIT_MAX = 60

# The countdown redraws on this cadence, not on every poll.
LABEL_REFRESH_SECONDS = 5

# Process start, as close as this module can observe it. Imported by the Stop
# hook at its own start.
_HOOK_STARTED = time.monotonic()


def wait_seconds() -> int:
    """The configured grace period, 1..60; the default 60 on anything outside it."""
    return CP.env_int(
        "CLAUDE_HANDOFF_UNATTENDED_WAIT", 60, minimum=1, maximum=UNATTENDED_WAIT_MAX
    )


def _effective_wait(configured: float, started: float, now: float) -> float:
    """The part of the configured grace period that still fits in the timeout; 0 for none."""
    room = HOOK_TIMEOUT_SECONDS - POST_WAIT_RESERVE_SECONDS - (now - started)
    return max(0.0, min(float(configured), room))


def persist(state_path: Path, _mutate=None, **updates) -> None:
    """Apply our own keys to what is on disk NOW, never a whole stale copy.

    The statusline rewrites this file every turn. `_mutate` runs on the fresh
    state after `updates`, for changes a keyword cannot express (removing a key).
    """
    try:
        with CP.locked_state(state_path) as fresh:
            fresh.update(updates)
            if _mutate is not None:
                mutated = _mutate(dict(fresh))
                fresh.clear()
                fresh.update(mutated)
    except Exception as exc:  # noqa: BLE001 - a lost note is not worth a broken turn
        print(f"checkpoint-offer: state write failed: {exc}", file=sys.stderr)


# ============================================================
# Who else drives this Stop
# ============================================================

# A task record carrying one of these is history, not work in flight.
TERMINAL_TASK_STATES = frozenset({
    "completed", "complete", "done", "finished", "failed", "error",
    "cancelled", "canceled", "stopped", "killed", "timeout", "timed_out",
})


def _ralph_owner(project: Path) -> str | None:
    """The session id in the ralph-loop plugin's state file.

    None: no loop here. An EMPTY string: the file has no `session_id:` line, and
    the plugin then drives whichever session reaches the Stop, so it claims all
    of them. The plugin removes the file on every terminal path (read off its
    `hooks/stop-hook.sh` 2026-08-17), so presence means a live loop.
    """
    path = project / ".claude" / "ralph-loop.local.md"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if stripped.startswith("session_id:"):
            return stripped.split(":", 1)[1].strip()
    return ""


def continuation_claimant(
    payload: dict | None = None, project: Path | None = None
) -> str:
    """The name of whatever already drives this Stop event, or "" if nothing does.

    `session_crons` (`/loop`, `CronCreate`, `ScheduleWakeup`) and
    `background_tasks` are Claude Code's own payload fields; the third signal is
    the ralph-loop state file. A record reporting a terminal status claims
    nothing; an unknown status still claims. `/goal` lives in the harness's
    memory and cannot be seen here; `stop_hook_active` bounds the cost to one
    block per operator turn.
    """
    payload = payload or {}
    for field in ("session_crons", "background_tasks"):
        for item in payload.get(field) or ():
            if isinstance(item, dict):
                status = str(item.get("status") or "").strip().lower()
                if status in TERMINAL_TASK_STATES:
                    continue
            return field
    if project is not None:
        owner = _ralph_owner(project)
        if owner is not None and owner in ("", CP.session_id(payload)):
            return "ralph-loop"
    return ""


# ============================================================
# The unattended continuation
# ============================================================

# Short on purpose: a blocking Stop reason is printed to the operator's
# transcript at every pause. `{open_items}` and `{standing}` follow Anthropic's
# Opus 5.5 guidance on unattended runs (operator decision 2026-09-27); a hook
# cannot reach the system prompt, so the standing instruction goes here.
UNATTENDED_WRAPPER = """\
{usage}, unattended on, {wait}s grace passed, so the turn continues. \
Resume the unfinished task. Decide alone. Never invent work. \
Do not touch the unattended switch, and do not run /compact.

{open_items}

Finished, or left only a judgement the operator owns? Run `python \
scripts/checkpoint-paths.py --done "<one line>"` and stop; this hook reads \
state, never prose. Continuation {done} of {maximum}.

{standing}"""

# From the second continuation on: only what changes, the counter and the one
# command that ends the stretch.
UNATTENDED_WRAPPER_REPEAT = """\
{usage}, unattended on, continuation {done} of {maximum}. {open_items} Resume \
the task, or end the stretch with `python scripts/checkpoint-paths.py --done \
"<one line>"` and stop."""


# The /second-wind self-run in unattended mode (spec 2026-10-14, section 3.7):
# the asyncRewake Stop registration wakes the model with this after the
# operator stayed silent at the menu.
SECOND_WIND_WRAPPER = """\
Context is about {used:.0f}% used, unattended on, the operator stayed silent for \
{wait} s at the menu. Run /second-wind now, following \
.claude/skills/second-wind/SKILL.md exactly. Step 0 first: write the handoff \
draft, with a complete `## Safe resume` block, dispatch the `goldfish` cold read \
and apply every FIX from its reply; the reply arrives only after a turn ends, so \
this step may take a turn of its own. Then Step 1 and the write in ONE turn: \
`python scripts/checkpoint-paths.py --kind second-wind`, then the draft on stdin \
to `python scripts/checkpoint-paths.py --write --kind second-wind`, without a \
second cold read and never ending the turn between the two: the Stop hook checks \
the archive once, at the first turn end after the start. Print ONE line naming \
the archive, then stop. Do NOT run /clear or /compact yourself: the Stop hook \
checks the handoff and sends the right one."""

SECOND_WIND_MAX_ATTEMPTS = 2

# Picking a /second-wind menu option (operator request 2026-10-15). Set by the
# Stop hook when the menu shows; consumed by the operator's next prompt
# (`unattended-resume.py`) and dropped by the self-run and by any reset, so a
# bare "1" or "2" means a pick only while a menu waits for its answer.
SECOND_WIND_MENU_KEY = "second_wind_menu_at"

# A UserPromptSubmit hook cannot rewrite the prompt (Claude Code 2.1.296: its
# output carries only additionalContext, sessionTitle, suppressOriginalPrompt),
# so the pick reaches the model as context beside the bare digit.
SECOND_WIND_PICKS = {
    "1": ("The operator typed 1 at the /second-wind menu: option 1 is chosen. "
          "Invoke the second-wind skill now through the Skill tool and follow "
          ".claude/skills/second-wind/SKILL.md exactly. Do nothing else first."),
    "2": ("The operator typed 2 at the /second-wind menu: option 2, continue as "
          "is. Do NOT run /second-wind. Say in one short line that the session "
          "continues, then carry on with the task in progress, if any."),
}

# Attended mode: the Stop hook blocks once and the model asks through the
# native AskUserQuestion selector, the only arrow-key + Enter choice Claude
# Code offers (no hook output renders one). Unattended mode keeps the plain
# menu, because an open question would hold the session through the countdown.
SECOND_WIND_ASK = """\
The /second-wind menu is on screen (context about {used:.0f}% used). Do not \
continue the task. Call the AskUserQuestion tool once, with exactly one \
question: question "Context is about {used:.0f}% used. What now?", header \
"Second wind", multiSelect false, and these two options in this order: \
label "/second-wind" with description "Save a handoff and free the context \
(recommended). {verdict}"; label "Continue as is" with description "Keep \
working in this context." Then act on the answer: "/second-wind" -> invoke \
the second-wind skill through the Skill tool and follow it exactly; \
"Continue as is" -> one short line saying the session continues, then stop; \
any other text -> treat it as an ordinary message from the operator."""


def _context_was_rebuilt(state: dict, previous_at: str | None) -> bool:
    """Did a compaction land between the previous continuation and this one?

    No compaction ever: NO. A missing previous stamp or an unparseable pair:
    YES, because withholding the full text can leave the assistant unable to
    name the command that ends the stretch.
    """
    compacted_at = state.get("last_compact_at")
    if not compacted_at:
        return False
    if not previous_at:
        return True
    try:
        return datetime.fromisoformat(str(compacted_at)) > datetime.fromisoformat(str(previous_at))
    except (TypeError, ValueError):
        return True


# Three continued turns in a row with no tool call end a stretch, per the Opus 5.5
# guidance ("stop after two or three automatic continuations on the same task").
# A tool call is the measure because reading and research are tool calls too.
# It is also the harness's measure: Claude Code ends a turn after 8 consecutive
# Stop blocks (`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`, default 8) and resets that
# count on every turn that made a tool call (read from the 2.1.296 binary,
# 2026-10-16), so this bound must stay below 8 and the total ceiling need not.
IDLE_CONTINUATION_LIMIT = 3

# Statuses that close a task. MEASURED 2026-09-27: 123 TaskUpdate calls on this
# machine carried only pending, in_progress and completed; `deleted` is accepted
# by the tool.
_CLOSED_TASK_STATUSES = frozenset({"completed", "deleted"})
_OPEN_ITEMS_SHOWN = 6
_OPEN_ITEM_CHARS = 80


def _transcript_facts(payload: dict, session: str) -> tuple[int | None, dict | None]:
    """(tool calls so far, task list) for THIS session, read from its transcript.

    The count is of distinct main-thread `tool_use` ids, compared across Stops.
    Tasks come from `TaskCreate` / `TaskUpdate` calls whose result is on disk
    and not an error (shapes MEASURED 2026-09-27 on real transcripts;
    `TodoWrite` appeared in none of 967). Unreadable: (None, None), unknown.
    """
    raw = payload.get("transcript_path")
    if not raw:
        return (None, None)
    seen: set[str] = set()
    calls: dict[str, tuple[str, dict]] = {}
    tasks: dict[str, dict] = {}
    try:
        with Path(raw).open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "tool_use" not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("isSidechain"):
                    continue
                if session and entry.get("sessionId") not in (None, session):
                    continue
                message = entry.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    kind = block.get("type")
                    if kind == "tool_use" and entry.get("type") == "assistant":
                        use_id = str(block.get("id") or "")
                        if not use_id or use_id in seen:
                            continue
                        seen.add(use_id)
                        name = block.get("name")
                        if name in ("TaskCreate", "TaskUpdate"):
                            args = block.get("input")
                            calls[use_id] = (name, args if isinstance(args, dict) else {})
                    elif kind == "tool_result":
                        call = calls.pop(str(block.get("tool_use_id") or ""), None)
                        if call is not None:
                            _apply_task_result(tasks, call, block, entry.get("toolUseResult"))
    except OSError as exc:
        print(f"checkpoint-offer: transcript unreadable for progress: {exc}",
              file=sys.stderr)
        return (None, None)
    return (len(seen), tasks or None)


def _apply_task_result(tasks: dict, call: tuple[str, dict], block: dict, result) -> None:
    """Fold one finished TaskCreate or TaskUpdate into `tasks`."""
    name, args = call
    if block.get("is_error"):
        return
    if name == "TaskCreate":
        task_id = None
        if isinstance(result, dict) and isinstance(result.get("task"), dict):
            task_id = result["task"].get("id")
        if task_id is None:
            text = block.get("content")
            if isinstance(text, list):
                text = " ".join(
                    str(part.get("text") or "") for part in text if isinstance(part, dict)
                )
            match = re.search(r"Task #(\S+) created", str(text or ""))
            task_id = match.group(1) if match else None
        if task_id is not None:
            tasks[str(task_id)] = {
                "subject": str(args.get("subject") or "").strip(),
                "status": "pending",
            }
        return
    if isinstance(result, dict) and result.get("success") is False:
        return
    task = tasks.get(str(args.get("taskId")))
    if task is None:
        return
    if args.get("status"):
        task["status"] = str(args["status"])
    if args.get("subject"):
        task["subject"] = str(args["subject"]).strip()


def _open_items_sentence(tasks: dict | None) -> str:
    """The sentence naming what is still open. With no task list, the assistant names them."""
    if not tasks:
        return ("List what is still open in the task you were given, then continue "
                "with it. If an item is blocked, say what is blocking it.")
    still_open = [
        task["subject"] or "(no subject)"
        for task in tasks.values()
        if task["status"] not in _CLOSED_TASK_STATUSES
    ]
    if not still_open:
        return ("Every item on your task list is marked completed. If the work is "
                "finished, end the stretch as below.")
    shown = [
        subject if len(subject) <= _OPEN_ITEM_CHARS
        else subject[: _OPEN_ITEM_CHARS - 3] + "..."
        for subject in still_open[:_OPEN_ITEMS_SHOWN]
    ]
    names = "; ".join(shown)
    if len(still_open) > _OPEN_ITEMS_SHOWN:
        names += f"; and {len(still_open) - _OPEN_ITEMS_SHOWN} more"
    return (f"Your task list still has open items: {names}. Continue with them. "
            "If one is blocked, say what is blocking it.")


def _turn_started_at(payload: dict, turn: str, session: str) -> datetime | None:
    """When the operator's message that opened `turn` was written; None when unknown.

    The first transcript entry carrying the turn's `promptId` is that message:
    the Stop hook's own feedback reuses the id but always comes later.
    """
    raw = payload.get("transcript_path")
    if not raw or not turn:
        return None
    needle = f'"promptId":"{turn}"'
    try:
        with Path(raw).open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if needle not in line.replace(" ", ""):
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("promptId") != turn:
                    continue
                if session and entry.get("sessionId") not in (None, session):
                    continue
                return datetime.fromisoformat(str(entry.get("timestamp")).replace("Z", "+00:00"))
    except (OSError, ValueError) as exc:
        print(f"checkpoint-offer: turn start unreadable: {exc}", file=sys.stderr)
    return None


# `scripts/prompt-archive.py` names a file by its UTC save time, to the second.
_ARCHIVE_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2}-\d{6})_prompt_")


def _open_work(payload: dict, tasks: dict | None, turn: str, session: str) -> bool:
    """Does STATE show work still owed? Never read from the assistant's prose.

    Yes on an open task-list item, or on a prompt archived for this session
    since the operator's message opened this turn (a `--done` in the turn has
    already ended the stretch before this is asked). Unknown answers yes, so an
    unreadable transcript or archive keeps today's behaviour. Background jobs
    are not asked here: `continuation_claimant` already steps aside for them.
    """
    if tasks and any(t["status"] not in _CLOSED_TASK_STATUSES for t in tasks.values()):
        return True
    started = _turn_started_at(payload, turn, session)
    if started is None:
        return True
    paths, refusal = CP.archived_prompts(session, CP.project_root(payload), CP.prompt_archive_tool())
    if refusal is not None:
        return True
    floor = started.astimezone(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    for path in paths:
        match = _ARCHIVE_STAMP.search(Path(path).name)
        if match is None or match.group(1) >= floor:
            return True
    return False


def _usage_phrase(used: float | None) -> str:
    return "Context use unknown" if used is None else f"{used:.0f}% used"


def _operator_spoke(fresh: str, session: str) -> bool:
    """Does this slice of transcript carry something the operator typed?

    `queue-operation` / `enqueue` is the load-bearing signal: Enter mid-turn
    queues the message and clears the screen, but the transcript records it
    (MEASURED 2026-08-17: 54 s before anything consumed it). Our own `/compact`
    enqueue is not the operator. Split on "\\n" only: `splitlines()` also breaks
    on U+2028, which transcripts carry literally.
    """
    for line in fresh.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "queue-operation":
            if (
                entry.get("operation") == "enqueue"
                and entry.get("content") != HA.COMPACT_COMMAND
                and (not session or entry.get("sessionId") in (None, session))
            ):
                return True
            continue
        if entry.get("type") != "user" or entry.get("isMeta"):
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.strip():
            return True
        if isinstance(content, list) and any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and str(block.get("text") or "").strip()
            for block in content
        ):
            return True
    return False


def _queue_pending(path: Path, session: str) -> bool:
    """Is a message already queued and not yet consumed?

    Counted over all four operations (MEASURED across 44 transcripts: 660
    enqueue, 422 remove, 231 dequeue, 1 popAll). Our own `/compact` is carried
    as a debt (`owed`): its enqueue and `remove` carry content, its consuming
    `dequeue` does not. The count has a floor of zero, because a negative count
    is deaf to the next real message. Any read failure answers True, which hands
    the turn back rather than talking over him.
    """
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return True
    pending = 0
    owed = 0
    try:
        with handle:
            for line in handle:
                if '"queue-operation"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "queue-operation":
                    continue
                if session and entry.get("sessionId") not in (None, session):
                    continue
                operation = entry.get("operation")
                if entry.get("content") == HA.COMPACT_COMMAND:
                    if operation == "enqueue":
                        owed += 1
                    elif operation in ("remove", "dequeue") and owed > 0:
                        owed -= 1
                    continue
                if operation == "enqueue":
                    pending += 1
                elif operation == "popAll":
                    pending = 0
                    owed = 0
                elif operation in ("remove", "dequeue"):
                    if "content" not in entry and owed > 0:
                        owed -= 1
                    else:
                        pending -= 1
                pending = max(pending, 0)
    except OSError:
        return True
    return pending > 0


def _wait_out_the_grace(payload: dict, session: str) -> tuple[bool, float]:
    """(spoke, seconds actually granted). Unknown counts as spoke.

    The countdown is written to the HERDR label, never to the terminal, and is
    never load-bearing. The pane lookup's cost comes out of the wait, because
    `granted` is recomputed from the process clock after it.
    """
    raw_path = payload.get("transcript_path")
    if not raw_path:
        return (True, 0.0)
    path = Path(raw_path)
    if not path.is_file():
        return (True, 0.0)
    if _queue_pending(path, session):
        return (True, 0.0)

    wait = wait_seconds()
    poll = CP.env_int("CLAUDE_HANDOFF_UNATTENDED_POLL", 2, minimum=1, maximum=60)
    try:
        offset = path.stat().st_size
    except OSError:
        return (True, 0.0)

    pane = None
    if _effective_wait(wait, _HOOK_STARTED, time.monotonic()) > 0:
        try:
            pane = HA.resolve_pane(session)
        except HA.HerdrUnavailable as exc:
            print(f"checkpoint-offer: countdown unavailable: {exc}", file=sys.stderr)

    labels_alive = pane is not None
    next_label = 0.0

    now = time.monotonic()
    granted = _effective_wait(wait, _HOOK_STARTED, now)
    deadline = now + granted
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if labels_alive and now >= next_label:
                remaining = int(deadline - now)
                try:
                    HA.set_label(pane, f"wait-{remaining}s-auto")
                except HA.HerdrUnavailable as exc:
                    labels_alive = False
                    print(
                        f"checkpoint-offer: countdown stopped: {exc}", file=sys.stderr
                    )
                next_label = now + LABEL_REFRESH_SECONDS

            time.sleep(min(poll, max(deadline - time.monotonic(), 0.1)))
            try:
                size = path.stat().st_size
            except OSError:
                return (True, granted)
            if size <= offset:
                continue
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    fresh = handle.read()
            except OSError:
                return (True, granted)
            offset = size
            if _operator_spoke(fresh, session):
                return (True, granted)
        return (False, granted)
    finally:
        # Both exits: a label frozen at "12s" is worse than none.
        if pane is not None:
            try:
                HA.clear_label(pane)
            except HA.HerdrUnavailable as exc:
                print(
                    f"checkpoint-offer: countdown not cleared: {exc}", file=sys.stderr
                )


def _notify_stall(reason: str) -> None:
    """Tell the operator his unattended run stopped involuntarily. Best effort, never fatal.

    A notice to the operator's own bot only. The targets live in `.env`, so
    `load_env()` runs first (MEASURED 2026-08-31: without it every target
    resolved empty and no notice was ever sent).
    """
    try:
        from scripts.utils.paths import load_env

        load_env()
    except Exception as exc:  # noqa: BLE001 - a notice worth trying is never worth a broken turn
        print(f"checkpoint-offer: .env not loaded for the stall notice: {exc}",
              file=sys.stderr)

    target = ""
    for var in (
        "CHECKPOINT_TELEGRAM_TARGET",
        "OPS_RADAR_TELEGRAM_TARGET",
        "ODIN_CADENCE_TELEGRAM_TARGET",
    ):
        if os.environ.get(var, "").strip():
            target = os.environ[var].strip()
            break
    if not target:
        return
    try:
        from scripts.utils import telegram_notify

        telegram_notify.notify(target, f"HEADING OS: unattended run stopped. {reason}")
    except Exception as exc:  # noqa: BLE001 - a missed notice never blocks the stop
        print(f"checkpoint-offer: stall notice failed: {exc}", file=sys.stderr)


def _pause_unattended(
    state: dict, state_path: Path, reason: str, *, notify: bool
) -> int:
    """Stop continuing this stretch; leave the operator's switch alone.

    Returning 0 IS the stop. Recorded ONCE, so `--unattended status` keeps the
    moment it stopped. `notify` is the caller's choice: the ceiling and the
    no-progress bound send a notice, the done marker does not (operator decision
    2026-09-27, after six notices in one night that said nothing).
    """
    if state.get("unattended_paused_at"):
        return 0

    persist(
        state_path,
        unattended_paused_at=CP.utc_now().isoformat(),
        unattended_stop_reason=reason,
        unattended_last_stop=reason,
    )
    if notify:
        _notify_stall(reason)
    return 0


def _new_unattended_window(fresh: dict) -> dict:
    """Clear the stretch counters for a turn the operator has just started.

    A done marker this hook has not acted on yet (no `unattended_paused_at`) was
    written during the turn now ending, and is carried across.
    """
    carry = None
    if fresh.get("unattended_done_at") and not fresh.get("unattended_paused_at"):
        carry = (fresh["unattended_done_at"], fresh.get("unattended_done_note"))
    clear_unattended_window(fresh)
    if carry:
        fresh["unattended_done_at"], fresh["unattended_done_note"] = carry
    return fresh


def unattended_turn(
    payload: dict,
    state: dict,
    state_path: Path,
    used: float | None,
    turn: str,
    note: str = "",
) -> int:
    """Wait for the operator, then either hand the turn back or continue it.

    `note` leads the continuation message: why a /second-wind run sent nothing,
    so the assistant can repair the handoff instead of the stretch ending
    silently (review finding, 2026-10-14).

    Ends a stretch on the done marker, the ceiling (`CLAUDE_HANDOFF_UNATTENDED_MAX`,
    100 until a measured night says otherwise) or the no-progress bound, which
    is what keeps a stretch under the harness's Stop-block cap (see
    `IDLE_CONTINUATION_LIMIT`). None lowers the switch.
    """
    maximum = CP.env_int(
        "CLAUDE_HANDOFF_UNATTENDED_MAX", 100, minimum=1, maximum=10000
    )

    # A `prompt_id` this hook did not continue is a turn the OPERATOR started: a
    # new stretch. An empty one clears nothing, which keeps the ceiling alive.
    if turn and state.get("unattended_turn_id") != turn:
        persist(state_path, _mutate=_new_unattended_window)
        state = CP.read_json(state_path)

    done = int(state.get("unattended_continuations") or 0)

    # Both ends before the wait: a finished stretch hands back at once.
    if state.get("unattended_done_at"):
        note = state.get("unattended_done_note") or "no note given"
        return _pause_unattended(
            state, state_path, f"the plan is finished: {note}", notify=False
        )
    if done >= maximum:
        return _pause_unattended(
            state,
            state_path,
            f"reached the ceiling of {maximum} continuations",
            notify=True,
        )

    session = CP.session_id(payload)

    # The no-progress bound, judged only on a turn this hook continued. An
    # unreadable count neither extends nor resets the streak.
    calls, tasks = _transcript_facts(payload, session)

    # A finished answer to a question owes nothing: hand the turn back at once,
    # with no grace wait and no continuation text (operator, 2026-10-19, HOS-248).
    # A /second-wind refusal note is a repair still owed, so it always continues.
    if not note and not _open_work(payload, tasks, turn, session):
        return 0

    baseline = state.get("unattended_tool_baseline")
    idle = int(state.get("unattended_idle_streak") or 0)
    ours = bool(turn) and state.get("unattended_turn_id") == turn
    if not ours:
        idle = 0
    elif isinstance(baseline, int) and calls is not None:
        idle = idle + 1 if calls <= baseline else 0
    if idle >= IDLE_CONTINUATION_LIMIT:
        return _pause_unattended(
            state,
            state_path,
            f"{idle} continued turns in a row made no tool call",
            notify=True,
        )

    spoke, granted = _wait_out_the_grace(payload, session)
    if spoke:
        return 0

    # Read before the persist below overwrites it.
    rebuilt = _context_was_rebuilt(state, state.get("unattended_last_at"))

    done += 1
    # The standing rules go out once per SESSION (`--done` does not remove them
    # from the assistant's context), and again after a compaction.
    show_rules = rebuilt or not state.get("unattended_rules_shown")
    persist(
        state_path,
        unattended_continuations=done,
        unattended_turn_id=turn,
        unattended_last_at=CP.utc_now().isoformat(),
        unattended_rules_shown=True,
        unattended_tool_baseline=calls,
        unattended_idle_streak=idle,
        # The session-wide record behind the `Unattended:` line (2026-10-14).
        # Not in `_WINDOW_KEYS`, so a new stretch never resets it.
        unattended_total=int(state.get("unattended_total") or 0) + 1,
        unattended_first_at=state.get("unattended_first_at") or CP.utc_now().isoformat(),
        unattended_last_at_total=CP.utc_now().isoformat(),
    )

    open_items = _open_items_sentence(tasks)
    if show_rules:
        reason = UNATTENDED_WRAPPER.format(
            usage=_usage_phrase(used),
            wait=int(granted),
            open_items=open_items,
            done=done,
            maximum=maximum,
            standing=UNATTENDED_STANDING_INSTRUCTION,
        )
    else:
        reason = UNATTENDED_WRAPPER_REPEAT.format(
            usage=_usage_phrase(used),
            open_items=open_items,
            done=done,
            maximum=maximum,
        )
    if note:
        reason = f"{note}\n\n{reason}"
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return 0
