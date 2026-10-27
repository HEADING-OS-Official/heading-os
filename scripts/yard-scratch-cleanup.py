#!/usr/bin/env python3
"""Remove a deleted YARD's scratch directory, but only once its branch is merged.

Runs on Herdr's `worktree.removed` event, through `yard-teardown.sh` in the
plugin directory. Also runnable by hand, which is how it is verified:

    .venv/bin/python scripts/yard-scratch-cleanup.py --worktree <yard path> --branch <branch> \
        [--engine-repo <clone>] [--data-root <data repository>]

The scratch directory is what `python scripts/data-path.py scratch` printed
inside that yard (registry name `checkout-scratch`). It is computed here from
the worktree's directory name through the same two functions, `checkout_name`
and `data_path`, never by running in the yard, which is gone by now, and never
through `checkout_scratch_dir`, which would CREATE the directory it is asked
about.

Three refusals, each a reason to keep rather than to guess:

  * the worktree is not one of this engine's yards. Herdr lays a worktree out as
    `<worktrees.directory>/<repo>/<branch-slug>`, so a yard's parent directory
    carries the engine clone's own name; the plugin is global and fires for
    every repository Herdr manages.
  * the branch tip is not an ancestor of `main` in the engine repository, or the
    branch cannot be resolved there. An unmerged yard keeps its scratch, because
    the prepared commit message and the raw measurements in it may be the only
    copy of work that never reached `main`.
  * the computed path is not a direct child of the `checkout-scratch` root, or
    is that root itself. Nothing but exactly that one directory is ever removed.

Exit 0 when the scratch was removed, was already absent, or was kept for a
stated reason; 2 on a refusal of the input itself. Every outcome prints one
`[YARD]` line on stderr, where the plugin log keeps it.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE_ROOT))

from scripts.utils.data_path import DataPathError, checkout_name, data_path  # noqa: E402


def log(message: str) -> None:
    print(f"[YARD] {message}", file=sys.stderr)


def _from_event() -> tuple[str, str]:
    """`data.worktree.path` and `data.worktree.branch` of the removal event.

    The same shape `yard-bootstrap.sh` reads for `worktree.created`; the
    `worktree_removed` payload carries the identical `WorktreeInfo` (MEASURED
    2026-10-12 against `herdr api schema --json` on herdr 0.9.3).
    """
    try:
        event = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
    except ValueError as exc:
        log(f"the removal event is not JSON ({exc}); nothing removed")
        return "", ""
    worktree = (event.get("data") or {}).get("worktree") if isinstance(event, dict) else None
    if not isinstance(worktree, dict):
        return "", ""
    return worktree.get("path") or "", worktree.get("branch") or ""


def is_merged(engine_repo: Path, branch: str) -> tuple[bool, str]:
    """Whether the branch tip is an ancestor of `main`, and why not when it is not."""
    if branch.startswith("-"):
        return False, f"branch {branch!r} reads as a git option, not a branch"
    proc = subprocess.run(
        ["git", "-C", str(engine_repo), "merge-base", "--is-ancestor", branch, "main"],
        capture_output=True, text=True, timeout=60)
    if proc.returncode == 0:
        return True, ""
    if proc.returncode == 1:
        return False, f"branch {branch} is not merged into main"
    return False, (f"branch {branch} could not be checked against main "
                   f"({proc.stderr.strip() or f'git exit {proc.returncode}'})")


def remove_scratch(scratch: Path, root: Path) -> bool:
    """Remove `scratch` only if it is exactly one directory directly under `root`."""
    if scratch.is_symlink():
        log(f"REFUSED: {scratch} is a symlink; nothing removed")
        return False
    scratch_r, root_r = scratch.resolve(), root.resolve()
    if scratch_r == root_r or scratch_r.parent != root_r:
        log(f"REFUSED: {scratch} is not a directory directly under the "
            f"checkout-scratch root {root}; nothing removed")
        return False
    if not scratch_r.exists():
        log(f"no scratch to remove at {scratch}")
        return True
    if not scratch_r.is_dir():
        log(f"REFUSED: {scratch} is not a directory; nothing removed")
        return False
    shutil.rmtree(scratch_r)
    log(f"removed scratch {scratch}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--worktree", help="the removed yard's path (default: the event)")
    parser.add_argument("--branch", help="the removed yard's branch (default: the event)")
    parser.add_argument("--engine-repo", type=Path, default=ENGINE_ROOT,
                        help="the engine clone whose main decides 'merged' "
                             "(default: the clone this script is in)")
    parser.add_argument("--data-root", type=Path, default=None,
                        help="the data repository (default: this machine's)")
    args = parser.parse_args(argv)

    event_path, event_branch = _from_event()
    worktree = args.worktree or event_path
    branch = args.branch or event_branch
    if not worktree:
        log("the removal event did not say which worktree; nothing removed")
        return 2

    engine_repo = args.engine_repo.resolve()
    if Path(worktree).resolve().parent.name != engine_repo.name:
        log(f"{worktree} is not a yard of {engine_repo}; nothing removed")
        return 0

    try:
        name = checkout_name(Path(worktree))
        scratch = data_path("checkout-scratch", data_root=args.data_root, checkout=name)
        # The root is asked for on its own, never derived from `scratch`, so a
        # registry that ever nests or moves the per-checkout address is refused
        # below rather than trusted.
        root = data_path("runtime:checkout-scratch/tmp", data_root=args.data_root)
    except DataPathError as exc:
        log(f"REFUSED: no scratch address for {worktree} ({exc}); nothing removed")
        return 2

    if not branch:
        log(f"kept scratch {scratch}: the event named no branch, so whether it "
            f"is merged cannot be checked")
        return 0
    merged, why = is_merged(engine_repo, branch)
    if not merged:
        log(f"kept scratch {scratch}: {why}")
        return 0
    return 0 if remove_scratch(scratch, root) else 2


if __name__ == "__main__":
    sys.exit(main())
