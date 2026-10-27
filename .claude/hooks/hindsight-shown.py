#!/usr/bin/env python3
"""hindsight-shown.py - Claude Code Stop hook. Holds an answer that hides its Hindsight check.

Operator decision, 2026-10-13: "I want to ALWAYS see that you also check
Hindsight memory and show information from it" (translated). The UserPromptSubmit hook
`hindsight-recall-inject.py` already asks Hindsight on every prompt and leaves a
per-session marker, `turn-<session>.json`, when it injected facts (or the
snapshot's answer). This hook reads that marker at the end of the turn: when
facts were injected and the final answer carries neither a "From Hindsight
memory" block nor a "Hindsight:" line nor "from snapshot of", it returns
{"decision": "block"} so the answer is completed before the turn ends.

It makes no Hindsight query of its own, so a compliant turn costs nothing.
It checks that the check is SHOWN, never that what is shown is faithful.

Anti-loop: honours `stop_hook_active`, so it holds a turn at most once.
Never fatal: any error is one stderr line and the turn ends normally.

Settings: HINDSIGHT_INJECT_STATE_DIR, the same override the inject hook reads.

Usage (manual run, reads the Stop payload on stdin):
    echo '{"session_id": "s", "last_assistant_message": "..."}' | python3 .claude/hooks/hindsight-shown.py
"""
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORKSPACE = Path(__file__).resolve().parent.parent.parent
SHOWN = ("from hindsight memory", "hindsight:", "from snapshot of")

REASON = """\
Hindsight returned {facts} fact(s) for this message, and the answer does \
not show the check. Add a blockquote opening "> 🧠 **From Hindsight memory** —" \
with what it returned and where it agrees or disagrees with the records, or one \
line "Hindsight: <nothing relevant / did not answer>". If the injected facts are \
off-topic for the question, query Hindsight yourself first."""


def _log(msg: str, exc: BaseException | None = None) -> None:
    tail = f": {exc}" if exc is not None else ""
    print(f"hindsight-shown: {msg}{tail}", file=sys.stderr)


def _state_dir() -> Path:
    """The marker directory `hindsight-recall-inject.py` writes, from the shared helper."""
    if str(WORKSPACE) not in sys.path:
        sys.path.insert(0, str(WORKSPACE))
    from scripts.utils.hook_common import hook_state_dir  # noqa: PLC0415
    return hook_state_dir("hindsight-recall", "HINDSIGHT_INJECT_STATE_DIR")


def _last_text(payload: dict) -> str | None:
    """The final assistant text of this turn, or None when it cannot be read."""
    text = payload.get("last_assistant_message")
    if isinstance(text, str):
        return text
    transcript = payload.get("transcript_path")
    if not transcript:
        return None
    parts: list[str] = []
    with open(transcript, encoding="utf-8") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            msg = entry.get("message") if isinstance(entry, dict) else None
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if entry.get("type") == "user" and (isinstance(content, str) or any(
                    isinstance(c, dict) and c.get("type") == "text" for c in content or [])):
                parts = []   # a new prompt starts a new turn
            elif entry.get("type") == "assistant" and isinstance(content, list):
                parts += [c.get("text", "") for c in content
                          if isinstance(c, dict) and c.get("type") == "text"]
    return "\n".join(parts)


def main() -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    session = str(payload.get("session_id") or "")
    if not session or "/" in session or session.startswith("."):
        return
    marker = _state_dir() / f"turn-{session}.json"
    if not marker.is_file():
        return
    if payload.get("stop_hook_active"):
        marker.unlink(missing_ok=True)
        return
    facts = json.loads(marker.read_text(encoding="utf-8")).get("facts", 0)
    text = _last_text(payload)
    if text is None:
        _log("answer text unreadable, check not made")
        return
    if any(s in text.lower() for s in SHOWN):
        marker.unlink(missing_ok=True)
        return
    print(json.dumps({"decision": "block", "reason": REASON.format(facts=facts)}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - never hold a turn hostage, never hide the failure
        _log("unexpected failure, check not made", exc)
    sys.exit(0)
