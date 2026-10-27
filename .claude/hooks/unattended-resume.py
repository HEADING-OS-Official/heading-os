#!/usr/bin/env python3
"""UserPromptSubmit hook: the operator's instruction resumes a paused stretch.

The operator's next instruction clears a paused stretch, at prompt submission:
the window belongs to his turn, which begins when he presses Enter. Until
2026-08-20 the clear waited for the next Stop, so the status bar showed the
stretch paused through the whole turn his message had resumed.

Cheap by construction. The common case - no pause marker - is one JSON read and
an exit; nothing is written and nothing is printed. The rule is that this hook
never blocks, and writes to STDOUT only for one thing: a bare "1" or "2" typed
while a /second-wind menu waits for its answer (`AU.SECOND_WIND_MENU_KEY`) gets
the pick as `additionalContext`, because a UserPromptSubmit hook cannot rewrite
the prompt itself (operator request 2026-10-15). Any prompt consumes the menu;
any other text is left untouched. A prompt that cannot resume a stretch must
not cost the operator a visible error. It is not silent on stderr, and never was:
`CP.project_root` and `CP.read_json` both report there from inside the same try
block, and a failed clear now says so too - the pause marker surviving is what
keeps the status bar showing "unattended paused" for a stretch already resumed.

The Stop hook keeps its own `prompt_id` comparison. That is not redundancy to
delete: this hook is not registered in every clone, and a stretch that could
only be resumed from here would strand itself wherever it is missing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WORKSPACE))

# The `/compact` that /second-wind's Stop hook submits through HERDR. The
# harness delivers it here as an ordinary prompt, and it is NOT the operator
# speaking: treating it as a new instruction would reset the continuation
# counter on every compaction and retire the ceiling that bounds a night.
# `_queue_pending` in `scripts/utils/checkpoint_autonomy.py` excludes it for the
# same reason. Imported from its one owner; the literal is only the fallback for
# a clone without the module, and nothing checks the two stay equal.
try:
    from scripts.utils.herdr_agent import COMPACT_COMMAND
except Exception as _exc:  # noqa: BLE001 - a bundled clone may not carry the module
    print(f"unattended-resume: herdr_agent unavailable ({_exc}); "
          f"using the literal /compact", file=sys.stderr)
    COMPACT_COMMAND = "/compact"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - a malformed payload must not surface
        return 0

    # A payload that is valid JSON but not an object still reaches `.get`.
    # `[]`, `"x"`, `3` and `null` all parse, then raise an uncaught
    # AttributeError. Swept 2026-08-23 across every stdin hook: six crashed on
    # all four shapes. Same defect checkpoint-inject.py fixed on 2026-08-20;
    # the sweep is how the rest were found.
    if not isinstance(payload, dict):
        return 0

    if str(payload.get("prompt") or "").strip() == COMPACT_COMMAND:
        return 0

    try:
        from scripts.utils import checkpoint_autonomy as AU
        from scripts.utils import checkpoint_paths as CP

        payload = CP.validate_payload(payload, "unattended-resume")
        project = CP.project_root(payload)
        path = CP.state_path(project, CP.session_slug(payload))
        state = CP.read_json(path)
        # A /second-wind menu waiting for its answer: this prompt consumes it,
        # and a bare "1" or "2" picks that option (operator request 2026-10-15).
        pick = None
        if state.get(AU.SECOND_WIND_MENU_KEY):
            pick = AU.SECOND_WIND_PICKS.get(str(payload.get("prompt") or "").strip())
            with CP.locked_state(path) as fresh:
                fresh.pop(AU.SECOND_WIND_MENU_KEY, None)
        if pick:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit", "additionalContext": pick}}))
        # Read first, write only if there is something to clear. A prompt in a
        # session that never raised the switch touches no file at all.
        if not (state.get("unattended_done_at") or state.get("unattended_paused_at")):
            return 0
        with CP.locked_state(path) as fresh:
            AU.clear_unattended_window(fresh)
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        # Stderr, not stdout. The docstring's reason for swallowing covers
        # stdout, which for a UserPromptSubmit hook is injected into the prompt;
        # it never covered the record. If `write_json_atomic` fails inside
        # `locked_state` (a read-only mount, ENOSPC, a permission change under
        # `.claude/state/`), the pause marker survives and the status bar keeps
        # rendering "unattended paused" for a stretch the operator has already
        # resumed - the exact defect this hook was written to fix - with nothing
        # anywhere recording that the clear failed.
        print(f"unattended-resume: could not clear the pause window: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
