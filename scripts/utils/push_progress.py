#!/usr/bin/env python3
"""The progress record of a commit-and-push run of scripts/push-all.py (HOS-346).

A push runs in the background for a minute or more, and the chat shows nothing
that moves while it does. push-all writes one small record as it goes; the
Claude Code status line (`.claude/hooks/checkpoint-statusline.py`) and
`scripts/push-progress.py` read it and draw the same one-line segment.

  runtime:push/state/push-progress.json           mode 600, written atomically
      run_id, mode (full | repo), repos, state (running | done | failed | partial),
      repo, step, step_n, step_total, started_at, step_started_at, ended_at,
      step_durations ("repo/step": seconds), medians ("repo/step": seconds,
      from history), result, pid, pid_start. Times are epoch seconds.
  runtime:push/state/push-progress-history.jsonl  mode 600, one line per `done` run
      {"run_id", "mode", "ended_at", "steps": {"data/checks": 4.1, ...}}

Each repository has three steps: `checks` (the Lific archive, every scan and
ratchet, through the .memory-index assertion), `commit`, and `push + tests`
(the push preconditions, the supervised push with the pre-push suite git runs,
and the engine/data compatibility check).

Every public function is best-effort: an error inside it prints one note line to
stderr and returns, so nothing here can change what push-all does, prints or
exits with. The writer resolves the directory with `data_write_dir`; readers use
`data_path`, which creates nothing. Both are imported only when needed, so
`render` costs nothing beyond the standard library.

Never holds a secret: step names, repository names, timings and a result
sentence built from fixed words, the step and an exception's TYPE name.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

RECORD_FILE = "push-progress.json"
HISTORY_FILE = "push-progress-history.jsonl"
STEPS = ("checks", "commit", "push + tests")
HISTORY_RUNS = 5           # medians over the last five done runs of the same mode
SHOW_AFTER_S = 600         # an end state, or a dead writer, shows for ten minutes
BAR_CELLS = 10
RESULT_MAX = 40

_record: dict | None = None    # the writer's own copy of the record it last wrote


def _note(what: str, exc: BaseException) -> None:
    print(f"  note: push progress {what} failed ({type(exc).__name__}: {exc}); "
          f"the push carries on", file=sys.stderr)


def _write_dir() -> Path:
    from scripts.utils.data_path import data_write_dir
    return data_write_dir("runtime:push/state")


def _read_dir() -> Path:
    from scripts.utils.data_path import data_path
    return data_path("runtime:push/state")


def _keys(repos: list[str]) -> list[str]:
    return [f"{repo}/{step}" for repo in repos for step in STEPS]


def _history(folder: Path) -> list[dict]:
    try:
        lines = (folder / HISTORY_FILE).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("steps"), dict):
            out.append(row)
    return out


def _medians(folder: Path, mode: str) -> dict[str, float]:
    rows = [r for r in _history(folder) if r.get("mode") == mode][-HISTORY_RUNS:]
    seen: dict[str, list[float]] = {}
    for row in rows:
        for key, secs in row["steps"].items():
            if isinstance(secs, (int, float)):
                seen.setdefault(key, []).append(float(secs))
    return {key: round(statistics.median(v), 1) for key, v in seen.items()}


def _save() -> None:
    from scripts.utils.atomic import atomic_write_text
    atomic_write_text(_write_dir() / RECORD_FILE, json.dumps(_record) + "\n", mode=0o600)


def _close_step(now: float) -> None:
    if _record and _record.get("step"):
        key = f"{_record['repo']}/{_record['step']}"
        _record["step_durations"][key] = round(now - _record["step_started_at"], 1)


def start(mode: str, repos: list[str]) -> None:
    """Open a run in state `running`, with the per-step medians from history."""
    global _record
    try:
        from scripts.utils.install_person import _start_time
        now = time.time()
        _record = {
            "run_id": uuid.uuid4().hex[:12], "mode": mode, "repos": list(repos),
            "state": "running", "repo": None, "step": None, "step_n": 0,
            "step_total": len(STEPS) * len(repos), "started_at": now,
            "step_started_at": now, "ended_at": None, "step_durations": {},
            "medians": _medians(_write_dir(), mode), "result": None,
            "pid": os.getpid(), "pid_start": _start_time(os.getpid()) or "",
        }
        _save()
    except Exception as exc:  # noqa: BLE001 - best-effort by contract, reported
        _note("start", exc)


def step(repo: str, name: str) -> None:
    """Close the previous step's duration and start the next one."""
    try:
        if _record is None:
            return
        now = time.time()
        _close_step(now)
        _record.update(repo=repo, step=name, step_n=_record["step_n"] + 1,
                       step_started_at=now)
        _save()
    except Exception as exc:  # noqa: BLE001 - best-effort by contract, reported
        _note("step", exc)


def _outcome(exit_code, skipped: list[str], exc: BaseException | None) -> tuple[str, str]:
    where = f"{_record.get('repo') or '-'} · {_record.get('step') or '-'}"
    if isinstance(exc, KeyboardInterrupt):
        return "failed", "interrupted"
    if exc is not None and not isinstance(exc, SystemExit):
        return "failed", f"crashed: {type(exc).__name__}"
    if exit_code == 0:
        return "done", "ok"
    if exit_code == 2:
        return "failed", f"refused at {where}"
    if exit_code == 3:
        names = ", ".join(skipped)
        if len(skipped) < len(_record.get("repos") or []):
            return "partial", f"skipped: {names}"
        return "failed", f"nothing pushed: {names}"
    return "failed", f"push failed at {where}"


def finish(exit_code, skipped: list[str], exc: BaseException | None = None) -> None:
    """Write the end state; a `done` run appends its step timings to history."""
    try:
        if _record is None:
            return
        now = time.time()
        _close_step(now)
        state, result = _outcome(exit_code, [s.lower() for s in skipped], exc)
        _record.update(state=state, result=result, ended_at=now)
        _save()
        if state == "done":
            line = json.dumps({"run_id": _record["run_id"], "mode": _record["mode"],
                               "ended_at": now, "steps": _record["step_durations"]})
            fd = os.open(_write_dir() / HISTORY_FILE,
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc2:  # noqa: BLE001 - best-effort by contract, reported
        _note("finish", exc2)


def last() -> dict | None:
    """The writer's own copy of the record it last built, for its end message."""
    return dict(_record) if _record is not None else None


def read() -> dict | None:
    """The record on disk, or None when there is none or it cannot be read."""
    try:
        data = json.loads((_read_dir() / RECORD_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - best-effort by contract, reported
        _note("read", exc)
        return None
    return data if isinstance(data, dict) else None


def duration(secs: float) -> str:
    """`45s`, `2m41s` or `1h03m`."""
    s = max(0, int(round(secs)))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _writer_alive(record: dict) -> bool:
    from scripts.utils.install_person import alive_since
    try:
        pid = int(record.get("pid"))
    except (TypeError, ValueError):
        return False
    return alive_since(pid, str(record.get("pid_start") or ""))


def writer_gone(record: dict | None) -> bool:
    """True for a `running` record whose writer is no longer alive."""
    try:
        return (isinstance(record, dict) and record.get("state") == "running"
                and not _writer_alive(record))
    except Exception as exc:  # noqa: BLE001 - best-effort by contract, reported
        _note("writer check", exc)
        return False


def _left(record: dict, now: float) -> float | None:
    medians = record.get("medians") or {}
    keys = _keys(record.get("repos") or [])
    n = int(record.get("step_n") or 0)
    if n == 0:  # before the first step: every step is still ahead
        later, current = keys, None
    else:
        current = f"{record.get('repo')}/{record.get('step')}"
        if current not in keys:
            return None
        later = keys[keys.index(current) + 1:]
    total = 0.0
    if current is not None:
        if current not in medians:
            return None
        total += max(0.0, medians[current] - (now - record["step_started_at"]))
    for key in later:
        if key not in medians:
            return None
        total += medians[key]
    return total


def render(record: dict | None, now: float, *, always: bool = False,
           dead_since: float | None = None) -> str:
    """The one-line segment, or "" when there is nothing to show.

    Shown while the run is `running` and its writer lives, for ten minutes after
    it ended, and for ten minutes after its dead writer was first seen.
    `dead_since` is that moment, which only the reader knows (the status line
    keeps it per run in its own session state); without it the window counts
    from the start of the step the writer died in. `always` (the CLI) drops the
    ten-minute window, so the last result is still told when asked.
    """
    try:
        if not isinstance(record, dict):
            return ""
        state = record.get("state")
        if state == "running":
            if not _writer_alive(record):
                since = dead_since if dead_since is not None else float(record.get("step_started_at") or 0)
                if always or now - since <= SHOW_AFTER_S:
                    return "push ✗ stopped (writer gone)"
                return ""
            n, total = int(record.get("step_n") or 0), int(record.get("step_total") or 0)
            filled = max(0, min(BAR_CELLS, round(BAR_CELLS * (n - 1) / total))) if total else 0
            bar = "▓" * filled + "░" * (BAR_CELLS - filled)
            elapsed = duration(now - float(record["started_at"]))
            left = _left(record, now)
            tail = "estimating" if left is None else f"~{duration(left)} left"
            where = f"{record.get('repo')} · {record.get('step')}" if n else "starting"
            return f"push [{bar}] {n}/{total} {where} · {elapsed} / {tail}"
        if state in ("done", "failed", "partial"):
            ended = float(record.get("ended_at") or 0)
            if not always and now - ended > SHOW_AFTER_S:
                return ""
            took = duration(ended - float(record.get("started_at") or ended))
            if state == "done":
                return f"push ✓ ok · {took}"
            return f"push ✗ {str(record.get('result') or '')[:RESULT_MAX]} · {took}"
        return ""
    except Exception as exc:  # noqa: BLE001 - best-effort by contract, reported
        _note("render", exc)
        return ""
