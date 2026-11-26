#!/usr/bin/env python3
"""Read-only view of the Lific tracker for /prime: the board, and the issues
whose work finished where no merge will ever close them.

WHY THIS EXISTS. Issues reach `done` two ways in this workspace. Most carry a
commit, so `closes HOS-7` in the merge message, fed to `lific git-hook` by
HELM (that one message, never a range; `.claude/rules/lific.md` item 6),
closes them as a side effect of the merge. The rest produce no commit at
all -- a decision, a piece of research, a document written into the data overlay
-- and nothing will ever close those. They would sit in `active` forever, and
nothing would notice. A yard that finishes one says so by opening its closing
comment with the marker below, and this script is what finds those comments.

IT ONLY READS. No status is changed here, by anyone. This is a detector in the
same sense `scripts/ops-radar.py` is one: it reports, the operator acts. That is
also what keeps it usable from a YARD, where changing an issue's status is
forbidden.

WHY THE MARKER IS MATCHED ON THE WHOLE FIRST LINE and never as a substring.
A comment discussing the marker would otherwise fire it, and this repository has
already shipped a gate whose pattern matched its own explanation. `.claude/rules/lific.md`
and `reference/lific-integration-detail.md` both print the marker in prose; both
would be live commands under a substring scan.

WHY IT DOES NOT USE `lific search`. MEASURED 2026-09-18 against a throwaway
project: search DOES index comment bodies (`result_type: "comment"`), correcting
an earlier reading of the FTS schema. But it matches TOKENS, not the phrase:
searching `DONE MOVED MIRIM` returned the same comment as the full marker, and
so did `decision not code`. A token match cannot decide this. Enumerating
`active` issues is exact and costs nothing, because `active` is by definition
the small set.

THE DOSSIER (HOS-256). `dossier HOS-N` assembles everything about one issue,
read-only, in this order of authority: files whose `Task:` header names it
(prompts, handoffs, briefs), the issue and its `[dossier]` records in Lific,
commits in both repositories that mention it, and older files that mention it
only as text ("possibly related"). Paths only, never full texts. When Lific
does not answer, the first line says the journal is missing and the rest is
built from files and git. `HEADING_OS_LIFIC_DB` points every `lific` call here
at another database file, which is how a verification run reads a copy.

Usage:
    python scripts/lific-board.py                  # every project
    python scripts/lific-board.py --project HOS    # one project
    python scripts/lific-board.py --json           # machine-readable
    python scripts/lific-board.py dossier HOS-9001 [--json]
    python scripts/lific-board.py wait HOS-9001 HOS-9002 [--interval 60]

THE WAIT AND THE "WAITING ON HELM" PANEL (2026-11-01). Both read each active
issue's NEWEST non-[dossier] comment by `seq` and report it when it asks for
HELM: a last line `branch <name> ready for merge`, an ESCALATE line, or the done
marker. `wait` blocks until that is true for one of the issues named, and
answers at once when it already is.

THE ANSWER STATE (HOS-401, 2026-11-20). A yard that posts a design or a
question waits for HELM's answer and carries none of those three lines; HOS-397
waited 40 minutes so. `answer_state` reads such an issue as `answer` when its
newest non-[dossier] comment is newer than its newest `brief` record AND the
yard that brief names is idle; a yard whose pane cannot be found reads `answer`
too, noted `yard pane unknown`, because a loud reading beats a silent one. The
idle half keeps a yard's "taken" comment, posted while it works, off the list.
`wait` reports EVERY issue that waits, never only the first.

Exit codes: 0 read cleanly, or `lific` is not installed (nothing to report).
2 when `lific` is present and a command failed -- never a silent "all clear".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: The whole first line of a comment, and nothing less, asks a human to close
#: the issue by hand. Compared case-insensitively after stripping surrounding
#: whitespace; everything after that first line is free prose.
DONE_REQUEST_MARKER = "MIRIM, THIS ISSUE MUST BE MOVED TO DONE"

#: Statuses an issue can hold, in the order this workspace walks them.
#: `backlog` is Mirim's inbox, `todo` is operator-approved and ready to hand to
#: a yard, `active` is allocated, `done` and `cancelled` are terminal.
STATUS_ORDER = ("backlog", "todo", "active", "done", "cancelled")

CMD_TIMEOUT = 20


class LificError(RuntimeError):
    """A `lific` invocation failed. Raised rather than swallowed: a reader that
    reports "nothing due" because it could not read is worse than no reader."""


def first_line_requests_done(content: str, marker: str = DONE_REQUEST_MARKER) -> bool:
    """True when the comment's first non-empty line IS the marker.

    Exact line equality, case-insensitive, whitespace-stripped. A comment that
    merely contains the phrase does not count, which is the whole point.
    """
    for line in (content or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return stripped.casefold() == marker.strip().casefold()
    return False


#: The last line of a yard's closing comment (`.claude/rules/lific.md` item 5).
READY_LINE = re.compile(r"^branch \S+ ready for merge\.?$", re.IGNORECASE)
#: A line a yard opens with ESCALATE, bare or numbered ("**3. ESCALATE.**").
ESCALATE_LINE = re.compile(r"^[\s*#\d.)-]*ESCALATE\b")


def newest_comment(comments: list) -> dict | None:
    """The newest comment that is not a `[dossier]` journal record, by `seq`.

    By `seq`, never by list position: `lific comment list` does not return
    comments newest-last, and taking the last row once missed a yard that had
    been ready for merge for an hour (HOS-311, MEASURED 2026-11-01: the row last
    in the list was seq 5207, the newest was seq 5215).
    """
    real = [c for c in comments if isinstance(c, dict)
            and not str(c.get("content", "")).startswith("[dossier]")]
    return max(real, key=lambda c: (c.get("seq") or 0, c.get("created_at") or ""), default=None)


def waiting_on_helm(content: str) -> str | None:
    """What a yard's comment asks of HELM: "done", "escalate", "ready", or None."""
    if first_line_requests_done(content):
        return "done"
    lines = [line.strip() for line in (content or "").splitlines() if line.strip()]
    if any(ESCALATE_LINE.match(line) for line in lines):
        return "escalate"
    if lines and READY_LINE.match(lines[-1]):
        return "ready"
    return None


#: Pane states that count as a yard waiting rather than working: the same set
#: as `PANE_PASSES` in `scripts/herdr-relay.py`.
YARD_IDLE = ("idle", "done")


def dossier_record(content: str) -> dict | None:
    """The header of a `[dossier]` journal record: its time, head and event; None otherwise."""
    first = next((ln.strip() for ln in (content or "").splitlines() if ln.strip()), "")
    m = _RECORD_HEAD.match(first)
    if not m:
        return None
    return {"when": m.group("when"), "head": m.group("rest"),
            "event": m.group("rest").split(" · ")[0]}


def answer_state(comments: list, yard_idle) -> dict | None:
    """`answer` when the newest non-[dossier] comment follows the newest brief and its yard is idle.

    `yard_idle(name)` answers True, False or None (unknown) for the yard the
    newest brief record names (name None when there is no brief or no `Yard:`
    line). A working yard (False) is not waiting; unknown is, with a note.
    Consulted only where `waiting_on_helm()` returned None for the newest comment.
    """
    newest = newest_comment(comments)
    if newest is None:
        return None
    briefs = [c for c in comments if isinstance(c, dict)
              and (dossier_record(str(c.get("content", ""))) or {}).get("event") == "brief"]
    brief = max(briefs, key=lambda c: c.get("seq") or 0, default=None)
    if brief is not None and (newest.get("seq") or 0) <= (brief.get("seq") or 0):
        return None
    yard = None
    for line in str((brief or {}).get("content", "")).splitlines():
        if line.strip().startswith("Yard:"):
            yard = line.strip()[len("Yard:"):].strip() or None
            break
    idle = yard_idle(yard)
    if idle is False:
        return None
    return {"state": "answer", "note": None if idle else "yard pane unknown", "comment": newest}


def _core_terminals() -> list | None:
    """The raw herdr-core terminal records, read only under herdr-core; None otherwise or on failure.

    Asked only with `HC_TERMINAL_ID` set and `hc` on PATH, the condition under
    which `herdr_agent.agents()` itself already asks herdr-core: this process
    runs in an hc terminal, so a server is up. They span every repository;
    callers take `core_yard_records()`, never these.
    """
    from scripts.utils import herdr_agent as HA
    if not (os.environ.get("HC_TERMINAL_ID", "").strip() and shutil.which(HA.HC_BIN)):
        return None
    try:
        return HA._core_agents()
    except (HA.HerdrUnavailable, HA.PaneGone) as exc:
        print(f"lific-board: herdr-core terminals not listed: {exc}", file=sys.stderr)
        return None


def repo_yard_records(records: list, repo_root: Path | None = None) -> dict:
    """herdr-core records of THIS repository's yards, grouped by yard checkout.

    herdr-core lists the terminals of every repository, so a checkout of
    another repository with the same folder name would otherwise lend its
    status to a yard here (HOS-401 review round 2). A record's cwd counts only
    when it passes `trusted_git_dir` (no git runs in it before that), is a
    linked worktree, is its own toplevel, and its git common dir's parent is
    `repo_root` (default: this checkout's repository root).
    """
    if not records:
        return {}
    from scripts.utils import statusline_cache as SC
    if repo_root is None:
        common = SC._git_path(SC._ROOT, "--git-common-dir")
        if not common:
            return {}
        repo_root = Path(common).resolve().parent
    by_checkout: dict = {}
    for record in records or []:
        if not isinstance(record, dict) or not record.get("cwd"):
            continue
        try:
            checkout = Path(str(record["cwd"])).resolve()
        except OSError as exc:
            print(f"lific-board: terminal {record.get('pane_id')}: cwd unreadable: {exc}",
                  file=sys.stderr)
            continue
        by_checkout.setdefault(checkout, []).append(record)
    mine: dict = {}
    for checkout, rows in by_checkout.items():
        if not SC.trusted_git_dir(checkout) or not SC._in_yard(checkout):
            continue
        top = SC._git_path(checkout, "--show-toplevel")
        common = SC._git_path(checkout, "--git-common-dir")
        if not top or not common or Path(top).resolve() != checkout:
            continue
        if Path(common).resolve().parent != repo_root:
            continue
        mine[checkout] = rows
    return mine


def core_yard_records(repo_root: Path | None = None) -> dict | None:
    """`repo_yard_records` of the herdr-core terminals; None when they cannot be read.

    The one shared lookup of herdr-core yard records, used by
    `herdr_yard_idle()` and the status line's `yards()` when the TUI listing fails.
    """
    records = _core_terminals()
    return None if records is None else repo_yard_records(records, repo_root)


def herdr_yard_idle():
    """A `yard_idle` for `answer_state` over the live Herdr agents, listed once per checker.

    A yard's records are those whose cwd's directory name, or workspace id, is
    the yard's name; idle when every one is in YARD_IDLE. No name, no matching
    record or Herdr unavailable is unknown (None).

    `agents()` raises when `herdr` is on PATH with no TUI server, before it asks
    herdr-core, and it promises never to hide that TUI error, so it stays as it
    is. Under herdr-core (`HC_TERMINAL_ID` set, `hc` on PATH) this falls back to
    `core_yard_records()`, this repository's yard terminals, whose `cwd` is the
    yard checkout. Without
    the fallback every issue read `answer (yard pane unknown)` on a herdr-core
    machine, a working yard's "taken" comment included (HOS-401, MEASURED
    2026-11-20: `herdr agent list` answered server_not_running).
    """
    from scripts.utils import herdr_agent as HA
    cache: dict = {}

    def listed() -> list | None:
        try:
            found = HA.agents()
        except HA.HerdrUnavailable as exc:
            print(f"lific-board: herdr agents not listed: {exc}", file=sys.stderr)
            core = core_yard_records()
            return None if core is None else [r for rows in core.values() for r in rows]
        # agents() may append herdr-core records of every repository: keep the
        # TUI ones as they are and only this repository's yard records of the rest.
        tui = [r for r in found if isinstance(r, dict)
               and HA.backend_for(str(r.get("pane_id") or "")) == "tui"]
        core = [r for r in found if isinstance(r, dict) and r not in tui]
        return tui + [r for rows in repo_yard_records(core).values() for r in rows]

    def idle(name: str | None) -> bool | None:
        if not name:
            return None
        if "records" not in cache:
            cache["records"] = listed()
        records = cache["records"]
        if records is None:
            return None
        mine = [r for r in records if isinstance(r, dict)
                and (Path(str(r.get("cwd") or "")).name == name or r.get("workspace_id") == name)]
        if not mine:
            return None
        return all(r.get("agent_status") in YARD_IDLE for r in mine)

    return idle


def wait_label(comments: list, yard_idle) -> str | None:
    """What an issue's newest comment asks of HELM, as `wait` prints it; None when nothing."""
    newest = newest_comment(comments)
    if newest is None:
        return None
    state = waiting_on_helm(newest.get("content", ""))
    if state is not None:
        return state
    found = answer_state(comments, yard_idle)
    if found is None:
        return None
    return f"answer ({found['note']})" if found["note"] else "answer"


def _run_lific(args: list[str]) -> object:
    """Run one `lific` subcommand with `--json` and parse its stdout.

    List form, never a shell string. Raises LificError on a non-zero exit or on
    output that is not JSON, so the caller cannot mistake a failure for silence.
    """
    db = os.environ.get("HEADING_OS_LIFIC_DB", "").strip()
    proc = subprocess.run(
        ["lific", *args, *(["--db", db] if db else []), "--json"],
        capture_output=True,
        text=True,
        timeout=CMD_TIMEOUT,
    )
    if proc.returncode != 0:
        raise LificError(
            f"lific {' '.join(args)} exited {proc.returncode}: "
            f"{proc.stderr.strip() or '(no stderr)'}"
        )
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise LificError(
            f"lific {' '.join(args)} returned output that is not JSON: {exc}"
        ) from exc


def collect(project: str | None = None) -> dict:
    """Read the board and the pending close-requests.

    Shapes are the ones MEASURED 2026-09-18 against lific 2.9.0: `project list`
    yields objects carrying `identifier` and `name`; `issue list` yields
    `identifier`, `title`, `status`, `priority`; `comment list` yields
    `content`, `author`, `created_at`.
    """
    projects = _run_lific(["project", "list"])
    if project:
        projects = [p for p in projects if p.get("identifier") == project]
        if not projects:
            raise LificError(f"no project with identifier {project!r}")

    board: list[dict] = []
    close_requests: list[dict] = []
    waiting: list[dict] = []
    yard_idle = herdr_yard_idle()

    for proj in projects:
        ident = proj.get("identifier")
        if not ident:
            raise LificError(f"project row with no identifier: {proj!r}")
        issues = _run_lific(["issue", "list", "--project", ident, "--limit", "500"])
        counts = dict.fromkeys(STATUS_ORDER, 0)
        for issue in issues:
            status = issue.get("status")
            counts[status] = counts.get(status, 0) + 1
            if status != "active":
                continue
            # Every page: [dossier] records must never push a done marker past the newest 50.
            comments = _all_comments(issue["identifier"])
            newest = newest_comment(comments)
            state = waiting_on_helm(newest.get("content", "")) if newest else None
            note = None
            if state is None:
                found = answer_state(comments, yard_idle)
                if found is not None:
                    state, note = "answer", found["note"]
            if state in ("ready", "escalate", "answer"):
                waiting.append({
                    "project": ident,
                    "identifier": issue["identifier"],
                    "title": issue.get("title", ""),
                    "state": state,
                    "note": note,
                    "seq": newest.get("seq"),
                    "at": newest.get("created_at", ""),
                })
            for comment in comments:
                if not first_line_requests_done(comment.get("content", "")):
                    continue
                close_requests.append({
                    "project": ident,
                    "identifier": issue["identifier"],
                    "title": issue.get("title", ""),
                    "author": comment.get("author", "unknown"),
                    "at": comment.get("created_at", ""),
                })
                break
        board.append({
            "project": ident,
            "name": proj.get("name", ""),
            "counts": counts,
            "total": len(issues),
        })

    return {"board": board, "close_requests": close_requests, "waiting_on_helm": waiting}


def render_text(data: dict) -> str:
    """Render for /prime. Empty output means there is nothing to say.

    Quiet when every project is idle: a project whose issues are all `done`,
    `cancelled` or `backlog` has nothing waiting on anyone, so it prints
    nothing rather than a line the reader learns to skip.
    """
    lines: list[str] = []
    for row in data["board"]:
        counts = row["counts"]
        waiting = counts.get("active", 0) + counts.get("todo", 0)
        if not waiting:
            continue
        lines.append(
            f"{row['project']}: {counts.get('active', 0)} active, "
            f"{counts.get('todo', 0)} ready to hand out, "
            f"{counts.get('backlog', 0)} in backlog."
        )

    requests = data["close_requests"]
    if requests:
        lines.append("")
        lines.append("Finished, and no merge will close these. Close them by hand:")
        for req in requests:
            lines.append(f"  {req['identifier']}  {req['title']}")
            lines.append(f"      asked by {req['author']} at {req['at']}")
        lines.append("  Close with: lific issue update <ID> --status done")

    waiting = data.get("waiting_on_helm", [])
    if waiting:
        lines.append("")
        lines.append("Waiting on HELM (the newest comment of an active issue):")
        for row in waiting:
            what = {"ready": "ready for merge", "escalate": "ESCALATE",
                    "answer": "ANSWER waits for HELM's answer"}.get(row["state"], str(row["state"]))
            line = f"  {row['identifier']}  {what}  (comment seq {row['seq']}, {row['at']})"
            if row.get("note"):
                line += f" ({row['note']})"
            lines.append(line)

    return "\n".join(lines)


# ============================================================
# The dossier of one issue
# ============================================================

#: The kinds of file that carry a `Task:` header.
DOSSIER_KINDS = ("prompt", "handoff", "brief")
#: Test-shaped handoffs of 2026-08-23..29, kept (M003) and never "possibly related".
_TEST_SHAPED = re.compile(r"^2026-08-(2[3-9])-\d{6}_handoff_compact-unknown_")
_HEADER_TIME = ("Saved:", "Generated:", "Sent:")
_RECORD_HEAD = re.compile(r"^\[dossier\] (?P<when>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC · (?P<rest>.*)$")


def _parse_time(value: str) -> datetime | None:
    value = value.strip().replace("Z", "+00:00")
    for parse in (datetime.fromisoformat,
                  lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc),
                  lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)):
        try:
            when = parse(value)
            # Every event in UTC, so a `+04:00` header and a UTC record compare and print alike.
            return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _file_time(path: Path, head: list[str]) -> datetime:
    for line in head:
        if line.startswith(_HEADER_TIME):
            when = _parse_time(line.split(":", 1)[1])
            if when:
                return when
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)


def _scan_files(ident: str) -> tuple[list[dict], list[str]]:
    """(files whose `Task:` names `ident`, files that only mention it), from the three archives."""
    from scripts.utils import dossier
    from scripts.utils.data_path import data_path
    word = re.compile(rf"\b{re.escape(ident)}\b")
    tasked, related = [], []
    try:
        archives = (("prompt", data_path("prompt-archive")),
                    ("handoff", data_path("handoff-archive")),
                    ("brief", data_path("brief-archive")))
    except Exception as exc:  # noqa: BLE001 - reported, and the dossier still prints Lific and git
        print(f"lific-board: the archives are unresolvable ({exc})", file=sys.stderr)
        archives = ()
    for event, directory in archives:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(f"lific-board: cannot read {path.name}: {exc}", file=sys.stderr)
                continue
            head = text.splitlines()[:12]
            if ident in dossier.header_tasks("\n".join(head)):
                tasked.append({"event": event, "path": dossier.rel(path),
                               "at": _file_time(path, head), "text": text})
            elif word.search(text) and not _TEST_SHAPED.match(path.name):
                related.append(dossier.rel(path))
    return tasked, related


def _commits(ident: str) -> list[str]:
    from scripts.utils.paths import get_data_root
    word = re.compile(rf"\b{re.escape(ident)}\b")
    out = []
    for label, repo in (("engine", Path(__file__).resolve().parent.parent), ("data", get_data_root())):
        try:
            proc = subprocess.run(  # nosec B603 B607 - fixed argv, read-only
                ["git", "-C", str(repo), "log", "--all", "--fixed-strings", f"--grep={ident}",
                 "--format=%h %cs %s%x00%B%x1e"],
                capture_output=True, text=True, timeout=CMD_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"lific-board: git log in {label} failed ({exc})", file=sys.stderr)
            continue
        for chunk in proc.stdout.split("\x1e"):
            line, _, body = chunk.strip().partition("\x00")
            if line and word.search(body):
                out.append(f"{label} {line}")
    return out


#: `lific comment list` returns 50 by default, newest first, and at most 500
#: per call; `--offset` reads the next page (MEASURED 2026-10-24 on a copy:
#: 120 comments, 50 without `--limit`, all 120 with `--limit 500`).
COMMENT_PAGE = 500


def _all_comments(ident: str) -> list:
    """Every comment of an issue, page by page, so an old journal record is never cut off."""
    rows: list = []
    seen: set = set()
    while True:
        page = _run_lific(["comment", "list", ident, "--limit", str(COMMENT_PAGE),
                           "--offset", str(len(rows))])
        if not isinstance(page, list):
            raise LificError(f"lific comment list {ident} returned {type(page).__name__}")
        new = [c for c in page if not (isinstance(c, dict) and c.get("id") in seen)]
        seen.update(c.get("id") for c in new if isinstance(c, dict))
        rows.extend(new)
        # A page that brings nothing new ends the read even if Lific ignored the offset.
        if len(page) < COMMENT_PAGE or not new:
            return rows


def collect_dossier(ident: str) -> dict:
    """Everything about one issue, read-only. Lific failing is a field, never an exception."""
    tasked, related = _scan_files(ident)
    issue, records, comments, lific_error = None, [], 0, None
    try:
        issue = _run_lific(["issue", "get", ident])
        for comment in _all_comments(ident):
            content = comment.get("content", "") or ""
            head = dossier_record(content)
            if not head:
                comments += 1
                continue
            lines = content.strip().splitlines()
            path = lines[-1][2:].strip() if lines[-1].startswith("→ ") else ""
            records.append({"at": _parse_time(head["when"]), "head": head["head"],
                            "event": head["event"],
                            "summary": [ln for ln in lines[1:] if not ln.startswith("→ ")],
                            "path": path})
    except (LificError, subprocess.TimeoutExpired, OSError) as exc:
        lific_error = str(exc)

    # A list per (event, path): one file can carry several records (a relink,
    # a re-post), and each must stay visible. A file takes its oldest unmatched
    # record; every other record is a chronology line of its own.
    by_key: dict[tuple[str, str], list[dict]] = {}
    for rec in sorted(records, key=lambda r: r["at"] or datetime.min.replace(tzinfo=timezone.utc)):
        if rec["path"]:
            by_key.setdefault((rec["event"], rec["path"]), []).append(rec)
    matched: set[int] = set()
    events = []
    for f in tasked:
        waiting = by_key.get((f["event"], f["path"])) or []
        rec = waiting.pop(0) if waiting else None
        if rec is not None:
            matched.add(id(rec))
        events.append({"at": f["at"], "event": f["event"], "path": f["path"],
                       "head": rec["head"] if rec else f["event"], "journal": rec is not None,
                       "summary": rec["summary"] if rec else [], "text": f["text"]})
    for rec in records:
        if id(rec) not in matched:
            events.append({"at": rec["at"], "event": rec["event"],
                           "path": rec["path"], "head": rec["head"], "journal": True,
                           "summary": rec["summary"], "text": ""})
    events.sort(key=lambda e: e["at"] or datetime.min.replace(tzinfo=timezone.utc))

    now, awaiting = "", ""
    from scripts.utils import checkpoint_paths as CP
    for e in reversed(events):
        if not now and e["event"] in ("handoff", "close"):
            lines = (CP.safe_resume_lines(e["text"]) if e["text"] else []) or e["summary"]
            now = lines[0] if lines else ""
        if not awaiting and e["event"] == "handoff" and e["text"]:
            for line in CP.safe_resume_lines(e["text"]):
                if line.startswith("Awaiting approval:"):
                    awaiting = line.split(":", 1)[1].strip()
        if now and awaiting:
            break
    counts = {kind: sum(1 for f in tasked if f["event"] == kind) for kind in DOSSIER_KINDS}
    from scripts.utils.paths import get_data_root
    return {"identifier": ident, "data_root": str(get_data_root()), "issue": issue, "lific_error": lific_error, "now": now,
            "awaiting": awaiting, "events": events, "counts": counts,
            "other_comments": comments, "commits": _commits(ident), "related": related}


def render_dossier(data: dict) -> str:
    lines = []
    if data["lific_error"]:
        lines.append(f"Lific did not answer; journal missing ({data['lific_error'][:160]})")
    issue = data["issue"] or {}
    lines.append(f"{data['identifier']}  {issue.get('title', '(title unknown)')}  "
                 f"[{issue.get('status', 'status unknown')}]")
    lines.append(f"Now: {data['now'] or 'no handoff or close yet'}")
    lines.append(f"Awaiting approval: {data['awaiting'] or 'nothing recorded'}")
    lines.append("")
    lines.append(f"Chronology (paths relative to {data['data_root']}):")
    if not data["events"]:
        lines.append("  (no file and no record names this issue)")
    for e in data["events"]:
        when = (e["at"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                if e["at"] else "time unknown")
        mark = "" if e["journal"] or data["lific_error"] else "  (no journal record)"
        lines.append(f"  {when}  {e['head']}{mark}")
        if e["path"]:
            lines.append(f"      {e['path']}")
    c = data["counts"]
    lines.append("")
    lines.append(f"Full texts: {c['prompt']} prompt(s), {c['handoff']} handoff(s), "
                 f"{c['brief']} brief(s); {data['other_comments']} other comment(s) in Lific.")
    lines.append("")
    lines.append("Commits:" if data["commits"] else "Commits: none mention it.")
    lines.extend(f"  {line}" for line in data["commits"])
    if data["related"]:
        lines.append("")
        lines.append("Possibly related (mention it as text, no Task: header):")
        lines.extend(f"  {path}" for path in data["related"])
    return "\n".join(lines)


def main_dossier(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="lific-board.py dossier",
                                     description="Everything about one Lific issue, read-only.")
    parser.add_argument("identifier", help="the issue, e.g. HOS-9001")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Z]{2,5}-\d+", args.identifier):
        print(f"lific-board: {args.identifier!r} is not an issue id", file=sys.stderr)
        return 2
    if shutil.which("lific") is None:
        print("lific-board: lific is not installed; the journal is not read", file=sys.stderr)
    data = collect_dossier(args.identifier)
    if args.as_json:
        for e in data["events"]:
            e.pop("text", None)
            e["at"] = e["at"].isoformat() if e["at"] else None
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(render_dossier(data))
    return 0


def _print_hit(ident: str, comment: dict, what: str) -> None:
    print(f"{ident} seq {comment.get('seq')} {comment.get('created_at', '')}: {what}")
    print(str(comment.get("content", ""))[-600:])


def main_wait(argv: list[str]) -> int:
    """Block until the issues need HELM, then print EVERY one that does and exit 0.

    It answers AT ONCE when an issue's newest comment already asks for HELM
    (ready for merge, ESCALATE, the done marker, or `answer_state`), so a
    closing comment posted before the wait began is never missed; that was the
    defect of the scratch watcher this replaces (HOS-311, 2026-11-01). Otherwise
    it polls, and at the end of the first round that finds any newer
    non-[dossier] comment it prints every such issue, not only the first: a
    second issue's comment in the same window used to become the next baseline
    unseen (HOS-401, 2026-11-20). `--seen HOS-N=SEQ` marks a comment HELM has
    already acted on: that issue then waits for a comment newer than SEQ.
    """
    parser = argparse.ArgumentParser(prog="lific-board.py wait",
                                     description="Wait until a yard's issue needs HELM.")
    parser.add_argument("identifiers", nargs="+", help="the issues, e.g. HOS-9001 HOS-9002")
    parser.add_argument("--interval", type=float, default=60.0, help="seconds between reads (default 60)")
    parser.add_argument("--seen", action="append", default=[], metavar="HOS-N=SEQ",
                        help="a comment HELM already acted on; repeatable")
    args = parser.parse_args(argv)
    bad = [i for i in args.identifiers if not re.fullmatch(r"[A-Z]{2,5}-\d+", i)]
    seen: dict[str, int] = {}
    for item in args.seen:
        m = re.fullmatch(r"([A-Z]{2,5}-\d+)=(\d+)", item)
        if not m:
            bad.append(item)
            continue
        seen[m.group(1)] = int(m.group(2))
    if bad:
        print(f"lific-board: not an issue id or HOS-N=SEQ: {', '.join(bad)}", file=sys.stderr)
        return 2
    base: dict[str, int] = {}
    hits = 0
    try:
        yard_idle = herdr_yard_idle()
        for ident in args.identifiers:
            comments = _all_comments(ident)
            newest = newest_comment(comments)
            seq = (newest or {}).get("seq") or 0
            label = wait_label(comments, yard_idle)
            if label and seq > seen.get(ident, -1):
                _print_hit(ident, newest, f"already waiting on HELM ({label})")
                hits += 1
            base[ident] = max(seq, seen.get(ident, 0))
    except (LificError, subprocess.TimeoutExpired) as exc:
        print(f"lific-board: {exc}", file=sys.stderr)
        return 2
    if hits:
        return 0
    while True:
        time.sleep(args.interval)
        yard_idle = herdr_yard_idle()
        for ident in args.identifiers:
            try:
                comments = _all_comments(ident)
            except (LificError, subprocess.TimeoutExpired) as exc:
                print(f"lific-board: {ident} not read this round: {exc}", file=sys.stderr)
                continue
            newest = newest_comment(comments)
            if newest and (newest.get("seq") or 0) > base[ident]:
                _print_hit(ident, newest, wait_label(comments, yard_idle) or "new comment")
                hits += 1
        if hits:
            return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["dossier"]:
        return main_dossier(argv[1:])
    if argv[:1] == ["wait"]:
        return main_wait(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", help="Limit to one project identifier, e.g. HOS")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Machine-readable output")
    args = parser.parse_args(argv)

    if shutil.which("lific") is None:
        # Not an error: a clone without Lific installed has no tracker to read.
        if args.as_json:
            print(json.dumps({"installed": False, "board": [], "close_requests": []}))
        return 0

    try:
        data = collect(args.project)
    except (LificError, subprocess.TimeoutExpired) as exc:
        print(f"lific-board: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps({"installed": True, **data}, indent=2, ensure_ascii=False))
    else:
        text = render_text(data)
        if text:
            print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
