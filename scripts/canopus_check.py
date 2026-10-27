#!/usr/bin/env python3
"""The two clauses: is a slice's committed record still telling the truth?

One note per slice says what was approved, where its contract lived, and, once
the slice shipped, which commit retired that contract. This module holds the
note to it, over the repository the note is committed to.

    python scripts/canopus_check.py
    python scripts/canopus_check.py --json

**No clause reads a timestamp.** GIT_COMMITTER_DATE and GIT_AUTHOR_DATE are
environment variables the writing session sets, and on 2026-08-06 two of them
put an implementation commit nine hours BEFORE the approval commit it descends
from: a timestamp comparison got the order wrong and `git merge-base
--is-ancestor` got it right. Ancestry and content, never clocks.

    C1  the contract did not move between its approval and the end of its life
    C2  HEAD descends from the approval commit

Two more clauses, C3 (the contract was red at the approval sha) and C4 (the
target is green at HEAD), ran the contract tests. Those tests left the tree in
the 2026-09-21 test-suite split and none are written any more, so both clauses
were removed on 2026-10-16 with the `--range` flag that scoped them.

**Cost bound.** At most two git commands per note, over EVERY note: C2 is one
(`merge-base --is-ancestor`), and C1 is one (`diff --quiet`) for a live note
and two for a retired one, whose window end costs a `cat-file -e` first.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import subprocess
import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE_ROOT))
from scripts.utils.canopus_note import NoteError, note_dir, note_paths, read_note  # noqa: E402

GIT_TIMEOUT = 60


def git_child_env() -> dict:
    """The parent environment with every GIT_* variable removed.

    A blanket prefix, not a named denylist. Naming the variables you happened to
    think of is this project's most-repeated defect: a guard that covers the case
    in front of its author rather than the class. GIT_DIR is merely the cheapest
    of them, and GIT_WORK_TREE, GIT_COMMON_DIR, GIT_INDEX_FILE,
    GIT_OBJECT_DIRECTORY, GIT_CEILING_DIRECTORIES and
    GIT_DISCOVERY_ACROSS_FILESYSTEM redirect discovery just as well. The next
    release of git may add another, and a prefix covers it in advance.

    This is a CORRECTNESS fix before it is a security one. When this module runs
    inside a git hook, git itself exports GIT_DIR and GIT_INDEX_FILE pointing at
    the HOOK's repository. The engine's pre-commit and pre-push hooks run the
    suite, so an unscrubbed child would ask the wrong repository about a note's
    approval sha.

    Nothing is lost by the scrub: every command this module runs is a local read
    (diff, merge-base, cat-file) and none touches the network.
    """
    return {key: value for key, value in os.environ.items()
            if not key.startswith("GIT_")}


class CheckError(RuntimeError):
    """A clause that could not be MEASURED, as distinct from one that failed.

    Raised inward and never outward: every clause answers `(ok, message)`, so a
    measurement that could not be taken is reported in the message rather than
    crashing the run that was meant to report it.
    """


def _git(root: Path, *argv: str) -> subprocess.CompletedProcess:
    """Run git in *root* and hand back the whole result, returncode included.

    The whole result, never a collapsed one: C1 and C2 are exit-code clauses,
    and 1 (the answer is no) has to be told apart from 128 (the question could
    not be asked). The retired lifecycle's git helper returned None for both.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(root), *argv], capture_output=True, text=True,
            timeout=GIT_TIMEOUT, check=False, env=git_child_env(),
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise CheckError(f"git {' '.join(argv)} could not run: {exc}") from exc


def _window_end(root: Path, note: dict) -> str:
    """The commit where the contract's life ends: HEAD, or its retirement.

    A retired note names the commit that REMOVED the contract, so the window
    that must be unmoved ends where the contract last EXISTED. Diffing through
    the removal itself reports a moved contract against every shipped slice,
    from the first one onward — the reading this clause exists to remove. Asking
    whether the path is still there at the recorded sha also covers the other
    authoring convention (a sha naming the last commit at which the contract
    stood) without having to choose between the two.
    """
    retired = note.get("retired_sha")
    if not retired:
        return "HEAD"
    present = _git(root, "cat-file", "-e", f"{retired}:{note['contract']}")
    return retired if present.returncode == 0 else f"{retired}^"


def _reports(clause):
    """A clause ANSWERS. An unmeasurable clause reports; it does not raise."""
    @functools.wraps(clause)
    def wrapper(root, note):
        try:
            return clause(Path(root), note)
        except CheckError as exc:
            return False, (f"{note.get('slug', '?')}: {clause.__name__} could not "
                           f"be measured: {exc}")
    return wrapper


@_reports
def C1(root: Path, note: dict) -> tuple[bool, str]:
    """The contract did not move between its approval and the end of its life."""
    slug, contract, sha = note["slug"], note["contract"], note["approval_sha"]
    end = _window_end(root, note)
    diff = _git(root, "diff", "--quiet", sha, end, "--", contract)
    if diff.returncode == 0:
        return True, f"{slug}: the contract {contract} is unmoved over {sha}..{end}"
    if diff.returncode == 1:
        return False, (f"{slug}: the contract {contract} moved after it was approved "
                       f"({sha}..{end}), so the record describes a target that shifted")
    return False, (f"{slug}: the contract {contract} could not be diffed over "
                   f"{sha}..{end}: {diff.stderr.strip()}")


@_reports
def C2(root: Path, note: dict) -> tuple[bool, str]:
    """HEAD descends from the approval commit. Ancestry, never a clock."""
    slug, sha = note["slug"], note["approval_sha"]
    ancestry = _git(root, "merge-base", "--is-ancestor", sha, "HEAD")
    if ancestry.returncode == 0:
        return True, f"{slug}: HEAD descends from the approval {sha}"
    if ancestry.returncode == 1:
        return False, (f"{slug}: HEAD does not descend from the approval commit "
                       f"{sha}, so the work and the record are on separate histories")
    return False, (f"{slug}: ancestry from {sha} could not be read: "
                   f"{ancestry.stderr.strip()}")


def _text(value) -> str:
    """The field as a usable string, or an empty one when it is not a string.

    This was `str(value).strip()`, which converts a YAML null into the
    non-empty `"None"`. `read_note` hands frontmatter straight from
    `yaml.safe_load`, so a `approval_sha:` with nothing after the colon arrives
    as None, the note passed as checkable, and C1 then put that same None into
    an argv list. `subprocess.run` raises TypeError on it; TypeError is not in
    `_git`'s except tuple, so it never became a CheckError, and `main` has no
    try around its clause loop — the run died before the remaining notes were
    checked. That is the outcome the docstring below names, arriving through a
    different exception. An int or a list does the same.
    """
    return value.strip() if isinstance(value, str) else ""


def _unreadable(note: dict) -> str:
    """Why this note cannot be checked at all, or an empty string.

    `read_note` parses; it does not validate, so a hand-edited note can reach
    here without the fields the clauses index. Naming the gap is a report; a
    KeyError traceback is a run that stops before the other notes are checked.
    """
    missing = [name for name in ("slug", "approval_sha", "contract")
               if not _text(note.get(name))]
    return f"the note is missing {', '.join(missing)}" if missing else ""


def _row(slug: str, clause: str, ok: bool, message: str) -> dict:
    """One finding, in the shape `--json` prints and the text listing reads."""
    return {"slug": slug, "clause": clause, "ok": ok, "message": message}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Hold every Canopus slice note to the two clauses.")
    parser.add_argument("--root", default=str(ENGINE_ROOT),
                        help="repository to check (default: the engine)")
    parser.add_argument("--json", action="store_true",
                        help="one JSON row per clause on stdout, nothing else; with no note to "
                             "check at all, a single row whose clause is 'scope' saying so")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    rows: list[dict] = []
    for path in note_paths():
        slug, clauses = path.stem, []
        try:
            note = read_note(slug)
            note.setdefault("slug", slug)
            complaint = _unreadable(note)
            clauses = [] if complaint else [C1, C2]
        except NoteError as exc:
            complaint, clauses = str(exc), []
        if complaint:
            rows.append(_row(slug, "note", False, f"{slug}: {complaint}"))
        for clause in clauses:
            rows.append(_row(slug, clause.__name__, *clause(root, note)))
    reported = [row for row in rows if not row["ok"]]
    if not rows:
        # A repository with no slice note is the ORDINARY state, not an error, so
        # this exits 0 exactly as a clean run does. What it must never do is READ
        # like one. `0 clause(s) over 0 note(s); 0 report(s)` under a green CI
        # tick is indistinguishable from two clauses that ran and held, and the
        # standard's own third rule is that an uncalibrated check is worse than
        # none because it manufactures confidence. This is that failure wearing a
        # tick, so the one thing owed here is to say which of the two happened.
        #
        # The `--json` payload carries it as a row rather than as a new top-level
        # shape, so a caller iterating rows keeps working and a caller reading
        # `ok` still sees nothing wrong; `clause == "scope"` is what distinguishes
        # it from a clause verdict, and no clause is named that.
        # Since HOS-131 the notes live in the data repository, so a clone with
        # none has no note directory at all, and the message says which.
        where = ("under the registry name slice-notes in the data repository"
                 if note_dir() is not None else
                 "anywhere: there is no data repository to hold the slice notes")
        nothing = (f"NOTHING WAS CHECKED: no slice note {where}, so the two "
                   f"clauses ran zero times. This is an empty bill, not a clean one.")
        print(json.dumps([_row("-", "scope", True, nothing)], indent=2) if args.json
              else f"canopus-check: {nothing}")
        return 0
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            print(f"{'ok    ' if row['ok'] else 'REPORT'} {row['clause']}  "
                  f"{row['message']}")
        print(f"{len(rows)} clause(s) over {len({row['slug'] for row in rows})} "
              f"note(s); {len(reported)} report(s)")
    return 1 if reported else 0


if __name__ == "__main__":
    sys.exit(main())
