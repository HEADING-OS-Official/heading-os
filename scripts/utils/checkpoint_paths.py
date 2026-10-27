#!/usr/bin/env python3
"""Path, state and payload seam shared by the checkpoint hooks, /handover and /clear-handover.

Every path the checkpoint system writes is keyed by SESSION, because several
sessions routinely share one tree and anything shared is last-writer-wins
(MEASURED 2026-08-16: session A at 46% wrote the state file session B read).

Two roots, deliberately separate:

  - the PROJECT root comes from the hook payload and decides where state lives;
  - the ENGINE root is the tree owning this `scripts/utils/` (engine monorepo or
    plugin bundle) and decides which layout applies.

The archive follows the data overlay when this is a HEADING OS tree with a real
overlay; otherwise `state_root()/handoff`, or `.claude/handoff/` in a repository
that installs the plugin bundle.

Stdlib only at module level: `checkpoint-save.py` runs after the session context
is gone, so an import this module cannot satisfy costs a handoff nobody can
regenerate. The in-tree names it uses (`data_overlay_present`, `get_data_root`,
`data_path`, `data_write_dir`, `get_default_tz`) are imported inside function
bodies and wrapped, so a failing sibling degrades to the fallback. Add another
the same way or not at all.

The autonomy half (unattended, the wait, the stop conditions) is
`scripts/utils/checkpoint_autonomy.py`. Incident history behind
this code: `code-history.md` under the registry name `skill-design-references`
(skill `checkpoint`).
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import subprocess  # nosec B404 - fixed argv, never shell=True
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# Pointer dirs and state files are per session and never revisited once it ends.
KEEP_DAYS = 14
KEEP_MAX = 25

# A pointer carries at most this much summary, bounded at the WRITE so the file
# equals what gets injected (MEASURED: a live pointer reached 32261 bytes against
# an 8000-character inject cap).
MAX_POINTER_SUMMARY = 6000


def force_utf8() -> None:
    """Windows defaults to CP1252, which breaks the status bar's box characters."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001 - never break a hook over stream setup
                print(f"checkpoint: stream reconfigure failed: {exc}", file=sys.stderr)


# ============================================================
# Payload validation: the one check at every hook's entry
# ============================================================
#
# Each hook reads its payload through `read_payload`, so no code below it guards
# a field's type. A field of the wrong type is treated as ABSENT and named on
# stderr; a payload that is not a JSON object is the empty payload. Claude Code
# sends the documented shapes (https://code.claude.com/docs/en/hooks), so this
# is the single place that answers for anything else.

_STR, _BOOL, _LIST, _NUM, _INT = "str", "bool", "list", "num", "int"

PAYLOAD_SCHEMA: dict = {
    "session_id": _STR, "cwd": _STR, "transcript_path": _STR, "source": _STR,
    "trigger": _STR, "compact_summary": _STR, "prompt_id": _STR, "prompt": _STR,
    "stop_hook_active": _BOOL, "session_crons": _LIST, "background_tasks": _LIST,
    "workspace": {"current_dir": _STR, "project_dir": _STR},
    "model": {"display_name": _STR},
    "context_window": {
        "used_percentage": _NUM, "remaining_percentage": _NUM,
        "context_window_size": _INT,
        "current_usage": {"input_tokens": _INT, "cache_read_input_tokens": _INT,
                          "cache_creation_input_tokens": _INT},
    },
}


def _valid(value, kind) -> bool:
    if kind == _STR:
        return isinstance(value, str)
    if kind == _BOOL:
        return isinstance(value, bool)
    if kind == _LIST:
        return isinstance(value, list)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    # NaN and Infinity: `json.loads` accepts both by default.
    return math.isfinite(value) and (kind == _NUM or isinstance(value, int))


def validate_payload(payload, hook: str, schema: dict = PAYLOAD_SCHEMA, where: str = "") -> dict:
    """`payload` with every field of the wrong type removed. Always a dict."""
    if not isinstance(payload, dict):
        if payload is not None:
            print(f"{hook}: payload was {type(payload).__name__}, not an object; "
                  "continuing with defaults", file=sys.stderr)
        return {}
    clean = {}
    for key, value in payload.items():
        kind = schema.get(key)
        if kind is None:
            clean[key] = value
        elif value is None:
            continue  # null is absent
        elif isinstance(kind, dict):
            if isinstance(value, dict):
                clean[key] = validate_payload(value, hook, kind, f"{where}{key}.")
            else:
                print(f"{hook}: {where}{key} is {type(value).__name__}, not an object; "
                      "ignoring it", file=sys.stderr)
        elif not _valid(value, kind):
            print(f"{hook}: {where}{key} is {type(value).__name__} {value!r:.40}; "
                  "ignoring it", file=sys.stderr)
        else:
            clean[key] = value
    return clean


def read_payload(hook: str, *, empty_ok: bool = False) -> dict | None:
    """The hook's stdin payload, validated. None when stdin is not JSON at all.

    Empty stdin is unparseable unless `empty_ok`. Each hook decides what an
    unparseable payload means for it, which is why that case is returned.
    """
    try:
        raw = sys.stdin.read()
    except Exception as exc:  # noqa: BLE001 - an unreadable stdin is an unparseable payload
        print(f"{hook}: stdin read failed: {exc}", file=sys.stderr)
        return None
    if not raw.strip() and empty_ok:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        print(f"{hook}: payload parse error: {exc}", file=sys.stderr)
        return None
    return validate_payload(payload, hook)


# ============================================================
# Roots and addresses
# ============================================================

def engine_root() -> Path:
    """The tree that owns `scripts/utils/`: three fixed parents of this file.

    Correct exactly while this file sits at `<root>/scripts/utils/`; a caller
    that cannot vouch for that passes its own root (see `handoff_dir`).
    """
    return Path(__file__).resolve().parent.parent.parent


def is_engine_tree(root: Path) -> bool:
    """True for the HEADING OS monorepo, false for a built plugin bundle.

    Says nothing about whether a data overlay is mounted: a public clone answers
    True with no overlay. Ask `data_overlay_present()` for that.
    """
    return (root / "config" / "routing-map.yaml").is_file()


def project_root(payload: dict | None = None) -> Path:
    """Where state belongs: the tree this session is working in."""
    payload = payload or {}
    workspace = payload.get("workspace") or {}
    candidates = [workspace.get(k) for k in ("project_dir", "current_dir")]
    candidates += [payload.get("cwd"), os.environ.get("CLAUDE_PROJECT_DIR"),
                   os.environ.get("WORKSPACE_ROOT"), os.getcwd()]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            path = Path(candidate).expanduser()
            if path.is_dir():
                return path.resolve()
        except Exception as exc:  # noqa: BLE001 - a bad candidate is skipped, not fatal
            print(f"checkpoint: unusable project candidate {candidate!r}: {exc}",
                  file=sys.stderr)
    return Path(os.getcwd()).resolve()


def handoff_dir(project: Path, root: Path | None = None) -> Path:
    """The archive root: the data overlay's `handoff-archive`, or project-local.

    `root` is the tree the CALLER booted from: in a venv with the engine
    installed as a package a bundled hook can import the ENGINE's copy of this
    module, so `engine_root()` would name the wrong tree (MEASURED 2026-08-16).

    Two questions in order: is this a HEADING OS tree, then is a real overlay
    mounted. A public clone is an engine tree with no overlay, and its handoffs
    must go to the gitignored state directory, never into the pushed repository.

    Never raises: this runs after the session context is gone, so a resolver
    failure (a moved `HEADING_OS_DATA` raises `DataRootError` by design)
    redirects to `state_root()/handoff` instead of losing the handoff.
    """
    root = root or engine_root()
    if is_engine_tree(root):
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        try:
            from scripts.utils.paths import data_overlay_present

            if data_overlay_present():
                from scripts.utils.data_path import data_path
                return data_path('handoff-archive')
        except Exception as exc:  # noqa: BLE001 - losing the handoff is the worse outcome
            print(f"checkpoint: cannot resolve the data overlay ({exc}); "
                  "filing the handoff under this checkout's state directory "
                  "(`state_root`)",
                  file=sys.stderr)
        return state_root(project) / "handoff"
    return project / ".claude" / "handoff"


#: The runtime store row and kind the pointers, their lock and the quarantine
#: live under: `<data root>/runtime-junk/handoff/state/`. Read by
#: scripts/next-signal.py as well.
RUNTIME_SERVICE = "handoff"
RUNTIME_KIND = "state"
LATEST_DIRNAME = ".latest"
QUARANTINE_DIRNAME = ".quarantine"


def runtime_state_root(root: Path | None = None, *, create: bool = False) -> Path | None:
    """`<data root>/runtime-junk/handoff/state`, or None when the archive is project-local.

    Pointers, their lock and the redaction quarantine are runtime state; the
    dated archives stay where `handoff_dir` puts them. Follows `handoff_dir`
    branch for branch, and on None the caller keeps the pointers beside the
    archive. `create=True` is for the writer only (store init: 0700 root,
    README); readers resolve without touching the disk. Never raises, for the
    reason `handoff_dir` gives.
    """
    root = root or engine_root()
    if not is_engine_tree(root):
        return None
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from scripts.utils.paths import data_overlay_present, get_data_root
        from scripts.utils.data_path import data_write_dir
        from scripts.utils.runtime_dir import runtime_root

        if not data_overlay_present():
            return None
        data_root = get_data_root()
        if create:
            return data_write_dir("runtime:handoff/state", data_root=data_root)
        return runtime_root(data_root) / RUNTIME_SERVICE / RUNTIME_KIND
    except Exception as exc:  # noqa: BLE001 - losing the handoff is the worse outcome
        print(f"checkpoint: cannot resolve the runtime store ({exc}); "
              "keeping the pointers beside the handoff archive",
              file=sys.stderr)
        return None


def latest_root(handoff: Path, root: Path | None = None, *, create: bool = False) -> Path:
    """The pointer tree: `<runtime store>/.latest`, or `<handoff>/.latest`."""
    state = runtime_state_root(root, create=create)
    return (state if state is not None else handoff) / LATEST_DIRNAME


def quarantine_root(handoff: Path, root: Path | None = None, *, create: bool = False) -> Path:
    """Where an UNREDACTED handoff goes when redaction fails: never the archive."""
    state = runtime_state_root(root, create=create)
    return (state if state is not None else handoff) / QUARANTINE_DIRNAME


def latest_dir(handoff: Path, slug: str, root: Path | None = None) -> Path:
    """This session's pointer dir, the only one safe to inject."""
    return latest_root(handoff, root) / slug


#: The per-checkout pointer pairs sit in `.latest/.checkout/<checkout name>/`.
#: The leading dot keeps the name out of the session-slug namespace:
#: `safe_slug` never returns one, so no session's pointer dir can collide.
#:
#: The machine-wide pair `.latest/{summary,prompt}.md` that stood here until
#: HOS-158 is RETIRED: nothing writes it and nothing may read it. It was one
#: pair for every checkout on the machine, so /prime in a fresh YARD presented
#: HELM's last handoff as that yard's own (MEASURED 2026-09-28). The files left
#: on disk are not deleted; they are frozen, and a reader that fell back to
#: them would bring the defect back, so no reader does.
CHECKOUT_DIRNAME = ".checkout"


def checkout_key(root: Path | None = None) -> str:
    """The name of the checkout `root` is: its directory name, as the data repository spells it.

    `root` is the tree the CALLER runs from (its own `__file__`), never a
    constant, the common git directory or `CLAUDE_PROJECT_DIR`, so HELM and each
    YARD get their own answer. Asked of `data_path.checkout_name`, the one owner
    of that rule; a tree that cannot answer (a plugin bundle, a name the data
    repository refuses) falls back to a safe slug of the same directory name and
    says so, because a hook that raises costs the handoff.
    """
    root = Path(root or engine_root()).resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from scripts.utils.data_path import checkout_name

        return checkout_name(root)
    except Exception as exc:  # noqa: BLE001 - a hook must not raise; see above
        key = safe_slug(root.name.lstrip("."), 64)
        print(f"checkpoint: cannot name this checkout through the data "
              f"repository ({type(exc).__name__}: {exc}); using {key!r}",
              file=sys.stderr)
        return key


def checkout_latest_dir(handoff: Path, root: Path | None = None) -> Path:
    """THIS checkout's newest-handoff pair, the one /prime and /next read.

    Written beside every session pointer by `checkpoint-save.py` and
    `checkpoint-paths.py --write`, under a lock in this directory.
    """
    return latest_root(handoff, root) / CHECKOUT_DIRNAME / checkout_key(root)


STATE_DIR_ENV = "HEADING_OS_STATE_DIR"


def state_root(project: Path) -> Path:
    """Where the checkpoint hooks' state lands. The test-isolation seam.

    `HEADING_OS_STATE_DIR` redirects it, and only when the default would land in
    THIS clone: a project root that already points elsewhere was chosen
    deliberately (the plugin-bundle contract), and overriding it is a behaviour
    change, not isolation. It is asked ahead of the payload because the payload
    `cwd` beats `CLAUDE_PROJECT_DIR` in `project_root()` (MEASURED 2026-09-01:
    a test sweep wrote `checkpoint-True.json` into the live tree that way).

    A relative value is refused with a line on stderr and the default used: a
    relative path resolves against wherever the harness launched the hook, and a
    hook that raises costs the handoff.
    """
    default = project / ".claude" / "state"
    try:
        if project.resolve() != engine_root():
            if _inside_data_repository(project):
                # The data repository is never a plugin bundle's project. A
                # `cd` into it before a CLI call made it the "project" and put
                # a done marker in `<data>/.claude/state` (MEASURED 2026-10-24,
                # a session that ran from the data repository): the marker was
                # lost and the data-path push ratchet refused. The state then
                # lands in THIS clone, so the test seam applies first.
                print(f"checkpoint: {project} is inside the data repository; "
                      f"keeping state with this checkout", file=sys.stderr)
                return _state_dir_override() or checkout_state_root(engine_root())
            return default
    except OSError as exc:  # pragma: no cover - an unresolvable root is not fatal
        print(f"checkpoint: cannot resolve {project} ({exc}); "
              f"leaving the state directory at its default", file=sys.stderr)
        return default
    return _state_dir_override() or checkout_state_root(project)


def _state_dir_override() -> Path | None:
    """`HEADING_OS_STATE_DIR` when it is absolute; None otherwise, with a line for a relative one."""
    env = os.environ.get(STATE_DIR_ENV)
    if env:
        candidate = Path(env).expanduser()
        if candidate.is_absolute():
            return candidate
        print(
            f"checkpoint: {STATE_DIR_ENV}={env!r} is relative and is being "
            f"ignored. A relative value resolves against whatever directory the "
            f"harness launched the hook in.",
            file=sys.stderr)
    return None


def _inside_data_repository(project: Path) -> bool:
    """True when `project` is the data repository or a directory inside it. Never raises."""
    try:
        root = engine_root()
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from scripts.utils.paths import data_overlay_present, get_data_root

        if not data_overlay_present():
            return False
        data = get_data_root().resolve()
        here = project.resolve()
        return here == data or data in here.parents
    except Exception as exc:  # noqa: BLE001 - a hook must not raise; see state_root
        print(f"checkpoint: cannot tell whether {project} is in the data "
              f"repository ({type(exc).__name__}: {exc})", file=sys.stderr)
        return False


def checkout_state_root(project: Path) -> Path:
    """THIS engine checkout's hook state, in the data repository. Not created.

    The registry name `checkout-state`, one directory per checkout, so a YARD
    and HELM keep their state apart. A tree with NO data repository (a public
    clone) keeps it in `.claude/state/`; a data repository that cannot answer
    falls back the same way and says so, because a hook that raises costs the
    handoff. `.claude/hooks/_dispatch.py::_state_dir` answers the same question
    for the PreToolUse walls and must agree with this function.
    """
    fallback = project / ".claude" / "state"
    if str(project) not in sys.path:
        sys.path.insert(0, str(project))
    try:
        from scripts.utils.data_path import checkout_name, data_path
        from scripts.utils.paths import data_overlay_present

        if not data_overlay_present():
            return fallback
        return data_path("checkout-state", checkout=checkout_name(project))
    except Exception as exc:  # noqa: BLE001 - a hook must not raise; see above
        print(f"checkpoint: cannot resolve this checkout's state in the data "
              f"repository ({exc}); using {fallback}", file=sys.stderr)
        return fallback


def state_path(project: Path, slug: str) -> Path:
    return state_root(project) / f"checkpoint-{slug}.json"


# The value `session_id` returns when neither the payload nor the environment
# names one. Every id-less session shares it, so `.latest/session/` is a
# CROSS-SESSION bucket; anything that says whose handoff it found asks
# `session_id_is_known` first.
FALLBACK_SESSION_ID = "session"


def _session_candidates(payload: dict | None):
    return ((payload or {}).get("session_id"), os.environ.get("CLAUDE_CODE_SESSION_ID"))


def session_id(payload: dict | None = None) -> str:
    """The session's own id, from the payload or from `CLAUDE_CODE_SESSION_ID`.

    The skill gets no payload and reads the environment variable, which Claude
    Code exports to child processes (verified on 2.1.228).
    """
    for candidate in _session_candidates(payload):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return FALLBACK_SESSION_ID


def session_id_is_known(payload: dict | None = None) -> bool:
    """False when `session_id` fell back to the shared sentinel. Same test."""
    return any(isinstance(c, str) and c.strip() for c in _session_candidates(payload))


def safe_slug(value: str, max_len: int = 32) -> str:
    cleaned = "".join(
        ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in (value or "")
    )
    return cleaned[:max_len].strip("-") or "session"


def session_slug(payload: dict | None = None) -> str:
    return safe_slug(session_id(payload))


def transcript_dir(project: Path | str) -> Path | None:
    """Where Claude Code keeps this workspace's session transcripts.

    The harness replaces every `/` and `.` in the project path with `-`. The one
    copy of that rule: callers are `compaction-probe.py`,
    `archive-transcripts.py` and others. None off POSIX, where the rule does not
    cover a backslash or a drive colon and a guess would read as an empty
    directory.
    """
    if os.name != "posix":
        return None
    mangled = str(Path(project).resolve()).replace("/", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / mangled


def transcript_dirs_below(parent: Path | str) -> list[Path] | None:
    """Every transcript directory whose project sat BELOW `parent`, sorted.

    Read from the store, never from the filesystem under `parent`, because the
    point is to find directories whose checkout no longer exists: a YARD's
    transcripts outlive its deleted worktree. None off POSIX, as above.

    The match is a prefix on the mangled name, and the slug rule is lossy, so
    it over-matches in one known way: a SIBLING of `parent` whose name extends
    it (`/a/b-c` for `/a/b`, or `/a/b.c`, both `-a-b-c`) is reported too.
    Nothing here can tell the two apart; a caller that cares filters by name.
    """
    own = transcript_dir(parent)
    if own is None:
        return None
    prefix = own.name + "-"
    try:
        entries = sorted(own.parent.iterdir())
    except OSError:
        return []
    return [p for p in entries
            if p.name.startswith(prefix) and len(p.name) > len(prefix) and p.is_dir()]


def utc_now() -> datetime:
    """A SERIALIZED timestamp: JSON state, audit lines. Not for a filename."""
    return datetime.now(timezone.utc)


def local_now() -> datetime:
    """A DISPLAY / calendar-day timestamp in the operator's zone: archive filenames.

    UTC filenames filed work done just after local midnight, east of UTC, under the
    previous day (MEASURED 2026-08-20). Nothing reads the stamp back; every
    consumer orders by mtime. The import is deferred because `get_default_tz`
    reads `.env` and only the filename builders should pay for it.
    """
    try:
        from scripts.utils.workspace import get_default_tz
    except Exception:  # noqa: BLE001 - UTC is the wrong day, not a lost handoff
        return datetime.now(timezone.utc)
    return datetime.now(get_default_tz())


def env_int(name: str, default: int, *, minimum: int = 0, maximum: int = 100) -> int:
    """An integer from the environment; the DEFAULT on anything invalid or out of range."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < minimum or value > maximum:
        return default
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "auto")


def offer_bucket(state: dict) -> int:
    """The hysteresis bucket of the pending offer; 0 for a missing or unreadable one."""
    raw = state.get("offer_bucket") or state.get("current_bucket") or 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


# ============================================================
# Native compaction and its record
# ============================================================

def compact_point() -> tuple[str, str] | None:
    """Where native auto-compact is configured to fire, or None if nothing says.

    The percentage form wins: a token count is measured against the smaller of
    the setting and the model's window, so it changes meaning with the model.
    """
    percent = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "").strip()
    if percent.isdigit():
        return ("percent", percent)
    window = os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "").strip()
    if window.isdigit():
        return ("tokens", window)
    return None


COMPACT_HISTORY_MAX = 20


def record_peak(state: dict, used: float | None, when_iso: str) -> None:
    """Carry the HIGHEST context reading seen since the last compaction.

    The statusline rewrites `used_percentage` every turn, so without this the
    level compaction fired at is gone by the next render (MEASURED 2026-08-19).
    Only `record_compaction` resets it.
    """
    if used is None:
        return
    previous = state.get("peak_used_percentage")
    if (
        isinstance(previous, (int, float))
        and not isinstance(previous, bool)
        and float(previous) >= float(used)
    ):
        return
    state["peak_used_percentage"] = float(used)
    state["peak_used_at"] = when_iso


def record_compaction(state: dict, when_iso: str, trigger: str) -> dict | None:
    """Close the current peak into a compaction record. None when no reading exists.

    `used_pct_at_or_above` is a LOWER BOUND: the harness can compact between two
    renders. `configured` is what the environment asks for, so one line answers
    configured against observed.
    """
    peak = state.pop("peak_used_percentage", None)
    peak_at = state.pop("peak_used_at", None)
    if not isinstance(peak, (int, float)) or isinstance(peak, bool):
        return None

    point = compact_point()
    window = state.get("context_window_size")
    entry = {
        "at": when_iso,
        "trigger": trigger,
        "used_pct_at_or_above": float(peak),
        "last_seen_at": peak_at,
        "configured": f"{point[0]}:{point[1]}" if point else None,
        "context_window_size": window if isinstance(window, int) else None,
    }
    history = state.get("compact_history")
    if not isinstance(history, list):
        history = []
    history.append(entry)
    state["compact_history"] = history[-COMPACT_HISTORY_MAX:]
    return entry


# ============================================================
# State files: read, write, lock
# ============================================================

def read_json(path: Path) -> dict:
    """This session's state, or `{}`. Always a dict, whatever the file holds.

    State files get hand-edited, and a non-object here would crash every hook
    on every turn.
    """
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a corrupt state file must not stop a turn
        print(f"checkpoint: unreadable state at {path.name}: {exc}", file=sys.stderr)
        return {}
    if not isinstance(parsed, dict):
        print(f"checkpoint: state at {path.name} is a "
              f"{type(parsed).__name__}, not an object; ignoring it",
              file=sys.stderr)
        return {}
    return parsed


def _write_atomic(path: Path, dump) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            dump(f)
        os.replace(tmp_name, path)
    except BaseException:
        # BaseException: a Ctrl-C during a Stop must not leave the temp file.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def write_json_atomic(path: Path, data: dict) -> None:
    _write_atomic(path, lambda f: json.dump(data, f, ensure_ascii=False, indent=2))


def write_text_atomic(path: Path, content: str) -> None:
    _write_atomic(path, lambda f: f.write(content))


LOCK_WAIT_SECONDS = 2.0
LOCK_POLL_SECONDS = 0.01


@contextlib.contextmanager
def file_lock(lock_path: Path, *, wait: float = LOCK_WAIT_SECONDS, label: str = "checkpoint"):
    """Hold an exclusive lock on `lock_path` for the block; yields whether it is held.

    Bounded, never blocking: a Stop hook has a 90-second budget and the
    statusline runs every turn, so on expiry the block proceeds UNLOCKED with a
    line on stderr. Without `fcntl` (Windows) the lock is skipped. Used beyond
    this module (email-intelligence, ops-radar, ...), hence `label`.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        fcntl = None

    if fcntl is None:
        yield False
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    held = False
    deadline = time.monotonic() + wait
    try:
        handle = open(lock_path, "a+")  # noqa: SIM115 - released in the finally
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    print(
                        f"{label}: {lock_path.name} busy for {wait:.0f}s; "
                        "writing unlocked",
                        file=sys.stderr,
                    )
                    break
                time.sleep(LOCK_POLL_SECONDS)
    except OSError as exc:
        print(f"{label}: could not open {lock_path.name}: {exc}", file=sys.stderr)

    try:
        yield held
    finally:
        if handle is not None:
            if held:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                handle.close()


@contextlib.contextmanager
def locked_state(path: Path, *, wait: float = LOCK_WAIT_SECONDS):
    """Read-modify-write one state file under an exclusive lock.

    Yields the dict; what the block leaves in it is written atomically on a
    clean exit, and nothing on an exception. Four processes write this file and
    the statusline writes back the whole dict it read, so an atomic write alone
    lost a concurrent `--unattended on` in 1 of 60 forced-overlap trials
    (MEASURED 2026-08-20). The lock is a sidecar `<name>.lock`, because the
    state file itself is replaced by `os.replace`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path.with_name(path.name + ".lock"), wait=wait):
        state = read_json(path)
        yield state
        write_json_atomic(path, state)


def bound_summary(text: str, archive_ref: str, limit: int = MAX_POINTER_SUMMARY) -> str:
    """Cut the pointer's copy at a stated place, naming where the rest lives."""
    if len(text) <= limit:
        return text
    return (
        text[:limit].rstrip()
        + f"\n\n[Cut at {limit} characters. The full summary is in {archive_ref}.]\n"
    )


def _prune(entries: list[tuple[float, Path]], remove) -> None:
    entries.sort(key=lambda item: item[0], reverse=True)
    cutoff = utc_now().timestamp() - KEEP_DAYS * 86400
    doomed = [p for mtime, p in entries if mtime < cutoff]
    doomed.extend(p for _, p in entries[KEEP_MAX:] if p not in doomed)
    for path in doomed:
        try:
            remove(path)
        except OSError as exc:
            print(f"checkpoint: could not prune {path.name}: {exc}", file=sys.stderr)


def prune_pointer_dirs(handoff: Path, keep_slug: str, root: Path | None = None) -> None:
    """Drop pointer dirs of sessions long gone. Never a checkout's pair, never an archive."""
    base = latest_root(handoff, root)
    if not base.is_dir():
        return
    entries: list[tuple[float, Path]] = []
    for child in base.iterdir():
        if not child.is_dir() or child.name in (keep_slug, CHECKOUT_DIRNAME):
            continue
        try:
            mtime = max(
                (f.stat().st_mtime for f in child.iterdir()),
                default=child.stat().st_mtime,
            )
        except OSError:
            continue
        entries.append((mtime, child))

    def remove(path: Path) -> None:
        for f in path.iterdir():
            if f.is_file():
                f.unlink()
        path.rmdir()

    _prune(entries, remove)


def prune_state_dir(state_dir: Path, keep_name: str) -> None:
    """Bound the per-session state files, then drop every lock whose state file is gone."""
    base = state_dir
    if not base.is_dir():
        return
    entries: list[tuple[float, Path]] = []
    for child in base.glob("checkpoint-*.json"):
        if child.name == keep_name or not child.is_file():
            continue
        try:
            entries.append((child.stat().st_mtime, child))
        except OSError:
            continue

    _prune(entries, lambda path: path.unlink())

    # The glob above cannot see `<name>.json.lock`, which grew forever beside the
    # bounded state files (MEASURED: 25 state files, 22 orphan locks).
    for lock in base.glob("checkpoint-*.json.lock"):
        if not lock.is_file():
            continue
        if not lock.with_name(lock.name[:-len(".lock")]).exists():
            try:
                lock.unlink()
            except OSError:
                continue


# ============================================================
# Carrying work across /clear, and the master instruction
# ============================================================
#
# `/clear` gives the session a NEW id (`CLAUDE_CODE_SESSION_ID` "is updated on
# /clear", https://code.claude.com/docs/en/env-vars), so a lookup keyed by
# session finds nothing on the other side. What survives is the Claude Code
# PROCESS: `CLAUDE_PID` plus its /proc start time, which a later process cannot
# reuse the way it can reuse a bare pid.

MASTER_PREFIX = "Master instruction:"
MASTER_NONE = "none archived for this session"
COMPACTED_PREFIX = "Compacted before this handoff:"

# The keys the process record uses in a session's state file.
PROCESS_KEYS = ("claude_pid", "claude_start")


def claude_process() -> dict | None:
    """`{"pid": int, "start": str}` for the Claude Code process, or None.

    `CLAUDE_PID` is documented for tool and hook commands, v2.1.214 or later,
    and not for the status line. The start time is field 22 of
    `/proc/<pid>/stat`, read after the LAST `)` because the command name may
    hold spaces or parentheses. Without a start time this answers None: a pid
    alone can name a later process.
    """
    raw = os.environ.get("CLAUDE_PID", "").strip()
    if not raw.isdigit():
        return None
    try:
        stat = Path(f"/proc/{raw}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    fields = stat.rsplit(")", 1)[-1].split()
    # fields[0] is field 3 (state), so field 22 is fields[19].
    if len(fields) < 20 or not fields[19].isdigit():
        return None
    return {"pid": int(raw), "start": fields[19]}


def record_process(state: dict, payload: dict | None = None) -> bool:
    """Stamp this session's process, id and transcript into `state`.

    Returns True when anything changed, so a caller can skip the write.
    """
    proc = claude_process()
    changed = False
    updates = {}
    if proc is not None:
        updates["claude_pid"] = proc["pid"]
        updates["claude_start"] = proc["start"]
    payload = payload or {}
    if session_id_is_known(payload):
        updates["session_id"] = session_id(payload)
    transcript = payload.get("transcript_path")
    if transcript:
        updates["transcript_path"] = transcript
    for key, value in updates.items():
        if state.get(key) != value:
            state[key] = value
            changed = True
    if changed:
        state["process_seen_at"] = utc_now().isoformat()
    return changed


def previous_session_state(project: Path, proc: dict, current_slug: str) -> tuple[Path, dict] | None:
    """The newest OTHER session's state that names this same process.

    This checkout's state directory only, so a yard and HELM never see each
    other's sessions. Newest by `process_seen_at`, else `updated_at`.
    """
    directory = state_root(project)
    if not directory.is_dir():
        return None
    best = None
    best_key = ""
    for path in directory.glob("checkpoint-*.json"):
        if path.stem == f"checkpoint-{current_slug}":
            continue
        state = read_json(path)
        if state.get("claude_pid") != proc["pid"] or state.get("claude_start") != proc["start"]:
            continue
        key = str(state.get("process_seen_at") or state.get("updated_at") or "")
        if best is None or key > best_key:
            best, best_key = (path, state), key
    return best


def archived_prompts(session: str, cwd: Path, tool: Path) -> tuple[list[str], str | None]:
    """THIS session's archived prompt paths, oldest first, or why there are none.

    Never raises. The contract of `scripts/prompt-archive.py`, read from its
    source 2026-09-22: `--json` goes before `list`, the answer is a JSON array of
    absolute paths oldest first, `--session` matches the first 8 characters, and
    an empty `--session` exits 1. An empty session is refused here too: the
    archive is one directory shared by every session on the machine.
    """
    if not session.strip():
        return [], "no session id reached this hook"
    try:
        if not tool.is_file():
            return [], f"{tool.name} is not in this checkout"
        proc = subprocess.run(  # nosec B603 - fixed argv, no shell
            [sys.executable, str(tool), "--json", "list", "--session", session],
            capture_output=True, text=True, cwd=str(cwd), timeout=15,
        )
    except Exception as exc:  # noqa: BLE001 - a hook must not raise over this
        print(f"checkpoint: {tool.name} could not be run "
              f"({type(exc).__name__}: {exc})", file=sys.stderr)
        return [], f"{type(exc).__name__} running {tool.name}"
    if proc.returncode != 0:
        print(f"checkpoint: {tool.name} exited {proc.returncode}: "
              f"{proc.stderr.strip()}", file=sys.stderr)
        return [], f"{tool.name} exited {proc.returncode}"
    try:
        answer = json.loads(proc.stdout)
    except ValueError as exc:
        print(f"checkpoint: {tool.name} answered something unreadable: {exc}",
              file=sys.stderr)
        return [], f"unreadable answer from {tool.name}"
    if not isinstance(answer, list) or not all(isinstance(p, str) for p in answer):
        return [], f"{tool.name} answered {type(answer).__name__}, not a list of paths"
    return answer, None


def prompt_archive_tool(root: Path | None = None) -> Path:
    """`scripts/prompt-archive.py` in the caller's checkout; `HEADING_OS_PROMPT_ARCHIVE` overrides it for a test."""
    override = os.environ.get("HEADING_OS_PROMPT_ARCHIVE")
    if override:
        return Path(override)
    return (root or engine_root()) / "scripts" / "prompt-archive.py"


def master_value(paths: list[str], refusal: str | None, carried: str | None = None) -> str:
    """What follows `Master instruction:`. Never empty.

    The newest archived prompt; else the one a `/clear` carried over, said so;
    else "none archived". A failed lookup says so, which is not "none".
    """
    if refusal is not None:
        return f"unknown, the prompt archive could not be asked ({refusal})"
    if paths:
        return paths[-1]
    if carried:
        return f"{carried} (carried over by /clear from the previous session)"
    return MASTER_NONE


def carried_master(state: dict | None) -> str | None:
    carried = (state or {}).get("carried_master_instruction")
    return carried if isinstance(carried, str) else None


def master_instruction(session: str, cwd: Path, root: Path | None = None,
                       state: dict | None = None) -> str:
    """The master instruction line's value for `session`, asked now. Total."""
    paths, refusal = archived_prompts(session, cwd, prompt_archive_tool(root))
    return master_value(paths, refusal, carried_master(state))


def master_line_in(text: str, limit: int = 20) -> str | None:
    """The value of the first `Master instruction:` line in the head of `text`."""
    for line in text.splitlines()[:limit]:
        if line.startswith(MASTER_PREFIX):
            return line[len(MASTER_PREFIX):].strip()
    return None


def master_block(current: str, handoff: str | None) -> str:
    """The master instruction as a session start shows it.

    When a newer prompt was archived after the handoff, both are shown, so a
    task given after the checkpoint is not hidden behind it.
    """
    if handoff and handoff != current and not current.startswith(MASTER_NONE):
        return (f"Master instruction (newest): {current}\n"
                f"Handoff written under: {handoff}")
    return f"{MASTER_PREFIX} {current}"


PROMPTS_HEADING = "## Prompts archived in this session"


def prompts_section(paths: list[str], refusal: str | None) -> str:
    """The handoff's prompt-archive section. Three outcomes, three different claims.

    "Nothing was archived" and "the archive could not be asked" are different
    statements, and a heading with nothing under it turns the second into the
    first. The post-compact hook and `checkpoint-paths.py --write` share it.
    """
    if refusal is not None:
        return (f"{PROMPTS_HEADING}\n\nThe prompt archive could not be asked "
                f"({refusal}), so nothing is listed here. That is not a "
                f"statement that no prompt was archived.\n")
    if not paths:
        return f"{PROMPTS_HEADING}\n\nNo prompt was archived for this session.\n"
    listed = "\n".join(paths)
    return (f"{PROMPTS_HEADING}\n\nOldest first, read from the archive at the moment "
            f"this handoff was written, so a prompt saved seconds ago is "
            f"here. Each file holds the operator's words verbatim beside the "
            f"expansion that carried them out, and the last one is the "
            f"instruction this session was executing.\n\n{listed}\n")


TASK_PROMPTS_HEADING = "## Prompts of this handoff's tasks in other sessions"


def task_prompts(tasks: list[str], cwd: Path, tool: Path) -> list[str]:
    """Every archived prompt whose `Task:` names one of `tasks`, any session, by name. Never raises.

    The same tool and contract as `archived_prompts`, with `list --task`
    (task dossier, HOS-256): a task outlives the session that started it.
    """
    found: set[str] = set()
    for task in tasks:
        try:
            proc = subprocess.run(  # nosec B603 - fixed argv, no shell
                [sys.executable, str(tool), "--json", "list", "--task", task],
                capture_output=True, text=True, cwd=str(cwd), timeout=15,
            )
            answer = json.loads(proc.stdout) if proc.returncode == 0 else []
        except Exception as exc:  # noqa: BLE001 - a hook must not raise over this
            print(f"checkpoint: prompts of {task} could not be listed "
                  f"({type(exc).__name__}: {exc})", file=sys.stderr)
            continue
        found.update(p for p in answer if isinstance(p, str))
    return sorted(found, key=lambda p: Path(p).name)


def task_prompts_section(tasks: list[str], session_paths: list[str], cwd: Path,
                         tool: Path) -> str:
    """The prompts of the handoff's tasks that THIS session did not save, or "".

    A separate section rather than more lines in `prompts_section`, whose last
    path is by definition the instruction this session was executing.
    """
    if not tasks:
        return ""
    mine = {Path(p).name for p in session_paths}
    other = [p for p in task_prompts(tasks, cwd, tool) if Path(p).name not in mine]
    if not other:
        return ""
    return (f"{TASK_PROMPTS_HEADING}\n\nOldest first: every archived prompt whose "
            f"`Task:` line names {', '.join(tasks)}, saved by another session.\n\n"
            + "\n".join(other) + "\n")


def safe_resume_lines(text: str, limit: int = 160) -> list[str]:
    """The first three content lines of `## Safe resume`, each cut to `limit`: a dossier summary."""
    body = sections(text, (SAFE_RESUME_HEADING[3:],)).splitlines()[1:]
    return [cut_line(ln, limit) for ln in _content_lines(body)[:3]]


# The handoff's own `## Safe resume` governs. This text said "continue the
# latest unfinished task" and "continue implementation" with no word about it,
# so a handoff whose Safe resume said "wait for the operator" handed the next
# session two opposite orders. Found 2026-10-27 by a goldfish cold read of a
# real second-wind handoff (HOS-263); the rule now defers instead of competing.
CONTINUATION_RULES = """\
Rules:
1. Treat repository state as authoritative.
2. Do not redo broad discovery unless the summary is insufficient.
3. Before making changes, briefly restate the current objective, constraints, files involved, and next concrete action.
4. When the handoff has a `## Safe resume` block, it governs: do its Next action, start nothing listed under Awaiting approval without the operator's word, and repeat nothing under Already done. Where this prompt and Safe resume disagree, Safe resume wins.
5. Without that block, continue implementation from the current repo state."""


def continuation_prompt(archive_ref: str) -> str:
    """The prompt pointer's text, and the archive's continuation section."""
    return ("Continue this Claude Code session from the saved handoff.\n\n"
            f"First read:\n\n@{archive_ref}\n\n"
            "Then resume as its `## Safe resume` block says, or, when it has "
            "none, continue the latest unfinished task.\n\n"
            f"{CONTINUATION_RULES}\n")


def _operator_text(entry: dict) -> str | None:
    """The text the operator typed in one transcript record, or None.

    A `user` record is usually a tool result, a meta line, a hook's output or a
    compaction summary. A typed slash command is rendered back as `/x args`.
    """
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return None
    if entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly"):
        return None
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        content = "\n".join(
            str(b.get("text") or "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    if not isinstance(content, str) or not content.strip():
        return None
    text = content.strip()
    if text.startswith(("<local-command-stdout>", "<local-command-caveat>",
                        "<local-command-stderr>")):
        return None
    if text.startswith("<command-name>"):
        name = text.split("<command-name>", 1)[1].split("</command-name>", 1)[0]
        args = ""
        if "<command-args>" in text:
            args = text.split("<command-args>", 1)[1].split("</command-args>", 1)[0]
        return f"{name} {args}".strip()
    return text


def transcript_digest(path: str | Path | None) -> dict | None:
    """Operator turns and compaction boundaries in a transcript. None if unreadable.

    `{"operator": [(timestamp, text), ...], "boundaries": [timestamp, ...]}`, in
    file order, main thread only. Streamed and split on newlines only, because
    U+2028 and its kin appear literally inside transcript records.
    """
    if not path:
        return None
    operator: list[tuple[str, str]] = []
    boundaries: list[str] = []
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"user"' not in line and '"compact_boundary"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
                    boundaries.append(str(entry.get("timestamp") or ""))
                    continue
                text = _operator_text(entry)
                if text is not None:
                    operator.append((str(entry.get("timestamp") or ""), text))
    except OSError as exc:
        print(f"checkpoint: transcript unreadable: {exc}", file=sys.stderr)
        return None
    return {"operator": operator, "boundaries": boundaries}


def iso_epoch(value: str | None) -> float | None:
    """An ISO timestamp as epoch seconds, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def session_handoffs(handoff: Path, slug: str, kinds: tuple[str, ...] | None = None) -> list[Path]:
    """This session's dated archive files, newest first by mtime; `kinds` filters the kind segment."""
    if not handoff.is_dir():
        return []
    found = []
    for path in handoff.glob(f"*_handoff_*_{slug}.md"):
        kind = path.name.split("_handoff_", 1)[1][: -len(f"_{slug}.md")]
        if kinds is not None and kind not in kinds:
            continue
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return [p for _, p in sorted(found, key=lambda item: item[0], reverse=True)]


UNATTENDED_PREFIX = "Unattended:"


def _local_minute(stamp: str) -> str:
    """An ISO stamp as `YYYY-MM-DD HH:MM` in the workspace timezone; the raw stamp if unparseable."""
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return str(stamp)
    try:
        from scripts.utils.workspace import get_default_tz

        return moment.astimezone(get_default_tz()).strftime("%Y-%m-%d %H:%M %Z")
    except Exception as exc:  # noqa: BLE001 - a line in UTC beats no line
        print(f"checkpoint: workspace timezone unavailable, UTC used: {exc}", file=sys.stderr)
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def unattended_line(state: dict) -> str:
    """One line on whether this session ever continued on its own, written by code.

    Operator decision 2026-10-14: a yard's summary says when it ran unattended,
    so the line goes into every handoff header, `--done` and the yard's report.
    """
    try:
        total = int(state.get("unattended_total") or 0)
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        return f"{UNATTENDED_PREFIX} never continued on its own."
    first = _local_minute(state["unattended_first_at"]) if state.get("unattended_first_at") else "unknown"
    last = _local_minute(state["unattended_last_at_total"]) if state.get("unattended_last_at_total") else "unknown"
    stop = state.get("unattended_last_stop") or "not stopped yet"
    line = (f"{UNATTENDED_PREFIX} continued on its own {total} time(s), "
            f"{first} to {last}; last stop: {stop}.")
    if state.get("reset_not_hosted_at"):
        # Spec 3.7, step 6: the self-run wrote a handoff but nothing could send it.
        line += (" /second-wind could not send /clear or /compact (no HERDR pane found) at "
                 f"{_local_minute(state['reset_not_hosted_at'])}.")
    return line


def safe_resume_missing(text: str) -> list[str]:
    """What the `## Safe resume` block lacks: the heading, or any of its three lines with a value."""
    body = sections(text, ("Safe resume",))
    if not body:
        return [SAFE_RESUME_HEADING]
    missing = []
    for key in SAFE_RESUME_KEYS:
        lines = (_LIST_MARKER.sub("", ln.strip()) for ln in body.splitlines())
        line = next((ln for ln in lines if ln.startswith(key)), None)
        if line is None or not line[len(key):].strip():
            missing.append(f"{SAFE_RESUME_HEADING}: {key}")
    return missing


def sections(text: str, names: tuple[str, ...]) -> str:
    """The named `## ` sections of a markdown handoff, in the order asked."""
    found: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            title = line[3:].strip()
            current = title if title.lower() in {n.lower() for n in names} else None
            if current is not None:
                found[current.lower()] = [line]
            continue
        if current is not None:
            found[current.lower()].append(line)
    return "\n\n".join(
        "\n".join(found[n.lower()]).strip() for n in names if n.lower() in found
    )


# The two blocks shown right after /clear, and no line is in both (HOS-213).
# ABOVE the `/clear` line, the synchronous SessionStart `systemMessage`: what was
# carried (`clear_passport`). BELOW it, the `--rewake` model turn: where the work
# resumes, in the handoff's own words (`resume_lines`). /clear-handover writes
# the `## Resume` block, in the operator's language, while the model still runs.
RESUME_HEADING = "## Resume"

# The block the Stop hook requires before /clear or /compact (2026-10-14): what
# the next session does first, what waits for the operator, what must not repeat.
SAFE_RESUME_HEADING = "## Safe resume"
SAFE_RESUME_KEYS = ("Next action:", "Awaiting approval:", "Already done (do not repeat):")
RESUME_LINES = 3
RESUME_LINE_CHARS = 240
_RESUME_FALLBACK = (("Objective", "Objective"), ("Current state", "Stopped at"),
                    ("Next steps", "Next"), ("Open issues", "Open"))
_LIST_MARKER = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
# /handover puts this caveat first under "## Current state" and "## Files touched";
# it is about the section, never the content of it.
_DISCLAIMER = re.compile(r"^may be outdated\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?…](?=\s|$)")


def cut_line(line: str, limit: int = RESUME_LINE_CHARS) -> str:
    """`line` whole when it fits; else up to its last sentence end inside `limit`;
    else its last whole word inside `limit` and "…". Never mid-word: a single
    word longer than `limit` is kept whole.

    A sentence end in the first third is not taken, because a line cut to a
    fragment like "Done." says less than the same line cut at a word.
    """
    line = line.strip()
    if len(line) <= limit:
        return line
    ends = [m.end() for m in _SENTENCE_END.finditer(line) if m.end() <= limit]
    if ends and ends[-1] >= limit // 3:
        return line[:ends[-1]]
    space = line.rfind(" ", 0, limit)
    if space <= 0:
        word_end = line.find(" ")
        return line if word_end < 0 else line[:word_end] + "…"
    return line[:space].rstrip() + "…"


def _content_lines(body: list[str]) -> list[str]:
    """Non-blank lines, list markers removed, the section disclaimer skipped."""
    out = []
    for ln in body:
        ln = _LIST_MARKER.sub("", ln.strip())
        if ln and not _DISCLAIMER.match(ln):
            out.append(ln)
    return out


def resume_block(text: str) -> str | None:
    """The `## Resume` section's first three content lines, or None when absent or empty."""
    body = sections(text, (RESUME_HEADING[3:],)).splitlines()[1:]
    lines = [cut_line(ln) for ln in _content_lines(body)]
    return "\n".join(lines[:RESUME_LINES]) or None


def resume_lines(text: str) -> str:
    """BELOW /clear: where the work resumes, verbatim from the handoff.

    The `## Resume` block when there is one; else the first content line of
    Objective, Current state, Next steps and Open issues, labelled, in the
    handoff's own words. No path and nothing `clear_passport` shows.
    """
    block = resume_block(text)
    if block:
        return block
    lines = []
    for section, label in _RESUME_FALLBACK:
        first = next(iter(_content_lines(sections(text, (section,)).splitlines()[1:])), "")
        if first:
            lines.append(f"{label}: {cut_line(first, RESUME_LINE_CHARS - len(label) - 2)}")
    return "\n".join(lines)


def _header(text: str, prefix: str) -> str | None:
    for ln in text.splitlines()[:20]:
        if ln.startswith(prefix):
            return ln[len(prefix):].strip() or None
    return None


def handoff_trigger(text: str) -> str:
    """manual, clear, second-wind or mechanical, from the `Trigger:` line, else the title."""
    raw = (_header(text, "Trigger:") or _header(text, "# Handoff -") or "").lower()
    for kind in ("mechanical", "second-wind", "clear", "manual"):
        if kind in raw:
            return kind
    return raw or "unknown"


def clear_passport(text: str, path: Path | None, *, session: str, master: str,
                   pins: int, written: str) -> str:
    """ABOVE /clear: what was carried. `written` is the fallback for a missing `Generated:`."""
    return "\n".join([
        f"Carried from session: {session or 'unknown'}",
        f"Handoff written: {_header(text, 'Generated:') or written} "
        f"({handoff_trigger(text)})",
        f"Master instruction: {master or 'unknown'}",
        f"Pins: {pins}",
        f"Handoff: {path if path is not None else 'not filed'}",
    ])


def set_header_lines(text: str, lines: list[str]) -> str:
    """Put script-owned header lines under the title, replacing older ones. Idempotent."""
    prefixes = tuple(line.split(":", 1)[0] + ":" for line in lines)
    body = [ln for ln in text.splitlines() if not ln.startswith(prefixes)]
    if body and body[0].startswith("# "):
        rest = body[1:]
        while rest and not rest[0].strip():
            rest = rest[1:]
        out = [body[0], ""] + lines + rest
    else:
        out = lines + [""] + body
    return "\n".join(out).rstrip("\n") + "\n"


def set_first_line(text: str, line: str) -> str:
    """Make `line` the first line of a pointer file, replacing an older master line."""
    prefix = line.split(":", 1)[0] + ":"
    body = text.splitlines()
    while body and (body[0].startswith(prefix) or not body[0].strip()):
        body = body[1:]
    return line + "\n\n" + "\n".join(body).rstrip("\n") + "\n"


# ============================================================
# Pins: items the operator fixed verbatim during the session
# ============================================================
#
# A pin is a sentence the operator wants kept word for word across compaction
# and /clear: "закрепи: ..." or `/pin ...` (operator decision 2026-09-27). It
# lives in a file, because a request to a summariser is not a guarantee.
# `scripts/pin.py` is the one writer; nothing here rewords or translates one.

PINS_FILENAME = "pins.md"
PINS_HEADING = "## Pinned items"
PINS_LEAD = ("Pinned by the operator with /pin or \"закрепи:\". Each item is the "
             "operator's own words: keep it verbatim, never reword or translate it.")
# A longer text is refused rather than cut, because a cut pin is not verbatim.
MAX_PIN_CHARS = 500


def pins_path(handoff: Path, slug: str, root: Path | None = None) -> Path:
    """This session's pins file, beside its pointer pair."""
    return latest_dir(handoff, slug, root) / PINS_FILENAME


def normalise_pin(text: str) -> str:
    """Whitespace collapsed to single spaces, and nothing else changed."""
    return " ".join(str(text).split())


def read_pins(path: Path) -> list[str]:
    """The pins in `path`, oldest first. Empty when there is no file. Total."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        print(f"checkpoint: pins unreadable at {path}: {exc}", file=sys.stderr)
        return []
    return [line for line in (ln.strip() for ln in text.splitlines()) if line]


def append_pins(path: Path, pins: list[str]) -> list[str]:
    """Add `pins` under the file's lock, skipping ones already there; returns those added."""
    added: list[str] = []
    with file_lock(path.with_name(path.name + ".lock"), label="pin"):
        current = read_pins(path)
        for pin in pins:
            if pin and pin not in current and pin not in added:
                added.append(pin)
        if added:
            write_text_atomic(path, "\n".join(current + added) + "\n")
    return added


def pins_block(pins: list[str], heading: str = PINS_HEADING) -> str:
    """The pins as a handoff section. Empty string when there are none."""
    if not pins:
        return ""
    return f"{heading}\n\n{PINS_LEAD}\n\n" + "\n".join(f"- {p}" for p in pins)


def drop_section(text: str, heading: str) -> str:
    """`text` without the `## ` section titled like `heading`."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        if line.startswith("## "):
            skipping = line.strip() == heading
        if not skipping:
            out.append(line)
    return "\n".join(out).rstrip("\n") + "\n"


def set_pins_section(text: str, pins: list[str]) -> str:
    """Put the pins section into a handoff archive, replacing an older one.

    Before `## Objective` when the file has one, else at the end.
    """
    text = drop_section(text, PINS_HEADING)
    block = pins_block(pins)
    if not block:
        return text
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## Objective":
            return "\n".join(lines[:i] + block.splitlines() + [""] + lines[i:]).rstrip("\n") + "\n"
    return text.rstrip("\n") + "\n\n" + block + "\n"
