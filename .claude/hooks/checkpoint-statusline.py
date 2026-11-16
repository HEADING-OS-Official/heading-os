#!/usr/bin/env python3
"""
checkpoint-statusline.py - Claude Code statusLine hook.

Reads `context_window` from the statusLine payload, computes a soft/hard
level, writes THIS session's checkpoint-<session-slug>.json
(`checkpoint_paths.state_root`) for the Stop hook, and prints the status line
to stdout. Each of the two menu points is offered once; nothing repeats in
between (operator decision 2026-10-14).

The line (HOS-349, approved 2026-11-07): row 1 is the session state (HELM or
YARD badge, directory and branch, unattended with its two counters, the Lific
issue, yards, repository state, model; a yard shows no `data` segment, because
a YARD never runs git in the data repository); row 2 names the yards that wait for the
operator and row 3 the push, each only while it has something to say. Herdr,
lific and git are read by a detached refresher into `statusline_cache`
(`scripts/utils/statusline_cache.py`); the render reads only that cache.

Thresholds (env-vars, optional), the same for every session:
  CLAUDE_HANDOFF_SOFT_THRESHOLD   default 45  (% used -> the /second-wind menu)
  CLAUDE_HANDOFF_HARD_THRESHOLD   default 55  (% used -> the last call)

Each render also records the Claude Code process (`CLAUDE_PID` and its start
time), the session id and the transcript path, which the SessionStart hook reads
after a `/clear` to find the session before it.

Never a blank bar, which is what a dead hook looks like: an unparseable payload
prints "Claude Code", and a field of the wrong shape is dropped at entry
(`CP.read_payload`).
"""

import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

_BOOT = Path(__file__).resolve()
for _candidate in [_BOOT.parent, *_BOOT.parents]:
    if (_candidate / "scripts" / "utils" / "checkpoint_paths.py").is_file():
        sys.path.insert(0, str(_candidate))
        break
from scripts.utils import checkpoint_paths as CP  # noqa: E402
from scripts.utils import checkpoint_autonomy as AU  # noqa: E402
from scripts.utils.colors import supports_ansi  # noqa: E402
from scripts.utils import statusline_cache as SC  # noqa: E402

CP.force_utf8()


# ANSI colors for the status line, plain text where the shared check says the
# terminal does not render them (NO_COLOR, TERM=dumb, a bare Windows console).
_USE_ANSI = supports_ansi()


C_RESET = "\033[0m" if _USE_ANSI else ""
C_DIM = "\033[2m" if _USE_ANSI else ""
C_CYAN = "\033[36m" if _USE_ANSI else ""
C_CYAN_B = "\033[1;36m" if _USE_ANSI else ""
C_YELLOW_B = "\033[1;33m" if _USE_ANSI else ""
C_GREEN = "\033[32m" if _USE_ANSI else ""
C_GREEN_B = "\033[1;32m" if _USE_ANSI else ""
C_YELLOW = "\033[33m" if _USE_ANSI else ""
C_RED = "\033[31m" if _USE_ANSI else ""
C_RED_B = "\033[1;31m" if _USE_ANSI else ""


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


def inside_data_repo(cwd: Path) -> bool:
    """True when `cwd` lies inside the data repository (both resolved, compared
    by path prefix). No git call is ever made there from this hook: a YARD never
    runs git in the data repository (HOS-349 round 3). An unresolvable data root
    is no data repository to protect, said on stderr."""
    try:
        from scripts.utils.paths import get_data_root
        root = get_data_root().resolve()
        here = cwd.resolve()
    except Exception as exc:  # noqa: BLE001 - never break the status line
        print(f"checkpoint-statusline: data root not resolved ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return False
    return here == root or root in here.parents


def own_checkout_is_yard() -> bool:
    """Whether the checkout this hook runs from is a yard, asked without git in
    the session's directory (the answer the refresher uses for its own checkout).
    Unsure counts as a yard."""
    try:
        from scripts.utils.clone_guard import is_main_clone
        return not is_main_clone(CP.engine_root())
    except Exception as exc:  # noqa: BLE001 - never break the status line
        print(f"checkpoint-statusline: cannot tell HELM from a yard ({type(exc).__name__}); "
              "showing YARD", file=sys.stderr)
        return True


def git_branch(cwd: Path) -> str:
    if inside_data_repo(cwd):
        return ""
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
    """True in a linked worktree (a yard): its git dir differs from the common one.
    Inside the data repository no git runs: the hook's own checkout answers."""
    if inside_data_repo(cwd):
        return own_checkout_is_yard()
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


def is_main_clone(cwd: Path) -> bool:
    """True when `cwd` is the root of the clone this hook runs from (HELM's own tree)."""
    try:
        return cwd.resolve() == CP.engine_root()
    except OSError:
        return False


def _truecolor(fg: tuple, bg: tuple) -> str:
    """A bold badge colour pair as 24-bit escapes; empty where ANSI is off."""
    if not _USE_ANSI:
        return ""
    return f"\033[1;38;2;{fg[0]};{fg[1]};{fg[2]};48;2;{bg[0]};{bg[1]};{bg[2]}m"


# The badge colours of the approved mockups (2026-11-07).
BADGE_HELM = _truecolor((0xE8, 0xFB, 0xFF), (0x2E, 0x5A, 0x60))
BADGE_YARD = _truecolor((0xFF, 0xF4, 0xD6), (0x5A, 0x4A, 0x1E))
BADGE_PAUSED = _truecolor((0xFF, 0xF4, 0xD6), (0x7A, 0x5F, 0x1E))
SEP = f" {C_DIM}·{C_RESET} "
WAITING_SHOWN = 3


def _count(state: dict, key: str) -> int:
    try:
        return max(0, int(state.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def counters_text(rounds: int, winds: int) -> str:
    """`↻ N ⟳ M`; `⟳ 0` omitted, and both when both are 0. One ordinary space
    between each glyph and its number, so they never touch (operator, 2026-11-07)."""
    if not rounds and not winds:
        return ""
    return f"↻ {rounds}" + (f" ⟳ {winds}" if winds else "")


def autonomy_segment(unattended: bool, paused: bool = False, rounds: int = 0, winds: int = 0) -> str:
    """Always present, so a switch that is off never looks like a mechanism that failed.

    `paused`: the stretch stopped (`--done` or a pause) while the switch stayed
    up; it waits for the operator either way.
    """
    counters = counters_text(rounds, winds)
    if not unattended:
        return f"{C_DIM}‖ manual{C_RESET}"
    if paused:
        tail = f" {counters}" if counters else ""
        # The badge already says `answer needed`; this says what paused.
        return f"{C_YELLOW}‖ paused{tail}{C_RESET}"
    tail = f" {counters}" if counters else ""
    return f"{C_GREEN_B}▶ unattended{C_RESET}{tail}"


def _age_text(minutes: int) -> str:
    return f"{minutes}m" if minutes < 120 else f"{minutes // 60}h"


def handoff_segment(fresh: dict | None, now: float) -> str:
    """The handoff, only once the Stop hook measured it due (stale); else "".

    The minutes are wall-clock since the archive was written, read now; the
    events are as of the last Stop.
    """
    if not isinstance(fresh, dict) or not fresh.get("due"):
        return ""
    mtime = fresh.get("archive_mtime")
    if not isinstance(mtime, (int, float)):
        return f"{C_YELLOW}⚠ handoff stale: none written{C_RESET}"
    minutes = max(0, int((now - mtime) // 60))
    events = int(fresh.get("events") or 0)
    return f"{C_YELLOW}⚠ handoff stale: {_age_text(minutes)} old · {events} events since{C_RESET}"


def issue_segment(issue) -> str:
    if not isinstance(issue, dict) or not issue.get("id"):
        return ""
    status = _clean(str(issue.get("status") or ""))
    tail = f" {C_DIM}{status}{C_RESET}" if status else ""
    return f"{C_CYAN}{_clean(str(issue['id']))}{C_RESET}{tail}"


def yards_segment(total, waiting) -> str:
    if not isinstance(total, int) or total <= 0:
        return ""
    count = len(waiting) if isinstance(waiting, list) else 0
    # One ordinary space between the flag and its number, as in the counters.
    flag = f" {C_YELLOW_B}⚑ {count}{C_RESET}" if count else ""
    return f"yards {total}{flag}"


def repo_segment(label: str, repo) -> str:
    """`✓` in sync, `↑N` ahead, `●N` not committed, `?` when git was unreadable."""
    head = f"{C_DIM}{label}{C_RESET} "
    if not isinstance(repo, dict) or not isinstance(repo.get("dirty"), int):
        return f"{head}{C_DIM}?{C_RESET}"
    ahead = repo.get("ahead") if isinstance(repo.get("ahead"), int) else 0
    dirty = repo["dirty"]
    marks = []
    if ahead > 0:
        marks.append(f"↑{ahead}")
    if dirty > 0:
        marks.append(f"●{dirty}")
    if not marks:
        return f"{head}{C_GREEN}✓{C_RESET}"
    return f"{head}{C_YELLOW}{' '.join(marks)}{C_RESET}"


def _since_minutes(since, now: float) -> int | None:
    if not isinstance(since, str) or not since:
        return None
    try:
        then = datetime.fromisoformat(since)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0, int((now - then.timestamp()) // 60))


def _clean(text: str) -> str:
    """Text from outside (an issue title) for the terminal: control characters dropped, never cut."""
    return "".join(ch for ch in text if ch.isprintable())


def waiting_row(waiting, now: float) -> list[tuple[str, str]]:
    """The yards that need the operator as (separator, item) pairs; [] when none does."""
    if not isinstance(waiting, list):
        return []
    rows = [w for w in waiting if isinstance(w, dict) and w.get("workspace")]
    if not rows:
        return []
    if len(rows) == 1:
        w = rows[0]
        name = _clean(str(w["workspace"]))
        head = f"{C_YELLOW_B}⚑ {name}{C_RESET}"
        if w.get("issue"):
            title = _clean(str(w.get("title") or ""))
            head += f"  {_clean(str(w['issue']))}" + (f" {title}" if title else "")
        items = [("", head)]
        if w.get("reason") == "escalated":
            items.append((SEP, f"{C_YELLOW}escalated: your decision{C_RESET}"))
        else:
            items.append((SEP, f"{C_YELLOW}waits for your input{C_RESET}"))
        minutes = _since_minutes(w.get("since"), now)
        if minutes is not None:
            items.append((SEP, f"{C_DIM}{_age_text(minutes)} ago{C_RESET}"))
        items.append(("   ", f"{C_DIM}herdr workspace focus {name}{C_RESET}"))
        return items
    items = []
    for w in rows[:WAITING_SHOWN]:
        text = f"{C_YELLOW_B}⚑ {_clean(str(w['workspace']))}{C_RESET}"
        if w.get("issue"):
            text += f" {_clean(str(w['issue']))}"
        reason = "escalated" if w.get("reason") == "escalated" else "waits for input"
        text += f" {C_YELLOW}{reason}{C_RESET}"
        minutes = _since_minutes(w.get("since"), now)
        if minutes is not None:
            text += f" {C_DIM}{_age_text(minutes)}{C_RESET}"
        items.append(("   " if items else "", text))
    more = len(rows) - WAITING_SHOWN
    if more > 0:
        items.append(("   ", f"{C_DIM}+{more} more: lific-board.py{C_RESET}"))
    return items


# ============================================================
# Width: nothing is cut (operator, 2026-11-07)
# ============================================================

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Claude Code draws the status line two columns in from each edge (measured on
# the 2026-11-07 screenshot `current.png`: text from column 2, its "…" cut two
# columns short of the rule), so a row may use COLUMNS - 4.
FOOTER_INSET = 4
CONTINUATION = "  "


def _cell(ch: str) -> int:
    """Terminal cells of one character: 2 for East Asian Wide or Fullwidth, 0 for combining."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def visible_len(text: str) -> int:
    """The cells `text` takes on screen, escapes excluded."""
    return sum(_cell(ch) for ch in _ANSI.sub("", text))


def _take(plain: str, cells: int) -> tuple[str, str]:
    """The longest head of `plain` that fits in `cells` (at least one character), and the rest."""
    used = 0
    for i, ch in enumerate(plain):
        if i and used + _cell(ch) > cells:
            return plain[:i], plain[i:]
        used += _cell(ch)
    return plain, ""


def row_width() -> int | None:
    """The columns a row may use, from `COLUMNS`, which Claude Code sets on every
    status-line run from its own terminal width; None when it is absent."""
    try:
        columns = int(os.environ.get("COLUMNS", ""))
    except ValueError:
        return None
    return max(10, columns - FOOTER_INSET) if columns > 0 else None


def _words(text: str) -> list[tuple[str, str]]:
    """Split at visible spaces into (escape in force where the word starts, the word)."""
    words: list[tuple[str, str]] = []
    active = ""
    start, current = "", ""
    for part in re.split(f"({_ANSI.pattern})", text):
        if _ANSI.fullmatch(part):
            active = "" if part == C_RESET or part == "\x1b[0m" else part
            current += part
            continue
        for ch in part:
            if ch == " ":
                words.append((start, current))
                start, current = active, ""
            else:
                current += ch
    words.append((start, current))
    return words


def _break_item(text: str, width: int, later: int) -> list[str]:
    """An item wider than the whole row, broken at its spaces (a word wider than
    the row at the row's width): the one place a segment may break inside itself.
    The first piece may take `width` columns, every later one `later`."""
    reset = C_RESET
    lines: list[str] = []
    line, used = "", 0
    for active, word in _words(text):
        wlen = visible_len(word)
        if wlen == 0:
            # A run of spaces or a bare escape: keep the escape, add no column.
            line += word
            continue
        # Every placement is measured against the row it lands on: `width` for
        # the first, `later` for each one after it, re-read after every flush.
        if used and used + 1 + wlen > (width if not lines else later):
            lines.append(line + reset)
            line, used = "", 0
        if wlen > (width if not lines else later):
            # The colour in force inside the word: its own leading escapes win.
            lead = re.match(f"(?:{_ANSI.pattern})*", word).group(0)
            active = active + lead
            while wlen > (width if not lines else later):
                head, word = _take(_ANSI.sub("", word), width if not lines else later)
                lines.append(active + head + reset)
                wlen = visible_len(word)
        if used:
            line += " " + word
            used += 1 + wlen
        else:
            line, used = active + word, wlen
    if line:
        lines.append(line + reset)
    return lines


def wrap_items(items: list[tuple[str, str]], width: int | None) -> list[str]:
    """Pack (separator, item) pairs into rows no wider than `width`, wrapping only
    between items; a continuation row is indented by two spaces."""
    if width is None:
        return ["".join(sep + item for sep, item in items)] if items else []
    rows: list[str] = []
    line, used = "", 0
    for sep, item in items:
        size = visible_len(item)
        if line and used + visible_len(sep) + size <= width:
            line += sep + item
            used += visible_len(sep) + size
            continue
        if line:
            rows.append(line)
        indent = CONTINUATION if rows else ""
        room = width - visible_len(indent)
        if size <= room:
            line, used = indent + item, visible_len(indent) + size
        else:
            pieces = _break_item(item, room, width - visible_len(CONTINUATION))
            rows.extend((indent if i == 0 else CONTINUATION) + piece for i, piece in enumerate(pieces[:-1]))
            lead = indent if len(pieces) == 1 else CONTINUATION
            last = pieces[-1] if pieces else ""
            line, used = lead + last, visible_len(lead) + visible_len(last)
    if line:
        rows.append(line)
    return rows


def _push_dead_seen(state_path: Path | None, run_id: str, now: float) -> float | None:
    """When this session first saw run `run_id`'s writer dead; recorded on the first sight.

    Kept in this session's own checkpoint state, because the push record is
    read-only to readers. None when the state cannot be read or written: the
    segment then counts from the step start, as it did before.
    """
    if state_path is None:
        return None
    try:
        with CP.locked_state(state_path) as state:
            seen = state.get("push_dead_seen")
            if not isinstance(seen, dict) or seen.get("run_id") != run_id \
                    or not isinstance(seen.get("at"), (int, float)):
                seen = {"run_id": run_id, "at": now}
                state["push_dead_seen"] = seen
            return float(seen["at"])
    except Exception as exc:  # noqa: BLE001 - never break the status line
        print(f"checkpoint-statusline: push stop time not kept: {exc}", file=sys.stderr)
        return None


def push_segment(now: float, state_path: Path | None = None) -> str:
    """The live commit-and-push progress (HOS-346), or "" when there is none.

    Drawn by `scripts.utils.push_progress.render` from the record push-all
    writes. A writer that died shows `stopped` for ten minutes from the moment
    this session first saw it dead (kept in `state_path`). Any failure, an
    import that does not load under this interpreter included, drops the
    segment and leaves the rest of the line.
    """
    try:
        from scripts.utils import push_progress

        record = push_progress.read()
        dead_since = None
        if push_progress.writer_gone(record):
            dead_since = _push_dead_seen(state_path, str(record.get("run_id")), now)
        text = push_progress.render(record, now, dead_since=dead_since)
    except Exception as exc:  # noqa: BLE001 - never break the status line
        print(f"checkpoint-statusline: push segment dropped: {exc}", file=sys.stderr)
        return ""
    if not text:
        return ""
    color = C_GREEN if "✓" in text else C_RED if "✗" in text else C_CYAN_B
    return f"{color}{text}{C_RESET}"


def build_status_line(
    payload: dict,
    project: Path,
    level: str | None,
    render: dict,
    push: str = "",
    now: float | None = None,
    width: int | None = None,
) -> str:
    """Row 1, the session state; row 2 the waiting yards and row 3 the push, each only when present."""
    now = CP.utc_now().timestamp() if now is None else now
    cache = render.get("cache") if isinstance(render.get("cache"), dict) else {}
    workspace = payload.get("workspace") or {}
    cwd_str = workspace.get("current_dir") or payload.get("cwd") or str(project)
    dir_name = Path(cwd_str).name or str(project)

    branch = git_branch(Path(cwd_str))
    # Inside the data repository there is no branch to read (no git runs there),
    # so the badge comes from the hook's own checkout and row 1 shows the
    # directory alone.
    if inside_data_repo(Path(cwd_str)):
        yard = is_linked_worktree(Path(cwd_str))
    else:
        yard = bool(branch) and is_linked_worktree(Path(cwd_str))
    paused = render["unattended"] and render["paused"]
    if paused:
        head = f"{BADGE_PAUSED} {'YARD' if yard else 'HELM'} {C_RESET} {C_YELLOW_B}answer needed{C_RESET}{SEP}"
    else:
        head = f"{BADGE_YARD if yard else BADGE_HELM} {'YARD' if yard else 'HELM'} {C_RESET} "
    # HELM in its own clone on `main` is the default and the badge says it, so
    # the pair is left out (operator decision 2026-11-07, HOS-349 round 1); it
    # returns as soon as either the directory or the branch differs.
    bare = not yard and branch == "main" and is_main_clone(Path(cwd_str))
    if yard and dir_name in (branch, branch.lower().replace("/", "-")):
        # herdr names a yard's directory after its branch (lowercased, `/` as
        # `-`): say it once, as the branch.
        head += f"{C_YELLOW_B}{branch}{C_RESET}"
    elif not bare:
        head += f"{C_CYAN_B}{dir_name}{C_RESET}"
        if branch:
            head += f" {C_DIM}on{C_RESET} {C_YELLOW_B}{branch}{C_RESET}"

    items = [head]
    if level == "hard":
        items.append(f"{C_RED_B}✖ context past /second-wind: hand over now{C_RESET}")
    items.append(autonomy_segment(render["unattended"], render["paused"],
                                  render["rounds"], render["winds"]))
    items.append(issue_segment(cache.get("issue")))
    if not yard:
        items.append(yards_segment(cache.get("yards_total"), cache.get("yards_waiting")))
    items.append(render.get("handoff") or "")
    repos = cache.get("repos")
    if isinstance(repos, dict):
        items.append(repo_segment("engine", repos.get("engine")))
        if not yard:
            # A YARD never runs git in the data repository (engine CLAUDE.md,
            # § HELM and YARD; HOS-349 round 2), so it has no state to show.
            items.append(repo_segment("data", repos.get("data")))
    model = (payload.get("model") or {}).get("display_name") or "Claude"
    items.append(f"{C_DIM}{model}{C_RESET}")

    width = row_width() if width is None else width
    first = [item for item in items if item]
    if bare:
        # The head ends in its own separator here; the next item continues it.
        first[0:2] = [first[0] + first[1]]
    rows = wrap_items([("" if i == 0 else SEP, item) for i, item in enumerate(first)], width)
    if not yard:
        rows += wrap_items(waiting_row(cache.get("yards_waiting"), now), width)
    if push:
        rows += wrap_items([("", push)], width)
    return "\n".join(rows)


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

    # The slow facts come from the cache; a stale one is refreshed by a detached
    # child that `main` starts after this lock is released (HOS-349).
    cache = state.get(SC.CACHE_KEY)
    cache = dict(cache) if isinstance(cache, dict) else {}
    refresh = SC.refresh_due(cache, datetime.fromisoformat(now_iso))
    if refresh:
        cache["refreshing_since"] = now_iso
        state[SC.CACHE_KEY] = cache

    return {
        "level": level,
        "unattended": unattended,
        "paused": stretch_ended,
        "rounds": _count(state, "unattended_total"),
        "winds": _count(state, "second_wind_total"),
        "handoff": handoff_segment(state.get("handoff_freshness"), CP.utc_now().timestamp()),
        "cache": cache,
        "refresh": refresh,
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
        # Nothing recorded; decide for the screen from a throwaway read, and
        # start no refresh, since its start could not be recorded.
        render = apply_render(CP.read_json(state_path), payload, cw, used, now_iso)
    elif render["refresh"]:
        workspace = payload.get("workspace") or {}
        cwd = Path(workspace.get("current_dir") or payload.get("cwd") or str(project))
        SC.spawn_refresh(project, state_path, cwd)

    print(
        build_status_line(
            payload,
            project,
            render["level"],
            render,
            push_segment(CP.utc_now().timestamp(), state_path),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
