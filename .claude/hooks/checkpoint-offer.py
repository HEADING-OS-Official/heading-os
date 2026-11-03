#!/usr/bin/env python3
"""
checkpoint-offer.py - Claude Code Stop hook.

Reads THIS session's checkpoint-<session-slug>.json (`checkpoint_paths.state_root`),
written by checkpoint-statusline.py, and decides what this pause does:

  - after `/second-wind` or `/clear-handover`: verify the handoff on disk, Safe
    resume block included, then send `/clear` or `/compact` through HERDR
    (`_reset`). /second-wind sends `/clear` only when nothing runs in the
    background (`background_work.background_state`); /clear-handover always
    sends `/clear`, and refuses when work is seen running;
  - at the first threshold and at the last call: show the /second-wind menu as
    a `systemMessage`. Attended, the Stop is blocked once so the model asks
    through the native AskUserQuestion selector (arrow keys + Enter); no hook
    output can draw a selector itself. Unattended, it spends no model turn and
    the second registration, `--rewake` with asyncRewake, waits out the grace
    and wakes the model to run /second-wind by itself (`_rewake`). Either way
    a bare "1" or "2" as the next prompt picks an option
    (`.claude/hooks/unattended-resume.py`);
  - a reset refused only because the operator typed: retried at the next quiet
    Stop while its handoff is still fresh, refreshed first when it went stale
    (`_retry_reset`, HOS-315);
  - handoff freshness, a trigger separate from the menu (HOS-315): once the
    handoff is 3 events or 60 minutes of work old, unattended asks for a light
    /handover (write only), attended says so in one line (`_freshness`). Never
    in a yard;
  - unattended on, any other pause: `checkpoint_autonomy.unattended_turn` hands
    the turn back at once unless state shows open work (`_open_work`), then
    waits out a grace period and continues unless the operator speaks or the
    stretch has ended.

HERDR sends only `/clear` and `/compact`, never typed text (spec 2026-10-14
under the registry name `drafts`, section 5).
"""

import json
import sys
import time
from pathlib import Path

_BOOT = Path(__file__).resolve()
_ROOT = _BOOT.parent
for _candidate in [_BOOT.parent, *_BOOT.parents]:
    if (_candidate / "scripts" / "utils" / "checkpoint_paths.py").is_file():
        sys.path.insert(0, str(_candidate))
        _ROOT = _candidate
        break
from scripts.utils import checkpoint_paths as CP  # noqa: E402
from scripts.utils import checkpoint_autonomy as AU  # noqa: E402
from scripts.utils import herdr_agent as HA  # noqa: E402
from scripts.utils import background_work as BW  # noqa: E402
from scripts.utils.colors import GREEN, RESET, supports_ansi  # noqa: E402

CP.force_utf8()

# Colour on one word only; the recommendation is carried by the word.
RECOMMENDED = f"{GREEN}recommended{RESET}" if supports_ansi() else "recommended"


# One menu at the first threshold and a last call at the second, no repeat in
# between (operator decision 2026-10-14). English, by his decision of
# 2026-09-27. The verdict line is computed from the background NOW; the choice
# itself is made again when /second-wind runs (`_reset`).
MENU = """\
{headline}

1. /second-wind - save a handoff and free the context ({recommended}).
   {verdict}
2. Continue as is.

How /second-wind decides, at the moment it runs:
- Nothing in the background:
    handoff -> /clear   -> a new session reads the handoff
- /loop, /goal, agents or monitors running:
    handoff -> /compact -> the same session reads the handoff
    (/compact keeps them running; /clear would stop them)

Remember: Claude Code auto-compacts at 60%, whether you want it or not :){footer}"""


def _verdict(status: str, names: list[str]) -> str:
    if status == "none":
        return "Nothing runs in the background now, so it will CLEAR."
    if status == "running":
        return f"Running now: {', '.join(names)}. So it will COMPACT."
    return "The background cannot be read now, so it will COMPACT."


def build_menu(level: str, used: float, status: str, names: list[str], unattended: bool) -> str:
    """The threshold menu, as a `systemMessage`."""
    if level == "hard":
        headline = (f"Context is about {used:.0f}% used. Last call: Claude Code "
                    "auto-compacts at 60%, mid-turn.")
    else:
        headline = f"Context is about {used:.0f}% used."
    if unattended:
        footer = (f"\n\nUnattended: /second-wind runs by itself in "
                  f"{AU.wait_seconds()} s. Type 1 or 2 and Enter to pick, or "
                  "anything else to decide yourself.")
    else:
        footer = ("\n\nPick with the arrow keys and Enter in the question below, "
                  "or type 1 or 2 and Enter.")
    return MENU.format(headline=headline, recommended=RECOMMENDED,
                       verdict=_verdict(status, names), footer=footer)


def _used_percentage(state: dict) -> float | None:
    raw = state.get("used_percentage")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _verify_reset(project: Path, session: str, since: float, kind: str) -> tuple[Path | None, list[str]]:
    """(archive, what is missing) for a /clear-handover or /second-wind run that began at `since`.

    Checked ON DISK, never from the model's words (operator decision
    2026-09-27): a `_handoff_<kind>_` archive written after the start whose
    header carries the script-written `Master instruction:` line and, since
    2026-10-14, a complete `## Safe resume` block; and this session's pointer
    pair rewritten after the start with that line first.
    """
    missing: list[str] = []
    slug = CP.safe_slug(session)
    try:
        hdir = CP.handoff_dir(project, _ROOT)
    except Exception as exc:  # noqa: BLE001 - an unresolvable overlay is a missing fact
        return None, [f"the handoff archive could not be resolved ({exc})"]
    archive = None
    for path in CP.session_handoffs(hdir, slug, (kind,)):
        if path.stat().st_mtime >= since:
            archive = path
        break
    if archive is None:
        missing.append(f"a _handoff_{kind}_ archive written after the run started")
    else:
        text = archive.read_text(encoding="utf-8", errors="replace")
        if CP.master_line_in(text) is None:
            missing.append(f"the Master instruction line in {archive.name} "
                           "(write it with checkpoint-paths.py --write)")
        missing.extend(CP.safe_resume_missing(text))
    pointers = CP.latest_dir(hdir, slug, _ROOT)
    for name in ("summary.md", "prompt.md"):
        pointer = pointers / name
        try:
            fresh = pointer.stat().st_mtime >= since
            first = pointer.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
        except OSError:
            missing.append(f"this session's pointer {name}")
            continue
        if not fresh:
            missing.append(f"this session's pointer {name}, updated after the start")
        elif not first.startswith(CP.MASTER_PREFIX):
            missing.append(f"the Master instruction line first in pointer {name}")
    return archive, missing


# The two refusals that mean only "the operator typed": the one kind retried.
STALE_WAITING = "a message from the operator is waiting"
STALE_TYPED = "the operator typed something after the check"

# A tool call that starts background work, as the transcript records it.
STALE_TOOLS = ('"name":"CronCreate"', '"name":"ScheduleWakeup"', '"name":"Agent"', '"name":"Monitor"')


def _stale_since(transcript: str | None, offset: int, session: str) -> str | None:
    """Why a decision taken at `offset` bytes of the transcript no longer holds, or None.

    Re-read immediately before the send (spec 2026-10-14, section 4): the
    operator may have typed, a compaction may have run, or background work may
    have started between the check and the send.
    """
    if not transcript:
        return None
    # A message queued while the model was writing the handoff sits BEFORE
    # `offset`, so the slice below cannot see it (review finding, 2026-10-14).
    if AU._queue_pending(Path(transcript), session):
        return STALE_WAITING
    try:
        with open(transcript, encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            fresh = handle.read()
    except OSError as exc:
        return f"the transcript could not be re-read ({exc})"
    if AU._operator_spoke(fresh, session):
        return STALE_TYPED
    if '"compact_boundary"' in fresh:
        return "a compaction happened after the check"
    if any(tool in fresh.replace(" ", "") for tool in STALE_TOOLS):
        return "background work started after the check"
    return None


def _reset(payload: dict, state: dict, state_path: Path, project: Path) -> bool:
    """Finish a /clear-handover or /second-wind run. True when this Stop was one.

    A skill cannot type a built-in command, so `checkpoint-paths.py --kind
    clear|second-wind` records the start and this Stop checks the handoff, then
    submits the command through HERDR. /second-wind sends `/clear` only when
    nothing runs in the background and `/compact` otherwise, unknown included;
    /clear-handover always sends `/clear`, but refuses when work is SEEN
    running. The request is consumed whatever the outcome, so a failed run is
    reported once and never retried. Nothing here blocks.
    """
    requested = state.get("reset_requested_at") or state.get("clear_handover_requested_at")
    if not requested:
        return False
    kind = state.get("reset_requested_kind") or "clear"
    AU.persist(state_path, _mutate=lambda fresh: {
        k: v for k, v in fresh.items()
        if k not in ("reset_requested_at", "reset_requested_kind", "clear_handover_requested_at",
                     AU.SECOND_WIND_MENU_KEY)})
    label = "/second-wind" if kind == "second-wind" else "/clear-handover"

    unattended = AU.unattended_mode(state)

    def say(text: str, refusals: int = 0) -> bool:
        # Any refusal but "the operator typed" ends a retry chain: its count resets.
        AU.persist(state_path, reset_refused_reason=text, reset_refusals=refusals)
        if unattended:
            # Nobody may be there to read a systemMessage: the refusal goes to
            # the continuation in `main` instead, so the stretch does not end
            # silently (review finding, 2026-10-14).
            AU.persist(state_path, reset_refusal_note=f"{label}: {text}")
            print(f"{label}: {text}", file=sys.stderr)
            return False
        print(json.dumps({"systemMessage": f"{label}: {text}"}, ensure_ascii=False))
        return True

    since = CP.iso_epoch(requested)
    session = CP.session_id(payload)
    if since is None or not CP.session_id_is_known(payload):
        return say("nothing sent. The start of the run or this session's id could "
                   "not be read, so the handoff cannot be checked.")
    # One second of slack: mtime and the ISO stamp come from two clocks.
    archive, missing = _verify_reset(project, session, since - 1.0, kind)
    if missing:
        return say("nothing sent. Missing on disk: " + "; ".join(missing)
                   + ". Nothing was lost; the context is intact.")

    transcript = payload.get("transcript_path")
    try:
        offset = Path(transcript).stat().st_size if transcript else 0
    except OSError:
        offset = 0
    status, names = BW.background_state(payload, project)
    running = ", ".join(names) if names else "background work of unknown state"

    if kind == "clear":
        # The operator typed /clear-handover himself: refuse only on work SEEN
        # running; an unknown state sends, as /clear-handover always has.
        if status == "running":
            return say(f"handover verified ({archive.name}), /clear NOT sent: {running} "
                       "is running and /clear could stop it. Type /clear to proceed "
                       "anyway, or /second-wind to keep it running.")
        command = HA.CLEAR_COMMAND
    else:
        command = HA.CLEAR_COMMAND if status == "none" else HA.COMPACT_COMMAND

    try:
        pane, how = HA.resolve_pane_how(session)
    except HA.HerdrUnavailable as exc:
        pane, how = None, f"herdr lookup failed: {exc}"
    print(f"checkpoint-offer: pane {pane} found by {how}" if pane
          else f"checkpoint-offer: no pane: {how}", file=sys.stderr)
    if pane is None:
        AU.persist(state_path, reset_not_hosted_at=CP.utc_now().isoformat())
        if kind == "second-wind" and unattended:
            # Not a dead end (spec 3.7, step 6): record it and fall through to
            # the unattended continuation in `main`, which keeps the work going;
            # Claude Code auto-compacts at 60%.
            AU.persist(state_path, reset_refused_reason=(
                f"handover verified ({archive.name}); {how}, nothing sent, work continues"))
            print(f"{label}: {how}, nothing sent; continuing", file=sys.stderr)
            return False
        return say(f"handover verified ({archive.name}), now type {command}. The hook "
                   f"could not send it: {how}.")

    stale = _stale_since(transcript, offset, session)
    if stale:
        if stale in (STALE_WAITING, STALE_TYPED) and not AU.in_yard():
            # Retried at the next quiet Stop (`_retry_reset`, HOS-315), until the
            # RESET_RETRY_MAX-th refusal in a row, which drops it.
            refusals = int(CP.read_json(state_path).get("reset_refusals") or 0) + 1
            if refusals >= RESET_RETRY_MAX:
                return say(f"handover verified ({archive.name}), {command} NOT sent: "
                           f"{stale}. Refused {refusals} times in a row because the "
                           f"operator typed, so not retried again. Run {label} once "
                           "more when you want the context freed.")
            AU.persist(state_path, reset_retry={
                "kind": kind, "requested_at": requested, "archive": str(archive),
                "refused_at": CP.utc_now().isoformat()})
            return say(f"handover verified ({archive.name}), {command} NOT sent: "
                       f"{stale}. It is tried again at the next quiet pause.",
                       refusals=refusals)
        return say(f"handover verified ({archive.name}), {command} NOT sent: {stale}.")
    try:
        HA.submit_command(pane, command)
    except HA.HerdrUnavailable as exc:
        return say(f"handover verified ({archive.name}), {command} NOT sent: {exc}.")
    now = CP.utc_now().isoformat()
    sent = {"reset_sent_at": now, "reset_sent_command": command,
            "reset_sent_pane": pane, "second_wind_attempts": 0, "reset_refusals": 0}
    if command == HA.CLEAR_COMMAND:
        sent.update(clear_sent_at=now, clear_sent_pane=pane)
    AU.persist(state_path, **sent)
    if command == HA.CLEAR_COMMAND:
        why = "nothing runs in the background; the next session receives the handoff"
    else:
        why = f"{running} keeps running; this session re-reads the handoff"
    print(json.dumps({"systemMessage": f"{label}: handover verified ({archive.name}). "
                      f"{command} sent through HERDR (pane {pane}): {why}."},
                     ensure_ascii=False))
    return True


# The refusal for typing that drops a reset instead of recording a retry: the
# original refusal and two retries (review finding, HOS-315 round 1).
RESET_RETRY_MAX = 3

RESET_REFRESH = """\
{label} was refused because the operator typed, and its handoff ({archive}) went \
stale since: {events} event(s) / {minutes} min of work ({detail}). Refresh it, \
then the reset is retried: run `python scripts/checkpoint-paths.py --kind {kind}`, \
then pass a fresh handoff, written per .claude/skills/second-wind/SKILL.md Step 2 \
with a complete `## Safe resume` block, on stdin to `python \
scripts/checkpoint-paths.py --write --kind {kind}`, in ONE turn and without the \
goldfish cold read. Print ONE line naming the archive, then stop. Do NOT run \
/clear or /compact yourself."""


def _retry_reset(payload: dict, state: dict, state_path: Path, project: Path,
                 started: float) -> bool:
    """Retry a reset refused only because the operator typed (HOS-315 part 2). True when this Stop was one.

    Waits for a quiet Stop: none in the same hook run that refused it, and none
    while a message is still queued. A still-fresh handoff is sent again
    through `_reset`, which re-checks everything; a stale one is refreshed
    first, by the model, and `_reset` then sends at the following Stop.
    """
    retry = state.get("reset_retry")
    if not isinstance(retry, dict):
        return False
    refused = CP.iso_epoch(retry.get("refused_at"))
    if refused is None:
        print("checkpoint-offer: unreadable reset_retry record dropped", file=sys.stderr)
        AU.persist(state_path, _mutate=lambda fresh: {
            k: v for k, v in fresh.items() if k != "reset_retry"})
        return False
    if refused >= started:
        # Refused by this very hook run: not yet a quiet Stop.
        return False
    transcript = payload.get("transcript_path")
    session = CP.session_id(payload)
    if transcript and AU._queue_pending(Path(transcript), session):
        return False
    AU.persist(state_path, _mutate=lambda fresh: {
        k: v for k, v in fresh.items() if k != "reset_retry"})
    kind = retry.get("kind") if retry.get("kind") in ("clear", "second-wind") else "clear"
    label = "/second-wind" if kind == "second-wind" else "/clear-handover"
    archive = Path(str(retry.get("archive") or ""))
    if not archive.is_file():
        AU.persist(state_path, reset_refusals=0)
        print(json.dumps({"systemMessage": (
            f"{label}: not retried, its handoff {archive.name or '(unnamed)'} is no "
            "longer on disk.")}, ensure_ascii=False))
        return True
    fresh = AU.handoff_freshness(payload, project, _ROOT, archive=archive)
    if fresh["due"]:
        reason = RESET_REFRESH.format(
            label=label, archive=archive.name, events=fresh["events"],
            minutes=fresh["minutes"], detail=AU.freshness_detail(fresh), kind=kind)
        print(json.dumps({"decision": "block", "reason": reason,
                          "systemMessage": f"{label}: the handoff went stale while "
                          "you typed; refreshing it before the reset."},
                         ensure_ascii=False))
        return True
    rearmed = dict(state)
    rearmed.update(reset_requested_at=retry.get("requested_at"), reset_requested_kind=kind)
    rearmed.pop("reset_retry", None)
    AU.persist(state_path, reset_requested_at=retry.get("requested_at"),
               reset_requested_kind=kind)
    return _reset(payload, rearmed, state_path, project)


def _freshness(payload: dict, state_path: Path, project: Path) -> dict | None:
    """Measure the handoff's age, record it for the status line, return it. None in a yard.

    A source that cannot be read counts as nothing; each is said once per
    session, on stderr, and in the attended line when one is printed.
    """
    if AU.in_yard():
        return None
    try:
        fresh = AU.handoff_freshness(payload, project, _ROOT)
    except Exception as exc:  # noqa: BLE001 - a Stop hook never crashes over a measurement
        print(f"checkpoint-offer: handoff freshness not measured: {exc}", file=sys.stderr)
        return None
    archive = fresh["archive"]
    try:
        mtime = Path(archive).stat().st_mtime if archive else None
    except OSError:
        mtime = None
    said = list(CP.read_json(state_path).get("freshness_unread_said") or [])
    names = [u.split(" (", 1)[0] for u in fresh["unread"]]
    fresh["unread_new"] = [u for u, n in zip(fresh["unread"], names, strict=True) if n not in said]
    for line in fresh["unread_new"]:
        print(f"checkpoint-offer: not counted toward handoff freshness: {line}",
              file=sys.stderr)
    AU.persist(state_path, handoff_freshness={
        "archive": archive, "archive_mtime": mtime, "since": fresh["since"],
        "events": fresh["events"], "minutes": fresh["minutes"], "due": fresh["due"],
        "measured_at": CP.utc_now().isoformat()},
        freshness_unread_said=sorted(set(said) | set(names)))
    return fresh


def _freshness_line(fresh: dict | None, state_path: Path) -> int:
    """Attended: one line when the handoff went stale, once per handoff. Always returns 0."""
    if not fresh or not fresh["due"]:
        return 0
    key = str(fresh["since"])
    if CP.read_json(state_path).get("freshness_said_for") == key:
        return 0
    AU.persist(state_path, freshness_said_for=key)
    line = AU.FRESH_LINE.format(events=fresh["events"], minutes=fresh["minutes"],
                                detail=AU.freshness_detail(fresh))
    if fresh.get("unread_new"):
        line += " Not counted: " + "; ".join(fresh["unread_new"]) + "."
    print(json.dumps({"systemMessage": line}, ensure_ascii=False))
    return 0


def _refresh_note(fresh: dict | None, state_path: Path) -> str:
    """Unattended: the light-refresh instruction, at most FRESH_REFRESH_MAX_ASKS times per handoff."""
    if not fresh or not fresh["due"]:
        return ""
    key = str(fresh["since"])
    asked = CP.read_json(state_path).get("freshness_refresh_asked") or {}
    times = int(asked.get("times") or 0) if asked.get("for") == key else 0
    if times >= AU.FRESH_REFRESH_MAX_ASKS:
        return ""
    AU.persist(state_path, freshness_refresh_asked={"for": key, "times": times + 1})
    return AU.FRESH_REFRESH_NOTE.format(events=fresh["events"], minutes=fresh["minutes"],
                                        detail=AU.freshness_detail(fresh))


# A pending menu mark older than this belongs to an earlier Stop.
PENDING_MAX_AGE = 30.0


def _rewake(payload: dict) -> int:
    """The asyncRewake Stop registration: wait out the grace, then wake the model to run /second-wind.

    Runs beside the ordinary Stop registration on every Stop and does nothing
    unless that one has just shown the menu in unattended mode
    (`second_wind_pending_at`). Exit 2 wakes the idle session with stderr as a
    system reminder (documented for every asyncRewake hook; task 0 of the
    /second-wind plan checks it for Stop). Exit 0, silent, when the operator
    spoke, when no menu is pending, or after the attempts ceiling.
    """
    project = CP.project_root(payload)
    state_path = CP.state_path(project, CP.session_slug(payload))
    # The two registrations start together; give the menu a moment to be marked.
    deadline = time.monotonic() + 3.0
    state = CP.read_json(state_path)
    while not state.get("second_wind_pending_at") and time.monotonic() < deadline:
        time.sleep(0.2)
        state = CP.read_json(state_path)
    pending = CP.iso_epoch(state.get("second_wind_pending_at"))
    if pending is None:
        return 0
    if CP.utc_now().timestamp() - pending > PENDING_MAX_AGE:
        # Left by a menu whose own rewake missed it; never act on it later.
        AU.persist(state_path, _mutate=lambda fresh: {
            k: v for k, v in fresh.items() if k != "second_wind_pending_at"})
        return 0
    attempts = int(state.get("second_wind_attempts") or 0)
    AU.persist(state_path, _mutate=lambda fresh: {
        k: v for k, v in fresh.items() if k != "second_wind_pending_at"})
    if attempts >= AU.SECOND_WIND_MAX_ATTEMPTS:
        AU.persist(state_path, reset_refused_reason=(
            f"/second-wind self-run skipped: {attempts} attempts wrote no verified handoff"))
        return 0
    spoke, granted = AU._wait_out_the_grace(payload, CP.session_id(payload))
    if spoke:
        return 0
    # The self-run answers the menu: a later bare digit is no longer a pick.
    AU.persist(state_path, _mutate=lambda fresh: {
        k: v for k, v in fresh.items() if k != AU.SECOND_WIND_MENU_KEY},
        second_wind_attempts=attempts + 1)
    used = _used_percentage(state) or 0.0
    print(AU.SECOND_WIND_WRAPPER.format(used=used, wait=int(granted)), file=sys.stderr)
    return 2


def main() -> int:
    if "--rewake" in sys.argv[1:]:
        return _rewake(CP.read_payload("checkpoint-offer") or {})
    payload = CP.read_payload("checkpoint-offer")
    if payload is None:
        # Said on stderr: in unattended mode this is the stretch stopping silently.
        print("checkpoint-offer: unreadable Stop payload, this turn ends unmanaged",
              file=sys.stderr)
        return 0

    started = CP.utc_now().timestamp()
    project = CP.project_root(payload)
    state_path = CP.state_path(project, CP.session_slug(payload))
    state = CP.read_json(state_path)
    unattended = AU.unattended_mode(state)

    # The process record the SessionStart hook reads after a `/clear`. Here as
    # well as in the status line, because `CLAUDE_PID` is documented for hooks.
    probe = dict(state)
    if CP.record_process(probe, payload):
        AU.persist(state_path, **{k: probe[k] for k in (
            *CP.PROCESS_KEYS, "session_id", "transcript_path", "process_seen_at")
            if k in probe})

    # Before the `stop_hook_active` guard: sending `/clear` or `/compact` prints
    # no decision, and a verified handover must not be dropped because another
    # hook blocked.
    if _reset(payload, state, state_path, project):
        return 0
    if _retry_reset(payload, CP.read_json(state_path), state_path, project, started):
        return 0

    # Anti-loop guard, crossed only on a turn this hook itself continued in
    # unattended mode (by `prompt_id`; an absent one never matches).
    turn = payload.get("prompt_id") or ""
    ours = bool(turn) and state.get("unattended_turn_id") == turn
    if payload.get("stop_hook_active") and not unattended and state.get(AU.SECOND_WIND_MENU_KEY):
        # The continuation our block started has ended, so the selector was
        # answered: a later bare digit is no longer a pick.
        AU.persist(state_path, _mutate=lambda fresh: {
            k: v for k, v in fresh.items() if k != AU.SECOND_WIND_MENU_KEY})
    if payload.get("stop_hook_active") and not (unattended and ours):
        return 0

    # Measured at every Stop that gets this far, so the status line shows the
    # handoff's age; acted on only where nothing else spoke (HOS-315).
    fresh = _freshness(payload, state_path, project)

    # Unknown usage stops every path except the unattended continuation.
    used = _used_percentage(state)
    if used is None and not unattended:
        return _freshness_line(fresh, state_path)

    # Something else drives this Stop (`/loop`, a background agent, ralph-loop):
    # it is recorded, the menu still shows (it names what runs), and the plain
    # unattended continuation below stays out of its way.
    claimant = AU.continuation_claimant(payload, project)
    if claimant:
        AU.persist(
            state_path,
            continuation_claimant=claimant,
            continuation_seen_at=CP.utc_now().isoformat(),
        )

    level = state.get("offer_level")
    if used is not None and state.get("needs_compact_offer") and level in ("soft", "hard"):
        # Mark the offer delivered (hysteresis) on the fresh file, not the entry copy.
        AU.persist(
            state_path,
            needs_compact_offer=False,
            offer_level=None,
            last_offered_bucket=CP.offer_bucket(state),
            last_offer_at=CP.utc_now().isoformat(),
        )
        status, names = BW.background_state(payload, project)
        message = build_menu(level, used, status, names, unattended)
        output = {"systemMessage": message}
        if not unattended:
            # Arrow keys and Enter: the model asks through AskUserQuestion
            # (operator request 2026-10-15). One blocked Stop, one model turn.
            output.update(decision="block", reason=AU.SECOND_WIND_ASK.format(
                used=used, verdict=_verdict(status, names)))
        # A bare "1" or "2" as the next prompt picks an option (`unattended-resume.py`).
        AU.persist(state_path, **{AU.SECOND_WIND_MENU_KEY: CP.utc_now().isoformat()})
        print(json.dumps(output, ensure_ascii=False))
        if unattended:
            # The wait and the self-run belong to the asyncRewake registration
            # (`_rewake`), so the menu is on screen during the countdown.
            # A new showing restores the attempt budget, so failures at earlier
            # menus never silence a later one (review finding, 2026-10-14).
            AU.persist(state_path, second_wind_pending_at=CP.utc_now().isoformat(),
                       second_wind_attempts=0)
        return 0

    note = CP.read_json(state_path).get("reset_refusal_note") or ""
    if note:
        AU.persist(state_path, _mutate=lambda fresh: {
            k: v for k, v in fresh.items() if k != "reset_refusal_note"})
    if unattended:
        refresh = _refresh_note(fresh, state_path)
        if refresh:
            note = f"{note}\n\n{refresh}" if note else refresh
    if unattended and (not claimant or note):
        return AU.unattended_turn(payload, state, state_path, used, turn, note=note)
    if not unattended:
        return _freshness_line(fresh, state_path)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
