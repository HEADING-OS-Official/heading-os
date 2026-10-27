"""The task dossier: which issues a session works on, and the [dossier] journal in Lific.

Files stay the original; Lific holds a short pointer record per event (spec
`2026-10-24-lific-dossier-design`, registry name `drafts`). Nothing here may
block or fail the caller: every record goes to the outbox (registry name
`dossier-outbox`) and a detached flusher, never waited for, sends it; /prime
flushes too. No Lific call runs in a writer, so none runs in a hook.

WHICH LIFIC TRANSPORT. The `lific` CLI's default `sql` backend, writing the
SQLite file directly, and not `--backend http`. MEASURED 2026-10-24 on a copy of
the database (spec Q1): `lific comment add --user <bot>` records the bot as the
author and `transport cli` in the audit log. The sql backend needs no running
server, no network and no credential, so a stopped server never costs a record;
the server's own SQLite handles the second writer. `HEADING_OS_LIFIC_DB` points
every call here, reads included, at another database file: that is how a
verification run uses a throwaway copy and never the live one.

WHO IS THE AUTHOR. The bot username comes from `LIFIC_BOT_USER` in the
gitignored `.env`, never from code. Unset means no direct post at all, because
the CLI's own default author is the first admin, the operator
(`.claude/rules/lific.md` item 7); every record then waits in the outbox.

Usage (library):
    from scripts.utils import dossier
    dossier.post(["HOS-9001"], "handoff", "manual", "0000aaaa", ["Next: x"], "<data-root-relative path>")
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils.data_path import data_path
from scripts.utils.secret_patterns import iter_patterns

MARKER = "[dossier]"
EVENTS = ("prompt", "handoff", "brief", "close", "relink", "unlink")
STATE_KEY = "dossier_tasks"
#: Ids carried across /clear and not yet confirmed (HOS-259); see `carried_tasks`.
CARRIED_KEY = "dossier_carried"
#: ASCII only: `\d` would also take other scripts' digits, which Lific refuses.
TASK_RE = re.compile(r"\b(?P<key>[A-Z]{2,5})-(?P<num>[0-9]+)\b", re.ASCII)
#: The project key assumed when Lific cannot list its projects.
FALLBACK_KEYS = frozenset({"HOS"})
LIFIC_TIMEOUT = 10
SUMMARY_LIMIT = 160
#: Statuses after which an issue receives no record but its own `close`.
CLOSED = frozenset({"done", "cancelled"})
#: Refusals (Lific answered no, not a timeout) before a record leaves the outbox.
MAX_REFUSALS = 3
#: Newest comments read before a re-send, to see whether a timed-out send landed.
DUPLICATE_WINDOW = 50
#: Events a closed issue still receives: its own close, and link corrections.
CLOSED_EXEMPT = frozenset({"close", "relink", "unlink"})
#: What `lific issue get` prints on stderr for an issue that does not exist
#: (read 2026-10-24 on the copy: `Error: NotFound("issue HOS-9001 not found")`).
NOT_FOUND = "NotFound("
#: A lock file older than this belongs to a process that is gone.
STALE_LOCK_SECONDS = 120
#: How long a writer waits for the outbox lock before it spills instead.
QUEUE_LOCK_WAIT = 0.2
#: Fast failures (Lific answered no, for a reason other than a missing issue)
#: before a record is set aside; a timeout is never one.
MAX_SOFT_STRIKES = 5
#: Phrases, matched case-insensitively over the WHOLE stderr of a failed call,
#: that name a fault of the database or its transport, never of the record: the
#: pass stops, `down`. MEASURED 2026-10-24 on a copy with another connection in
#: BEGIN EXCLUSIVE: `comment add` and `issue get` both failed after 5.1 s with
#: one line, `Error: Database(SqliteFailure(Error { code: DatabaseBusy,
#: extended_code: 5 }, Some("database is locked")))`. "database table is
#: locked" and "database or disk is full" are SQLite's own texts for
#: SQLITE_LOCKED and SQLITE_FULL, not observed here. Anything else that fails
#: is `refused`, the record's own answer (HELM review rounds 7 and 8).
LIFIC_DOWN = ("database is locked", "database table is locked", "database is busy",
              "databasebusy", "readonly database", "read-only database", "disk i/o",
              "unable to open database", "database or disk is full")
#: Sends in one flush, every one refused, that count as a systemic refusal.
SYSTEMIC_REFUSALS = 3
#: The flusher's log is rotated to `<log>.1` past this size.
LOG_CAP_BYTES = 1_000_000
#: The project-key cache is refreshed at most this often.
PROJECTS_REFRESH_SECONDS = 600
#: A spill `.tmp` older than this was left by a writer that died mid-write.
SPILL_ORPHAN_SECONDS = 600
#: Flushes in a row a record fails unstruck (refused, or its issue not found)
#: before /prime names it.
STUCK_FLUSHES = 3
#: `last_error` of an entry whose issue Lific does not find.
MISSING_ERROR = "issue not found"
ENGINE_ROOT = Path(__file__).resolve().parent.parent.parent
DB_ENV = "HEADING_OS_LIFIC_DB"
BOT_ENV = "LIFIC_BOT_USER"


# ============================================================
# The session's task list
# ============================================================

def session_tasks(state: dict | None) -> list[str]:
    tasks = (state or {}).get(STATE_KEY) or []
    return [t for t in tasks if isinstance(t, str)]


def add_tasks(state: dict, ids: list[str]) -> None:
    current = session_tasks(state)
    for ident in ids:
        if ident not in current:
            current.append(ident)
    state[STATE_KEY] = current


def drop_task(state: dict, ident: str) -> None:
    state[STATE_KEY] = [t for t in session_tasks(state) if t != ident]
    if ident in carried_tasks(state):
        state[CARRIED_KEY] = [t for t in carried_tasks(state) if t != ident]


# Carried across /clear, not yet confirmed (HOS-259). No writer reads these:
# they become tasks only when a prompt saved by `prompt-archive.py` names them
# in its verbatim half (or a confirmed `update_issue ... active`); the first
# such prompt that does not name one drops it, with a relink pointer.

def carried_tasks(state: dict | None) -> list[str]:
    tasks = (state or {}).get(CARRIED_KEY) or []
    return [t for t in tasks if isinstance(t, str)]


def carry_into(state: dict, prev: dict | None) -> list[str]:
    """The previous session's tasks AND its own candidates become this one's candidates."""
    confirmed = session_tasks(state)
    current = carried_tasks(state)
    for ident in session_tasks(prev) + carried_tasks(prev):
        if ident not in current and ident not in confirmed:
            current.append(ident)
    state[CARRIED_KEY] = current
    return current


def confirm_carried(state: dict, ident: str) -> None:
    if ident in carried_tasks(state):
        state[CARRIED_KEY] = [t for t in carried_tasks(state) if t != ident]
    add_tasks(state, [ident])


def settle_plan(state: dict | None, verbatim: str) -> tuple[list[str], list[str]]:
    """(confirmed, dropped) candidates for a prompt whose verbatim half is `verbatim`."""
    carried = carried_tasks(state)
    named = set(find_ids(verbatim, {t.split("-", 1)[0] for t in carried}))
    return [t for t in carried if t in named], [t for t in carried if t not in named]


def relink_command(ident: str, rel_path: str) -> str:
    return (f".venv/bin/python scripts/dossier-journal.py post --issue {ident} "
            f"--event relink --path {rel_path}")


def dropped_line(ident: str, rel_path: str) -> str:
    return (f"Carried {ident} dropped: not named in this prompt; relink with "
            f"{relink_command(ident, rel_path)}")


def session_state(session: str | None = None, project: Path | None = None) -> dict:
    """`session`'s checkpoint state (this session when None), or {}. Never raises."""
    try:
        from scripts.utils import checkpoint_paths as CP
        sid = session or CP.session_id()
        return CP.read_json(CP.state_path(project or CP.project_root(), CP.safe_slug(sid)))
    except Exception as exc:  # noqa: BLE001 - a missing list means `Task: none`, never a failure
        print(f"dossier: task list unreadable ({type(exc).__name__}: {exc})", file=sys.stderr)
        return {}


def settle_carried(session: str, confirmed: list[str], dropped: list[str], rel_path: str,
                   project: Path | None = None) -> None:
    """Apply `settle_plan` after the prompt file is on disk, and note each drop for /prime. Never raises."""
    if not confirmed and not dropped:
        return
    try:
        from scripts.utils import checkpoint_paths as CP
        path = CP.state_path(project or CP.project_root(), CP.safe_slug(session))
        with CP.locked_state(path) as state:
            for ident in confirmed:
                if ident in carried_tasks(state):
                    confirm_carried(state, ident)
            state[CARRIED_KEY] = [t for t in carried_tasks(state) if t not in dropped]
    except Exception as exc:  # noqa: BLE001 - the prompt file already says what was decided
        print(f"dossier: carried tasks not settled ({type(exc).__name__}: {exc})", file=sys.stderr)
    try:
        box = _dropped(_outbox())
        box.parent.mkdir(parents=True, exist_ok=True)
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with box.open("a", encoding="utf-8") as fh:
            for ident in dropped:
                fh.write(json.dumps({"issue": ident, "path": rel_path, "session": session,
                                     "at": at}, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 - the prompt file carries the pointer anyway
        print(f"dossier: dropped-carry note not written ({type(exc).__name__}: {exc})",
              file=sys.stderr)


def tasks_for_session(session: str | None = None, project: Path | None = None) -> list[str]:
    """The confirmed task list in `session`'s checkpoint state (this session when None). Never raises."""
    return session_tasks(session_state(session, project))


def find_ids(text: str, keys: set[str] | frozenset[str]) -> list[str]:
    """Issue ids in `text` whose project key is a known Lific project, in order, unique."""
    seen: list[str] = []
    for m in TASK_RE.finditer(text or ""):
        ident = f"{m.group('key')}-{m.group('num')}"
        if m.group("key") in keys and ident not in seen:
            seen.append(ident)
    return seen


def task_header(ids: list[str]) -> str:
    return f"Task: {', '.join(ids) if ids else 'none'}"


def header_tasks(text: str, max_lines: int = 12) -> list[str]:
    """The ids on a file's `Task:` header line within its first lines, or []."""
    for line in (text or "").splitlines()[:max_lines]:
        if line.startswith("Task:"):
            value = line[len("Task:"):].strip()
            if value == "none":
                return []
            return [v.strip() for v in value.split(",") if v.strip()]
    return []


def rel(path: Path | str) -> str:
    """`path` relative to the data root when it sits under it, else as given."""
    try:
        from scripts.utils.paths import get_data_root
        return str(Path(path).resolve().relative_to(get_data_root().resolve()))
    except Exception as exc:  # noqa: BLE001 - a record path must never fail its caller
        print(f"dossier: cannot make {path} data-relative ({type(exc).__name__}); "
              f"keeping it as given", file=sys.stderr)
        return str(path)


def cut(line: str, limit: int = SUMMARY_LIMIT) -> str:
    line = " ".join((line or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


# ============================================================
# Lific
# ============================================================

def db_args() -> list[str]:
    db = os.environ.get(DB_ENV, "").strip()
    return ["--db", db] if db else []


def _projects_cache() -> Path:
    box = _outbox()
    return box.with_name(box.name + ".projects.json")


def project_keys() -> frozenset[str]:
    """Project keys from the cache the flusher refreshes; `FALLBACK_KEYS` without it.

    No Lific call: this runs in the UserPromptSubmit hook on every prompt that
    names an id, where a hung Lific would cost the turn.
    """
    try:
        keys = json.loads(_projects_cache().read_text(encoding="utf-8"))
        found = frozenset(k for k in keys if isinstance(k, str) and k)
        return found or FALLBACK_KEYS
    except FileNotFoundError:
        return FALLBACK_KEYS
    except (OSError, ValueError, TypeError) as exc:
        print(f"dossier: project cache unreadable ({type(exc).__name__}); using "
              f"{sorted(FALLBACK_KEYS)}", file=sys.stderr)
        return FALLBACK_KEYS


def refresh_project_keys() -> None:
    """Ask Lific for its project keys and rewrite the cache. For the lock holder only. Never raises.

    At most once per `PROJECTS_REFRESH_SECONDS`, read from a stamp file that
    every ATTEMPT renews, failed or empty ones included, so a Lific that does
    not answer is asked once per window rather than on every flush. A failure
    keeps the old cache and costs the flush nothing.
    """
    try:
        cache = _projects_cache()
        stamp = cache.with_name(cache.name + ".stamp")
        if stamp.is_file() and time.time() - stamp.stat().st_mtime < PROJECTS_REFRESH_SECONDS:
            return
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(datetime.now(timezone.utc).isoformat(timespec="seconds"), encoding="utf-8")
        if shutil.which("lific") is None:
            print("dossier: lific not on PATH; project keys not refreshed", file=sys.stderr)
            return
        out = subprocess.run(["lific", "project", "list", *db_args(), "--json"],  # nosec B603 - list argv
                             capture_output=True, text=True, timeout=LIFIC_TIMEOUT)
        if out.returncode != 0:
            print(f"dossier: project list failed (exit {out.returncode}); cache kept",
                  file=sys.stderr)
            return
        keys = sorted({r.get("identifier") for r in json.loads(out.stdout)
                       if isinstance(r, dict) and r.get("identifier")})
        if not keys:
            print("dossier: project list empty; cache kept", file=sys.stderr)
        else:
            tmp = cache.with_name(cache.name + ".tmp")
            tmp.write_text(json.dumps(keys), encoding="utf-8")
            os.replace(tmp, cache)
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        print(f"dossier: project keys not refreshed ({type(exc).__name__}: {exc}); cache kept",
              file=sys.stderr)


def _failure(stderr: str, returncode: int) -> tuple[str, str]:
    """`(kind, first line)` for a failed Lific call: `down`, `missing` or `refused`.

    One rule for every call that can fail on a record (the status read and the
    send): `down` only on `LIFIC_DOWN`, anywhere in stderr; `missing` on
    Lific's NotFound for the issue; everything else `refused`. A malformed id
    is `refused`: MEASURED 2026-10-24 on the copy, `issue get` and `comment
    add` of "HOS-1," both exit 1 with `Error: BadRequest("invalid sequence
    number in: HOS-1,")`, while "hos-1" is `NotFound("issue hos-1 not found")`.
    """
    first = (stderr.strip().splitlines() or [f"exit {returncode}"])[0][:200]
    if any(phrase in stderr.lower() for phrase in LIFIC_DOWN):
        return "down", first
    # The first line only: a strike must not come from a later line that could
    # echo the record's text. Lific's NotFound is one line (MEASURED, above).
    if NOT_FOUND + '"issue' in first:
        return "missing", first
    return "refused", first


def issue_state(ident: str) -> tuple[str, str | None, str | None, int | None]:
    """`("ok", status, updated_at, row id)`, `("missing", None, None, None)`,
    `("refused", why, None, None)` or `("down", None, None, None)`. One CLI call.

    `missing` only when Lific itself says the issue does not exist; `down`
    when Lific did not answer (a timeout, `LIFIC_DOWN`, output that is not
    JSON), because nothing can be concluded about the issue then; `refused`
    when Lific answered no about this id (`_failure`). `updated_at` is the only
    time `issue get` gives (MEASURED 2026-10-24: no closed-at field), and a
    comment moves it too.
    """
    if shutil.which("lific") is None:
        return "down", None, None, None
    try:
        out = subprocess.run(["lific", "issue", "get", ident, *db_args(), "--json"],  # nosec B603 - list argv
                             capture_output=True, text=True, timeout=LIFIC_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"dossier: status of {ident} unreadable ({type(exc).__name__})", file=sys.stderr)
        return "down", None, None, None
    if out.returncode != 0:
        kind, first = _failure(out.stderr, out.returncode)
        if kind != "missing":
            print(f"dossier: status of {ident} unreadable ({first})", file=sys.stderr)
        return kind, (first if kind == "refused" else None), None, None
    try:
        data = json.loads(out.stdout)
        status, updated, row = data.get("status"), data.get("updated_at"), data.get("id")
    except (ValueError, AttributeError) as exc:
        print(f"dossier: status of {ident} unparseable ({type(exc).__name__})", file=sys.stderr)
        return "down", None, None, None
    if not isinstance(status, str):
        return "down", None, None, None
    return ("ok", status, updated if isinstance(updated, str) else None,
            row if type(row) is int else None)


def _lific_db() -> Path | None:
    """The Lific database file: `HEADING_OS_LIFIC_DB`, else `[database] path` in
    `~/.config/lific/lific.toml` (where the operator pinned it, 2026-09-18); None
    when neither names an existing file."""
    db = os.environ.get(DB_ENV, "").strip()
    if not db:
        try:
            import tomllib
            conf = tomllib.loads((Path.home() / ".config" / "lific" / "lific.toml").read_text(encoding="utf-8"))
            db = str(conf.get("database", {}).get("path", "")).strip()
        except (OSError, ValueError, AttributeError) as exc:
            print(f"dossier: lific.toml unreadable ({type(exc).__name__}); no close time", file=sys.stderr)
            return None
    return Path(db) if db and Path(db).is_file() else None


def _close_time(row: int | None) -> datetime | None:
    """When the issue last moved into done or cancelled, from Lific's own
    `status_transitions`, read-only. For the flusher only, never a hook.

    MEASURED 2026-10-24 on a copy: `lific issue update -s done` and `lific
    git-hook` ("closes HOS-x") each write one row (`to_status` 'done',
    `created_at` UTC, the second the status changed), and so does every other
    path: the row is written by the database's own trigger
    `status_transitions_capture`, AFTER UPDATE OF status ON issues. None when the database
    or the row cannot be read; the caller then falls back to `updated_at`. A
    busy database waits 1 s at most, so a flush deadline is overrun by one CLI
    call and this read, inside /prime's margin.
    """
    db = _lific_db()
    if row is None or db is None:
        return None
    try:
        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
        try:
            found = con.execute("SELECT max(created_at) FROM status_transitions WHERE issue_id = ? "
                                "AND to_status IN ('done', 'cancelled')", (row,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        print(f"dossier: close time unreadable ({exc}); updated_at used", file=sys.stderr)
        return None
    return _lific_time(found[0] if found and isinstance(found[0], str) else None)


def _clean(summary: list[str]) -> tuple[list[str], bool]:
    """Drop every summary line if any carries a secret-shaped string.

    A secret in git can be scrubbed; one in Lific's audit log cannot (spec 9).
    """
    joined = "\n".join(summary)
    for pattern, _desc in iter_patterns(joined):
        if pattern.search(joined):
            return [], True
    return summary, False


def format_record(event: str, kind: str, session: str, summary: list[str],
                  path: str, when: datetime) -> str:
    head = f"{MARKER} {when.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC · {event}"
    if kind:
        head += f" · {kind}"
    head += f" · session {session[:8]}"
    lines = [head, *[cut(s) for s in summary if s.strip()], f"→ {path}"]
    return "\n".join(lines)


def _event_id(ident: str, event: str, path: str, content: str) -> str:
    """One id per distinct record: the same line queued twice (a crash between
    append and rewrite) is sent once, while two records for one file, say two
    relinks, stay two."""
    return hashlib.sha256(f"{ident}|{event}|{path}|{content}".encode()).hexdigest()[:16]


def _outbox() -> Path:
    """The outbox belongs to its database: a record queued against a copy flushes to that copy."""
    db = os.environ.get(DB_ENV, "").strip()
    if db:
        return Path(db).with_name(Path(db).name + ".dossier-outbox.jsonl")
    return data_path("dossier-outbox")


def load_engine_env() -> None:
    """The checkout's `.env` into `os.environ`, never overwriting a set variable.

    `LIFIC_BOT_USER` lives there; a detached flusher or a CLI run otherwise
    sees it only if something else happened to load the file first.
    """
    try:
        from scripts.utils.paths import load_env
        load_env(ENGINE_ROOT)
    except Exception as exc:  # noqa: BLE001 - a missing .env means the records wait, said below
        print(f"dossier: .env not loaded ({type(exc).__name__}: {exc})", file=sys.stderr)


_bot_missing_said = False


def _lific_argv(ident: str, content: str) -> list[str] | None:
    global _bot_missing_said
    if not os.environ.get(BOT_ENV, "").strip():
        load_engine_env()
    user = os.environ.get(BOT_ENV, "").strip()
    if not user:
        if not _bot_missing_said:
            print(f"dossier: {BOT_ENV} not set; records stay queued", file=sys.stderr)
            _bot_missing_said = True
        return None
    if shutil.which("lific") is None:
        return None
    return ["lific", "comment", "add", *db_args(), "--user", user, "--json",
            "-c", content, ident]


def _send(ident: str, content: str) -> tuple[str, str]:
    """`("ok", "")`, `("down", why)`, `("missing", line)` or `("refused", first stderr line)`.

    `down` stops the pass: a timeout, Lific missing, no bot user, or a failure
    naming the database or its transport (`LIFIC_DOWN`, `_failure`). `missing` is Lific
    saying the issue does not exist (it was deleted after the status read).
    Everything else is `refused`, this record's own answer; a refusal that is
    really Lific-wide (an unknown bot user, `Error: NotFound("user '<name>'
    not found")`, MEASURED 2026-10-24 on the copy) refuses every record, which
    strikes none (`_pass`) and shows as the systemic line in /prime.
    """
    argv = _lific_argv(ident, content)
    if argv is None:
        return "down", "no bot user or no lific"
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=LIFIC_TIMEOUT)  # nosec B603 - list argv, no shell
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"dossier: lific unreachable ({type(exc).__name__}); kept", file=sys.stderr)
        return "down", type(exc).__name__
    if proc.returncode != 0:
        kind, first = _failure(proc.stderr, proc.returncode)
        if kind == "down":
            print(f"dossier: Lific cannot write ({first}); records stay queued", file=sys.stderr)
        elif kind == "refused":
            print(f"dossier: lific refused the record for {ident}: {first}", file=sys.stderr)
        return kind, first
    return "ok", ""


def _already_posted(ident: str, content: str) -> bool | None:
    """Whether the issue's newest comments hold exactly `content`; None when Lific did not answer.

    A send that timed out may have landed. None stops the pass: never send
    after a check that could not be made.
    """
    if shutil.which("lific") is None:
        return None
    try:
        out = subprocess.run(["lific", "comment", "list", ident, *db_args(), "--limit",  # nosec B603 - list argv
                              str(DUPLICATE_WINDOW), "--json"],
                             capture_output=True, text=True, timeout=LIFIC_TIMEOUT)
        if out.returncode != 0:
            print(f"dossier: comments of {ident} unreadable (exit {out.returncode})",
                  file=sys.stderr)
            return None
        rows = json.loads(out.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"dossier: comments of {ident} unreadable ({type(exc).__name__})", file=sys.stderr)
        return None
    return any(isinstance(r, dict) and r.get("content") == content for r in rows)


# ============================================================
# Post, outbox, flush
# ============================================================

def _lock_path(box: Path) -> Path:
    return box.with_name(box.name + ".lock")


def _steal_stale(lock: Path) -> None:
    """Remove a dead holder's lock without ever removing a live one.

    The lock is first RENAMED to a name of its own, so two contenders cannot
    both remove it; if what was moved turns out to be fresh (recreated between
    the age check and the rename), it is linked back unless a new lock already
    stands there.
    """
    aside = lock.with_name(f"{lock.name}.stale-{uuid.uuid4().hex[:8]}")
    try:
        os.rename(lock, aside)
    except FileNotFoundError:
        return
    try:
        age = time.time() - aside.stat().st_mtime
        if age <= STALE_LOCK_SECONDS:
            with contextlib.suppress(FileExistsError):
                os.link(aside, lock)
            print(f"dossier: {lock.name} was renewed during a stale check; left to its holder",
                  file=sys.stderr)
        else:
            print(f"dossier: {lock.name} is {age:.0f}s old; removed a dead holder's lock",
                  file=sys.stderr)
    finally:
        with contextlib.suppress(FileNotFoundError):
            aside.unlink()


@contextlib.contextmanager
def _box_lock(box: Path, wait: float = 0.0):
    """Yield this holder's token while the outbox lock is held, "" when it is not.

    `os.open(O_CREAT | O_EXCL)`, the same on every platform: `fcntl` is absent
    on Windows, and `file_lock` proceeds unlocked after its wait, which a flush
    that rewrites the file must never do. The lock holds a token of its own,
    and release removes it only while it still holds that token. A lock file
    older than `STALE_LOCK_SECONDS` is a dead holder's (`_steal_stale`); a live
    flush refreshes its lock as it goes (`_touch`).
    """
    lock = _lock_path(box)
    lock.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()} {uuid.uuid4().hex}\n"
    deadline = time.monotonic() + wait
    fd = None
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > STALE_LOCK_SECONDS:
                _steal_stale(lock)
                continue
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
    if fd is None:
        yield ""
        return
    try:
        os.write(fd, token.encode())
        os.close(fd)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            lock.unlink()
        print(f"dossier: lock token not written ({exc}); lock removed", file=sys.stderr)
        yield ""
        return
    try:
        yield token
    finally:
        try:
            if lock.read_text(encoding="utf-8") == token:
                lock.unlink()
            else:
                print(f"dossier: {lock.name} changed hands while held; left to its new holder",
                      file=sys.stderr)
        except FileNotFoundError:
            print(f"dossier: {lock.name} was gone at release", file=sys.stderr)
        except OSError as exc:
            # The pass is done and settled; a lock left behind ages out
            # (`STALE_LOCK_SECONDS`), so its counts must not turn into -1.
            print(f"dossier: {lock.name} not released ({exc}); it expires after "
                  f"{STALE_LOCK_SECONDS}s", file=sys.stderr)


def _owns(box: Path, token: str) -> bool:
    """Whether the lock file still holds `token`: checked before every rewrite."""
    try:
        return bool(token) and _lock_path(box).read_text(encoding="utf-8") == token
    except OSError:
        return False


def _touch(box: Path) -> None:
    """Keep a long flush's lock younger than `STALE_LOCK_SECONDS`."""
    try:
        os.utime(_lock_path(box))
    except OSError as exc:
        print(f"dossier: lock not refreshed ({type(exc).__name__}); a pass over "
              f"{STALE_LOCK_SECONDS}s may lose it", file=sys.stderr)


def _rejected(box: Path) -> Path:
    return box.with_name(box.name.removesuffix(".jsonl") + ".rejected.jsonl")


def _append_bytes(path: Path, data: bytes) -> None:
    """Append `data` (whole lines) in one write; a torn last line gets its newline first.

    The caller holds the outbox lock. Without the newline a record would glue
    onto a line a crash left unfinished, and both would be lost to the parser.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as fh:
        fh.seek(0, os.SEEK_END)
        lead = b""
        if fh.tell() > 0:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                lead = b"\n"
        fh.seek(0, os.SEEK_END)
        fh.write(lead + data)


def _append_text(path: Path, text: str) -> None:
    _append_bytes(path, text.encode("utf-8"))


def _append(path: Path, entry: dict) -> None:
    """One record line. The caller holds the outbox lock."""
    _append_text(path, json.dumps(entry, ensure_ascii=False) + "\n")


def _spills(box: Path) -> list[Path]:
    """Entries queued while a flush held the lock, one file each, oldest first."""
    return sorted(box.parent.glob(box.name + ".spill-*"), key=lambda f: f.name)


def _queue(entry: dict) -> None:
    """Append under the lock; when the lock stays busy, spill to a file of its own.

    An unlocked append could land between a flush's re-read and its replace
    and be lost, so a writer that cannot get the lock never appends. A spill file is
    written whole (`.tmp`, then `os.replace`) under a unique name no flush
    rewrites; the next flush that holds the lock moves it into the outbox.
    """
    box = _outbox()
    box.parent.mkdir(parents=True, exist_ok=True)
    with _box_lock(box, QUEUE_LOCK_WAIT) as held:
        if held:
            _append(box, entry)
            return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    spill = box.with_name(f"{box.name}.spill-{stamp}-{uuid.uuid4().hex[:8]}")
    tmp = spill.with_name(spill.name + ".tmp")
    tmp.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, spill)
    print(f"dossier: outbox busy; record spilled to {spill.name}", file=sys.stderr)


def _merge_spills(box: Path) -> int:
    """Move spilled entries into the outbox. The caller holds the outbox lock.

    Each spill is appended and only then removed, so a crash between the two
    leaves a duplicate line (the event id makes flush send it once), never a
    loss. A spill `.tmp` older than `SPILL_ORPHAN_SECONDS` was left by a writer
    that died before its rename: merged when it holds one complete record, its
    bytes moved to the corrupt file otherwise.
    """
    merged = 0
    for spill in _spills(box):
        if spill.name.endswith(".tmp"):
            try:
                if time.time() - spill.stat().st_mtime < SPILL_ORPHAN_SECONDS:
                    continue
            except FileNotFoundError:
                continue
            raw = spill.read_bytes()
            data = raw.strip()
            if not data:
                # Nothing to keep, and a writer suspended mid-write may still rename it.
                continue
            if b"\n" not in data and _parse(data) is not None:
                _append_bytes(box, data + b"\n")
            else:
                _to_corrupt(box, [line for line in raw.split(b"\n") if line.strip()])
            print(f"dossier: orphaned {spill.name} taken in", file=sys.stderr)
            spill.unlink()
            merged += 1
            continue
        data = spill.read_bytes()
        if data.strip():
            _append_bytes(box, data if data.endswith(b"\n") else data + b"\n")
        spill.unlink()
        merged += 1
    return merged


def _in_yard() -> bool:
    """A yard spawns nothing that outlives its command (M029.1). Unsure counts as a yard."""
    if os.environ.get("HEADING_OS_YARD") == "1":
        return True
    try:
        from scripts.utils.clone_guard import is_main_clone
        return not is_main_clone(ENGINE_ROOT)
    except Exception as exc:  # noqa: BLE001 - unsure means no detached process
        print(f"dossier: cannot tell HELM from a yard ({type(exc).__name__}); not spawning",
              file=sys.stderr)
        return True


def _venv_python() -> Path | None:
    """The checkout's own interpreter, never `sys.executable` (M046.2)."""
    for candidate in (ENGINE_ROOT / ".venv" / "bin" / "python",
                      ENGINE_ROOT / ".venv" / "Scripts" / "python.exe"):
        if candidate.exists():
            return candidate
    return None


def _flush_log(box: Path) -> Path:
    return box.with_name(box.name + ".flush.log")


def _cap_log(log: Path) -> None:
    """Past `LOG_CAP_BYTES`, rotate the log to `<log>.1` (replacing the older one).

    Only the flush holding the outbox lock calls this, at its start; hooks and
    writers never rotate. A rename, never a rewrite: a process still writing
    to the file it opened keeps writing to `.1`, so no line is lost. Where the
    rename fails (Windows, while another process has the log open) it is said
    and tried again by the next flush.
    """
    try:
        if log.is_file() and log.stat().st_size > LOG_CAP_BYTES:
            os.replace(log, log.with_name(log.name + ".1"))
    except OSError as exc:
        print(f"dossier: flusher log not rotated ({exc})", file=sys.stderr)


def _detach_kwargs(windows: bool) -> dict:
    """Popen arguments that let the flusher outlive its parent, per platform."""
    if windows:
        return {"creationflags": getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def _start_flusher() -> None:
    """Start one detached `dossier-journal.py flush` in HELM and never wait for it.

    No Lific call runs inside a hook's critical path (HELM, 2026-10-24): a hung
    Lific would cost a hook its budget and with it the pointers or the /clear
    carry-over. A yard starts nothing (its records wait for HELM's next record
    or /prime). The flusher's lines go to a log beside the outbox, rotated by the flush.
    """
    if _in_yard():
        return
    python = _venv_python()
    if python is None:
        print("dossier: no .venv interpreter in this checkout; flusher not started, "
              "records stay queued", file=sys.stderr)
        return
    log = _flush_log(_outbox())
    try:
        detach = _detach_kwargs(os.name == "nt")
        with log.open("a", encoding="utf-8") as fh:
            subprocess.Popen([str(python), str(ENGINE_ROOT / "scripts" / "dossier-journal.py"),  # nosec B603 - list argv
                              "flush"],
                             cwd=str(ENGINE_ROOT), stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
                             **detach)
    except OSError as exc:
        print(f"dossier: flusher not started ({exc}); the next record or /prime flushes",
              file=sys.stderr)


def post(ids: list[str], event: str, kind: str, session: str,
         summary: list[str], path: str) -> dict:
    """Queue one record for each issue in `ids` and start a detached flusher. Never raises.

    Only appends: no Lific call here, so every hook and CLI writer returns at
    once. Closed issues are filtered by the flush, where Lific is asked.
    """
    result = {"posted": [], "queued": [], "redacted": False}
    if not ids:
        return result
    try:
        summary, redacted = _clean(summary)
        result["redacted"] = redacted
        if redacted:
            print("dossier: secret-shaped text in the summary; posting without it", file=sys.stderr)
        content = format_record(event, kind, session, summary, path, datetime.now(timezone.utc))
        for ident in ids:
            _queue({"id": _event_id(ident, event, path, content), "issue": ident, "event": event,
                    "content": content, "attempts": 0,
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            result["queued"].append(ident)
        _start_flusher()
    except Exception as exc:  # noqa: BLE001 - the caller's file is already written; never fail it
        print(f"dossier: post failed ({type(exc).__name__}: {exc})", file=sys.stderr)
    return result


def _corrupt(box: Path) -> Path:
    return box.with_name(box.name.removesuffix(".jsonl") + ".corrupt.jsonl")


def _parse(line: bytes | str) -> dict | None:
    """A record line as a dict, or None when it is not one.

    Not one: torn or garbled bytes, not UTF-8, not a JSON object, `id`, `issue`
    or `content` missing, or `attempts` / `soft` / `stuck_flushes` present
    and not a non-negative integer (missing means 0).
    """
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict):
        return None
    if not all(isinstance(entry.get(k), str) and entry.get(k) for k in ("id", "issue", "content")):
        return None
    for key in ("attempts", "soft", "stuck_flushes"):
        value = entry.get(key, 0)
        if type(value) is not int or value < 0:
            return None
    return entry


def _byte_lines(path: Path) -> list[bytes]:
    """The non-blank lines of a JSONL file as raw bytes, split on newline only.

    Bytes, so a line that is not valid UTF-8 can be moved aside exactly as it
    was; newline only, because `str.splitlines` also splits inside a record on
    U+2028, U+2029 and U+0085, which `json.dumps(ensure_ascii=False)` leaves
    unescaped.
    """
    if not path.is_file():
        return []
    return [line for line in path.read_bytes().split(b"\n") if line.strip()]


def _read_box(box: Path) -> list[dict]:
    """The valid records of a JSONL file; a line that is not one is skipped (see `_quarantine`)."""
    return [e for e in (_parse(line) for line in _byte_lines(box)) if e is not None]


def _write_lines(box: Path, lines: list[bytes]) -> None:
    tmp = box.with_name(box.name + ".tmp")
    tmp.write_bytes(b"".join(line + b"\n" for line in lines))
    os.replace(tmp, box)


def _to_corrupt(box: Path, lines: list[bytes]) -> None:
    """Append raw lines to the corrupt file, skipping any already there byte for byte.

    The skip is what lets a flush killed between this append and the outbox
    rewrite run again without counting a line twice.
    """
    corrupt = _corrupt(box)
    present = set(_byte_lines(corrupt))
    new = []
    for line in lines:
        if line not in present:
            present.add(line)
            new.append(line)
    if new:
        _append_bytes(corrupt, b"".join(line + b"\n" for line in new))


def _quarantine(box: Path) -> int:
    """Move every outbox line that is not a record to the corrupt file. The caller holds the lock.

    A torn or garbled line must not stop every flush; its exact bytes are kept,
    one line each, never deleted (M003), beside the outbox, and /prime names it.
    """
    lines = _byte_lines(box)
    bad = [line for line in lines if _parse(line) is None]
    if not bad:
        return 0
    _to_corrupt(box, bad)
    _write_lines(box, [line for line in lines if _parse(line) is not None])
    print(f"dossier: {len(bad)} unreadable outbox line(s) moved to {_corrupt(box).name}",
          file=sys.stderr)
    return len(bad)


def _entry_event(entry: dict) -> str:
    """The record's event; read from its head line for entries queued before `event` was stored."""
    if entry.get("event"):
        return str(entry["event"])
    head = str(entry.get("content", "")).split("\n", 1)[0].split(" · ")
    return head[1] if len(head) > 1 else ""


def _record_time(entry: dict) -> datetime | None:
    """When the record was written: its `at`, else the minute on its head line; None if neither."""
    try:
        if isinstance(entry.get("at"), str):
            return datetime.fromisoformat(entry["at"]).astimezone(timezone.utc)
        head = str(entry.get("content", "")).split("\n", 1)[0]
        stamp = head.removeprefix(MARKER + " ").split(" UTC", 1)[0]
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        # A hand-edited `at` such as year 1 with an offset overflows `astimezone`.
        return None


def _lific_time(value: str | None) -> datetime | None:
    """A Lific `updated_at` ("2026-10-24 10:59:20", UTC: MEASURED 2026-10-24, an issue
    created at 14:59 +04:00 read `created_at` 10:59:18) as a datetime; None if absent."""
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if value else None
    except ValueError:
        return None


def _settle(box: Path, posted: set[str], fields: dict[str, dict], aside: dict[str, str]) -> None:
    """Re-read the outbox and remove or update only the entries this pass handled.

    `fields` sets counters on an entry that stays (`attempts`, `soft`,
    `last_error`, `stuck_flushes`). Read again immediately before the rewrite,
    so a line queued meanwhile is kept; a line that is not a record is kept
    byte for byte for `_quarantine`. An entry set aside moves to the rejected
    file with its reason, once per id, never deleted (M003).
    """
    rejected = _rejected(box)
    already = {e.get("id") for e in _read_box(rejected)} if aside else set()
    keep: list[bytes] = []
    for line in _byte_lines(box):
        entry = _parse(line)
        if entry is None:
            keep.append(line)
            continue
        ident = entry["id"]
        if ident in posted:
            continue
        if ident not in fields and ident not in aside:
            keep.append(line)
            continue
        entry.update(fields.get(ident, {}))
        if ident in aside:
            if ident not in already:
                _append(rejected, {**entry, "reason": aside[ident]})
                already.add(ident)
            continue
        keep.append(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
    _write_lines(box, keep)


class _LostLock(Exception):
    """The lock no longer holds this pass's token: stop before touching the outbox or Lific."""


def _pass(box: Path, token: str, posted: set[str], handled: set[str], stats: dict,
          deadline: float | None = None, bounds: dict | None = None) -> str:
    """One pass over the outbox: `done`, `down`, or `stopped` (the deadline came).

    A refused record (by the send or by the status read) never stops the pass.
    Strikes (a missing issue, a refusal) are gathered as the pass goes and
    applied only at its end, and only when it did not end on `down` and at
    least one record was actually SENT in it (an earlier send found already
    landed does not count); otherwise they are dropped, because a pass that
    sent nothing cannot tell a bad record from a Lific refusing everything.
    Either way a failed entry keeps `fail_kind` and `last_error`, and
    `stuck_flushes` counts the flushes in a row it failed unstruck with that
    kind (refused, or `missing`). A status or comment read Lific does not
    answer stops the pass with the entry kept and no strike. A done or
    cancelled issue is set aside only for a record written at or after its
    close: the time Lific recorded the transition (`_close_time`), else its
    `updated_at`, taken from the first read in this flush that saw it closed
    (`bounds`, so this flush's own posts, which move `updated_at`, cannot
    widen it); one written earlier is posted.
    `handled` carries every
    id this flush dealt with, so a later pass never takes one twice; `stats`
    adds up the flush's sends for `_note_systemic`. With `deadline` (a
    `time.monotonic()` value) no Lific call is started after it; the entry
    under way is left as it was.
    """
    def settle(*args) -> None:
        if not _owns(box, token):
            raise _LostLock
        _settle(box, *args)

    def late() -> bool:
        # Checked before every Lific call, so a flush runs past its deadline
        # by one call at most (`LIFIC_TIMEOUT`), never by an entry's three.
        if deadline is not None and time.monotonic() >= deadline:
            print("dossier: flush deadline reached; the rest waits", file=sys.stderr)
            return True
        return False

    missing: dict[str, tuple[dict, int]] = {}
    refused: dict[str, tuple[dict, str]] = {}
    sent = False
    ending = "done"
    for entry in _read_box(box):
        rid, issue = entry["id"], entry["issue"]
        if rid in handled:
            continue
        if late():
            ending = "stopped"
            break
        _touch(box)
        state, status, updated, row = issue_state(issue)
        if state == "down":
            ending = "down"
            break
        if state == "missing":
            missing[rid] = (entry, entry.get("attempts", 0) + 1)
            handled.add(rid)
            continue
        if state == "refused":
            # Not counted for the systemic line: that one counts sends only.
            refused[rid] = (entry, status)
            handled.add(rid)
            continue
        if status in CLOSED and _entry_event(entry) not in CLOSED_EXEMPT:
            # The bound comes only from a read that saw the issue closed: the
            # close time Lific recorded, else `updated_at`, fixed for the flush.
            if bounds is not None and issue in bounds:
                bound = bounds[issue]
            else:
                bound = _close_time(row) or _lific_time(updated)
                if bounds is not None:
                    bounds[issue] = bound
            written = _record_time(entry)
            # Strictly after: `at` has whole seconds, so a record of the close's own
            # second may be from before it, and is posted rather than lost.
            if written is not None and bound is not None and written > bound:
                print(f"dossier: {issue} is {status}; {_entry_event(entry)} record written "
                      f"after it, moved to {_rejected(box).name}", file=sys.stderr)
                settle(set(), {}, {rid: f"issue {status}"})
                handled.add(rid)
                continue
        if late():
            ending = "stopped"
            break
        landed = _already_posted(issue, entry["content"])
        if landed is None:
            ending = "down"
            break
        if not landed:
            if late():
                ending = "stopped"
                break
            if not _owns(box, token):
                raise _LostLock
            outcome, why = _send(issue, entry["content"])
            if outcome == "down":
                ending = "down"
                break
            stats["attempted"] += 1
            if outcome == "missing":
                missing[rid] = (entry, entry.get("attempts", 0) + 1)
                handled.add(rid)
                continue
            if outcome == "refused":
                stats["refused"] += 1
                stats["first"] = stats["first"] or why
                refused[rid] = (entry, why)
                handled.add(rid)
                continue
            sent = True
            stats["sent"] += 1
        posted.add(rid)
        handled.add(rid)
        # Settle after every success, so an interrupted flush never re-posts.
        settle({rid}, {}, {})
    if not (missing or refused):
        return ending
    strike = ending != "down" and sent
    fields: dict[str, dict] = {}
    aside: dict[str, str] = {}

    def unstruck(entry: dict, kind: str, why: str) -> dict:
        # A new kind of failure starts the count again.
        same = entry.get("fail_kind") == kind
        return {"fail_kind": kind, "last_error": why,
                "stuck_flushes": (entry.get("stuck_flushes", 0) if same else 0) + 1}

    for rid, (entry, why) in refused.items():
        if strike:
            soft = entry.get("soft", 0) + 1
            fields[rid] = {"soft": soft, "fail_kind": "refused", "last_error": why, "stuck_flushes": 0}
            if soft >= MAX_SOFT_STRIKES:
                aside[rid] = f"refused: {why}"
        else:
            fields[rid] = unstruck(entry, "refused", why)
    for rid, (entry, n) in missing.items():
        if strike:
            fields[rid] = {"attempts": n, "fail_kind": "missing", "last_error": MISSING_ERROR,
                           "stuck_flushes": 0}
            if n >= MAX_REFUSALS:
                aside[rid] = "missing"
        else:
            fields[rid] = unstruck(entry, "missing", MISSING_ERROR)
    if not strike:
        print(f"dossier: {len(missing) + len(refused)} strike(s) dropped: the pass "
              f"{'ended with Lific down' if ending == 'down' else 'sent nothing'}", file=sys.stderr)
    if aside:
        print(f"dossier: {len(aside)} record(s) moved to {_rejected(box).name}", file=sys.stderr)
    if fields or aside:
        settle(set(), fields, aside)
    return ending


def _systemic(box: Path) -> Path:
    return box.with_name(box.name + ".systemic.json")


def _note_systemic(box: Path, stats: dict) -> None:
    """Count flushes in a row in which Lific refused every send. The caller holds the lock.

    A flush that sent anything, or that leaves the outbox empty, clears the
    count; one with at least `SYSTEMIC_REFUSALS` sends, every one refused,
    adds one. No record is ever set aside for it; /prime says it
    (`systemic_state`). Never raises.
    """
    path = _systemic(box)
    try:
        if stats["sent"] or not _read_box(box):
            if path.is_file():
                path.unlink()
            return
        if stats["attempted"] < SYSTEMIC_REFUSALS or stats["refused"] != stats["attempted"]:
            return
        count = systemic_state()["count"] + 1
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"count": count, "first": stats["first"]}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, path)
        print(f"dossier: Lific refused every record ({count} flush(es) in a row): "
              f"{stats['first']}", file=sys.stderr)
    except OSError as exc:
        print(f"dossier: systemic count not written ({exc})", file=sys.stderr)


def systemic_state() -> dict:
    """`{"count": n, "first": line}`: flushes in a row where Lific refused every send. Never raises."""
    try:
        data = json.loads(_systemic(_outbox()).read_text(encoding="utf-8"))
        count = data.get("count") if isinstance(data, dict) else None
        if type(count) is int and count >= 0:
            return {"count": count, "first": str(data.get("first", ""))}
    except FileNotFoundError:
        return {"count": 0, "first": ""}
    except (OSError, ValueError) as exc:
        print(f"dossier: systemic count unreadable ({type(exc).__name__})", file=sys.stderr)
    return {"count": 0, "first": ""}


def stuck_records() -> dict:
    """`{"count": n, "named": [[issue, last_error], ...]}` (at most 3 named): records refused
    unstruck for `STUCK_FLUSHES` flushes or more. For /prime; never sets one aside. Never raises."""
    try:
        stuck = [e for e in _read_box(_outbox()) if e.get("stuck_flushes", 0) >= STUCK_FLUSHES]
        return {"count": len(stuck),
                "named": [[e["issue"], str(e.get("last_error", ""))] for e in stuck[:3]]}
    except Exception as exc:  # noqa: BLE001 - a count must never fail /prime
        print(f"dossier: outbox unreadable ({type(exc).__name__}: {exc})", file=sys.stderr)
        return {"count": 0, "named": []}


#: Passes one flush makes at most: the first, one over spills merged after
#: it, and one more over spills that arrived during that one.
MAX_PASSES = 3


def flush(deadline: float | None = None) -> dict:
    """Send queued records once each, oldest first; keep what still waits. Never raises.

    Takes the outbox lock without waiting and SKIPS everything when another
    process holds it. The holder rotates the flusher log and refreshes the
    project keys, then passes over the outbox (`_pass`), checking the lock
    still holds its token before every send and every rewrite. Lines that are
    not records go to the corrupt file first. Spills are merged before the
    lock is released whatever way the pass ended; a pass that ended `done` is
    followed by another over what they brought, `MAX_PASSES` in all. With
    `deadline` (seconds from now) no new entry is started after it, and the
    lock is released as usual.
    """
    global _bot_missing_said
    _bot_missing_said = False
    until = time.monotonic() + deadline if deadline is not None else None
    try:
        box = _outbox()
        with _box_lock(box) as token:
            if not token:
                print("dossier: another flush holds the outbox; skipped", file=sys.stderr)
                return {"posted": 0, "left": outbox_count()}
            _cap_log(_flush_log(box))
            refresh_project_keys()
            if not box.is_file() and not _spills(box):
                if _systemic(box).is_file():
                    _note_systemic(box, {"sent": 0, "attempted": 0, "refused": 0, "first": ""})
                return {"posted": 0, "left": 0}
            posted: set[str] = set()
            handled: set[str] = set()
            stats = {"attempted": 0, "refused": 0, "sent": 0, "first": ""}
            bounds: dict[str, datetime | None] = {}
            lost = False
            try:
                passes = 0
                while passes < MAX_PASSES:
                    if passes and not _owns(box, token):
                        raise _LostLock
                    if not _merge_spills(box) and passes:
                        break
                    _quarantine(box)
                    passes += 1
                    if _pass(box, token, posted, handled, stats, until, bounds) != "done":
                        break
            except _LostLock:
                lost = True
                print("dossier: the outbox lock changed hands mid-pass; pass stopped",
                      file=sys.stderr)
            except (OSError, ValueError) as exc:
                print(f"dossier: outbox unreadable ({exc}); left as is", file=sys.stderr)
                return {"posted": len(posted), "left": -1}
            finally:
                # Whatever ended the pass, nothing waits in a spill file past this lock.
                if _owns(box, token):
                    try:
                        _merge_spills(box)
                    except OSError as exc:
                        print(f"dossier: spills not merged ({exc}); the next flush merges them",
                              file=sys.stderr)
            if not lost and _owns(box, token):
                _note_systemic(box, stats)
            return {"posted": len(posted), "left": outbox_count()}
    except Exception as exc:  # noqa: BLE001 - a flush runs inside other work; never fail it
        print(f"dossier: flush failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return {"posted": 0, "left": -1}


def _reason_kind(reason: str) -> str:
    if reason.startswith("refused"):
        return "refused"
    if reason.startswith("issue "):
        return "closed"
    return reason or "missing"


def _unseen(path: Path, entries: list) -> list:
    """The entries appended to `path` since the last call, and mark them seen. Never raises."""
    seen_file = path.with_name(path.name + ".seen")
    try:
        seen = int(seen_file.read_text().strip() or 0) if seen_file.is_file() else 0
    except (OSError, ValueError) as exc:
        print(f"dossier: {seen_file.name} unreadable ({exc}); counting from 0", file=sys.stderr)
        seen = 0
    if len(entries) != seen:
        tmp = seen_file.with_name(seen_file.name + ".tmp")
        tmp.write_text(str(len(entries)))
        os.replace(tmp, seen_file)
    return entries[seen:] if seen <= len(entries) else entries


def rejected_unseen() -> dict[str, int]:
    """Rejections since the last call, by reason (refused, missing, closed). For /prime. Never raises."""
    try:
        rejected = _rejected(_outbox())
        if not rejected.is_file():
            return {}
        counts: dict[str, int] = {}
        for entry in _unseen(rejected, _read_box(rejected)):
            kind = _reason_kind(str(entry.get("reason", "missing")))
            counts[kind] = counts.get(kind, 0) + 1
        return counts
    except Exception as exc:  # noqa: BLE001 - a count must never fail /prime
        print(f"dossier: rejected records unreadable ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return {}


def _dropped(box: Path) -> Path:
    return box.with_name(box.name.removesuffix(".jsonl") + ".dropped.jsonl")


def dropped_unseen() -> list[dict]:
    """Carried tasks dropped since the last call (HOS-259). For /prime. Never raises."""
    try:
        dropped = _dropped(_outbox())
        if not dropped.is_file():
            return []
        notes = []
        for line in _byte_lines(dropped):
            try:
                entry = json.loads(line.decode("utf-8"))
            except ValueError as exc:  # UnicodeDecodeError included: a torn line is not a note
                print(f"dossier: unreadable dropped-carry note skipped ({type(exc).__name__})",
                      file=sys.stderr)
                continue
            if isinstance(entry, dict) and entry.get("issue") and entry.get("path"):
                notes.append(entry)
        return _unseen(dropped, notes)
    except Exception as exc:  # noqa: BLE001 - a pointer must never fail /prime
        print(f"dossier: dropped-carry notes unreadable ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return []


def corrupt_unseen() -> int:
    """Unreadable outbox lines moved aside since the last call. For /prime. Never raises."""
    try:
        corrupt = _corrupt(_outbox())
        return len(_unseen(corrupt, _byte_lines(corrupt))) if corrupt.is_file() else 0
    except Exception as exc:  # noqa: BLE001 - a count must never fail /prime
        print(f"dossier: corrupt file unreadable ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 0


def outbox_count() -> int:
    try:
        box = _outbox()
        # Spill `.tmp` files count too: an orphaned one is merged only once it is old.
        files = ([box] if box.is_file() else []) + _spills(box)
        # errors="replace": one bad byte must never make /prime read 0.
        return sum(1 for f in files
                   for line in f.read_bytes().decode("utf-8", errors="replace").split("\n")
                   if line.strip())
    except Exception as exc:  # noqa: BLE001 - a count must never fail /prime or a hook
        print(f"dossier: outbox unreadable ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 0
