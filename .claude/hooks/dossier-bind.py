#!/usr/bin/env python3
"""Bind Lific issues to this session, never asking anyone (task dossier, spec section 4).

UserPromptSubmit: issue ids named in an operator's message join the session's
task list. A yard brief (its first line is the `X-HEADING-BRIEF` marker) binds only the ids on its
`Task:` line, never every id the brief mentions in passing.
PostToolUse on `mcp__lific__update_issue`: an issue set `active` joins; one set
`done` or `cancelled` leaves, and only when the response confirms the update.
`create_issue` binds nothing: creating a follow-up or a capture is not working
on it (spec section 4 narrowed by HELM, 2026-10-24). `bulk_update` is not
watched: it answers with a count and names no issue.

The list lives under `dossier_tasks` in this session's checkpoint state and is
read by every writer of a prompt, handoff or brief. Empty means `Task: none`
and nothing posted. Across /clear `checkpoint-inject.py` carries it as
candidates under `dossier_carried` (HOS-259), which no writer reads: an id
there is confirmed by the verbatim half of a prompt `prompt-archive.py` saves,
or by a confirmed `update_issue ... active`, and never by a message naming it.

A hook must never cost the turn: every failure is a line on stderr and exit 0.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils import checkpoint_paths as CP  # noqa: E402
from scripts.utils import dossier  # noqa: E402

#: The marker `scripts/herdr-brief.py` puts on every brief.
BRIEF_MARKER = "X-HEADING-BRIEF"


def _candidates(text: str) -> bool:
    return bool(dossier.TASK_RE.search(text or ""))


def _is_brief(prompt: str) -> bool:
    return prompt.lstrip().split("\n", 1)[0].startswith(BRIEF_MARKER + ":")


def changes(payload: dict) -> tuple[list[str], list[str]]:
    """(ids to add, ids to drop) for one hook payload."""
    event = payload.get("hook_event_name")
    if event == "UserPromptSubmit":
        prompt = payload.get("prompt", "") or ""
        if not _candidates(prompt):
            return [], []
        if _is_brief(prompt):
            line = next((ln for ln in prompt.splitlines() if ln.startswith("Task:")), "")
            return dossier.find_ids(line, dossier.project_keys()), []
        return dossier.find_ids(prompt, dossier.project_keys()), []
    if event != "PostToolUse":
        return [], []
    tool = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input") or {}
    if tool.endswith("__update_issue"):
        ident = str(tool_input.get("identifier", ""))
        if not dossier.TASK_RE.fullmatch(ident):
            return [], []
        status = str(tool_input.get("status", ""))
        if status not in ("done", "cancelled", "active"):
            return [], []
        # Only a confirmed update moves the list: the MCP answer opens
        # `Updated HOS-9001: HOS-9001 | active | ...` (read 2026-10-24).
        text = json.dumps(payload.get("tool_response", ""), ensure_ascii=False)
        if f"Updated {ident}:" not in text:
            print(f"dossier-bind: {ident} not confirmed updated, task list unchanged",
                  file=sys.stderr)
            return [], []
        if status in ("done", "cancelled"):
            return [], [ident]
        if status == "active":
            return [ident], []
    return [], []


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError as exc:
        print(f"dossier-bind: unreadable payload ({exc})", file=sys.stderr)
        return 0
    try:
        add, drop = changes(payload if isinstance(payload, dict) else {})
        if not add and not drop:
            return 0
        path = CP.state_path(CP.project_root(payload), CP.session_slug(payload))
        with CP.locked_state(path) as state:
            carried = dossier.carried_tasks(state)
            if (payload.get("hook_event_name") == "PostToolUse"
                    or _is_brief(str(payload.get("prompt", "") or ""))):
                # A confirmed `active`, or a brief's own `Task:` line, is an
                # explicit act on the issue, so it confirms a carried candidate
                # too (HOS-259).
                for ident in add:
                    dossier.confirm_carried(state, ident)
            else:
                # A message naming a carried id does not confirm it: only the
                # verbatim half of a saved prompt does (prompt-archive.py).
                dossier.add_tasks(state, [i for i in add if i not in carried])
            for ident in drop:
                dossier.drop_task(state, ident)
    except Exception as exc:  # noqa: BLE001 - a hook must never cost the turn
        print(f"dossier-bind: task list not updated ({type(exc).__name__}: {exc})",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
