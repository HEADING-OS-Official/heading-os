"""Short yard codes (`w100`) under herdr-core: allocate one, resolve one.

A yard made by `scripts/yard.py create <slug>` under herdr-core gets the branch
`w<N>-<slug>`, so the branch, the yard folder, the hc terminal name and the
space name the window derives from the folder are all the same string, and
the operator can name the yard by its code alone (HOS-379).

N needs no state file. It is 1 + the largest number found in the local branch
names matching `^w([0-9]+)-` and in the subjects of the commits on `main`
matching `\\(yard w([0-9]+)-` (HELM's merge commits end `(yard <branch>)`), and
at least 100. A merged branch stays in main's history and an unmerged one stays
a branch, so a code is never reused. Two creates at the same moment are out of
scope: the second fails on `git worktree add -b` because the branch exists.

`yard_for_code` is the ONE resolver of a bare code; `scripts/yard.py` and
`.claude/hooks/_dispatch.py` both import it.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

#: An argument that is a bare code, e.g. `w100`.
CODE_RE = re.compile(r"^w[0-9]+$")
#: A branch (or slug) that already carries a code.
CODED_BRANCH_RE = re.compile(r"^w([0-9]+)-")
#: HELM's merge subject for a coded yard, `... (yard w100-slug)`.
MERGE_SUBJECT_RE = re.compile(r"\(yard w([0-9]+)-")
FIRST_CODE = 100


class YardCodeError(Exception):
    """A code resolved to zero or several worktrees, or git could not answer."""

    def __init__(self, message: str, matches: int = -1):
        super().__init__(message)
        self.matches = matches


def is_code(argument: str) -> bool:
    return bool(CODE_RE.match(argument or ""))


def _git(engine: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(engine), *args], capture_output=True,
                               text=True, errors="replace", check=False, timeout=60)
    if completed.returncode != 0:
        raise YardCodeError(f"git {' '.join(args[:2])} failed in {engine}: "
                            f"{completed.stderr.strip()[:200]}")
    return completed.stdout


def next_code_number(engine: Path) -> int:
    """The N for the next `w<N>-<slug>`; raises YardCodeError when git fails."""
    numbers = [FIRST_CODE - 1]
    # The full ref, stripped by hand: `%(refname:short)` answers `heads/<name>`
    # when a tag of the same name exists, which the anchored match would skip.
    for ref in _git(engine, "for-each-ref", "--format=%(refname)",
                    "refs/heads").splitlines():
        match = CODED_BRANCH_RE.match(ref.strip().removeprefix("refs/heads/"))
        if match:
            numbers.append(int(match.group(1)))
    for subject in _git(engine, "log", "main", "--format=%s").splitlines():
        numbers.extend(int(found) for found in MERGE_SUBJECT_RE.findall(subject))
    return max(numbers) + 1


def worktree_branches(engine: Path) -> list[tuple[Path, str]]:
    """(path, branch) of every registered worktree that has a branch."""
    pairs: list[tuple[Path, str]] = []
    path: Path | None = None
    for line in _git(engine, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line.split(" ", 1)[1])
        elif line.startswith("branch refs/heads/") and path is not None:
            pairs.append((path, line[len("branch refs/heads/"):]))
        elif not line.strip():
            path = None
    return pairs


def yard_for_code(engine: Path, code: str) -> tuple[Path, str]:
    """(path, branch) of the one worktree whose branch starts `<code>-`.

    Raises YardCodeError, carrying the match count, when zero or several match
    or git cannot list the worktrees.
    """
    if not is_code(code):
        raise YardCodeError(f"{code!r} is not a yard code (w<N>)", 0)
    found = [(path, branch) for path, branch in worktree_branches(engine)
             if branch.startswith(f"{code}-")]
    if len(found) != 1:
        raise YardCodeError(f"code {code} matches {len(found)} worktree(s)"
                            + (": " + ", ".join(b for _, b in found) if found else ""),
                            len(found))
    return found[0]
