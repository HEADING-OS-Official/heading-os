#!/usr/bin/env python3
"""
hindsight-hint-async.py - run the memory plugin's prompt hook in the background.

The Hindsight coding-agent plugin answers the session's first prompt with a
`reflect` synthesis, and it does so SYNCHRONOUSLY inside the UserPromptSubmit
hook: the prompt does not reach the model until reflect returns or times out,
and reflect in the plugin's query form takes minutes on a populated bank. The
measurements are in the HOS-175 survey in the data repository (registry name
`research-surveys`); they stay out of this public file on purpose.

Claude Code can run a command hook with `"async": true`: the prompt goes through
at once and the hook's `additionalContext` is delivered when it finishes. In
an interactive session it arrives at the first tool result after the hook
exits; a session that goes idle first receives it with the next prompt (the
same survey records the probe that established this). This wrapper is what makes the plugin's
hook safe to register that way:

  python3 hindsight-hint-async.py -- node <plugin>/dist/claude-hook.js

Where it is registered: in the USER-level `~/.claude/settings.json`, as an
`"async": true` UserPromptSubmit hook, because the plugin's script path is per
machine and the plugin serves every project on it. That entry names this file
by its absolute path in the main clone, so every checkout, a yard included,
runs the main clone's copy. It is deliberately NOT in
the engine's platform settings templates: a user-level hook already fires in
this repository, so a template entry would register it a second time and start
two reflects per prompt.

  - ONE run per session at a time. Synchronously, the harness never started the
    next prompt's hook before this one returned; in the background it does, and
    the plugin records its answer only when reflect ends, so every prompt in the
    meantime would start another reflect. A second run while one is in flight
    exits silently.
  - The session's first two runs take one of HINDSIGHT_HINT_SLOTS machine-wide
    slots (default 1), waiting up to HINDSIGHT_HINT_MAX_WAIT seconds (900).
    Two because the plugin makes at most two reflect attempts per session;
    later runs only fetch the page list and go straight through. Sessions
    started together each sending a reflect at once is how the server was seen
    to answer one and fail the others (recorded in the same survey).
  - A harness envelope (`<task-notification>`, the shared
    `hook_common.is_machine_envelope`) is skipped. The harness submits
    background-task completions through UserPromptSubmit, and the plugin spent
    reflect attempts on them as if they were goals.
  - A prompt whose first line is the yard-brief marker is skipped, and the skip
    is recorded in `runs.jsonl` as `skipped_brief`. A brief is long, a reflect
    on it held the machine-wide slot until it timed out (HOS-209 in the
    tracker), and the yard agent already has the brief in hand. The marker is
    read from the release gate, `_dispatch.py` beside this file, as
    `scripts/herdr-brief.py` reads it, so the literal exists once. If the gate
    cannot be loaded the skip is off for that run, logged, and the hint runs.
  - Only `hookSpecificOutput` is passed on. A background hook's `systemMessage`
    goes to the model rather than to the operator, so the plugin's banner would
    be noise in the context. Its `additionalContext` is cut at a line boundary
    to stay under the harness's 10,000-character cap, with a note saying so;
    past the cap the harness would inject only a 2,000-character preview.
  - From the session's third run on, a block identical to one already
    forwarded in this session is not forwarded again (operator decision
    2026-10-16): the plugin answers every later prompt with the same tool guide.
  - Every failure is logged to stderr and the hook exits 0: a hint that does not
    arrive must never break a prompt.

State lives under HINDSIGHT_HINT_STATE_DIR, else the shared runtime store
(`runtime:hook-state/state`, subdirectory `hindsight-hint`), because the slots
must be one set for every checkout on the machine. One line per run goes to
`runs.jsonl` there: waited, ran, outcome, context size.
"""

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts.utils.pid_liveness import pid_runs_program  # noqa: E402

SLOTTED_RUNS = 2
GATE = Path(__file__).resolve().parent / "_dispatch.py"
# The harness cap is 10,000 characters per string (hooks reference, read
# 2026-10-16); the margin leaves room for the cut note.
MAX_CONTEXT_CHARS = 9500
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _log(msg: str, exc: BaseException | None = None) -> None:
    tail = f": {exc}" if exc is not None else ""
    print(f"hindsight-hint-async: {msg}{tail}", file=sys.stderr)


def _state_dir() -> Path:
    from scripts.utils.hook_common import hook_state_dir  # noqa: PLC0415
    path = hook_state_dir("hindsight-hint", "HINDSIGHT_HINT_STATE_DIR")
    (path / "sessions").mkdir(parents=True, exist_ok=True)
    return path


def _brief_marker() -> str | None:
    """The yard-brief marker from the release gate, or None if it cannot be loaded."""
    try:
        spec = importlib.util.spec_from_file_location("heading_release_gate", GATE)
        if spec is None or spec.loader is None:
            raise ImportError(f"no loader for {GATE}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return str(module._BRIEF_MARKER)
    except Exception as exc:  # noqa: BLE001 - any failure only turns the skip off
        _log("could not read the brief marker from the release gate, not skipping briefs", exc)
        return None


@contextlib.contextmanager
def _session_claim(lock: Path):
    """Yield True when this run owns the session, False when another run does."""
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                holder = int(lock.read_text().strip() or "0")
            except (OSError, ValueError) as exc:
                _log("unreadable session lock, treating it as held", exc)
                yield False
                return
            if holder and pid_runs_program(holder, "hindsight-hint-async"):
                yield False
                return
            lock.unlink(missing_ok=True)   # left by a run that died; take it over
            continue
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        try:
            yield True
        finally:
            lock.unlink(missing_ok=True)
        return
    yield False


@contextlib.contextmanager
def _slot(state: Path, slots: int, max_wait: float):
    """Hold one of `slots` machine-wide flock slots; yield False on giving up."""
    deadline = time.monotonic() + max_wait
    handles = [open(state / f"slot-{i}.lock", "a") for i in range(slots)]  # noqa: SIM115
    try:
        while True:
            for h in handles:
                try:
                    fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    yield True
                finally:
                    fcntl.flock(h, fcntl.LOCK_UN)
                return
            if time.monotonic() >= deadline:
                yield False
                return
            time.sleep(0.25)
    finally:
        for h in handles:
            h.close()


def _record(state: Path, row: dict) -> None:
    row["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with (state / "runs.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as exc:
        _log("could not append runs.jsonl", exc)


def _bump(counter: Path) -> int:
    """This session's run number, starting at 1. Only the session's owner calls it."""
    try:
        n = int(counter.read_text().strip() or "0") + 1
    except FileNotFoundError:
        n = 1
    except (OSError, ValueError) as exc:
        _log("unreadable run counter, starting again", exc)
        n = 1
    tmp = counter.with_suffix(".tmp")
    tmp.write_text(str(n))
    os.replace(tmp, counter)
    return n


def _cap(context: str) -> str:
    """`context` whole when it fits MAX_CONTEXT_CHARS, else cut at a line boundary with a note."""
    if len(context) <= MAX_CONTEXT_CHARS:
        return context
    note = ("\n\n[Cut by hindsight-hint-async.py to stay under the hook output cap; "
            f"{len(context)} characters were returned.]")
    cut = context[: MAX_CONTEXT_CHARS - len(note)]
    if "\n" in cut:
        cut = cut[: cut.rfind("\n")]
    return cut + note


def _forward(stdout: str) -> str:
    """The target's hookSpecificOutput alone, capped, as the JSON a hook prints, or ''."""
    try:
        out = json.loads(stdout) if stdout.strip() else {}
    except ValueError as exc:
        _log("target printed something that is not JSON", exc)
        return ""
    spec = out.get("hookSpecificOutput") if isinstance(out, dict) else None
    if not isinstance(spec, dict) or not isinstance(spec.get("additionalContext"), str) \
            or not spec.get("additionalContext"):
        return ""
    spec = {**spec, "additionalContext": _cap(spec["additionalContext"])}
    return json.dumps({"hookSpecificOutput": spec})


def _seen_before(seen: Path, forwarded: str) -> bool:
    """Whether this session already forwarded `forwarded`; records it when not."""
    digest = hashlib.sha256(forwarded.encode()).hexdigest()
    try:
        known = set(seen.read_text().split())
    except FileNotFoundError:
        known = set()
    except OSError as exc:
        _log("unreadable forwarded-block record, forwarding", exc)
        return False
    if digest in known:
        return True
    try:
        with seen.open("a", encoding="utf-8") as f:
            f.write(digest + "\n")
    except OSError as exc:
        _log("could not record the forwarded block", exc)
    return False


def main(argv: list[str]) -> None:
    if "--" not in argv or not argv[argv.index("--") + 1:]:
        _log("usage: hindsight-hint-async.py -- <plugin hook command...>")
        return
    target = argv[argv.index("--") + 1:]
    raw = sys.stdin.read()
    try:
        ev = json.loads(raw or "{}")
    except ValueError as exc:
        _log("stdin is not JSON", exc)
        return
    if not isinstance(ev, dict):
        _log(f"stdin was {type(ev).__name__}, not an object")
        return
    prompt = str(ev.get("prompt") or "").strip()
    if not prompt:
        return
    try:
        from scripts.utils.hook_common import is_machine_envelope  # noqa: PLC0415
    except ImportError as exc:
        _log("shared hook helpers unavailable, envelope check skipped", exc)
    else:
        if is_machine_envelope(prompt):
            return

    state = _state_dir()
    session = hashlib.sha256(str(ev.get("session_id") or "no-session").encode()).hexdigest()[:16]
    row = {"session": session}
    marker = _brief_marker()
    if marker and prompt.split("\n", 1)[0].strip() == marker:
        row["outcome"] = "skipped_brief"
        _record(state, row)
        return
    with _session_claim(state / "sessions" / f"{session}.lock") as owned:
        if not owned:
            row["outcome"] = "session_busy"
            _record(state, row)
            return
        run = _bump(state / "sessions" / f"{session}.runs")
        slots = max(1, int(os.environ.get("HINDSIGHT_HINT_SLOTS", "1")))
        max_wait = float(os.environ.get("HINDSIGHT_HINT_MAX_WAIT", "900"))
        t0 = time.monotonic()
        with (_slot(state, slots, max_wait) if run <= SLOTTED_RUNS
              else contextlib.nullcontext(True)) as got:
            row.update(run=run, waited_s=round(time.monotonic() - t0, 1))
            if not got:
                row["outcome"] = "queue_timeout"
                _record(state, row)
                return
            t1 = time.monotonic()
            try:
                done = subprocess.run(target, input=raw, capture_output=True, text=True,
                                      timeout=float(os.environ.get("HINDSIGHT_HINT_RUN_TIMEOUT", "900")))
            except (OSError, subprocess.SubprocessError) as exc:
                _log("target did not run to completion", exc)
                row.update(ran_s=round(time.monotonic() - t1, 1), outcome="target_failed")
                _record(state, row)
                return
            forwarded = _forward(done.stdout)
            seen = state / "sessions" / f"{session}.forwarded"
            # Every forwarded block is recorded; only from run 3 on does a repeat stop it.
            repeat = bool(forwarded) and _seen_before(seen, forwarded) and run > SLOTTED_RUNS
            outcome = "repeat_suppressed" if repeat else ("context" if forwarded else "empty")
            row.update(ran_s=round(time.monotonic() - t1, 1), exit=done.returncode,
                       outcome=outcome, chars=len(forwarded))
            if repeat:
                forwarded = ""
            _record(state, row)
            if done.stderr.strip():
                _log("target stderr: " + done.stderr.strip()[-500:])
            if forwarded:
                print(forwarded)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as exc:  # noqa: BLE001 - never break a prompt, but never hide it either
        _log("unexpected failure", exc)
    sys.exit(0)
