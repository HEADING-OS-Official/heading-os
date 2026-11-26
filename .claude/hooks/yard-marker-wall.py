#!/usr/bin/env python3
"""PreToolUse wall: a session in a yard without `HEADING_OS_YARD=1` is refused every tool.

WHY. The yard bootstrap starts the agent with `HEADING_OS_YARD=1` exported into
the agent PROCESS, and every yard-side guard that reads the marker is inert
without it. Until 2026-11-19 an unmarked yard session got only a SessionStart
alert (`check_yard_marker` in `session-start.py`) and could then call every
tool. Operator decision 2026-11-18: the boundary sits in the yard session
itself, so an unmarked session can do nothing at all.

WHICH CHECKOUT IS JUDGED. This file's own: `Path(__file__).resolve().parents[2]`,
the same root `WORKSPACE` is in `_dispatch.py`. The payload's `cwd` is never
read. "Is this a yard" is decided from the checkout, never from the marker
(`not clone_guard.is_main_clone(root)`, as `_yard_in_a_yard()` does), because a
check that read the marker to decide whether the marker matters would prove
itself and fire never. When that question cannot be answered, the call is
refused with the exception text: unsure is not "not a yard".

WHY A SEPARATE FILE. `_dispatch.py` is registered only for a list of tool
matchers; this wall is registered with matcher `.*`, so it sees every tool,
MCP tools no matcher names included.

THE LIMIT, NOT CLOSED HERE. The settings loader walks up from the session's cwd
and does nothing when it finds no `.claude/hooks/yard-marker-wall.py`, so a
session whose cwd is outside any checkout is not covered by this wall.

Output: exactly one deny object on stdout and exit 0, or nothing and exit 0.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _reason(root: Path) -> str:
    return (f"YARD MARKER MISSING: this session runs in the yard {root} without "
            "HEADING_OS_YARD=1, so it is refused every tool. Nothing inside the "
            "session can repair this. Close it and start it again: "
            f"cd {root}, then HEADING_OS_YARD=1 claude --dangerously-skip-permissions")


def _deny(reason: str) -> None:
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, sys.stdout)


def main() -> int:
    # The payload is read so the harness's pipe is drained; its content never
    # changes the answer, and a payload that does not parse is an ordinary call.
    try:
        json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError) as exc:
        print(f"yard-marker-wall: payload not parsed ({type(exc).__name__}); "
              "judged like any other call", file=sys.stderr)

    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts.utils.clone_guard import is_main_clone
        in_yard = not is_main_clone(ROOT)
    except Exception as exc:  # noqa: BLE001 - refused with its text, never swallowed
        _deny(_reason(ROOT) + "\n\n" + f"{type(exc).__name__}: {exc}")
        return 0

    if in_yard and os.environ.get("HEADING_OS_YARD") != "1":
        _deny(_reason(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
