"""The environment every `claude` process the engine starts must run under.

Usage:
    from scripts.utils.claude_child import claude_child_env
    subprocess.run(["claude", "-p", ...], env=claude_child_env(drop=("WORKSPACE_ROOT",)))

WHY THIS EXISTS (HOS-410). The operator's instruction is that nothing sends
telemetry. `~/.claude/settings.json` carries the four switches below in its
`env` block, and an interactive session applies them. A child started with
`--setting-sources project` never reads user settings, so it gets only what
its parent's environment holds, and a systemd timer's environment holds none
of them. MEASURED 2026-11-22: the nightly suite's `rules-ab.py` auth probe
created its project directory at 01:30:38.638 and Claude Code's feature-flag
cache in `~/.claude.json` was rewritten at 01:30:38.869, while every
interactive session, which carries the switches, left it untouched.

So the switches are set here, on every child, whatever the parent has and
whatever `--setting-sources` says. `scripts/check-claude-child-env.py` refuses
any place in the engine that starts `claude` without this helper.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

#: Each set to "1". Documented at https://code.claude.com/docs/en/env-vars:
#: CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC covers the other three and also
#: auto-updates and feature-flag fetching; the three are named as well so a
#: change in what the umbrella covers does not reopen any of them.
TELEMETRY_SWITCHES: dict[str, str] = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_BUG_COMMAND": "1",
}


def claude_child_env(base: Mapping[str, str] | None = None, *, drop: Iterable[str] = ()) -> dict[str, str]:
    """`base` (default: this process's environment) minus `drop`, with the switches forced on."""
    source = os.environ if base is None else base
    dropped = set(drop)
    env = {k: v for k, v in source.items() if k not in dropped}
    env.update(TELEMETRY_SWITCHES)
    return env
