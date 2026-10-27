"""Small helpers the Claude Code hooks under `.claude/hooks/` share.

Stdlib only at import: the harness runs hooks with the system `python3`, which
has none of the engine's dependencies.

- `post_tool_advice` is the one way a PostToolUse hook speaks to the model.
- `is_machine_envelope` is the one test the memory hooks use to tell a prompt
  the harness submitted on its own from a question the operator typed.
- `hook_state_dir` is where a hook keeps its runtime state and run log.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Prompts the harness submits through UserPromptSubmit that nobody typed. A
# background task's completion arrives as `<task-notification>...`; recalling
# memory for it spends a lookup on machine text, and the Stop hook that holds
# an answer without its Hindsight check then holds a turn no person asked.
MACHINE_ENVELOPE_PREFIXES = ("<task-notification>",)


def post_tool_advice(message: str) -> None:
    """Deliver one advisory to the model from a PostToolUse hook.

    `hookSpecificOutput.additionalContext` is the documented PostToolUse field;
    a top-level `additionalContext` and stderr on exit 0 never reach the model
    (https://code.claude.com/docs/en/hooks, read 2026-10-16).
    """
    json.dump({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                      "additionalContext": message}}, sys.stdout)


def is_machine_envelope(prompt: str) -> bool:
    """True when the prompt is a harness envelope rather than an operator's words."""
    return prompt.lstrip().startswith(MACHINE_ENVELOPE_PREFIXES)


def hook_state_dir(subdir: str, override_env: str | None = None) -> Path:
    """`<runtime:hook-state/state>/<subdir>`, or the path in `override_env` when set.

    The override lets a test point a hook at a scratch directory. The store
    directory is created (at the store's own mode) by `data_write_dir`; the
    `<subdir>` beneath it is the caller's to create when it writes.
    """
    if override_env:
        override = os.environ.get(override_env)
        if override:
            return Path(override).expanduser()
    from scripts.utils.data_path import data_write_dir  # noqa: PLC0415
    return data_write_dir("runtime:hook-state/state") / subdir
