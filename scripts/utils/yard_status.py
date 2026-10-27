"""What every YARD of this repository is doing right now, classified.

Operator instruction, 2026-09-20: *"think about it: maybe there should be a
mechanism where HELM itself periodically goes and checks the state of every
yard it created, instead of waiting for me to tell it to."* (translated)

THE EVENING THAT PRODUCED THIS. Five yards were working. One finished its
analysis, wrote a good report into the chat, and stopped: the agent was alive,
the branch was at ZERO commits ahead of `main`, and the working tree was clean.
Everything it had understood existed only as prose inside a worktree that gets
deleted. A second yard had finished completely and stood ready to merge.
Neither was noticed until the operator asked. The cost is measured in hours.

WHY THIS IS A SEPARATE QUESTION FROM THE ONE ALREADY ANSWERED.
`yard_sessions.yards_without_a_session` asks whether a live agent stands in
each checkout, and `check_yards_without_a_session` in
`.claude/hooks/session-start.py` reports it. That catches a DEAD yard. It says
nothing about a live agent that is idle having delivered nothing, and it fires
once, at SessionStart, which is before any yard of that session exists.

WHY THIS IS A MODULE AND NOT JUST THE CLI. A Stop hook is the intended
periodic caller and runs at the end of every turn, so shelling out to a CLI
would pay an interpreter start every time. `scripts/yard-status.py` carries a
hyphen and cannot be imported at all, so the reading lives here under a name
that can, and the CLI is a thin renderer over it.

THE FOUR STATES, and the reason a boolean was not enough:

  dead     no agent process in the checkout            restart it, or delete
                                                        it after merging
  stalled  agent alive, nothing committed, nothing     re-task it: it is done
           changed, and quiet longer than the          thinking and has written
           threshold                                    nothing down
  ready    commits ahead of `main`, or a staged tree   HELM merges, on the
           with nothing unstaged that has also been     operator's word
           been quiet longer than the threshold
  working  anything else                               leave it alone

THE THRESHOLD IS A PARAMETER AND NOT A MEASUREMENT, and this sentence exists so
nobody later reads the default as one. A yard may legitimately be quiet while
it reads or thinks. Nothing here establishes how long that legitimately lasts;
`DEFAULT_QUIET_MINUTES` is 20 because a number was needed, and the honest way
to replace it is a sample over real evenings. Until then a `stalled` verdict is
a prompt to look, never a finding.

Nothing here starts, stops, restarts or writes to anything. It reads.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from scripts.utils.checkpoint_paths import transcript_dir
from scripts.utils.clone_guard import CloneGuardError, worktree_roots
from scripts.utils.proc_cwd import processes_in
from scripts.utils.yard_sessions import AGENT_COMM

#: Chosen, not measured. See the module docstring.
DEFAULT_QUIET_MINUTES = 20

#: What each verdict means, in the order they are worth acting on.
VERDICTS = ("dead", "stalled", "ready", "working", "unknown")

ACTION = {
    "dead": "no agent in it; merge the branch first, then restart or delete",
    "stalled": "quiet with nothing written down; re-task it",
    "ready": "a finished tree; merge on the operator's word",
    "working": "leave it alone",
    "unknown": "could not be read; say so rather than reporting it clean",
}


def _git(root: Path, *args: str) -> str | None:
    """One git question, or None when git could not answer it.

    None is not an empty string. A caller that reads a failed `git` as "no
    commits ahead" reports a yard with unmerged work as having none, which is
    the exact direction this whole module exists to fail away from.

    TRAILING newlines only, and the distinction is not fussiness. `git status
    --porcelain` encodes the index in column 1 and the working tree in column
    2, so a leading space is DATA: ` M a.txt` means "modified, not staged".
    MEASURED 2026-09-20 with `.strip()` here: the first line lost its leading
    space, `_tree_counts` read column 1 as `M`, and an unstaged edit was
    counted as staged work, so a yard mid-edit read as a finished tree.
    """
    try:
        done = subprocess.run(("git", "-C", str(root), *args),
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.rstrip("\n")


def _tree_counts(root: Path) -> tuple[int, int] | None:
    """(staged, unstaged) path counts, or None when git could not answer."""
    porcelain = _git(root, "status", "--porcelain")
    if porcelain is None:
        return None
    staged = unstaged = 0
    for line in porcelain.splitlines():
        if len(line) < 2:
            continue
        if line[0] not in " ?":
            staged += 1
        if line[1] != " ":
            unstaged += 1
    return staged, unstaged


def _quiet_minutes(root: Path) -> float | None:
    """Minutes since this yard's session last wrote to its transcript.

    None means no transcript exists yet, which is the ordinary state of a yard
    whose agent has been started and not yet spoken to. It is NOT evidence of
    silence, and the caller must not treat it as any.
    """
    try:
        directory = transcript_dir(root)
    except Exception:                      # noqa: BLE001 - reported as unknown
        return None
    if directory is None or not directory.is_dir():
        return None
    newest = None
    for path in directory.glob("*.jsonl"):
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or stamp > newest:
            newest = stamp
    if newest is None:
        return None
    return max(0.0, (time.time() - newest) / 60.0)


def _verdict(*, agent: bool | None, ahead: int | None,
             counts: tuple[int, int] | None, quiet: float | None,
             quiet_for: float) -> str:
    """Classify one yard. Anything unreadable is `unknown`, never `working`."""
    if agent is None or counts is None or ahead is None:
        return "unknown"
    staged, unstaged = counts
    if not agent:
        return "dead"
    if ahead > 0:
        # A committed branch is ready whatever the agent is doing now. Nothing
        # about the working tree can take a commit back.
        return "ready"
    settled = quiet is not None and quiet >= quiet_for
    if staged > 0 and unstaged == 0 and settled:
        # A staged tree with nothing unstaged is the shape a briefed yard is
        # asked to end in. It is ALSO the shape a yard passes through for a few
        # seconds every time it runs `git add` mid-work, which is why this
        # needs the same two signals `stalled` needs. MEASURED 2026-09-20 on
        # the first run of this tool: three of four yards read `ready`, and one
        # of them had staged its files two minutes earlier and was still
        # working. A verdict that fires mid-work is a verdict nobody trusts.
        return "ready"
    if ahead == 0 and staged == 0 and unstaged == 0:
        # A yard that has written nothing is only STALLED once it has also been
        # quiet. Without the second half this fires on every yard in the
        # seconds after it is created, which is an alarm delivered at the one
        # moment everything is right, and an alarm like that teaches its reader
        # to skip the line.
        if quiet is not None and quiet >= quiet_for:
            return "stalled"
        return "working"
    return "working"


def sweep(path: Path | str | None = None, *,
          quiet_for: float = DEFAULT_QUIET_MINUTES,
          proc_root: Path | str = Path("/proc")) -> dict:
    """Every yard of this repository, classified.

    Returns `{"yards": [...], "note": str | None}`. `note` carries the reason
    when the sweep could not be taken at all, and is None on an ordinary sweep
    including one that found no yards. A caller prints `note` whenever it is
    set; silence about a sweep that did not happen reads as a clean result.

    `proc_root` is a parameter so a test can build a bench, and is deliberately
    not read from the environment: a variable naming an alternative `/proc`
    would be a way to blind this from outside the process.
    """
    try:
        roots = list(worktree_roots(path))
    except CloneGuardError as exc:
        return {"yards": [], "note": f"could not resolve the main clone ({exc})"}

    yards = []
    for root in roots:
        agents = processes_in(root, proc_root=proc_root, comm=AGENT_COMM,
                              nested=False)
        agent = None if agents is None else bool(agents)
        ahead_raw = _git(root, "rev-list", "--count", "main..HEAD")
        try:
            ahead = int(ahead_raw) if ahead_raw is not None else None
        except ValueError:
            ahead = None
        counts = _tree_counts(root)
        quiet = _quiet_minutes(root)
        yards.append({
            "path": str(root),
            "name": root.name,
            "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD"),
            "agent": agent,
            "ahead": ahead,
            "staged": None if counts is None else counts[0],
            "unstaged": None if counts is None else counts[1],
            "quiet_minutes": None if quiet is None else round(quiet, 1),
            "verdict": _verdict(agent=agent, ahead=ahead, counts=counts,
                                quiet=quiet, quiet_for=quiet_for),
        })

    yards.sort(key=lambda y: (VERDICTS.index(y["verdict"]), y["name"]))
    return {"yards": yards, "note": None}


def needs_attention(result: dict) -> list[dict]:
    """The yards a human should look at, in the order worth looking."""
    return [y for y in result["yards"]
            if y["verdict"] in ("dead", "stalled", "ready", "unknown")]
