#!/usr/bin/env python3
"""Does anything a session loads reach the private operational layer?

    python scripts/check-operational-layer-armed.py

READS ONLY. It never writes `~/.claude/CLAUDE.md`, and it never writes anything
else either: that file sits outside both repositories and belongs to the
operator. This reports; a person acts.

WHY THIS IS A CHECK AND NOT A COMMENT. Until 2026-09-20 the engine's own
`CLAUDE.md` ended with `@../.heading-os-data/CLAUDE.operational.md`, so a clone
either had the layer or was a public clone with no overlay to have it from.
Three measurements that day retired that line: a relative path reaches the main
clone and no worktree; the SessionStart hook that compensated cannot carry the
chain, because hook stdout is capped at 10,000 UTF-16 units and the overflow is
persisted behind a ~2,000-character preview in silence; and wiring it in both
places loaded the whole policy layer TWICE in the main clone, counted at 2 by
asking a live session for one section heading.

So the layer is now wired once, by hand, in the USER-level `~/.claude/CLAUDE.md`
by absolute path. That is the right channel -- uncapped, outside every project,
identical in a worktree and the main clone -- and it has one property the old
line did not: **a fresh clone is not wired, and nothing about the clone says
so.** The conduct rules are simply absent and the session opens looking normal.
That is the state this script exists to name.

WHERE IT RUNS. `bash scripts/setup-platform.sh --check` calls it, beside the
hook-registration check, because "what on this clone is not armed" is one
question and an operator should not have to know it has two answers. It is a
separate file rather than another branch of `scripts/merge-platform-settings.py`
because that module is stdlib-only BY CONTRACT so it can run before `.venv`
exists, and the resolvers this needs (`get_data_root`, the chain traversal) are
not stdlib. They are, however, stdlib-only THEMSELVES, so this file keeps the
same property: it imports nothing outside the standard library and this
repository, and runs under a bare system `python3`.

EXIT CODES, and the middle one is the point:

    0  armed, or nothing to arm (no private overlay, which is a public clone)
    1  the overlay holds the layer and no root a session loads reaches it
    2  the answer could not be established

Two is not one. A check that cannot tell must never read as armed, and must not
read as broken either; `scripts/setup-platform.sh` already draws that line for a
missing interpreter and this follows it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.claude_md_chain import (  # noqa: E402
    delivering_roots,
    user_instruction_root,
)

OPERATIONAL_LAYER_FILENAME = "CLAUDE.operational.md"


def check(workspace_root, *, data_root=None, user_root=None) -> tuple[int, list[str]]:
    """`(exit code, lines to print)`. Never raises, never writes.

    `data_root` and `user_root` are parameters rather than reads of the
    environment so that a test can hand in a tree, and they default to the
    workspace's own resolvers so that an operator who moves the overlay with
    `HEADING_OS_DATA` moves this too.
    """
    workspace_root = Path(workspace_root)
    if data_root is None:
        try:
            from scripts.utils.paths import get_data_root
            data_root = get_data_root()
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            return 2, [f"UNKNOWN: the data root did not resolve "
                       f"({type(exc).__name__}: {exc}), so whether the "
                       f"operational layer is reachable was not established."]
    source = Path(data_root) / OPERATIONAL_LAYER_FILENAME

    try:
        present = source.is_file()
    except OSError as exc:
        return 2, [f"UNKNOWN: {source} could not be read ({exc}), so whether "
                   f"the operational layer is reachable was not established."]
    if not present:
        # A public clone. There is no private layer to import, so there is
        # nothing to arm and nothing wrong. Reported rather than silent,
        # because "no output" and "checked and fine" must not look alike.
        return 0, [f"no private operational layer at {source}; nothing to "
                   f"import on this clone."]

    root = Path(user_root) if user_root is not None else user_instruction_root()
    roots = delivering_roots(workspace_root / "CLAUDE.md", source,
                             user_root=root)
    if roots:
        where = ", ".join(label for label, _path in roots)
        return 0, [f"armed: {source.name} is reached from {where}."]

    return 1, [
        f"NOT ARMED: nothing a session loads imports {source}, so the private",
        f"  operational layer and the conduct rules it imports are ABSENT from",
        f"  every session in this clone, including this one.",
        f"  Fix: add this line to {root}",
        f"",
        f"    @{source}",
        f"",
        f"  That file is yours and is not in either repository, so nothing here",
        f"  writes it. It is the user-level root: outside every project, loaded",
        f"  whole with no size cap, and identical in the main clone and in a",
        f"  worktree. Do NOT also add the import to this repository's",
        f"  CLAUDE.md; two roots walking one chain load the layer twice.",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true",
        help="accepted and ignored; this script only ever checks, and the flag "
             "exists so setup-platform.sh can pass its arguments straight "
             "through")
    parser.add_argument("--data-root", default=None,
                        help="override the private overlay root (tests)")
    parser.add_argument("--user-root", default=None,
                        help="override the user-level CLAUDE.md (tests)")
    args, _unknown = parser.parse_known_args()

    code, lines = check(Path(__file__).resolve().parent.parent,
                        data_root=args.data_root, user_root=args.user_root)
    stream = sys.stdout if code == 0 else sys.stderr
    for line in lines:
        print(line, file=stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
