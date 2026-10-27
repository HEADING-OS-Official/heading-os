#!/usr/bin/env python3
"""Save each task-shaping prompt as its own file, so no compaction can lose it.

Operator instruction, 2026-09-20 (HOS-21), his words: *"This way, no matter how
many times the workspace did /compact, Mirim will ALWAYS remember the exact,
original PROMPTs."*

WHY A FILE AND NOT THE HANDOFF. The handoff already carries a SUMMARY of the
session, and a summary is a retelling. The retelling is exactly what compaction
keeps and the original wording is exactly what it drops, so the thing most worth
preserving is the one thing the existing mechanism does not preserve. A file per
prompt is addressable, survives every compaction, and can be re-read verbatim at
any distance from the turn that produced it.

WHAT IS IN ONE FILE, and both halves are the operator's decision of 2026-09-20:
his own words untouched, and beneath them the expansion he approved. The
expansion is the MASTER INSTRUCTION for execution; the verbatim half is what
makes the expansion checkable against what he actually asked for.

WHICH PROMPTS, and the boundary moved on 2026-09-22. It used to be the approval
gate in `.claude/rules/prompt-refinement.md`, and MEASURED that day the gate had
produced ONE file in two days, because a direct command, a `!` message, a reply
to the assistant's own question and an unattended stretch all bypass the gate by
construction. The boundary is now the SHAPE OF THE TASK: a file is written when
a message fixes the objective, scope, deliverables or acceptance of a task, and
again when a later message changes one of them, whether or not the flow stopped
for approval. A message that only advances work already shaped writes nothing.

His decision of 2026-09-20 survives that move and is the reason it stopped where
it did: an archive of every keystroke is a place nobody looks. One evening of
work carries around forty messages and perhaps six real tasks, and the test
above writes about six files. The rule owns the test; this module owns the file.

THERE IS NO LEDGER, DELIBERATELY. The filenames ARE the order: each carries the
same timestamp shape the handoff archive uses and the session slug, so sorting
by name gives the sequence and filtering by slug gives one session. A second
store would be a second thing to keep in step, and the thing it would track is
already written on the front of every file.

**A PROMPT FILE IS WRITTEN LONG BEFORE ANY HANDOFF EXISTS, AND THAT ORDERING IS
THE WHOLE REASON THIS MODULE HOLDS NO STATE.** Operator, 2026-09-20: *"those
prompt files got created way before we are getting to the point when we create
the hand-off files."* A prompt is archived the moment it is saved. A handoff
is written much later, at a compaction or on demand, and often never. So the
handoff must not be told about a prompt when the prompt is saved: it must ASK,
at the moment it is written, and receive everything saved since the session
began. `entries()` answers that by reading the directory and nothing else.

The failure this avoids is specific. Register a prompt INTO the handoff when it
is saved, and every prompt saved after the last handoff is lost, which is
precisely the window a compaction falls into. Reading the directory at write
time has no such window: a prompt saved one second before the handoff is in it.

The same property makes the archive usable with no handoff at all. `list` works
on a session that has never compacted, and the files survive a session that
ends without writing one.

Usage:
  python scripts/prompt-archive.py save --slug commit-gate-speed \\
      --original /path/to/verbatim.txt --expanded /path/to/expansion.md
  python scripts/prompt-archive.py list                  # this session, in order
  python scripts/prompt-archive.py --json list --session <slug>
  python scripts/prompt-archive.py latest                # the newest, one path
  python scripts/prompt-archive.py --json list --task HOS-9001   # every session's, for one task

TASK DOSSIER (HOS-256). Each file carries a `Task:` line naming the Lific issues
in this session's task list (`Task: none` when it is empty), and after the file
is on disk one `[dossier]` record of event `prompt` goes to each of them through
`scripts/utils/dossier.py`. Lific failing never fails the save: the record waits
in the outbox. `list --task` answers across sessions, because a task outlives
the session that started it.

Ids carried across /clear (HOS-259) are candidates, not tasks, until a saved
prompt decides them: the first one saved after the carry keeps those its
verbatim half names and drops the rest, with one `Carried <ID> dropped: ...`
line under `Task:` per dropped id carrying the relink command, which /prime
repeats once.

`save` prints the absolute path of what it wrote, and that path is what goes
into the handoff and into the sentence spoken before execution.

Exit codes:
  0  done.
  1  a named refusal: an unreadable source, an empty prompt, no writable overlay.
  2  bad usage.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils import checkpoint_paths as CP
from scripts.utils import dossier
from scripts.utils.data_path import DataPathError, data_path
from scripts.utils.paths import DataRootError
from scripts.utils.colors import BOLD, CYAN, GRAY, GREEN, RED, RESET

#: `2026-09-20-205510_prompt_<slug>_<session>.md`, matching the handoff
#: archive's own `<stamp>_handoff_<trigger>_<session>.md` exactly, so the two
#: sort together and a reader who knows one knows the other.
_NAME_RE = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2}-\d{6})_prompt_(?P<slug>.+?)_(?P<session>[0-9a-f-]{8,})\.md$")

_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")


class PromptArchiveError(RuntimeError):
    """A named refusal. Never a silent fallback: losing a prompt is the point."""


def archive_dir(create: bool = False) -> Path:
    """The registry name `prompt-archive`, optionally created."""
    target = data_path("prompt-archive")
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def _read_source(value: str) -> str:
    """A file path, or `-` for stdin. Never a literal on the command line.

    Bash executes every backtick span inside a double-quoted argument, so a
    prompt passed inline arrives with its code names cut out. That has happened
    in this workspace and the text was silently shorter than what was typed.
    """
    if value == "-":
        return sys.stdin.read()
    path = Path(value).expanduser()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptArchiveError(f"cannot read {path}: {exc}") from exc


def entries(session: str | None = None) -> list[Path]:
    """Every archived prompt, oldest first; one session's when named.

    `None` means no filter. An EMPTY STRING is refused rather than treated as
    None, the operator's requirement of 2026-09-20: *"it matters that a handoff takes
    only the prompts of its own session, not everything lying on disk"*
    (translated). A caller with no session gets an error, never another session's
    prompts.
    """
    if session is not None and not session.strip():
        raise PromptArchiveError(
            "an empty session is not a wildcard: pass None for every session, "
            "or a session id to filter by")
    directory = archive_dir()
    if not directory.is_dir():
        return []
    found = []
    for path in directory.iterdir():
        match = _NAME_RE.match(path.name)
        if not match:
            continue
        if session is not None and not match.group("session").startswith(
                session[:8]):
            continue
        found.append(path)
    # By NAME, not by mtime: a file copied or restored keeps its place in the
    # sequence, and the stamp is what the sequence means.
    return sorted(found, key=lambda p: p.name)


def task_entries(task: str) -> list[Path]:
    """Every archived prompt whose `Task:` header names `task`, oldest first, any session."""
    found = []
    for path in entries(None):
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError as exc:
            print(f"prompt-archive: cannot read {path.name}: {exc}", file=sys.stderr)
            continue
        if task in dossier.header_tasks(head):
            found.append(path)
    return found


def objective_line(expanded: str) -> str:
    """The expansion's `Objective:` line, else its first non-empty line."""
    lines = [ln.strip() for ln in expanded.splitlines() if ln.strip()]
    for line in lines:
        if line.lower().lstrip("-* ").startswith("objective:"):
            return dossier.cut(line.lstrip("-* "))
    return dossier.cut(lines[0]) if lines else ""


def save(original: str, expanded: str, *, slug: str,
         session_id: str | None = None, now: datetime | None = None) -> Path:
    """Write one prompt file and return its absolute path."""
    if not _SLUG_OK.match(slug):
        raise PromptArchiveError(
            f"slug {slug!r} must be lowercase letters, digits and hyphens, "
            f"starting with a letter or digit, at most 61 characters")
    if not original.strip():
        raise PromptArchiveError(
            "the verbatim half is empty; an archive of the operator's words "
            "that holds none of them is worse than no archive")
    if not expanded.strip():
        raise PromptArchiveError(
            "the expansion is empty, so there is no master instruction to save")

    session = session_id or CP.session_id() or "unknown-session"
    stamp_at = now or datetime.now(timezone.utc)
    stamp = stamp_at.strftime("%Y-%m-%d-%H%M%S")
    short = session[:8] if session != "unknown-session" else session

    directory = archive_dir(create=True)
    path = directory / f"{stamp}_prompt_{slug}_{short}.md"

    # A file `entries()` cannot match is a prompt saved and lost (MEASURED
    # 2026-09-22: the sentinel session id `"session"` produced such a name).
    # Asked of `_NAME_RE` itself so writer and reader cannot drift; never widen
    # the regex for the sentinel, which every id-less session shares.
    if not _NAME_RE.match(path.name):
        raise PromptArchiveError(
            f"this session's id is {session!r}, which cannot be written into an "
            f"archive filename, so the file would be invisible to `list` and to "
            f"the handoff; pass --session with the real id")

    sequence = len(entries(session=short)) + 1
    # Ids carried across /clear are decided here and only here (HOS-259): the
    # verbatim half confirms the ones it names, and the rest are dropped.
    state = dossier.session_state(session)
    confirmed, dropped = dossier.settle_plan(state, original)
    tasks = dossier.session_tasks(state)
    tasks += [t for t in confirmed if t not in tasks]
    rel_path = dossier.rel(path)
    carried = "".join(f"\n{dossier.dropped_line(t, rel_path)}" for t in dropped)

    body = f"""# Prompt: {slug}

Saved: {stamp_at.isoformat()}
Session: {session}
Sequence: {sequence} in this session
{dossier.task_header(tasks)}{carried}

## The operator's words, verbatim

Reproduced byte for byte. Nothing here is corrected, reformatted or translated,
including the dashes and the quotation marks, because the point of this half is
that it is not a retelling.

{original.rstrip()}

## The approved expansion, which is the MASTER INSTRUCTION

Execute from this. Where it and the verbatim half above disagree, the
disagreement is a finding to raise, not a gap to fill by choosing one.

{expanded.rstrip()}
"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    dossier.settle_carried(session, confirmed, dropped, rel_path)
    for ident in dropped:
        print(f"dossier: {dossier.dropped_line(ident, rel_path)}", file=sys.stderr)
    dossier.post(tasks, "prompt", f"#{sequence}", session, [objective_line(expanded)],
                 rel_path)
    return path


def _cmd_save(args) -> int:
    path = save(_read_source(args.original), _read_source(args.expanded),
                slug=args.slug, session_id=args.session)
    if args.json:
        print(json.dumps({"path": str(path)}))
    else:
        print(f"{BOLD}{GREEN}prompt archived{RESET}")
        print(f"  {path}")
        print(f"{GRAY}  Read this file before executing. It is the master "
              f"instruction, and its path belongs in the handoff.{RESET}")
    return 0


def _cmd_list(args) -> int:
    session = args.session if args.session is not None else CP.session_id()
    found = task_entries(args.task) if args.task else entries(session=session)
    if args.json:
        print(json.dumps([str(p) for p in found], indent=2))
        return 0
    if not found:
        print(f"{GRAY}no archived prompt for "
              f"{session or 'this session'}{RESET}")
        return 0
    print(f"{BOLD}{CYAN}{len(found)} archived prompt(s), in order{RESET}")
    for i, path in enumerate(found, 1):
        print(f"  {i}. {path}")
    return 0


def _cmd_latest(args) -> int:
    session = args.session if args.session is not None else CP.session_id()
    found = entries(session=session)
    if not found:
        print(f"{GRAY}no archived prompt for "
              f"{session or 'this session'}{RESET}", file=sys.stderr)
        return 1
    print(found[-1])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine output")
    sub = parser.add_subparsers(dest="command", required=True)

    saver = sub.add_parser("save", help="archive one prompt")
    saver.add_argument("--slug", required=True,
                       help="short kebab-case name for this prompt")
    saver.add_argument("--original", required=True,
                       help="file holding the operator's words verbatim, or -")
    saver.add_argument("--expanded", required=True,
                       help="file holding the expansion, or -")
    saver.add_argument("--session", default=None, help="override the session id")
    saver.set_defaults(func=_cmd_save)

    lister = sub.add_parser("list", help="this session's prompts, in order")
    lister.add_argument("--session", default=None,
                        help="a session slug; omit for this session")
    lister.add_argument("--task", default=None,
                        help="a Lific issue id: every session's prompts for it")
    lister.set_defaults(func=_cmd_list)

    latest = sub.add_parser("latest", help="the newest prompt's path")
    latest.add_argument("--session", default=None)
    latest.set_defaults(func=_cmd_latest)

    args = parser.parse_args()
    try:
        return args.func(args)
    except (PromptArchiveError, DataPathError, DataRootError) as exc:
        print(f"{RED}{exc}{RESET}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
