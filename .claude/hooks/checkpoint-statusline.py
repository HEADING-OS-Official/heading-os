#!/usr/bin/env python3
"""
checkpoint-statusline.py - Claude Code statusLine hook.

Reads `context_window` from the statusLine payload, computes a soft/hard
level, writes THIS session's checkpoint-<session-slug>.json
(`checkpoint_paths.state_root`) for the Stop hook, and prints a single-line
status to stdout. Each of the two menu points is offered once; nothing repeats
in between (operator decision 2026-10-14).

Thresholds (env-vars, optional), the same for every session:
  CLAUDE_HANDOFF_SOFT_THRESHOLD   default 45  (% used -> the /second-wind menu)
  CLAUDE_HANDOFF_HARD_THRESHOLD   default 55  (% used -> the last call)

Each render also records the Claude Code process (`CLAUDE_PID` and its start
time), the session id and the transcript path, which the SessionStart hook reads
after a `/clear` to find the session before it.

Never a blank bar, which is what a dead hook looks like: an unparseable payload
prints "Claude Code", and a field of the wrong shape is dropped at entry
(`CP.read_payload`) and renders as `context: n/a`.
"""

import subprocess
import sys
from pathlib import Path

_BOOT = Path(__file__).resolve()
for _candidate in [_BOOT.parent, *_BOOT.parents]:
    if (_candidate / "scripts" / "utils" / "checkpoint_paths.py").is_file():
        sys.path.insert(0, str(_candidate))
        break
from scripts.utils import checkpoint_paths as CP  # noqa: E402
from scripts.utils import checkpoint_autonomy as AU  # noqa: E402
from scripts.utils.colors import supports_ansi  # noqa: E402

CP.force_utf8()


# ANSI colors for the status line, plain text where the shared check says the
# terminal does not render them (NO_COLOR, TERM=dumb, a bare Windows console).
_USE_ANSI = supports_ansi()


C_RESET = "\033[0m" if _USE_ANSI else ""
C_DIM = "\033[2m" if _USE_ANSI else ""
C_CYAN_B = "\033[1;36m" if _USE_ANSI else ""
C_YELLOW_B = "\033[1;33m" if _USE_ANSI else ""
C_GREEN = "\033[32m" if _USE_ANSI else ""
C_GREEN_B = "\033[1;32m" if _USE_ANSI else ""
C_YELLOW = "\033[33m" if _USE_ANSI else ""
C_RED = "\033[31m" if _USE_ANSI else ""


def coerce_used(cw: dict) -> float | None:
    used = cw.get("used_percentage")
    if used is not None:
        return float(used)
    remaining = cw.get("remaining_percentage")
    if remaining is not None:
        return 100.0 - float(remaining)
    return None


# The three input-token classes of one API usage record. Their SUM is the context
# the request carried; `input_tokens` alone is only the uncached part (MEASURED
# 2026-08-31 over 35,667 usage records: input 2, cache read 193725).
_TOKEN_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def context_tokens(usage: dict) -> int | None:
    """The whole context in tokens, or None when the payload names no class.

    Recorded because a percentage alone cannot settle where compaction fires. An
    absent class counts as zero: the API omits a class it has nothing for.
    """
    present = [usage[key] for key in _TOKEN_KEYS if usage.get(key) is not None]
    return sum(int(v) for v in present) if present else None


def _last_offered(state: dict) -> int:
    """The highest bucket already offered; 0, said on stderr, for a hand-edited bad value."""
    raw = state.get("last_offered_bucket")
    if not raw:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        print(
            f"checkpoint-statusline: last_offered_bucket is a "
            f"{type(raw).__name__} {raw!r}; reading it as 0",
            file=sys.stderr,
        )
        return 0


def git_branch(cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            # A ref name need not be UTF-8, and a strict decode raises ValueError.
            errors="replace",
            timeout=2,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


def is_linked_worktree(cwd: Path) -> bool:
    """True in a linked worktree (a yard): its git dir differs from the common one."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=2,
        )
        lines = result.stdout.splitlines()
        return result.returncode == 0 and len(lines) == 2 and lines[0] != lines[1]
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def progress_bar(used: float) -> str:
    remaining = max(0, min(100, round(100 - used)))
    filled = round(remaining / 10)
    empty = 10 - filled
    return "[" + "█" * filled + "░" * empty + "]"


def autonomy_segment(unattended: bool, ended: bool = False, hard: int | None = None) -> str:
    """One always-present segment naming whether unattended is live.

    Always rendered, so a switch that is off never looks like a mechanism that
    failed. `ended`: the stretch stopped while the switch stayed up. `hard` is
    the last-call threshold, shown when unattended can run /second-wind itself.
    """
    at = f" {hard}%" if hard is not None else ""
    if unattended:
        if ended:
            # Two spaces after the glyph: U+23F8 renders narrower than U+23F5.
            return f"{C_YELLOW}⏸  unattended paused{at}{C_RESET}"
        return f"{C_GREEN_B}⏵ unattended{at}{C_RESET}"
    return f"{C_DIM}⏸  manual{C_RESET}"


def build_status_line(
    payload: dict,
    project: Path,
    used: float | None,
    level: str | None,
    unattended: bool = False,
    stretch_ended: bool = False,
    hard: int | None = None,
) -> str:
    parts: list[str] = []

    workspace = payload.get("workspace") or {}
    cwd_str = workspace.get("current_dir") or payload.get("cwd") or str(project)
    dir_name = Path(cwd_str).name or str(project)

    branch = git_branch(Path(cwd_str))
    if branch and is_linked_worktree(Path(cwd_str)):
        # herdr names a yard's directory after its branch: say it once.
        if dir_name == branch:
            parts.append(f"{C_DIM}YARD{C_RESET} {C_YELLOW_B}{branch}{C_RESET}")
        else:
            parts.append(f"{C_DIM}YARD{C_RESET} {C_CYAN_B}{dir_name}{C_RESET}")
            parts.append(f"{C_DIM}on{C_RESET} {C_YELLOW_B}{branch}{C_RESET}")
    else:
        parts.append(f"{C_CYAN_B}{dir_name}{C_RESET}")
        if branch:
            parts.append(f"{C_DIM}on{C_RESET} {C_YELLOW_B}{branch}{C_RESET}")

    if used is None:
        parts.append(f"{C_DIM}context: n/a{C_RESET}")
    else:
        if level == "hard":
            color = C_RED
            tail = f" {C_RED}⛔ last call before auto-compact{C_RESET}"
        elif level == "soft":
            color = C_YELLOW
            tail = f" {C_YELLOW}⚠ /second-wind suggested{C_RESET}"
        else:
            color = C_GREEN
            tail = ""
        bar = progress_bar(used)
        remaining = max(0, min(100, round(100 - used)))
        parts.append(f"{color}{bar} {remaining}%{C_RESET}{tail}")

    parts.append(autonomy_segment(unattended, stretch_ended, hard))

    model = (payload.get("model") or {}).get("display_name") or "Claude"
    parts.append(f"{C_DIM}{model}{C_RESET}")

    return " ".join(parts)


def apply_render(
    state: dict, payload: dict, cw: dict, used: float | None, now_iso: str
) -> dict:
    """Decide this render, stamp it into `state` in place, return what the bar needs.

    Every input to the offer decision is read from the same dict the decision is
    written into, inside ONE hold of `CP.locked_state`, so a bucket the Stop
    hook consumed a moment ago cannot be re-offered. Nothing here prints, forks
    or sleeps, so the locked span stays short.
    """
    previous_last_offered = _last_offered(state)
    cfg = AU.config()

    if used is None:
        level = None
        bucket = 0
    else:
        if used >= cfg["hard"]:
            level = "hard"
        elif used >= cfg["soft"]:
            level = "soft"
        else:
            level = None
        bucket = AU.threshold_bucket(used, cfg)

    # The operator's switches are read here and NEVER written below: this dict
    # is written every render, and listing them would erase his choice.
    unattended = AU.unattended_mode(state)
    # The STRETCH ended while the SWITCH stayed up.
    stretch_ended = bool(
        state.get("unattended_done_at") or state.get("unattended_paused_at")
    )

    state.update(
        {
            "session_id": CP.session_id(payload),
            "transcript_path": payload.get("transcript_path"),
            "soft_threshold": cfg["soft"],
            "hard_threshold": cfg["hard"],
            "updated_at": now_iso,
        }
    )
    # The measurement keys, only from a render that measured: a null here would
    # leave the Stop hook with no reading at all.
    if used is not None:
        state.update(
            {
                "used_percentage": used,
                "remaining_percentage": cw.get("remaining_percentage"),
                "context_window_size": cw.get("context_window_size"),
                "context_input_tokens": context_tokens(cw.get("current_usage") or {}),
                "current_bucket": bucket,
            }
        )

    CP.record_process(state, payload)
    # After the update, so the next render cannot overwrite the peak.
    CP.record_peak(state, used, now_iso)

    if used is None:
        # Measured nothing, so decide nothing: a pending offer survives.
        pass
    elif level is not None:
        if bucket > previous_last_offered:
            state["needs_compact_offer"] = True
            state["offer_level"] = level
            state["offer_bucket"] = bucket
        else:
            state["needs_compact_offer"] = False
            state["offer_level"] = None
            state["offer_bucket"] = previous_last_offered
    else:
        # Below soft: no offer. last_offered_bucket is kept, so a dip and a
        # recovery do not re-fire the same offer; only a real compact resets it.
        state["needs_compact_offer"] = False
        state["offer_level"] = None
        state["offer_bucket"] = None

    return {
        "level": level,
        "unattended": unattended,
        "stretch_ended": stretch_ended,
        "hard": cfg["hard"],
    }


def main() -> int:
    payload = CP.read_payload("checkpoint-statusline", empty_ok=True)
    if payload is None:
        # Never break the status line.
        print("Claude Code")
        return 0

    cw = payload.get("context_window") or {}
    used = coerce_used(cw)

    project = CP.project_root(payload)
    state_path = CP.state_path(project, CP.session_slug(payload))
    now_iso = CP.utc_now().isoformat()

    # Read, decide and write inside one hold of the lock every writer shares.
    render = None
    try:
        with CP.locked_state(state_path) as state:
            render = apply_render(state, payload, cw, used, now_iso)
    except Exception as exc:
        # State write failure should not break the status line
        print(f"checkpoint-statusline: state write failed: {exc}", file=sys.stderr)
    if render is None:
        # Nothing recorded; decide for the screen from a throwaway read.
        render = apply_render(CP.read_json(state_path), payload, cw, used, now_iso)

    print(
        build_status_line(
            payload,
            project,
            used,
            render["level"],
            render["unattended"],
            render["stretch_ended"],
            render["hard"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
