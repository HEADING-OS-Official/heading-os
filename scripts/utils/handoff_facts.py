#!/usr/bin/env python3
"""The handoff facts a program can read from their records (HOS-387).

One writer for two handoffs. The SessionStart hook's mechanical `/clear`
handoff (`.claude/hooks/checkpoint-inject.py`) takes `git_status` and
`files_written_block` from here, and `scripts/checkpoint-paths.py --write`
takes `records_section` when `CLAUDE_HANDOFF_ASSEMBLED` is on; `--facts` prints
the same section and writes nothing.

Every fact has a fixed line for a source that did not answer, so silence never
reads as "nothing exists". The section is held to SECTION_LINES output lines,
cut in a fixed order (files written, then git status, then the oldest shown
operator messages). The caps are HELM's decision of 2026-11-19 (HOS-387, Phase 2
brief, correction 3).

`CP` functions are read as module attributes at call time, never imported by
name, so a caller that replaces one (`CP.utc_now`, `CP.session_id`) is obeyed.

Usage (library):
    from scripts.utils import handoff_facts as HF
    text = HF.records_section(kind, model_text, state, transcript, project)
"""
from __future__ import annotations

import importlib.util
import re
import subprocess  # nosec B404 - fixed argv, never shell=True
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.utils import checkpoint_paths as CP  # noqa: E402

# ============================================================
# Configuration
# ============================================================

MAX_WRITTEN = 25
SECTION_LINES = 60
OPERATOR_SHOWN = 15
OPERATOR_CHARS = 200
FILES_SHOWN = 10
GIT_SHOWN = 10
BRIEFS_SHOWN = 8
BRIEF_HEADER_LINES = 8
REVIEW_SUMMARY_CHARS = 160
#: Chosen, not measured (design note, § 1): the whole section's wall-clock budget.
BUDGET_SECONDS = 20.0

NOT_READ_BUDGET = "not read (time budget spent)"
# A relayed HELM brief reaches the transcript as a typed message: marked, so the
# next session does not read it as the operator's words (HELM, 2026-11-19).
BRIEF_MARKER = "X-HEADING-BRIEF:"
BRIEF_PREFIX = "[brief from HELM] "
# A background-task notification arrives the same way (HELM, 2026-11-19).
NOTIFICATION_TAG = "<task-notification>"
NOTIFICATION_PREFIX = "[system notification] "
_HELPER_MARKER = re.compile(r"^\[\.\.\. \d+ more")


# ============================================================
# The two helpers shared with the /clear mechanical handoff
# ============================================================

def git_status(project: Path, limit: int = 30) -> str:
    """`git status --short` of `project`, cut to `limit` lines with a marker."""
    try:
        proc = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["git", "status", "--short"], cwd=str(project),
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"(git status unavailable: {exc})"
    if proc.returncode != 0:
        # Not a repository, dubious ownership: empty stdout is not "(clean)".
        detail = (proc.stderr or "").strip().splitlines()
        return f"(git status unavailable: {detail[0] if detail else f'exit {proc.returncode}'})"
    lines = proc.stdout.rstrip().splitlines()
    if len(lines) > limit:
        lines = lines[:limit] + [f"[... {len(lines) - limit} more line(s)]"]
    return "\n".join(lines) or "(clean)"


def files_written_block(transcript: str | Path | None, limit: int = MAX_WRITTEN) -> str:
    """The files the session wrote that still exist, newest first, cut to `limit`."""
    try:
        from scripts.utils.session_scope import files_written
    except Exception as exc:  # noqa: BLE001 - an absent fact, not a failure
        return f"(unavailable: {exc})"
    paths = files_written(transcript)
    if paths is None:
        return "(the transcript could not be read)"
    live = sorted((p for p in paths if p.exists()),
                  key=lambda p: -p.stat().st_mtime)
    if not live:
        return "(none recorded)"
    shown = [p.as_posix() for p in live[:limit]]
    if len(live) > limit:
        shown.append(f"[... {len(live) - limit} more]")
    return "\n".join(shown)


# ============================================================
# Loading the scripts that own a fact (by path, never moved)
# ============================================================

def _load(name: str):
    """`scripts/<name>.py` as a module, loaded by path (its name is hyphenated)."""
    path = _ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"handoff_facts_{name.replace('-', '_')}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _say(what: str, exc: BaseException) -> str:
    print(f"handoff_facts: {what}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return f"{type(exc).__name__}: {exc}"


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


# ============================================================
# The facts, one function each; each returns its lines
# ============================================================

def _briefs(session: str) -> tuple[list[dict], str | None]:
    """This session's briefs from `brief-archive`, oldest first, or (empty, why not)."""
    from scripts.utils.data_path import data_path
    try:
        directory = data_path("brief-archive")
        names = sorted(p for p in directory.glob("*_brief_*.md"))
    except Exception as exc:  # noqa: BLE001 - reported as the fallback line
        return [], _say("brief archive", exc)
    found = []
    for path in names:
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                head = [next(handle, "") for _ in range(BRIEF_HEADER_LINES)]
        except OSError as exc:
            _say(f"brief {path.name}", exc)
            continue
        fields = {}
        for line in head:
            key, sep, value = line.rstrip("\n").partition(": ")
            if sep:
                fields[key] = value.strip()
        if fields.get("Session") == session:
            fields["name"] = path.name
            found.append(fields)
    return found, None


def _brief_lines(briefs: list[dict], why: str | None) -> list[str]:
    if why is not None:
        return [f"brief archive not read ({why})"]
    if not briefs:
        return ["No brief was sent by this session."]
    shown = briefs[-BRIEFS_SHOWN:]
    lines = [f"- {b['name']}: sent {b.get('Sent', '?')}, pane {b.get('Pane', '?')}, "
             f"branch {b.get('Branch', '?')}, {b.get('Task', 'Task: none')}" for b in shown]
    if len(briefs) > len(shown):
        lines.insert(0, f"[... {len(briefs) - len(shown)} more]")
    return lines


def _issue_ids(state: dict, model_text: str, briefs: list[dict]) -> list[str]:
    from scripts.utils import dossier
    stored = state.get(dossier.STATE_KEY) if isinstance(state, dict) else None
    ids = [t for t in stored if isinstance(t, str)] if isinstance(stored, list) else []
    for ident in dossier.record_ids(model_text or ""):
        if ident not in ids:
            ids.append(ident)
    for brief in briefs:
        for ident in dossier.record_ids(brief.get("Task", "").replace("Task:", "", 1)):
            if ident not in ids:
                ids.append(ident)
    return ids


def _issue_lines(ids: list[str], deadline: float) -> list[str]:
    if not ids:
        return ["No Lific issue is named by this session."]
    try:
        board = _load("lific-board")
    except Exception as exc:  # noqa: BLE001 - every id gets the fallback line
        why = _say("lific-board not loaded", exc)
        return [f"- {i}: status not read ({why})" for i in ids]
    lines = []
    for ident in ids:
        if time.monotonic() > deadline:
            lines.append(f"- {ident}: {NOT_READ_BUDGET}")
            continue
        try:
            record = board._run_lific(["issue", "get", ident])
        except (SystemExit, Exception) as exc:  # noqa: BLE001 - LificError included
            lines.append(f"- {ident}: status not read ({_say(f'issue {ident}', exc)})")
            continue
        if not isinstance(record, dict):
            lines.append(f"- {ident}: status not read (no issue object)")
            continue
        lines.append(f"- {ident} {record.get('status') or '?'}: "
                     f"{_one_line(record.get('title') or '', 120)}")
    return lines


def _terminals() -> tuple[dict[str, str], str | None]:
    """{resolved cwd: terminal address} from herdr, or (what was found, why not).

    Each backend on its own, as `herdr_agent.agents()` chooses them (herdr-core
    only under herdr-core or without the TUI, so a hook never starts a server),
    because `agents()` raises on a TUI error before it asks herdr-core: MEASURED
    2026-11-19 in a herdr-core yard with no TUI server running. "Why not" is set
    whenever a backend asked failed, so an unmatched yard never reads "none".
    """
    import os
    import shutil
    try:
        from scripts.utils import herdr_agent as HA
    except Exception as exc:  # noqa: BLE001 - reported as the fallback line
        return {}, _say("herdr_agent", exc)
    have_tui = shutil.which(HA.HERDR_BIN) is not None
    have_core = shutil.which(HA.HC_BIN) is not None
    asks = []
    if have_tui:
        asks.append(("herdr", HA._tui_agents))
    if have_core and (os.environ.get("HC_TERMINAL_ID", "").strip() or not have_tui):
        asks.append(("hc", HA._core_agents))
    if not asks:
        return {}, "neither herdr nor hc is on PATH"
    records, errors = [], []
    for name, ask in asks:
        try:
            records += ask()
        except (SystemExit, Exception) as exc:  # noqa: BLE001 - HerdrUnavailable included
            errors.append(f"{name}: {_one_line(_say(f'{name} terminals', exc), 100)}")
    found = {}
    for rec in records:
        address = rec.get("pane_id")
        for key in ("cwd", "foreground_cwd"):
            where = rec.get(key)
            if isinstance(where, str) and where and isinstance(address, str):
                try:
                    found.setdefault(str(Path(where).resolve()), address)
                except OSError as exc:
                    _say(f"terminal {address} cwd", exc)
                    continue
    return found, ("; ".join(errors) or None)


def _yard_lines(project: Path, briefed: set[str], deadline: float) -> tuple[list[str], list[str]]:
    """(lines, branches) for every yard of this repository."""
    if time.monotonic() > deadline:
        return [f"yards {NOT_READ_BUDGET}"], []
    try:
        from scripts.utils import yard_status
        result = yard_status.sweep(project)
    except (SystemExit, Exception) as exc:  # noqa: BLE001 - reported as the fallback line
        return [f"yards not read ({_say('yard sweep', exc)})"], []
    if result.get("note"):
        return [f"yards not read ({result['note']})"], []
    yards = result.get("yards") or []
    if not yards:
        return ["No yard exists."], []
    terminals, why = _terminals()
    lines, branches = [], []
    for yard in yards:
        branch = yard.get("branch") or "?"
        if yard.get("branch"):
            branches.append(yard["branch"])
        try:
            where = str(Path(yard["path"]).resolve())
        except (OSError, KeyError):
            where = str(yard.get("path"))
        terminal = terminals.get(where) or (f"not read ({why})" if why else "none")
        agent = {True: "yes", False: "no"}.get(yard.get("agent"), "unknown")
        mark = "briefed by this session" if branch in briefed else "not briefed by this session"
        lines.append(f"- {yard.get('name')}: branch {branch}, terminal {terminal}, "
                     f"ahead {yard.get('ahead')}, staged {yard.get('staged')}, "
                     f"unstaged {yard.get('unstaged')}, agent {agent}, "
                     f"verdict {yard.get('verdict')}, {mark}")
    return lines, branches


def _review_lines(branches: list[str], deadline: float) -> list[str]:
    if not branches:
        return ["No branch to look up."]
    try:
        reviewer = _load("review-branch")
    except (SystemExit, Exception) as exc:  # noqa: BLE001 - every branch gets the fallback line
        why = _say("review-branch not loaded", exc)
        return [f"- {b}: review file unreadable ({why})" for b in branches]
    import json
    from scripts.utils.data_path import data_path
    lines = []
    for branch in branches:
        if time.monotonic() > deadline:
            lines.append(f"- {branch}: {NOT_READ_BUDGET}")
            continue
        try:
            _base, _head, key = reviewer.resolve(branch, "main")
            store = data_path("branch-reviews") / f"{key}.json"
            if not store.is_file():
                lines.append(f"- {branch}: no review recorded")
                continue
            record = json.loads(store.read_text(encoding="utf-8"))
        except (SystemExit, Exception) as exc:  # noqa: BLE001 - reported as the fallback line
            lines.append(f"- {branch}: review file unreadable ({_say(f'review {branch}', exc)})")
            continue
        # Every value read from the record is type-checked before indexing (HOS-387 review).
        rounds = record.get("rounds") if isinstance(record, dict) else None
        if rounds is None and isinstance(record, dict):
            rounds = []
        if not isinstance(rounds, list):
            what = "the record" if not isinstance(record, dict) else "rounds"
            lines.append(f"- {branch}: review file unreadable ({what} is "
                         f"{type(record if what == 'the record' else rounds).__name__}, "
                         f"not the expected shape)")
            continue
        if not rounds or not isinstance(rounds[-1], dict):
            lines.append(f"- {branch}: no review recorded")
            continue
        last = rounds[-1]
        findings = last.get("findings")
        count = len(findings) if isinstance(findings, list) else "?"
        lines.append(f"- {branch}: round {len(rounds)} at {last.get('at')}, head "
                     f"{str(last.get('head') or '?')[:8]}, {count} finding(s): "
                     f"{_one_line(last.get('summary') or '', REVIEW_SUMMARY_CHARS)}")
    return lines


def _source_prefix(text: str) -> str:
    """The mark of a message that is not the operator's own words, or ""."""
    if BRIEF_MARKER in text:
        return BRIEF_PREFIX
    if text.lstrip().startswith(NOTIFICATION_TAG):
        return NOTIFICATION_PREFIX
    return ""


def _operator_lines(transcript) -> list[str]:
    digest = CP.transcript_digest(transcript)
    if digest is None:
        return ["operator messages not read (the transcript could not be read)"]
    said = digest.get("operator") if isinstance(digest, dict) else None
    if not isinstance(said, list):
        return ["operator messages not read (the transcript digest has no message list)"]
    said = [(str(item[0]), str(item[1])) for item in said
            if isinstance(item, (list, tuple)) and len(item) == 2]
    if not said:
        return ["No operator message in this session."]
    start = max(0, len(said) - OPERATOR_SHOWN)
    lines = [f"{n}. {when} {_source_prefix(text)}{_one_line(text, OPERATOR_CHARS)}"
             for n, (when, text) in enumerate(said, 1) if n > start]
    if start:
        lines.insert(0, f"[... {start} older message(s) not shown, numbers 1 to {start}]")
    return lines


def _background_line(transcript) -> str:
    from scripts.utils import background_work as BW
    status, names = BW.background_state({"transcript_path": str(transcript or "")} if transcript else {})
    if names:
        return f"running: {', '.join(names)}"
    return "background: not readable from a script; the Stop hook decides"


# ============================================================
# The section, and its cut
# ============================================================

def _cut(blocks: list[dict], limit: int) -> list[str]:
    """The blocks' lines, cut to `limit` in the fixed order of correction 3."""
    def total() -> int:
        return sum(len(b["head"]) + len(b["items"]) + (1 if b["cut"] else 0)
                   + len(b["tail"]) for b in blocks)

    for name, from_front in (("files", False), ("git", False), ("operator", True)):
        block = next((b for b in blocks if b["name"] == name), None)
        if block is None:
            continue
        while total() > limit and block["items"]:
            block["items"].pop(0 if from_front else -1)
            block["cut"] += 1

    lines: list[str] = []
    for b in blocks:
        lines += b["head"]
        marker = [f"[... {b['cut']} more not shown]"] if b["cut"] else []
        lines += (marker + b["items"]) if b["name"] == "operator" else (b["items"] + marker)
        lines += b["tail"]
    if len(lines) > limit:
        rest = len(lines) - (limit - 1)
        lines = lines[: limit - 1] + [f"[... {rest} more not shown]"]
    return lines


def _block(name: str, title: str | None, lines: list[str], lead: list[str] | None = None) -> dict:
    """A block: heading lines, cuttable items, and a helper's own marker kept last."""
    head = ([f"### {title}"] if title else []) + (lead or [])
    items = list(lines)
    tail = []
    if name in ("files", "git") and items and _HELPER_MARKER.match(items[-1]):
        tail = [items.pop()]
    if name == "operator" and items and items[0].startswith("[... "):
        head.append(items.pop(0))
    return {"name": name, "head": head, "items": items, "tail": tail, "cut": 0}


def records_section(kind: str, model_text: str, state: dict,
                    transcript, project: Path) -> str:
    """The `## Session records` section, at most SECTION_LINES lines. Never raises."""
    deadline = time.monotonic() + BUDGET_SECONDS
    session = CP.session_id() if CP.session_id_is_known() else ""
    briefs, why = _briefs(session) if session else ([], "the session id is not known")
    briefed = {b.get("Branch") for b in briefs if b.get("Branch")}

    ids = _issue_ids(state or {}, model_text, briefs)
    issues = _issue_lines(ids, deadline)
    yards, branches = _yard_lines(project, briefed, deadline)
    for branch in sorted(briefed):
        if branch not in branches:
            branches.append(branch)
    reviews = _review_lines(branches, deadline)
    operator = _operator_lines(transcript)
    files = files_written_block(transcript, limit=FILES_SHOWN).splitlines()
    git = git_status(project, limit=GIT_SHOWN).splitlines()
    ahead_line = []
    try:
        from scripts.utils.clone_guard import main_clone_path
        in_yard = Path(main_clone_path(project)).resolve() != Path(project).resolve()
    except (SystemExit, Exception) as exc:  # noqa: BLE001 - reported as the fallback line
        in_yard = False
        ahead_line = [f"commits ahead of main: not read ({_say('main clone', exc)})"]
    if in_yard:
        try:
            proc = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
                ["git", "rev-list", "--count", "main..HEAD"], cwd=str(project),
                capture_output=True, text=True, timeout=5)
            count = proc.stdout.strip() if proc.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired) as exc:
            count = None
            _say("rev-list", exc)
        ahead_line = [f"commits ahead of main: {count if count is not None else 'not read'}"]

    blocks = [
        _block("lead", None, [], [CP.RECORDS_HEADING, "",
               f"Read by the script at {CP.utc_now().isoformat(timespec='seconds')}. "
               "A line that says \"not read\" is a source that did not answer, not a "
               "statement that nothing exists."]),
        _block("issues", "Lific issues", issues),
        _block("yards", "Yards", yards),
        _block("briefs", "Briefs sent by this session", _brief_lines(briefs, why)),
        _block("reviews", "Branch reviews", reviews),
        _block("background", "Background", [_background_line(transcript)]),
        _block("operator", "Operator messages", operator),
        _block("files", "Files this session wrote", files),
        _block("git", "git status --short", git, ahead_line),
    ]
    return "\n".join(_cut(blocks, SECTION_LINES)) + "\n"
