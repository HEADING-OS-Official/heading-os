"""The status line's slow facts, read off the render path (HOS-349).

The status line renders often, so it never waits on herdr, lific or git. It
reads the `statusline_cache` dict from this session's checkpoint state, and
when that is older than REFRESH_AFTER seconds it starts ONE detached child,
this module run as `-m scripts.utils.statusline_cache`, which reads:

- the Lific issue in work and its status (HELM: the newest issue bound to this
  session; a yard: the issue in its branch name);
- the yards (HELM only): herdr workspaces that are linked worktrees of this
  repository and host an agent, and the ones that need the operator (a pane
  `blocked` on typed input, or an issue whose newest comment escalates);
- each repository's state: commits ahead, files not committed. The data
  repository is read in HELM only: a YARD never runs git there, read-only
  included (engine CLAUDE.md, § HELM and YARD).

The child writes the whole dict with a new `refreshed_at` and exits; it gives
up once CHILD_BUDGET seconds have passed, checked between calls on a
monotonic clock (each call has its own timeout). A part that fails keeps its
previous value and says one line on stderr. The checkpoint lock is never held across a subprocess.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.utils import checkpoint_paths as CP  # noqa: E402

CACHE_KEY = "statusline_cache"
# A cache older than this is refreshed.
REFRESH_AFTER = 30.0
# A refresh started longer ago than this is taken for dead and started again.
REFRESH_STALE = 60.0
# The whole child run, every call included.
CHILD_BUDGET = 30
GIT_TIMEOUT = 5
TITLE_LIMIT = 40


def _log(message: str) -> None:
    print(f"statusline-cache: {message}", file=sys.stderr)


def _age(stamp, now: datetime) -> float | None:
    """Seconds since an ISO stamp, or None when there is no readable stamp."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds()


def refresh_due(cache: dict | None, now: datetime) -> bool:
    """True when the cache is missing or old and no live refresh is under way."""
    cache = cache if isinstance(cache, dict) else {}
    refreshed = _age(cache.get("refreshed_at"), now)
    if refreshed is not None and refreshed < REFRESH_AFTER:
        return False
    running = _age(cache.get("refreshing_since"), now)
    return running is None or running >= REFRESH_STALE


def venv_python(project: Path) -> Path:
    """The checkout's own interpreter: `.venv/Scripts/python.exe` on Windows, `.venv/bin/python` elsewhere."""
    if os.name == "nt":
        return project / ".venv" / "Scripts" / "python.exe"
    return project / ".venv" / "bin" / "python"


def spawn_refresh(project: Path, state_path: Path, cwd: Path) -> None:
    """Start the refresher detached and return at once; never raises."""
    python = venv_python(project)
    if not python.is_file():
        _log(f"no {python}; cache not refreshed")
        return
    # Detached from this process's session (POSIX) or console (Windows), so the
    # render returns at once and the child outlives it.
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
              if os.name == "nt" else {"start_new_session": True})
    try:
        subprocess.Popen(
            [str(python), "-m", "scripts.utils.statusline_cache",
             "--state", str(state_path), "--cwd", str(cwd)],
            cwd=str(project),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **detach,
        )
    except OSError as exc:
        _log(f"refresher not started: {exc}")


# ============================================================
# Readers (child only)
# ============================================================

class OutOfTime(BaseException):
    """The child's budget is spent. A BaseException, so no `except Exception`
    in a reader can swallow it: only `run` handles it, and the child ends."""


def _check(deadline: float) -> None:
    """Between calls: end the run once the monotonic deadline has passed."""
    if time.monotonic() >= deadline:
        raise OutOfTime(f"refresh passed its {CHILD_BUDGET}s budget")

def _git(path: Path, *args: str) -> str | None:
    """One read-only git call; None when it fails. Never takes an optional lock."""
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(path), *args],
            capture_output=True, text=True, errors="replace", timeout=GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _log(f"git {args[0]} in {path.name}: {exc}")
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def repo_state(path: Path, base: str | None = None) -> dict:
    """`ahead`: commits not in `base` (default: the upstream); `dirty`: porcelain lines.

    `dirty` None means git could not be read. `ahead` None means there is no
    upstream (or no `base`), which shows no arrow.
    """
    status = _git(path, "status", "--porcelain")
    dirty = None if status is None else len([ln for ln in status.splitlines() if ln.strip()])
    ref = base or "@{upstream}"
    count = _git(path, "rev-list", "--count", f"{ref}..HEAD")
    try:
        ahead = int(count.strip()) if count is not None else None
    except ValueError:
        ahead = None
    return {"ahead": ahead, "dirty": dirty}


def _lific_board():
    """`scripts/lific-board.py`, loaded by path: its file name is hyphenated."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "lific_board_for_statusline", _ROOT / "scripts" / "lific-board.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _issue(board, ident: str) -> dict:
    record = board._run_lific(["issue", "get", ident])
    return record if isinstance(record, dict) else {}


def _first_task(text: str) -> str | None:
    from scripts.utils.dossier import TASK_RE

    match = TASK_RE.search(text or "")
    return match.group(0) if match else None


def _lific_time(stamp) -> str | None:
    """Lific's `YYYY-MM-DD HH:MM:SS` (UTC) as ISO, or None."""
    if not isinstance(stamp, str):
        return None
    try:
        then = datetime.fromisoformat(stamp.replace(" ", "T"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return then.isoformat()


def yards(board, repo_root: Path, deadline: float) -> tuple[int, list[dict]]:
    """The running yards of this repository and the ones that need the operator."""
    from scripts.utils import herdr_agent as HA

    payload = HA._run(["workspace", "list"], HA.LIST_TIMEOUT)
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    workspaces = result.get("workspaces") or []
    agents = HA.agents()
    status_by_ws: dict[str, list[str]] = {}
    for agent in agents:
        status_by_ws.setdefault(str(agent.get("workspace_id")), []).append(
            str(agent.get("agent_status") or ""))

    total = 0
    waiting: list[dict] = []
    for ws in workspaces:
        if not isinstance(ws, dict):
            continue
        tree = ws.get("worktree") if isinstance(ws.get("worktree"), dict) else {}
        if not tree.get("is_linked_worktree"):
            continue
        try:
            if Path(str(tree.get("repo_root"))).resolve() != repo_root:
                continue
        except OSError as exc:
            _log(f"workspace {ws.get('workspace_id')}: repo root unreadable: {exc}")
            continue
        name = str(ws.get("workspace_id") or "")
        if name not in status_by_ws:
            continue
        total += 1
        _check(deadline)
        branch = (_git(Path(str(tree.get("checkout_path"))), "branch", "--show-current") or "").strip()
        ident = _first_task(branch)
        title, reason, since = "", None, None
        if "blocked" in status_by_ws[name]:
            reason = "blocked"
        if ident:
            _check(deadline)
            try:
                title = str(_issue(board, ident).get("title") or "")
                newest = board.newest_comment(board._all_comments(ident))
                if reason is None and newest and board.waiting_on_helm(
                        str(newest.get("content") or "")) == "escalate":
                    reason = "escalated"
                    since = _lific_time(newest.get("created_at"))
            except Exception as exc:  # noqa: BLE001 - one yard's lific read never drops the rest
                _log(f"lific read for {ident}: {exc}")
        if reason:
            waiting.append({"workspace": name, "issue": ident, "title": title,
                            "reason": reason, "since": since})
    return total, waiting


def _in_yard(cwd: Path) -> bool:
    out = _git(cwd, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    lines = (out or "").splitlines()
    return len(lines) == 2 and lines[0] != lines[1]


def _helm_for_data() -> bool:
    """True only on a positive HELM answer, the gate before any git in the data
    repository: a YARD never runs git there, read-only included. Asked of the
    checkout this module runs from, so a session cwd elsewhere cannot answer for
    it; unsure counts as a yard (HOS-349 round 2). HEADING_OS_YARD present at
    all, whatever its value ("1", "true", empty), counts as a yard (round 3)."""
    if "HEADING_OS_YARD" in os.environ:
        return False
    try:
        from scripts.utils.clone_guard import is_main_clone
        return is_main_clone(_ROOT)
    except Exception as exc:  # noqa: BLE001 - unsure means the data repository is not read
        _log(f"cannot tell HELM from a yard ({type(exc).__name__}); data repository not read")
        return False


def _inside(path: Path, root: Path | None) -> bool:
    return root is not None and (path == root or root in path.parents)


def collect(state: dict, cwd: Path, previous: dict, deadline: float) -> dict:
    """Read every part; a part that fails keeps its value from `previous`.

    Every git call inspects the checkout this module runs from (`_ROOT`), never
    the session's `cwd`, which says only where the session stands; and none is
    made at all when that checkout lies inside the data repository: a YARD never
    runs git there (HOS-349 round 3)."""
    from scripts.utils import dossier
    from scripts.utils.paths import get_data_root

    cache = {key: previous.get(key) for key in ("issue", "yards_total", "yards_waiting", "repos")}
    try:
        data_root = get_data_root().resolve()
    except Exception as exc:  # noqa: BLE001 - no data repository is a state, not a crash
        _log(f"data repository: {exc}")
        data_root = None
    if _inside(_ROOT, data_root):
        _log("this checkout lies inside the data repository; no git call made")
        return cache
    top = (_git(_ROOT, "rev-parse", "--show-toplevel") or "").strip()
    checkout = Path(top).resolve() if top else _ROOT
    if _inside(checkout, data_root):
        _log("this checkout lies inside the data repository; no further git call made")
        return cache
    yard = _in_yard(checkout)

    repos = dict(previous.get("repos") or {})
    repos["engine"] = repo_state(checkout, "main" if yard else None)
    _check(deadline)
    if yard or not _helm_for_data():
        repos.pop("data", None)
    elif data_root is None:
        repos["data"] = {"ahead": None, "dirty": None}
    else:
        try:
            repos["data"] = repo_state(data_root)
        except Exception as exc:  # noqa: BLE001 - no data repository is a state, not a crash
            _log(f"data repository: {exc}")
            repos["data"] = {"ahead": None, "dirty": None}
    cache["repos"] = repos
    _check(deadline)

    try:
        board = _lific_board()
    except Exception as exc:  # noqa: BLE001
        _log(f"lific-board not loaded: {exc}")
        return cache

    if yard:
        ident = _first_task((_git(checkout, "branch", "--show-current") or "").strip())
    else:
        tasks = dossier.session_tasks(state)
        ident = tasks[-1] if tasks else None
    if ident:
        _check(deadline)
        try:
            cache["issue"] = {"id": ident, "status": str(_issue(board, ident).get("status") or "")}
        except Exception as exc:  # noqa: BLE001
            _log(f"issue {ident}: {exc}")
    else:
        cache["issue"] = None

    if yard:
        cache["yards_total"], cache["yards_waiting"] = 0, []
    else:
        common = (_git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir") or "").strip()
        repo_root = Path(common).resolve().parent if common else checkout
        _check(deadline)
        try:
            cache["yards_total"], cache["yards_waiting"] = yards(board, repo_root, deadline)
        except Exception as exc:  # noqa: BLE001
            _log(f"yards: {exc}")
    return cache


def run(state_path: Path, cwd: Path) -> int:
    from scripts.utils import checkpoint_autonomy as AU

    started = time.monotonic()
    deadline = started + CHILD_BUDGET
    state = CP.read_json(state_path)
    previous = state.get(CACHE_KEY) if isinstance(state.get(CACHE_KEY), dict) else {}
    try:
        fresh = collect(state, cwd, previous, deadline)
        fresh["refreshed_at"] = CP.utc_now().isoformat()
    except OutOfTime as exc:
        # The budget is spent: keep every old value and end the child now.
        _log(f"{exc} after {time.monotonic() - started:.1f}s; old values kept")
        fresh = {**previous}
    except Exception as exc:  # noqa: BLE001 - old values stay, said on stderr
        _log(f"refresh failed after {time.monotonic() - started:.1f}s: {exc}")
        fresh = {**previous}
    fresh["refreshing_since"] = None

    def _write(current: dict) -> dict:
        current[CACHE_KEY] = fresh
        return current

    AU.persist(state_path, _mutate=_write)
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--cwd", required=True, type=Path)
    args = parser.parse_args(argv)
    return run(args.state, args.cwd)


if __name__ == "__main__":
    sys.exit(main())
