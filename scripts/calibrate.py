#!/usr/bin/env python3

"""calibrate.py - Parse the active Claude Code session JSONL transcript into a clean envelope.

Used by the /calibrate skill to surface user corrections, preferences, repeated
patterns, errors, success signals, and voice violations from a finished session.

Usage:
    python scripts/calibrate.py [--session PATH] [--sessions-dir PATH]
                                [--since-utc TS] [--max-bytes N]
                                [--no-workspace]

Output: JSON envelope to stdout with session_id, session_path, started_at_utc,
ended_at_utc, event_count, truncated, user_turns, assistant_turns, tool_errors,
system_reminders, and (unless --no-workspace) workspace block with skills/rules/ceo_only_paths.

Exit codes: 0 ok, 2 no session found, 3 session unreadable, 4 the transcript
directory could not be resolved on this platform, 1 caller error (an
unparseable --since-utc, reported cleanly) or other parser crash. 4 is separate
from 2 because 2 asserts a completed search of a directory this one never named.

CEO-EYES-ONLY USAGE: emitted envelope may contain session content. Do not pipe
to external services. Consumed locally by the /calibrate skill and by
scripts/chronicle.py, which imports the parser and the truncation helpers.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.checkpoint_paths import transcript_dir  # noqa: E402
from scripts.utils.colors import YELLOW, RESET  # noqa: E402
from scripts.utils.workspace import get_workspace_root  # noqa: E402


def _derive_sessions_dir() -> Path | None:
    """The Claude Code session-transcript directory for this workspace, or None.

    The slug rule is NOT reimplemented here: its one owner is
    ``scripts/utils/checkpoint_paths.transcript_dir`` (a second copy that ate
    underscores was removed 2026-09-06).

    Returns None off POSIX, because that is what the owner returns rather than
    guessing a Windows slug it cannot verify. ``main`` says so out loud; a
    silent None would be the same silent-zero defect one level up.

    The ``CLAUDE_SESSIONS_DIR`` override and the ``--sessions-dir`` flag are
    kept and are checked FIRST: neither is about the slug, and an explicit
    directory is an answer the caller already has.
    """
    override = os.environ.get("CLAUDE_SESSIONS_DIR")
    if override:
        return Path(override)
    return transcript_dir(get_workspace_root())


# Import-time on purpose: `get_workspace_root()` has no raising path (MEASURED
# 2026-09-02). `scripts/chronicle.py` imports this name.
#
# The None is load-bearing: every consumer
# (`main` below, `scripts/chronicle.py:cmd_build`) must report the refusal
# rather than search a directory it could not name.
DEFAULT_SESSIONS_DIR: Path | None = _derive_sessions_dir()
DEFAULT_MAX_BYTES = 800_000


def _mtime(path: Path) -> float:
    """Modification time, or 0.0 for a file that vanished mid-sort.

    Transcripts rotate. `sorted(..., key=lambda p: p.stat().st_mtime)` stats
    each candidate AFTER the glob listed it, so a file deleted in between
    raised an uncaught FileNotFoundError and crashed the whole run with exit 1
    instead of skipping the one candidate that no longer exists.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def locate_session(sessions_dir: Path) -> Path | None:
    """Return the newest .jsonl file in sessions_dir by mtime, or None."""
    candidates = sorted(sessions_dir.glob("*.jsonl"), key=_mtime, reverse=True)
    return candidates[0] if candidates else None


def parse_jsonl(path: Path) -> tuple[list, list]:
    """Return (events, skipped_line_numbers). Tolerate malformed lines.

    "Malformed" covers unparseable JSON and well-formed JSON that is not an
    OBJECT (`null`, `123`, `[]`): every consumer calls `.get()` on an event.
    """
    events = []
    skipped = []
    # `errors="replace"` (2026-09-01): a non-UTF-8 byte otherwise raises from the
    # iteration itself and kills the run; replacing bounds the damage to that
    # one line, which then parses or is counted in `skipped`.
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped.append(lineno)
                continue
            if not isinstance(obj, dict):
                skipped.append(lineno)
                continue
            events.append(obj)
    return events, skipped


def _turn_text(content) -> str:
    """The readable prose of one turn, whichever shape the harness wrote it in.

    Claude Code writes `message.content` as a plain string for a typed user
    message and as a LIST OF BLOCKS for everything else — every assistant turn,
    and every user turn that carries a tool result.

    Both shapes matter: reading only the string shape dropped every assistant
    turn, 96% of the prose (MEASURED 2026-08-22).

    Only `text` blocks are prose. `tool_use` and `tool_result` payloads are
    machine traffic that would drown it, and a `thinking` block is written with
    an empty `thinking` field and a signature only — nothing to read.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    ]
    return "\n".join(parts)


def _message_content(ev: dict):
    """`ev["message"]["content"]`, or "" when `message` is not a mapping.

    The read used to be `(ev.get("message") or {}).get(...)`, and `or` only
    substitutes for a FALSY value: a line carrying `"message": "hello"` came
    through as the string, and the next `.get` raised AttributeError - out of
    `build_envelope`, out of `main`, and the run died on one odd line.
    `parse_jsonl`'s docstring calls that "the opposite of the tolerance this
    docstring promises", and the `tool_use` branch below carried a comment
    pointing AT those two lines as "the correct idiom". They were the same
    defect it was written to fix.
    """
    message = ev.get("message")
    return message.get("content", "") if isinstance(message, dict) else ""


def _result_text(content) -> str:
    """The readable payload of a tool_result block, whichever shape it carries.

    Usually a plain string. Some tools return a list of blocks instead, and a
    `str(...)` of that list would put Python repr punctuation into the envelope
    the detection prompts read.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and isinstance(b.get("text"), str)]
        return "\n".join(p for p in parts if p)
    return ""


_EXIT_CODE_RE = re.compile(r"^(?:Error:\s*)?Exit code (\d+)\b")


def _exit_code_from(text: str):
    """The exit code a Bash failure spells out, or None when there is none.

    The transcript records no numeric exit code anywhere; for Bash the harness
    writes it into the first line of the error text ("Exit code 1"). For every
    other tool there is no code at all, and None means exactly that - not zero,
    which would read as success. The `is_error` flag is what decides the record
    exists; this is diagnostic detail on top.
    """
    m = _EXIT_CODE_RE.match(text.strip())
    return int(m.group(1)) if m else None


def build_envelope(session_path: Path, events: list) -> dict:
    """Filter and shape events into the envelope schema.

    Tool traffic is read from CONTENT BLOCKS inside messages, the only place
    Claude Code puts it; no top-level tool events exist (MEASURED 2026-08-27).

    A failure is flagged by `is_error: true` on the tool_result block. The
    matching command comes from the tool_use block with the same id, so two
    calls of one tool in a turn are never cross-attributed.
    """
    user_turns = []
    assistant_turns = []
    tool_errors = []
    system_reminders = []
    tool_uses: dict[str, dict] = {}  # tool_use_id -> {"tool", "cmd"}
    for ev in events:
        ev_type = ev.get("type")
        ts = ev.get("timestamp", "")
        if ev_type == "user":
            text = _turn_text(_message_content(ev))
            if text:
                user_turns.append({"ts": ts, "text": text})
        elif ev_type == "assistant":
            text = _turn_text(_message_content(ev))
            if text:
                assistant_turns.append({"ts": ts, "text": text})
        elif ev_type == "system":
            content = ev.get("content", "")
            if isinstance(content, str) and content:
                system_reminders.append({"ts": ts, "text": content})

        # Tool traffic rides inside user and assistant messages alike, so this
        # runs for every event rather than as another branch of the chain above.
        blocks = _message_content(ev)
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                use_id = block.get("id")
                if not use_id:
                    continue
                # `.get("input", {})` only defaults when the KEY IS ABSENT, so an
                # explicit `"input": null` (or a string) goes straight to
                # AttributeError without this isinstance check.
                raw_input = block.get("input")
                cmd = raw_input.get("command", "") if isinstance(raw_input, dict) else ""
                tool_uses[use_id] = {"tool": block.get("name", ""), "cmd": cmd}
            elif btype == "tool_result" and block.get("is_error"):
                use = tool_uses.get(block.get("tool_use_id")) or {}
                stderr = _result_text(block.get("content"))
                tool_errors.append({
                    "ts": ts,
                    "tool": use.get("tool", ""),
                    "cmd": use.get("cmd", ""),
                    "exit_code": _exit_code_from(stderr),
                    "stderr": stderr,
                })
    started = events[0].get("timestamp", "") if events else ""
    ended = events[-1].get("timestamp", "") if events else ""
    return {
        "session_id": session_path.stem,
        "session_path": str(session_path),
        "started_at_utc": started,
        "ended_at_utc": ended,
        "event_count": len(events),
        "truncated": False,
        "user_turns": user_turns,
        "assistant_turns": assistant_turns,
        "tool_errors": tool_errors,
        "system_reminders": system_reminders,
    }


def _instant(ts: str):
    """An ISO timestamp as an aware datetime, or None if it will not parse."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def filter_since(events: list, since_utc: str) -> list:
    """Keep events at or after `since_utc`, comparing instants, not strings.

    A transcript mixes offset notations (`+00:00` and `Z`), so string order is
    not time order.

    An unparseable `--since-utc` is a caller error and raises. An unparseable
    event timestamp is data, so it is KEPT: over-reporting beats silently
    discarding a turn the filter could not read.
    """
    floor = _instant(since_utc)
    if floor is None:
        raise ValueError(f"--since-utc is not an ISO timestamp: {since_utc!r}")
    out = []
    for ev in events:
        stamp = _instant(ev.get("timestamp", ""))
        if stamp is None or stamp >= floor:
            out.append(ev)
    return out


def serialize(envelope: dict) -> str:
    """The one serialization of the envelope. `main` prints exactly this.

    One function, so the bytes `--max-bytes` measures are the bytes printed.
    Compact rather than indented, because the budget has to be the thing that is
    measured and the incremental shed arithmetic in `_entry_bytes` is exact only
    for this form. The consumer is the /calibrate skill, not the eye; pipe
    through `python -m json.tool` to read one by hand.
    """
    return json.dumps(envelope, ensure_ascii=False)


def envelope_bytes(envelope: dict) -> int:
    """Byte count of what `main` writes to stdout."""
    return len(serialize(envelope).encode("utf-8"))


def _entry_bytes(entry, siblings_left: int) -> int:
    """What the serialized envelope shrinks by when one list entry is dropped.

    An entry costs its own serialization plus the two-byte ", " separator that
    joined it to the next one; the last entry in a list carries no separator.
    The default json encoder serializes a nested value byte-for-byte the same
    inside the document as it does alone, so this delta is exact rather than an
    estimate, and `apply_truncation` can track the running size instead of
    re-serializing the whole envelope once per shed.
    """
    size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
    return size + 2 if siblings_left else size


def _drop_head(entries: list) -> int:
    """Pop `entries[0]` and return the bytes the envelope lost by it."""
    head = entries.pop(0)
    return _entry_bytes(head, len(entries))


def _pop_oldest(envelope: dict, keys: tuple[str, ...]) -> bool:
    """Drop the single oldest entry across `keys`. False when all are empty.

    Each list is already chronological, so the oldest overall is whichever
    list's head has the earliest `ts`. Shedding both sides together keeps the
    surviving tail a two-sided conversation; draining one list first would
    leave the questions without the answers.

    Compared as INSTANTS via `_instant`, not as strings, for the reason
    `filter_since` gives.
    """
    heads = [(envelope[k][0].get("ts", ""), k) for k in keys if envelope.get(k)]
    if not heads:
        return False

    def _age(head):
        moment = _instant(head[0])
        # An unplaceable stamp sheds FIRST. It cannot be compared against a
        # datetime without a TypeError, and falling back to string order is
        # the bug this function exists to stop.
        return (0, 0.0) if moment is None else (1, moment.timestamp())

    heads.sort(key=_age)
    envelope[heads[0][1]].pop(0)
    return True


def apply_truncation(envelope: dict, max_bytes: int) -> dict:
    """Shed oldest entries toward `max_bytes`, and never fake having reached it.

    Order: harness boilerplate first, then the oldest prose from both sides
    together, then the small diagnostic list. `tool_errors` goes last because
    it is cheap and it is what the /calibrate skill reads for failure patterns.

    The size is a target, not a guarantee — a `max_bytes` smaller than the bare
    metadata cannot be met by shedding anything. Callers check
    `envelope_bytes()` against their own budget; `main` warns on stderr.

    The running size is tracked incrementally through `_entry_bytes`, so the
    whole shed costs two serializations; one per shed was O(n squared) and hung
    on a large transcript (MEASURED 2026-08-31).
    """
    size = envelope_bytes(envelope)
    if size <= max_bytes:
        return envelope
    envelope["truncated"] = True
    # Re-read rather than adjust by hand: `truncated` may be a flip from False
    # or a key this envelope did not carry at all, and callers pass both shapes.
    size = envelope_bytes(envelope)
    while envelope["system_reminders"] and size > max_bytes:
        size -= _drop_head(envelope["system_reminders"])
    prose = ("user_turns", "assistant_turns")
    while size > max_bytes:
        # `_pop_oldest` owns the choice of which side is older, so read the
        # delta off whichever list it shortened rather than duplicating that
        # comparison here, where a second copy would be the one left unfixed.
        heads = {k: envelope[k][0] for k in prose if envelope.get(k)}
        before = {k: len(envelope.get(k) or ()) for k in prose}
        if not _pop_oldest(envelope, prose):
            break
        shed = next(k for k in prose if len(envelope.get(k) or ()) < before[k])
        size -= _entry_bytes(heads[shed], len(envelope[shed]))
    while envelope["tool_errors"] and size > max_bytes:
        size -= _drop_head(envelope["tool_errors"])
    return envelope


def _ceo_only_paths() -> list[str]:
    """The path prefixes the routing map resolves to `private`.

    This field was a hardcoded `[]` until 2026-08-24 while both the module
    docstring and this function's own docstring promised it was enumerated, so
    a consumer could not tell "no ceo-only paths exist" from "this is a stub" —
    the coverage claim `.claude/rules/scope-claims.md` forbids. The routing map
    is the workspace's single classification input, so read it rather than
    restate it: a second list would be the copy that stops being updated.

    Since HOS-232 the data repository is private by REPOSITORY and the map
    carries no rule for it, so its root comes first, as one absolute path that
    stands for every path under it; the map's own `private` keys follow.
    """
    try:
        from scripts.utils.workspace import (
            data_overlay_present, get_data_root, load_routing_map,
        )
    except ImportError:
        return []
    try:
        rules = load_routing_map()["rules"]
    except Exception as exc:  # a broken map must not take the envelope down
        print(f"{YELLOW}[workspace warning]{RESET} routing map unreadable "
              f"({exc}); ceo_only_paths omitted.", file=sys.stderr)
        return []
    data = [str(get_data_root())] if data_overlay_present() else []
    return data + sorted(k for k, dest in rules.items() if dest == "private")


def _listing(label: str, read) -> list[str]:
    """Run one optional directory read, degrading loudly instead of raising.

    `exists()` does not make a directory readable, and this block is the
    optional part of the envelope: it must never take the command down.
    """
    try:
        return sorted(read())
    except OSError as exc:
        print(f"{YELLOW}[workspace warning]{RESET} {label} unreadable "
              f"({exc.__class__.__name__}: {exc}); listed as empty.",
              file=sys.stderr)
        return []


def populate_workspace_block(repo_root: Path) -> dict:
    """Enumerate skills, rules, and ceo-only paths from the workspace."""
    skills_dir = repo_root / ".claude" / "skills"
    rules_dir = repo_root / ".claude" / "rules"
    skills = _listing(str(skills_dir), lambda: (
        p.name for p in skills_dir.iterdir() if p.is_dir() and p.name != "archive"
    )) if skills_dir.exists() else []
    rules = _listing(str(rules_dir), lambda: (
        p.name for p in rules_dir.glob("*.md")
    )) if rules_dir.exists() else []
    return {"skills": skills, "rules": rules, "ceo_only_paths": _ceo_only_paths()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, help="explicit session JSONL path")
    parser.add_argument("--sessions-dir", type=Path, default=DEFAULT_SESSIONS_DIR)
    parser.add_argument("--since-utc", type=str, help="filter events after this ISO timestamp")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--no-workspace", action="store_true", help="skip workspace block (testing)")
    args = parser.parse_args(argv)

    if args.session:
        session_path = args.session
    elif args.sessions_dir is None:
        # Exit 4, never 2. Exit 2 means "I looked in the transcript directory
        # and there were no sessions in it", and nothing looked: the slug owner
        # refused to guess a directory name for this platform. Reporting that
        # as an empty search is the silent-zero this whole seam exists to stop.
        print("the transcript directory could not be resolved on this platform "
              "(scripts/utils/checkpoint_paths.transcript_dir returns None off "
              "POSIX rather than guessing a slug it cannot verify). Pass "
              "--sessions-dir or set CLAUDE_SESSIONS_DIR.", file=sys.stderr)
        return 4
    else:
        session_path = locate_session(args.sessions_dir)
        if session_path is None:
            print(f"no session JSONL found in {args.sessions_dir}", file=sys.stderr)
            return 2

    try:
        events, skipped = parse_jsonl(session_path)
    except OSError as e:
        # Every OSError (a mistyped `--session` naming a directory included) is
        # "session unreadable", exit 3.
        print(f"session unreadable: {e}", file=sys.stderr)
        return 3

    if skipped:
        print(f"{YELLOW}[parser warning]{RESET} skipped {len(skipped)} malformed line(s): {skipped}", file=sys.stderr)

    if args.since_utc:
        try:
            events = filter_since(events, args.since_utc)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1

    envelope = build_envelope(session_path, events)
    envelope = apply_truncation(envelope, args.max_bytes)
    actual = envelope_bytes(envelope)
    if actual > args.max_bytes:
        print(f"{YELLOW}[truncation warning]{RESET} envelope is {actual} bytes "
              f"after shedding every droppable list; --max-bytes "
              f"{args.max_bytes} could not be met.", file=sys.stderr)
    if not args.no_workspace:
        envelope["workspace"] = populate_workspace_block(get_workspace_root())

    print(serialize(envelope))
    return 0


if __name__ == "__main__":
    sys.exit(main())
