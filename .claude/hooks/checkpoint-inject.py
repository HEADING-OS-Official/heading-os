#!/usr/bin/env python3
"""
checkpoint-inject.py - Claude Code SessionStart hook (matcher: compact|clear|resume).

What each source injects, by operator decision of 2026-09-27 (the handover plan,
"Yard 1"). Every case carries the `Master instruction:` line first, and after it
the session's pinned items verbatim when it has any ("Yard 2", P2; after a
`/clear` they are the previous session's, and they are copied into this one).

  - clear: the previous session OF THE SAME CLAUDE CODE PROCESS
    (`CP.claude_process`, the pid and its /proc start time) is found through
    the state files its hooks wrote. Its newest handoff is injected in full,
    once, when it was written after that session's last operator message;
    otherwise a MECHANICAL handoff is built from its transcript (master
    prompt, the last five operator messages verbatim, the files it wrote, git
    status, the transcript path) and filed in the archive. Either way that
    session is marked consumed, so a second `/clear` in a row carries nothing.
    Bounded under the 10,000-character hook cap
    (https://code.claude.com/docs/en/hooks); an oversized handoff is cut to its
    objective, state and next steps with the instruction to read the full file.
    Two different blocks reach the SCREEN, and no line is in both (HOS-213).
    Above the `/clear` line, the JSON `systemMessage` of this run says what was
    carried (`CP.clear_passport`): source session, when and how the handoff was
    written, master instruction, pins count, handoff path. Below it, a second
    registration, `--rewake` with `"asyncRewake": true`, wakes the model to show
    where the work resumes (`CP.resume_lines`, `_rewake`): the handoff's
    `## Resume` block, which /clear-handover writes in the operator's language,
    else the first line of its Objective, Current state, Next steps and Open
    issues. The injection itself moves into `additionalContext`, and its closing
    asks for that same lower block as the first reply.
  - resume: this session's own pointer pair, as below.
  - compact: an excerpt of this session's newest checkpoint written after the
    PREVIOUS compaction, with its time and path. The compaction summary is
    already in context, so the full handoff would only repeat it.

resume and any other source: THIS session's own pointer pair
(`CP.latest_dir`), never a fallback to "the newest handoff on disk", which with
several sessions open is usually somebody else's work. The shared pointer pair
serves scripts/next-signal.py only. Silent when this session has no pointer and
no pins; the registration excludes `startup`.

Truncates to bounded sizes so a very large handoff cannot dominate context.
"""

import json
import subprocess  # nosec B404 - fixed argv, never shell=True
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_BOOT = Path(__file__).resolve()
_ROOT = _BOOT.parent.parent.parent
for _candidate in [_BOOT.parent, *_BOOT.parents]:
    if (_candidate / "scripts" / "utils" / "checkpoint_paths.py").is_file():
        _ROOT = _candidate
        sys.path.insert(0, str(_candidate))
        break
from scripts.utils import checkpoint_paths as CP  # noqa: E402
from scripts.utils import checkpoint_autonomy as AU  # noqa: E402

CP.force_utf8()

MAX_SUMMARY_CHARS = 8000
MAX_PROMPT_CHARS = 4000

# The handoff's `## Safe resume` block governs both closings (2026-10-27,
# HOS-263): "continue the latest unfinished task" alone contradicted a Safe
# resume that said the next step waits for the operator.
UNATTENDED_CLOSING = (
    "UNATTENDED: resume from this handoff without asking for confirmation, as "
    "its `## Safe resume` block says when it has one, otherwise with the latest "
    "unfinished task. Anything under Awaiting approval still waits for the "
    "operator. Briefly restate the objective and the next concrete action, then "
    "proceed. Repository state is authoritative."
)

MANUAL_CLOSING = (
    "Use the handoff to resume as its `## Safe resume` block says, or, when it "
    "has none, continue the latest unfinished task. Repository state is "
    "authoritative."
)

UNATTENDED_AFTER_COMPACT = """# Checkpoint

UNATTENDED: continue the latest unfinished task from the compaction summary
above, without asking for confirmation. Briefly restate the objective and the
next concrete action, then proceed. Repository state is authoritative.

The summary may close with a next step that tells you to stop and wait for the
operator. That step describes the end of the turn the harness compacted, so it
is already done. Do not read it as a current instruction. Go on to the next
step that is still open.

The full handoff is not re-injected here: the compaction summary already in
context covers it, so only the master instruction and the checkpoint excerpt
above are added.
"""


def read_limited(path: Path, limit: int) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - an unreadable pointer is not fatal
        print(f"checkpoint-inject: unreadable pointer {path.name}: {exc}", file=sys.stderr)
        return ""
    if len(text) <= limit:
        return text
    # Unreachable from the current writer (at most ~6801 characters, MEASURED
    # 2026-08-20); a guard against a pointer written before that bound.
    return text[:limit] + "\n\n[Truncated by checkpoint-inject]\n"


# The hook cap is 10,000 characters for plain stdout
# (https://code.claude.com/docs/en/hooks, read 2026-09-27); past it the harness
# injects a 2,000-character preview and a file path. Everything this hook prints
# stays under this bound, which leaves room for the frame around the handoff.
MAX_INJECT = 9000
MAX_EXCERPT = 3500
OPERATOR_MESSAGES = 5
MAX_MESSAGE_CHARS = 1200
MAX_WRITTEN = 25
# `Safe resume` first: the next session reads it before anything else (2026-10-14).
CORE_SECTIONS = ("Safe resume", "Objective", "Current state", "Next steps")


def _stamp_of(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(
            timespec="seconds")
    except OSError:
        return "unknown time"


def _git_status(project: Path) -> str:
    try:
        proc = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["git", "status", "--short"], cwd=str(project),
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"(git status unavailable: {exc})"
    lines = proc.stdout.rstrip().splitlines()
    if len(lines) > 30:
        lines = lines[:30] + [f"[... {len(lines) - 30} more line(s)]"]
    return "\n".join(lines) or "(clean)"


def _written(transcript: str | None) -> str:
    try:
        from scripts.utils.session_scope import files_written
    except Exception as exc:  # noqa: BLE001 - an absent fact, not a failure
        return f"(unavailable: {exc})"
    paths = files_written(transcript)
    if paths is None:
        return "(the transcript could not be read)"
    live = sorted((p for p in paths if p.exists()),
                  key=lambda p: -p.stat().st_mtime)
    if not live:
        return "(none recorded)"
    shown = [p.as_posix() for p in live[:MAX_WRITTEN]]
    if len(live) > MAX_WRITTEN:
        shown.append(f"[... {len(live) - MAX_WRITTEN} more]")
    return "\n".join(shown)


def _bounded_handoff(path: Path, master: str, limit: int) -> str:
    """The handoff text, whole when it fits, else its core sections plus a pointer.

    `master` is the lead the caller shows first, the pinned items included, so
    neither is ever cut; the handoff's own copies of both are removed.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"{master}\n\nThe handoff at {path} could not be read ({exc})."
    if CP.master_line_in(text) is not None:
        # The handoff's own header line is shown by `master` above.
        text = "\n".join(ln for ln in text.splitlines()
                         if not ln.startswith(CP.MASTER_PREFIX))
    text = CP.drop_section(text, CP.PINS_HEADING)
    whole = f"{master}\n\nHandoff file: {path}\n\n{text}"
    if len(whole) <= limit:
        return whole
    core = CP.sections(text, CORE_SECTIONS) or text[: limit // 2]
    lead = (f"{master}\n\nRead the full file first: {path}\n"
            f"It is too long to inject whole; its safe-resume block, objective, "
            f"state and next steps follow.\n\n")
    return (lead + core)[:limit]


def _with_pins(master: str, pins: list[str]) -> str:
    """The master instruction, then the pinned items, verbatim. Never cut."""
    block = CP.pins_block(pins)
    return f"{master}\n\n{block}" if block else master


def _mechanical(prev: dict, project: Path, master: str, stale: Path | None,
                pins: list[str] | None = None) -> str | None:
    """A short handoff built by this script from the previous session's transcript.

    Anchors, not a retelling: a full handoff needs a model. None when the
    previous session left nothing to carry (no operator message at all), which
    is the second `/clear` in a row.
    """
    transcript = prev.get("transcript_path")
    digest = CP.transcript_digest(transcript)
    if digest is None or not digest["operator"]:
        return None
    last = digest["operator"][-OPERATOR_MESSAGES:]
    quoted = []
    for when, text in last:
        if len(text) > MAX_MESSAGE_CHARS:
            text = (text[:MAX_MESSAGE_CHARS]
                    + f"\n[cut here, {len(text) - MAX_MESSAGE_CHARS} more characters "
                    f"in the transcript]")
        quoted.append(f"--- {when}\n{text}")
    pinned = f"{CP.pins_block(pins)}\n\n" if pins else ""
    older = (f"\n\n## An earlier handoff of that session\n\n{stale} (written "
             f"before its last message, so it may be behind)") if stale else ""
    return f"""# Handoff - mechanical, built at /clear

{master}
Generated: {CP.utc_now().isoformat()}
Previous session: {prev.get("session_id") or "unknown"}
Task: {", ".join(_prev_tasks(prev)) or "none"}
Transcript: {transcript}

Built by the SessionStart hook from the previous session's transcript, because
that session wrote no handoff after its last message. These are anchors, not a
summary of the work: read the transcript for anything they do not settle.

{pinned}## The last {len(last)} operator message(s), verbatim

{chr(10).join(quoted)}

## Files that session wrote

{_written(transcript)}

## git status --short

{_git_status(project)}{older}
"""


def _prev_tasks(prev: dict) -> list[str]:
    """The task list of the session before /clear (task dossier, HOS-256)."""
    try:
        from scripts.utils import dossier
        return dossier.session_tasks(prev)
    except Exception as exc:  # noqa: BLE001 - the injection matters more than the list
        print(f"checkpoint-inject: task list unreadable: {exc}", file=sys.stderr)
        return []


def _carry_tasks(prev: dict, state_path: Path) -> list[str]:
    """Carry the previous session's tasks and candidates into this one as CANDIDATES.

    Never into `dossier_tasks` (HOS-259): no writer of this session posts to a
    candidate until a prompt `prompt-archive.py` saves names it.
    """
    try:
        from scripts.utils import dossier
        if not (dossier.session_tasks(prev) or dossier.carried_tasks(prev)):
            return []
        # No Lific call in this hook: the list is carried as is, and a closed
        # issue is filtered by the flush that would post to it.
        with CP.locked_state(state_path) as mine:
            return dossier.carry_into(mine, prev)
    except Exception as exc:  # noqa: BLE001 - the injection still goes out
        print(f"checkpoint-inject: task list not carried: {exc}", file=sys.stderr)
        return []


def _post_mechanical(prev: dict, filed: Path | None, bottom: str) -> None:
    """One `handoff` record of kind `mechanical` on each task, after the file is on disk."""
    tasks = _prev_tasks(prev)
    if not tasks or filed is None:
        return
    try:
        from scripts.utils import dossier
        sid = prev.get("session_id") if isinstance(prev.get("session_id"), str) else ""
        dossier.post(tasks, "handoff", "mechanical", sid or "unknown",
                     bottom.splitlines()[:3], dossier.rel(filed))
    except Exception as exc:  # noqa: BLE001 - the injection still goes out
        print(f"checkpoint-inject: dossier record not posted: {exc}", file=sys.stderr)


def _file_mechanical(text: str, hdir: Path, slug: str) -> Path | None:
    """File the mechanical handoff in the archive, redacted. None when it was not."""
    try:
        from scripts.utils.secret_patterns import redact
        clean = redact(text)
        if not isinstance(clean, str):
            raise TypeError(f"redact() returned {type(clean).__name__}")
        stamp = CP.local_now().strftime("%Y-%m-%d-%H%M%S")
        path = hdir / f"{stamp}_handoff_mechanical_{slug}.md"
        CP.write_text_atomic(path, clean)
        return path
    except Exception as exc:  # noqa: BLE001 - the injection matters more than the file
        print(f"checkpoint-inject: mechanical handoff not filed: {exc}", file=sys.stderr)
        return None


def _after_clear(payload: dict, project: Path, slug: str,
                 state_path: Path) -> tuple[str, str, str] | None:
    """(context, top, bottom): the injection, what was carried, where work resumes.

    `top` is drawn above the `/clear` line and `bottom` below it (`_rewake`);
    no line is in both (HOS-213).
    """
    proc = CP.claude_process()
    if proc is None:
        print("checkpoint-inject: CLAUDE_PID or its /proc start time is unavailable, "
              "so the session before /clear cannot be found", file=sys.stderr)
        return None
    found = CP.previous_session_state(project, proc, slug)
    if found is None:
        return None
    prev_path, prev = found
    if prev.get("clear_consumed_by"):
        return None
    prev_sid = prev.get("session_id") if isinstance(prev.get("session_id"), str) else ""
    prev_slug = CP.safe_slug(prev_sid) if prev_sid else prev_path.stem.removeprefix("checkpoint-")
    hdir = CP.handoff_dir(project, _ROOT)
    master_value = CP.master_instruction(prev_sid, project, _ROOT, prev)
    pins = CP.read_pins(CP.pins_path(hdir, prev_slug, _ROOT))

    digest = CP.transcript_digest(prev.get("transcript_path"))
    last_said = None
    if digest and digest["operator"]:
        last_said = CP.iso_epoch(digest["operator"][-1][0])
    handoffs = CP.session_handoffs(hdir, prev_slug)
    newest = handoffs[0] if handoffs else None
    fresh = newest is not None and (
        last_said is None or newest.stat().st_mtime >= last_said - 1.0)

    if fresh:
        text = newest.read_text(encoding="utf-8", errors="replace")
        written_under = CP.master_line_in(text)
        master = _with_pins(CP.master_block(master_value, written_under), pins)
        body = _bounded_handoff(newest, master, MAX_INJECT - 600)
        how = f"its handoff, written {_stamp_of(newest)}"
        top = CP.clear_passport(text, newest, session=prev_sid or prev_slug,
                                master=master_value, pins=len(pins),
                                written=_stamp_of(newest))
        bottom = CP.resume_lines(text) or (
            "The handoff names no objective, state or next step; read it first.")
    else:
        master = CP.master_block(master_value, None)
        body = _mechanical(prev, project, master, newest, pins)
        if body is None:
            body_consumed = True
        else:
            body_consumed = False
            filed = _file_mechanical(body, hdir, prev_slug)
            top = CP.clear_passport(body, filed, session=prev_sid or prev_slug,
                                    master=master_value, pins=len(pins),
                                    written=CP.utc_now().isoformat(timespec="seconds"))
            said = next((ln for ln in digest["operator"][-1][1].splitlines()
                         if ln.strip()), "")
            bottom = ("Stopped at: that session wrote no handoff after its last "
                      "message.\n"
                      f"Last message: {CP.cut_line(said, CP.RESUME_LINE_CHARS - 14)}")
            _post_mechanical(prev, filed, bottom)
            if len(body) > MAX_INJECT - 600:
                body = body[: MAX_INJECT - 700] + (
                    f"\n\n[Cut to fit the hook cap. Full text: {filed}]" if filed
                    else "\n\n[Cut to fit the hook cap.]")
        how = "a mechanical handoff built from its transcript"
        if body_consumed:
            body = None

    def _consume(fresh_state: dict) -> dict:
        fresh_state["clear_consumed_by"] = CP.session_id(payload)
        fresh_state["clear_consumed_at"] = CP.utc_now().isoformat()
        return fresh_state

    try:
        with CP.locked_state(prev_path) as locked:
            _consume(locked)
        with CP.locked_state(state_path) as mine:
            if master_value and not master_value.startswith(("unknown", CP.MASTER_NONE)):
                mine["carried_master_instruction"] = master_value.split(" (carried over", 1)[0]
            mine["carried_from_session"] = prev_sid
            for key in CARRIED_KEYS:
                if key in prev and key not in mine:
                    mine[key] = prev[key]
            if prev.get("session_unattended"):
                mine["carried_unattended"] = True
    except Exception as exc:  # noqa: BLE001 - the injection still goes out
        print(f"checkpoint-inject: consumed mark not written: {exc}", file=sys.stderr)
    tasks = _carry_tasks(prev, state_path)
    # The pins stay with the work: the new session inherits them, so its own
    # compactions and handoffs carry them too.
    if pins:
        try:
            CP.append_pins(CP.pins_path(hdir, slug, _ROOT), pins)
        except Exception as exc:  # noqa: BLE001 - the injection still goes out
            print(f"checkpoint-inject: pins not carried to this session: {exc}",
                  file=sys.stderr)

    if body is None:
        return None
    if tasks:
        body += (f"\n\nCarried, not yet this session's tasks: {', '.join(tasks)}. A prompt "
                 f"saved with prompt-archive.py binds the ones its verbatim half names "
                 f"and drops the rest. Full history: "
                 f".venv/bin/python scripts/lific-board.py dossier {tasks[0]}")
    return f"""# Carried across /clear

The session before this /clear, in this same Claude Code process
({prev_sid or prev_slug}), left {how}. It is injected once.

{body}

---

Your first reply shows where the work resumes and nothing else: the lines
between BEGIN and END below, verbatim, without the two marker lines. If the
operator has already asked for something, show them first and then answer.
Repository state is authoritative.
BEGIN
{bottom}
END
""", top, bottom


def _after_compact(payload: dict, project: Path, slug: str, state: dict, unattended: bool) -> str:
    session = CP.session_id(payload) if CP.session_id_is_known(payload) else ""
    master = CP.master_block(CP.master_instruction(session, project, _ROOT, state), None)
    parts = [master]
    try:
        hdir = CP.handoff_dir(project, _ROOT)
        pins = CP.pins_block(CP.read_pins(CP.pins_path(hdir, slug, _ROOT)))
    except Exception as exc:  # noqa: BLE001 - no pins is a missing fact
        print(f"checkpoint-inject: pins unresolved: {exc}", file=sys.stderr)
        pins = ""
    if pins:
        parts.append(pins)
    digest = CP.transcript_digest(payload.get("transcript_path"))
    floor = None
    if digest and digest["boundaries"]:
        stamps = [CP.iso_epoch(b) for b in digest["boundaries"]]
        stamps = [t for t in stamps if t is not None]
        now = CP.utc_now().timestamp()
        # The compaction that fired this hook is usually already the newest
        # boundary on disk; the floor is the one BEFORE it. A newest boundary
        # older than ten minutes cannot be this one, so it is the floor itself.
        if stamps and now - stamps[-1] < 600:
            floor = stamps[-2] if len(stamps) > 1 else None
        elif stamps:
            floor = stamps[-1]
    try:
        hdir = CP.handoff_dir(project, _ROOT)
        candidates = CP.session_handoffs(hdir, slug, ("manual", "auto", "clear", "second-wind"))
    except Exception as exc:  # noqa: BLE001 - no excerpt is a missing fact
        print(f"checkpoint-inject: handoff archive unresolved: {exc}", file=sys.stderr)
        candidates = []
    newest = candidates[0] if candidates else None
    if newest is not None and (floor is None or newest.stat().st_mtime > floor):
        text = newest.read_text(encoding="utf-8", errors="replace")
        core = CP.sections(text, CORE_SECTIONS)[:MAX_EXCERPT]
        parts.append(f"## This session's checkpoint before the compaction\n\n"
                     f"Written {_stamp_of(newest)}: {newest}\n\n{core}")
    if unattended:
        parts.append(UNATTENDED_AFTER_COMPACT)
    return "\n\n".join(parts)


# Carried across /clear in HELM and in a yard alike (operator decision
# 2026-10-14, "carry it over in HELM and in a yard", translated): the unattended switch and the
# session-wide record behind the `Unattended:` line. Read in `_after_clear`.
CARRIED_KEYS = (
    "session_unattended", "unattended_total", "unattended_first_at", "unattended_last_at_total",
    "unattended_last_stop", "second_wind_attempts",
)

# The state key the clear case leaves its banner under, for `--rewake`.
BANNER_KEY = "clear_banner"
# How long `--rewake` waits for the synchronous run to leave the banner (its own
# registration times out at 5 s), and the age past which a banner is not this
# clear's.
REWAKE_WAIT = 8.0
REWAKE_POLL = 0.2
REWAKE_MAX_AGE = 120.0

REWAKE_REASON = """/clear has just run in this terminal. This is not an error and nothing failed.
Before the operator types anything, show where the work resumes: your whole reply
is the lines between BEGIN and END below, verbatim, without those two marker
lines. Call no tool and no skill. Then stop and wait for the operator's next
message.
BEGIN
{banner}
END"""

# The same, when unattended is on for the new session (a yard by default, or a
# switch carried across /clear): the work goes on without the operator.
REWAKE_CONTINUE = """/clear has just run in this terminal after /second-wind or /clear-handover. \
This is not an error. Unattended is on for this session, so do not wait for the operator: \
show the lines between BEGIN and END below, then read `## Safe resume` in the injected \
handoff, recreate the open tasks it lists, and continue with its Next action. Do nothing \
listed under Awaiting approval without the operator's word.
BEGIN
{banner}
END"""


# After /second-wind sent `/compact` in an unattended session, nothing else
# starts the next turn: a manual compaction leaves the session idle.
REWAKE_AFTER_COMPACT = """/compact has just run in this terminal after /second-wind, and unattended is \
on for this session. This is not an error. Do not wait for the operator: read `## Safe resume` \
in the handoff the compaction left in context, and continue with its Next action. Do nothing \
listed under Awaiting approval without the operator's word."""


def _rewake_after_compact(state_path: Path) -> int:
    """Exit 2 to continue an unattended session that /second-wind just compacted; else 0.

    Only when this hook's own Stop counterpart sent the `/compact`
    (`reset_sent_command`) within `REWAKE_MAX_AGE`, so an operator's own
    `/compact` or the native one wakes nobody.
    """
    state = CP.read_json(state_path)
    sent = CP.iso_epoch(state.get("reset_sent_at"))
    if (state.get("reset_sent_command") != "/compact" or sent is None
            or CP.utc_now().timestamp() - sent > REWAKE_MAX_AGE
            or not AU.unattended_mode(state)):
        return 0
    try:
        with CP.locked_state(state_path) as mine:
            mine.pop("reset_sent_command", None)
    except Exception as exc:  # noqa: BLE001 - a second wake costs one turn
        print(f"checkpoint-inject: compact wake not consumed: {exc}", file=sys.stderr)
    print(REWAKE_AFTER_COMPACT, file=sys.stderr)
    return 2


def _rewake(payload: dict, state_path: Path) -> int:
    """The `asyncRewake` registration: exit 2 with the banner, so a model turn shows it.

    After `/clear` the harness draws every SessionStart message above the
    `/clear` line, so the synchronous `systemMessage` reads as something printed
    before the clear. MEASURED 2026-09-28 on 2.1.283 in a scratch session: a
    SessionStart hook registered with `"asyncRewake": true` that exits 2 wakes
    the model at once, with no input typed; its stderr arrives as a system
    reminder, and the reply is drawn BELOW `/clear`. A plain `async` hook drew
    nothing, a FileChanged hook drew nothing, and `/dev/tty` is not open to a
    hook (ENXIO).

    The banner is where the work resumes (`CP.resume_lines`), never the
    `systemMessage` block above the line. It comes from the synchronous run's
    state rather than being rebuilt here, because that run also marks the previous session consumed; this one
    takes it once and removes it. Exit 0, silent, when there is none: a `/clear`
    that carried nothing wakes nobody.
    """
    source = (payload.get("source") or "").strip()
    if source == "compact":
        return _rewake_after_compact(state_path)
    if source != "clear":
        return 0
    deadline = time.monotonic() + REWAKE_WAIT
    entry = None
    while entry is None:
        if isinstance(CP.read_json(state_path).get(BANNER_KEY), dict):
            try:
                with CP.locked_state(state_path) as mine:
                    entry = mine.pop(BANNER_KEY, None)
            except Exception as exc:  # noqa: BLE001 - a missed banner is not fatal
                print(f"checkpoint-inject: banner not taken: {exc}", file=sys.stderr)
                return 0
        if entry is None:
            if time.monotonic() >= deadline:
                return 0
            time.sleep(REWAKE_POLL)
    written = CP.iso_epoch(entry.get("at"))
    text = entry.get("text")
    if (not isinstance(text, str) or not text.strip() or written is None
            or CP.utc_now().timestamp() - written > REWAKE_MAX_AGE):
        return 0
    reason = REWAKE_CONTINUE if AU.unattended_mode(CP.read_json(state_path)) else REWAKE_REASON
    print(reason.format(banner=text.strip()), file=sys.stderr)
    return 2


def main() -> int:
    # An unparseable payload still has CLAUDE_CODE_SESSION_ID to fall back on.
    payload = CP.read_payload("checkpoint-inject", empty_ok=True) or {}

    # From THIS session's state, so a switch flipped mid-work survives the compaction.
    project = CP.project_root(payload)
    slug = CP.session_slug(payload)
    state_path = CP.state_path(project, slug)
    if "--rewake" in sys.argv[1:]:
        # Runs beside the synchronous registration, so it writes no process record.
        return _rewake(payload, state_path)
    state = CP.read_json(state_path)
    unattended = AU.unattended_mode(state)

    # This session's own process record, written before anything else, so a
    # `/clear` issued before its first turn still finds it.
    try:
        with CP.locked_state(state_path) as mine:
            CP.record_process(mine, payload)
    except Exception as exc:  # noqa: BLE001 - the injection matters more
        print(f"checkpoint-inject: process record not written: {exc}", file=sys.stderr)

    source = (payload.get("source") or "").strip()

    if source == "compact":
        print(_after_compact(payload, project, slug, state, unattended))
        return 0
    if source == "clear":
        carried = _after_clear(payload, project, slug, state_path)
        if carried:
            # JSON so the banner reaches the SCREEN: after /clear no model turn
            # runs until the operator types. MEASURED 2026-09-28 on 2.1.283 in a
            # scratch session: `systemMessage` shows at once as "SessionStart:clear
            # says: ...", and `additionalContext` of 9008 characters (JSON stdout
            # 10,817) reached the transcript whole, as plain stdout did.
            # But the harness draws every SessionStart message ABOVE the `/clear`
            # line, so on the new screen it reads as printed before the clear
            # (the operator's report of 2026-09-28). So the message carries only
            # what was carried, and where the work resumes is left in this
            # session's state for the `--rewake` registration, whose model turn
            # draws it BELOW `/clear`. One block sent to both was printed twice
            # (the operator's report of 2026-10-08, HOS-213).
            context, top, bottom = carried
            try:
                with CP.locked_state(state_path) as mine:
                    mine[BANNER_KEY] = {"text": bottom, "at": CP.utc_now().isoformat()}
            except Exception as exc:  # noqa: BLE001 - the injection still goes out
                print(f"checkpoint-inject: banner not left for --rewake: {exc}",
                      file=sys.stderr)
            print(json.dumps({"systemMessage": top, "hookSpecificOutput": {
                "hookEventName": "SessionStart", "additionalContext": context}},
                ensure_ascii=False))
        return 0

    latest = CP.latest_dir(CP.handoff_dir(project, _ROOT), slug, _ROOT)

    summary = read_limited(latest / "summary.md", MAX_SUMMARY_CHARS)
    prompt = read_limited(latest / "prompt.md", MAX_PROMPT_CHARS)
    pins = CP.read_pins(latest / CP.PINS_FILENAME)

    sections: list[str] = []
    if summary:
        sections.append(f"## Latest summary\n\n{summary}")
    if prompt:
        sections.append(f"## Continuation prompt\n\n{prompt}")
    if not sections and not pins:
        return 0
    pins_only = not sections
    if pins_only:
        sections.append("No handoff pointer was written in this session; only its "
                        "pinned items above are carried.")

    body = "\n\n".join(sections)
    closing = UNATTENDED_CLOSING if unattended else MANUAL_CLOSING
    session = CP.session_id(payload) if CP.session_id_is_known(payload) else ""
    master = _with_pins(
        CP.master_block(CP.master_instruction(session, project, _ROOT, state),
                        CP.master_line_in(summary)),
        pins)

    # "saved by this session" only when the session HAS an id: without one,
    # `.latest/session/` is a bucket every id-less session shares.
    if pins_only:
        provenance = f"This session ({slug}) has pinned items and no handoff."
    elif CP.session_id_is_known(payload):
        provenance = (f"A handoff saved by this session ({slug}) was found in the "
                      f"handoff archive.")
    else:
        provenance = (f"A handoff was found under the shared pointer slug "
                      f"'{slug}'. This session reported no id, so that bucket "
                      f"holds every id-less session's handoff and this one may "
                      f"belong to a DIFFERENT session.")

    if not pins_only:
        provenance += (" Check its Generated timestamp before trusting it over the "
                       "repository.")

    def _render(text: str) -> str:
        return f"""# Auto-injected handoff

{master}

{provenance}

{text}

---

{closing}
"""

    out = _render(body)
    if len(out) > MAX_INJECT:
        # The same bound as the clear branch: the master instruction and the
        # pins are never cut, the handoff body is, at a line boundary.
        note = (f"\n\n[Cut to stay under the hook output cap; the full text is "
                f"in {latest}]")
        room = max(0, MAX_INJECT - (len(out) - len(body)) - len(note))
        cut = body[:room]
        if "\n" in cut:
            cut = cut[: cut.rfind("\n")]
        out = _render(cut + note)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
