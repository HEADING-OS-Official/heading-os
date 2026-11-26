#!/usr/bin/env python3
"""Archive a deleted YARD's scratch directory; never delete it.

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

The directory is MOVED, whole, into the archive named `yard-scratch-archive` as
`<checkout name>-<UTC stamp YYYYmmddTHHMMSSZ>`, merged branch or not. Until
HOS-413 (2026-11-22) a merged yard's scratch was deleted, and a yard with no
commit counts as merged (its tip is `main`), so the probe scripts and evidence
of yards that never committed were lost (rule M003: never delete a record).
Nothing here deletes: a move that fails leaves the folder where it is.

Three refusals, each a reason to keep the folder where it is rather than guess:

  * the worktree is not one of this engine's yards. Herdr lays a worktree out as
    `<worktrees.directory>/<repo>/<branch-slug>`, so a yard's parent directory
    carries the engine clone's own name; the plugin is global and fires for
    every repository Herdr manages.
  * the computed path is not a direct child of the `checkout-scratch` root, is
    that root itself, is a symlink, or is not a directory. Nothing but exactly
    that one directory is ever moved.
  * the archive has no address, or the archive entry already exists.

Whether the branch is merged into `main` is only reported, in the log line.

Exit 0 when the scratch was archived, was already absent or empty, or the
worktree is not this engine's yard; 2 on a refusal or a failed move. Every
outcome prints one `[YARD]` line on stderr, where the plugin log keeps it.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
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
        log(f"the removal event is not JSON ({exc}); nothing taken from it")
        return "", ""
    data = event.get("data") if isinstance(event, dict) else None
    worktree = data.get("worktree") if isinstance(data, dict) else None
    if not isinstance(worktree, dict):
        if event:
            log("the removal event has no data.worktree object; nothing taken from it")
        return "", ""
    path, branch = worktree.get("path"), worktree.get("branch")
    return (path if isinstance(path, str) else "",
            branch if isinstance(branch, str) else "")


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


def archive_scratch(scratch: Path, root: Path, archive: Path, stamp: str) -> bool:
    """Move `scratch` into `archive`, only if it is one directory directly under `root`."""
    if scratch.is_symlink():
        log(f"REFUSED: {scratch} is a symlink; kept in place")
        return False
    scratch_r, root_r = scratch.resolve(), root.resolve()
    if scratch_r == root_r or scratch_r.parent != root_r:
        log(f"REFUSED: {scratch} is not a directory directly under the "
            f"checkout-scratch root {root}; kept in place")
        return False
    if not scratch_r.exists():
        log(f"no scratch at {scratch}; nothing to archive")
        return True
    if not scratch_r.is_dir():
        log(f"REFUSED: {scratch} is not a directory; kept in place")
        return False
    try:
        empty = not any(scratch_r.iterdir())
    except OSError as exc:
        log(f"REFUSED: {scratch} cannot be read ({exc}); kept in place")
        return False
    if empty:
        log(f"scratch {scratch} is empty; nothing to archive, kept in place")
        return True
    target = archive / f"{scratch_r.name}-{stamp}"
    if target.exists() or target.is_symlink():
        log(f"REFUSED: archive entry {target} already exists; kept {scratch} in place")
        return False
    try:
        archive.mkdir(parents=True, exist_ok=True)
        os.replace(scratch_r, target)
    except OSError as exc:
        log(f"could not archive {scratch} to {target} ({exc}); kept in place")
        return False
    log(f"archived scratch {scratch} to {target}")
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

    # The event is read only for what the arguments did not give, so a bad
    # event never blocks an explicit --worktree and --branch.
    event_path, event_branch = (("", "") if args.worktree and args.branch
                                else _from_event())
    worktree = args.worktree or event_path
    branch = args.branch or event_branch
    if not worktree:
        log("the removal event did not say which worktree; nothing archived")
        return 2

    engine_repo = args.engine_repo.resolve()
    if Path(worktree).resolve().parent.name != engine_repo.name:
        log(f"{worktree} is not a yard of {engine_repo}; nothing archived")
        return 0

    try:
        name = checkout_name(Path(worktree))
        scratch = data_path("checkout-scratch", data_root=args.data_root, checkout=name)
        # The root is asked for on its own, never derived from `scratch`, so a
        # registry that ever nests or moves the per-checkout address is refused
        # below rather than trusted.
        root = data_path("runtime:checkout-scratch/tmp", data_root=args.data_root)
        archive = data_path("yard-scratch-archive", data_root=args.data_root)
    except DataPathError as exc:
        log(f"REFUSED: no scratch or archive address for {worktree} ({exc}); kept in place")
        return 2

    if branch:
        merged, why = is_merged(engine_repo, branch)
        log(f"branch {branch} is merged into main" if merged else why)
    else:
        log("the event named no branch; whether it is merged was not checked")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return 0 if archive_scratch(scratch, root, archive, stamp) else 2


if __name__ == "__main__":
    sys.exit(main())
