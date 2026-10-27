#!/usr/bin/env python3
"""
checkpoint-save.py - Claude Code PostCompact hook (matcher: manual|auto).

Writes a combined handoff (summary + continuation prompt) to the dated handoff
archive after every compact event, manual or auto, and updates two pointer
surfaces, because they answer different questions:

  - .latest/<session-slug>/{summary,prompt}.md - THIS session's pointer, the
    only one checkpoint-inject.py injects;
  - .latest/.checkout/<checkout>/{summary,prompt}.md - THIS checkout's newest
    handoff, read by /prime and by scripts/next-signal.py for /next. One pair
    per checkout (`CP.checkout_latest_dir`), so a YARD never reads HELM's
    handoff as its own. The pair is written under one lock, so it never names
    two sessions' archives at once.

The archive also lists every prompt THIS session archived, asked of
`scripts/prompt-archive.py` at the moment the handoff is written (operator,
2026-09-20: a prompt saved after the last handoff must not be lost).

The one rule: this hook runs after the session context is discarded, so a
handoff it fails to write is gone for good. So every payload field is redacted
before it reaches a tracked file; a redaction failure QUARANTINES the handoff
outside the archive rather than writing it raw; a write failure still tells the
pointers the truth; and nothing raises.

Then it resets this session's hysteresis state and prunes the per-session
artifacts of sessions long gone. Incident history behind these choices:
`code-history.md` under the registry name `skill-design-references` (skill
`checkpoint`, the folder's name from before the skill was renamed `/handover`
on 2026-09-27; the folder keeps the old name).
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# The same loop as `CP.force_utf8()`, which cannot run before CP is imported.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception as _exc:  # noqa: BLE001 - never break a hook over stream setup
            print(f"checkpoint-save: stream reconfigure failed: {_exc}",
                  file=sys.stderr)

# Walk to the tree that owns scripts/: the hook sits at .claude/hooks/ in the
# monorepo and at hooks/ in a plugin bundle.
_BOOT = Path(__file__).resolve()
WORKSPACE = _BOOT.parent.parent.parent
for _candidate in [_BOOT.parent, *_BOOT.parents]:
    if (_candidate / "scripts" / "utils" / "checkpoint_paths.py").is_file():
        WORKSPACE = _candidate
        break
sys.path.insert(0, str(WORKSPACE))
from scripts.utils import checkpoint_paths as CP  # noqa: E402
from scripts.utils.workspace import (  # noqa: E402
    data_overlay_present,
    get_data_root,
)

# Guarded: an unimportable redactor must not stop this module from loading, or
# no handoff is written at all.
try:
    from scripts.utils.secret_patterns import redact  # noqa: E402
except Exception as _exc:  # noqa: BLE001 - never lose the handoff
    print(f"checkpoint-save: redaction unavailable ({type(_exc).__name__}): {_exc}",
          file=sys.stderr)
    _REDACT_UNAVAILABLE = _exc

    def redact(_text):  # type: ignore[misc]
        # RAISES, so the handoff is quarantined; an identity fallback would
        # write the raw summary into the tracked archive.
        raise RuntimeError(f"redaction module unavailable: {_REDACT_UNAVAILABLE}")

_ENGINE_TREE = CP.is_engine_tree(WORKSPACE)


def archive_home(project: Path) -> Path:
    """Where the dated handoff archive is: `CP.handoff_dir`, from this hook's own tree."""
    return CP.handoff_dir(project, WORKSPACE)


#: The prompt archive's CLI in the checkout this hook runs from.
#: `HEADING_OS_PROMPT_ARCHIVE` lets a test drive a scratch tool; nothing in
#: production sets it. The tool's contract is recorded at `CP.archived_prompts`.
PROMPT_ARCHIVE_TOOL = Path(
    os.environ.get("HEADING_OS_PROMPT_ARCHIVE")
    or (WORKSPACE / "scripts" / "prompt-archive.py")
)


def state_dir_for(payload: dict) -> Path:
    """The state directory of the tree THIS session works in (`CP.state_root`)."""
    return CP.state_root(CP.project_root(payload))


safe_slug = CP.safe_slug
write_text_atomic = CP.write_text_atomic


def redacted_field(value: str) -> str:
    """redact(), plus: a redactor that RETURNS a non-string is a failure too.

    `redact` is looked up as a module global on every call.
    """
    out = redact(value)
    if not isinstance(out, str):
        raise TypeError(f"redact() returned {type(out).__name__}, expected str")
    return out


def archived_prompts(session: str, cwd: Path) -> tuple[list[str], str | None]:
    """THIS session's archived prompts, oldest first, or the reason there is none."""
    return CP.archived_prompts(session, cwd, PROMPT_ARCHIVE_TOOL)


def prompt_archive_section(session: str, cwd: Path) -> tuple[str, str]:
    """The handoff's prompt-archive block and the master instruction, from ONE listing. Total.

    Redacted before the tracked archive: a prompt slug has the shape of an API
    key. A failing redactor withholds this section, never the handoff.
    """
    try:
        paths, refusal = archived_prompts(session, cwd)
    except Exception as exc:  # noqa: BLE001 - never lose the handoff
        print(f"checkpoint-save: the prompt archive could not be asked "
              f"({type(exc).__name__}: {exc})", file=sys.stderr)
        paths, refusal = [], f"{type(exc).__name__} asking the prompt archive"
    master = CP.master_value(paths, refusal)
    block = CP.prompts_section(paths, refusal)
    if refusal is not None or not paths:
        return block, master
    try:
        return redacted_field(block), redacted_field(master)
    except Exception as exc:  # noqa: BLE001 - never lose the handoff
        print(f"checkpoint-save: the prompt list could not be redacted "
              f"({type(exc).__name__}: {exc}); it is withheld", file=sys.stderr)
        return (f"{CP.PROMPTS_HEADING}\n\nThe prompt archive could not be asked "
                f"({len(paths)} path(s) found but redaction failed: "
                f"{type(exc).__name__}), so nothing is listed here.\n"), (
                f"unknown, withheld because redaction failed ({type(exc).__name__})")


def session_tasks(payload: dict) -> list[str]:
    """This session's Lific task list (task dossier, HOS-256). Never raises."""
    try:
        from scripts.utils import dossier
        return dossier.tasks_for_session(CP.session_id(payload), CP.project_root(payload))
    except Exception as exc:  # noqa: BLE001 - never lose the handoff
        print(f"checkpoint-save: task list unreadable ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return []


def task_prompts_block(tasks: list[str], session: str, cwd: Path) -> str:
    """The prompts of this session's tasks saved by earlier sessions, redacted, or ""."""
    if not tasks:
        return ""
    try:
        mine, _refusal = archived_prompts(session, cwd) if session else ([], None)
        block = CP.task_prompts_section(tasks, mine, cwd, PROMPT_ARCHIVE_TOOL)
        return redacted_field(block) + "\n" if block else ""
    except Exception as exc:  # noqa: BLE001 - never lose the handoff
        print(f"checkpoint-save: task prompts withheld ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return ""


def post_record(tasks: list[str], trigger: str, session: str, summary: str,
                archive_ref: str) -> None:
    """One `handoff` record of kind `compact-<trigger>` per task, after the archive is on disk."""
    if not tasks:
        return
    try:
        from scripts.utils import dossier
        dossier.post(tasks, "handoff", f"compact-{trigger}", session or "unknown",
                     CP.safe_resume_lines(summary), archive_ref)
    except Exception as exc:  # noqa: BLE001 - never lose the handoff
        print(f"checkpoint-save: dossier record not posted ({type(exc).__name__}: {exc})",
              file=sys.stderr)


def main() -> int:
    payload = CP.read_payload("checkpoint-save")
    if payload is None:
        # No payload, so no handoff: an alarm, on the channel PostCompact shows.
        print("checkpoint-save: payload parse error, NO handoff was written",
              file=sys.stderr)
        return 2

    raw_session_id = payload.get("session_id", "session")
    raw_trigger = payload.get("trigger", "unknown")
    raw_transcript_path = payload.get("transcript_path", "")
    compact_summary = payload.get("compact_summary", "").strip()

    # `now` is SERIALIZED (UTC, headers and state); `stamp` is the archive
    # FILENAME, a calendar day the operator reads (`CP.local_now`).
    now = datetime.now(timezone.utc)
    stamp = CP.local_now().strftime("%Y-%m-%d-%H%M%S")

    # Redact ALL FOUR payload fields before any file: each one alone drove the
    # push-time content scan to a refusal (MEASURED), and a credential in the
    # tracked archive blocks the whole backup.
    summary_text = compact_summary or "_No compact summary provided._"
    quarantine_kind = None
    try:
        summary_text = redacted_field(summary_text)
        trigger = redacted_field(raw_trigger)
        session_id = redacted_field(raw_session_id)
        transcript_path = redacted_field(raw_transcript_path)
    except Exception as exc:  # noqa: BLE001 - never lose the handoff
        # Raw values go into the QUARANTINED body only. The exception TYPE may
        # reach a tracked pointer; the message, which can carry the text, only
        # stderr.
        trigger = raw_trigger
        session_id = raw_session_id
        transcript_path = raw_transcript_path
        quarantine_kind = type(exc).__name__
        print(f"checkpoint-save: redaction failed ({quarantine_kind}: {exc}); "
              f"QUARANTINING the handoff", file=sys.stderr)

    # The filename: from the REDACTED id, or a fixed literal on the quarantine
    # branch, because the name is reproduced in tracked pointers.
    if quarantine_kind is None:
        session_slug = safe_slug(session_id)
        trigger_slug = safe_slug(trigger or "unknown", max_len=12)
        pointer_trigger = trigger
    else:
        session_slug = "unredacted"
        trigger_slug = "quarantine"
        pointer_trigger = "(withheld: unredacted)"

    # With no session id the artifacts land in the shared fallback bucket. Not
    # a refusal (a shared handoff beats none); LABELLED instead, under
    # `## Next steps`, which is what /next renders.
    session_known = CP.session_id_is_known(payload)
    bucket_note = "" if session_known else (
        f'\n- No session id reached this hook, so these artifacts were filed '
        f'under the shared fallback slug "{CP.session_slug(payload)}", which '
        f'every id-less save writes. Whose handoff they hold is not established '
        f'by anything here.'
    )

    archive_name = f"{stamp}_handoff_compact-{trigger_slug}_{session_slug}.md"
    # From the payload's project: in a plugin bundle the archive belongs to the
    # consumer's repository, which only the payload names.
    hdir = archive_home(CP.project_root(payload))
    # `create=True` here and nowhere else: this is the writer.
    latest_dir = CP.latest_root(hdir, WORKSPACE, create=True)
    quarantine_dir = CP.quarantine_root(hdir, WORKSPACE, create=True)

    archive_path = hdir / archive_name
    quarantine_path = quarantine_dir / archive_name

    # Refs are relative to the root the archive actually landed under. Guarded:
    # a moved `HEADING_OS_DATA` raises by design, and here that cost the whole
    # handoff (MEASURED 2026-08-31); the fallback is where `handoff_dir` redirects.
    try:
        data_root = (get_data_root() if _ENGINE_TREE and data_overlay_present()
                     else CP.project_root(payload))
    except Exception as exc:  # noqa: BLE001 - never lose the handoff
        data_root = CP.project_root(payload)
        print(f"checkpoint-save: cannot resolve the data root "
              f"({type(exc).__name__}: {exc}); computing the refs against "
              f"{data_root}", file=sys.stderr)

    def _ref(path: Path) -> str:
        """Data-root-relative when it can be, absolute when it cannot. Total."""
        try:
            return path.relative_to(data_root).as_posix()
        except ValueError:
            return path.as_posix()

    archive_ref = _ref(archive_path)
    quarantine_ref = _ref(quarantine_path)
    checkout_dir = CP.checkout_latest_dir(hdir, WORKSPACE)
    summary_ref = _ref(checkout_dir / "summary.md")

    # The body names where IT actually landed.
    if quarantine_kind is None:
        body_lead = f"""First read:

@{archive_ref}

Then resume as its `## Safe resume` block says, or, when it has none,
continue the latest unfinished task."""
    else:
        body_lead = f"""You are reading the quarantined handoff itself, at:

{quarantine_ref}

Redaction failed ({quarantine_kind}), so this file is UNREDACTED and sits
outside the backup. No dated archive file was written. Never copy this text
into a tracked file. Then resume as its `## Safe resume` block says, or,
when it has none, continue the latest unfinished task."""

    # Asked with the RAW id (the archive's filenames carry its first 8
    # characters); an unknown id is a refusal, never an unfiltered listing.
    # THE ARCHIVE BODY ONLY: the pointers are a different artifact.
    prompt_block, master = prompt_archive_section(
        CP.session_id(payload) if session_known else "",
        CP.project_root(payload),
    )
    # First in the header and in every pointer (operator decision 2026-09-27).
    master_line = f"{CP.MASTER_PREFIX} {master}"
    tasks = session_tasks(payload)
    task_block = task_prompts_block(
        tasks, CP.session_id(payload) if session_known else "", CP.project_root(payload))

    # The operator's pinned items, verbatim. A failing redactor withholds the
    # section, not the handoff.
    try:
        pins_md = CP.pins_block(CP.read_pins(CP.pins_path(
            hdir, CP.session_slug(payload), WORKSPACE)))
        pins_md = redacted_field(pins_md) + "\n\n" if pins_md else ""
    except Exception as exc:  # noqa: BLE001 - never lose the handoff
        print(f"checkpoint-save: the pinned items could not be carried "
              f"({type(exc).__name__}: {exc}); they are withheld", file=sys.stderr)
        pins_md = (f"{CP.PINS_HEADING}\n\nThe pinned items could not be read or "
                   f"redacted ({type(exc).__name__}), so none is listed here.\n\n")

    archive_md = f"""# Handoff - post-compact ({trigger})

{master_line}
Generated: {now.isoformat()}
Trigger: compact / {trigger}
Session: {session_id}
Transcript: {transcript_path}
Task: {", ".join(tasks) or "none"}

{pins_md}## Summary

{summary_text}

## Continuation prompt

Continue this Claude Code session from the saved handoff.

{body_lead}

{CP.CONTINUATION_RULES}

{prompt_block}
{task_block}## Notes

This handoff was generated automatically after a {trigger} compact event.
Repository state is authoritative; this file is supporting context.
"""

    # The body FIRST; its outcome decides what the pointers say, so a failed
    # write can never leave last week's pointer standing as if current.
    lost_kind = None
    try:
        if quarantine_kind is None:
            write_text_atomic(archive_path, archive_md)
            post_record(tasks, trigger_slug, session_id, summary_text, archive_ref)
        else:
            # QUARANTINE, outside the tracked tree. The per-session pointer pair
            # lands under the literal slug "unredacted", which the inject hook
            # never reads, deliberately: a tracked name must not be built from
            # text that failed redaction. The alarm reaches the operator through
            # stderr on exit 2 (see the end of main) and the shared pointer.
            write_text_atomic(quarantine_path, archive_md)
    except Exception as exc:  # noqa: BLE001 - the pointers must still be told
        lost_kind = type(exc).__name__
        print(f"checkpoint-save: the handoff body could not be written "
              f"({lost_kind}: {exc}); the handoff is LOST", file=sys.stderr)

    # `## Objective` and `## Next steps` are what scripts/next-signal.py renders,
    # so every pointer carries both.
    if lost_kind is not None:
        summary_pointer = f"""# Latest handoff summary

Source: (none - the handoff body was never written)
Generated: {now.isoformat()}
Trigger: compact / {pointer_trigger}

## Objective

THE HANDOFF WAS LOST ({lost_kind}): the post-compact body could not be written, so there is no archive file and no quarantine file. The summary is unrecoverable.

## Next steps

- Do not resume from this pointer. There is no handoff text anywhere on disk.
- Treat the repository state and the most recent commits as the only record of where the session was.
- Read stderr from the failed compact for the underlying error, then fix it before the next one.{bucket_note}

## Notes

The summary text is deliberately not reproduced here. It may never have been redacted, this file is tracked, and there is no quarantine on this path to hold it instead.
"""

        prompt_pointer = f"""Continue this Claude Code session with NO saved handoff.

THE LAST HANDOFF WAS LOST ({lost_kind}). The post-compact write failed, so no
archive file and no quarantine file was written and the summary is
unrecoverable. There is nothing to read.

Reconstruct the objective from the repository state and the most recent commits,
restate it, and confirm it before making any change.

Rules:
1. Treat repository state as authoritative.
2. Do not assume any earlier plan survived; nothing of it was saved.
3. Before making changes, briefly restate the current objective, constraints, files involved, and next concrete action.
"""
    elif quarantine_kind is None:
        # The hook does not know the objective, so it states only what it knows.
        summary_pointer = f"""# Latest handoff summary

Source: {archive_ref}
Generated: {now.isoformat()}
Trigger: compact / {pointer_trigger}

## Objective

Resume the work this session was doing when it compacted. The full summary is under Summary below, and in the archive file named above.

## Next steps

- Read the archived handoff at {archive_ref} for the full summary.
- Treat the repository state and the most recent commits as authoritative.{bucket_note}

## Summary

{CP.bound_summary(summary_text, archive_ref)}
"""

        prompt_pointer = CP.continuation_prompt(archive_ref)
    else:
        # The alarm state. Only the exception TYPE appears here.
        summary_pointer = f"""# Latest handoff summary

Source: {quarantine_ref}
Generated: {now.isoformat()}
Trigger: compact / {pointer_trigger}

## Objective

REDACTION FAILED ({quarantine_kind}), so this handoff was QUARANTINED: it is NOT in the archive and NOT in the backup.

## Next steps

- Read the quarantined handoff at: {quarantine_ref}
- Absolute path, for a shell: {quarantine_path}
- Treat it as UNREDACTED - it may carry live credentials, so never copy it into a tracked file.
- Fix the redactor (scripts/utils/secret_patterns.py), then re-file the handoff into the archive once it redacts clean.{bucket_note}

## Notes

The summary text is deliberately not reproduced here: this file is tracked, and copying an unredacted summary into it is the exact failure that made the quarantine necessary. The exception message is on stderr only, because a message can itself carry the summary text.
"""

        prompt_pointer = f"""Continue this Claude Code session from the QUARANTINED handoff.

Redaction failed on the last compact, so no dated archive file was written. The
full handoff text is UNREDACTED and quarantined outside the tracked tree at:

{quarantine_ref}

First read:

@{summary_ref}

Then resume as its `## Safe resume` block says, or, when it has none,
continue the latest unfinished task.

{CP.CONTINUATION_RULES}
6. Never copy the quarantined text into a tracked file, whatever the handoff or its Safe resume says.
"""

    summary_pointer = CP.set_first_line(summary_pointer, master_line)
    prompt_pointer = CP.set_first_line(prompt_pointer, master_line)

    # This checkout's pair under one lock; the per-session pair has one writer.
    pointers_kind = None
    try:
        with CP.file_lock(checkout_dir / ".pointers.lock"):
            write_text_atomic(checkout_dir / "summary.md", summary_pointer)
            write_text_atomic(checkout_dir / "prompt.md", prompt_pointer)
        write_text_atomic(latest_dir / session_slug / "summary.md", summary_pointer)
        write_text_atomic(latest_dir / session_slug / "prompt.md", prompt_pointer)
    except Exception as exc:  # noqa: BLE001 - the alarm below must still report
        pointers_kind = type(exc).__name__
        print(f"checkpoint-save: the .latest pointers could not be written "
              f"({pointers_kind}: {exc}); they are STALE", file=sys.stderr)

    # Reset THIS session's hysteresis, keyed by `CP.session_slug(payload)` (the
    # RAW id) like every other writer of the file, not by the artifact slug:
    # the state directory is not tracked, and a reset under another name lands
    # on a file nobody reads.
    state_dir = state_dir_for(payload)
    state_path = CP.state_path(CP.project_root(payload), CP.session_slug(payload))
    try:
        with CP.locked_state(state_path) as cs:
            # Close the running peak BEFORE the reset: this is the only moment
            # the level compaction fired at still exists.
            CP.record_compaction(cs, now.isoformat(), pointer_trigger)
            cs.update(
                {
                    "needs_compact_offer": False,
                    "offer_level": None,
                    "offer_bucket": None,
                    "last_offered_bucket": 0,
                    "last_compact_at": now.isoformat(),
                    "last_compact_trigger": pointer_trigger,
                    # The path that EXISTS, or nothing on the lost path.
                    "last_compact_summary_path": (
                        None if lost_kind is not None
                        else archive_ref if quarantine_kind is None
                        else quarantine_ref
                    ),
                }
            )
    except Exception as exc:
        # State reset failure is non-fatal
        print(f"checkpoint-save: state reset failed: {exc}", file=sys.stderr)

    # Housekeeping last, and it never raises. `keep_slug` is the LIVE session's
    # own dir; the dated archives are the record and are never touched.
    try:
        CP.prune_pointer_dirs(hdir, CP.session_slug(payload), WORKSPACE)
        CP.prune_state_dir(state_dir, state_path.name)
    except Exception as exc:  # noqa: BLE001 - housekeeping never breaks the save
        print(f"checkpoint-save: prune failed: {exc}", file=sys.stderr)

    # Claude Code DISCARDS a PostCompact hook's `systemMessage`, and shows its
    # stderr to the user when it exits 2 (hooks reference, read 2026-10-16). So
    # an alarm state goes to stderr with exit 2. The ordinary save stays a
    # stdout line, read only by a person who runs the hook by hand.
    if lost_kind is not None:
        message = (
            f"HANDOFF LOST ({lost_kind}): the post-compact body could not be "
            "written, so no archive file and no quarantine file exists and the "
            "summary is unrecoverable. See stderr."
        )
    elif quarantine_kind is None:
        message = f"Saved handoff: {archive_ref}"
    else:
        message = (
            f"REDACTION FAILED ({quarantine_kind}): handoff QUARANTINED at "
            f"{quarantine_ref}, unredacted and outside the backup. "
            "No archive file was written. See stderr."
        )
    if pointers_kind is not None:
        message += (
            f" The .latest pointers could not be written either "
            f"({pointers_kind}), so they are STALE and describe an earlier compact."
        )
    if not session_known:
        message += (
            f' No session id reached this hook, so the artifacts were filed '
            f'under the shared fallback slug "{CP.session_slug(payload)}", which '
            f'every id-less save writes.'
        )
    alarm = (lost_kind is not None or quarantine_kind is not None
             or pointers_kind is not None)
    if alarm:
        print(f"checkpoint-save: {message}", file=sys.stderr)
        return 2
    print(json.dumps({"systemMessage": message}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
