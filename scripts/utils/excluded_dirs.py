#!/usr/bin/env python3
"""The exclusion names that are a fact about the tree, and whether they still point at anything.

Two different things get called an "exclusion" in this repository and only one
of them belongs here.

A walker's OWN set is policy: the HEADING OS 2 ingest walker skips `.venv` and
`node_modules` because it indexes markdown, the overlay write guard skips
`.memory-index` because a rebuildable index is not the operator's data, the
corpus digest skips `__pycache__` because a rebuilt artefact must not move a
truth. Those sets differ from each other on purpose and unifying them would
change what each walker returns, which is a different defect wearing this one's
clothes. They stay where they are.

What is here is the other kind: a name that is a statement about the OPERATOR'S
TREE rather than about any one walker's job. Today there is exactly one. The
operator gathered the leftovers of an abandoned index rebuild and the whole
pre-move `_derived/` tree into `pre-migration-junk/` on 2026-09-19 instead of
deleting them, and deleted the folder by his own hand on 2026-09-20. Every
walker in the tree had to be told about it, and every one of them was told
separately, in its own words.

MEASURED 2026-09-20 (HOS-28): the name was spelled as five independent copies,

    scripts/ho2_ingest/walk.py                SKIP_DIRS (deleted 2026-10-16)
    scripts/heading_os_2/manifest.py          SKIP_DIRS
    scripts/utils/overlay_write_guard.py      _UNWATCHED
    scripts/utils/census_state.py             DIGEST_SKIP_DIRS
    config/memory-index.yaml                  deny_prefixes

of which one, `scripts/datastore-map.py`, imported its copy from a neighbour
rather than writing a sixth. A name repeated five times is a fix that lands in
one of them, and this repository's dominant defect shape is exactly that.

## Why each name carries a declared state, and what the check refuses

MEASURED 2026-09-21 on the operator's overlay: `pre-migration-junk` does not
exist. Nothing anywhere reported that. HOS-14 had already found the same shape
from the other side, four of five names in the write guard's `_UNWATCHED` absent
from the tree with the list saying nothing about it.

A check that simply refuses an absent name would have gone red on the day it
shipped, for a name whose absence is correct and expected, and a check that is
red on day one is a check somebody disables on day one. A check that only
REPORTS restores the silence it was written against.

So the state is DECLARED, and the check compares the declaration against the
disk in both directions:

    declared present, absent on disk  -> refuse. This is the HOS-14 silence.
    declared absent,  present on disk -> refuse. The holding pen came back, and
                                         the guard's own comment named that case
                                         ("a re-gathered folder is not cheap").
    declaration and disk agree        -> pass.

It is green today because today's state is written down. It cannot be kept green
by doing nothing: the day the tree changes, the check is red until somebody
records the new state with its date. That moves the cost from "notice a silence"
to "write down what you already did", which is the only version of this anyone
will actually keep.

The declaration is never deleted when a name stops matching. Operator rule
carried over from the write guard: an exclusion that names nothing is cheap, a
re-gathered folder is not, and nothing leaves these lists without his word.

## Console

    python scripts/utils/excluded_dirs.py            # the table, and any drift
    python scripts/utils/excluded_dirs.py --root DIR # check a tree by path

Exits non-zero on drift, 0 when the declarations match the disk, and 0 with a
named reason when there is no overlay to check (a fresh public clone, CI).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Exclusion", "SHARED", "NAMES", "REASONS", "Drift", "drift",
           "prune", "overlay_root", "WORK_DIRNAME", "in_work_dir"]


#: The working folder beside a document: drafts, helper scripts, build sources
#: and other material that produced the document without being it. Operator
#: decision 2026-10-03 (HOS-195): such material moves into a `_work/` folder next
#: to the document, and `_work/` is kept out of every memory store the way
#: archives are. Matched as a whole directory component, case-sensitively, at
#: any depth: `x/_work/a.md` is out, `x/_workshop/a.md` and `x/my_work/a.md` are
#: not.
#:
#: Deliberately NOT one of the `SHARED` names. Those are top-level facts about
#: the tree that every walker skips, the write guard and the census digest
#: included, and `drift()` checks them at the root only. `_work` sits at any
#: depth and is a rule about what MEMORY holds: the write guard must still see
#: writes into it and the census must still count it. It is read by the memory
#: walkers alone: `hindsight-sync.py`, `utils/doc_scope.py`, `memory-index.py`
#: and the HEADING OS 2 ingest walker.
WORK_DIRNAME = "_work"


def in_work_dir(rel: str) -> bool:
    """True when a DIRECTORY component of the POSIX path `rel` is `_work`.

    The last component is the file itself and is not tested, so a file that
    happens to be called `_work` is not a working folder.
    """
    return WORK_DIRNAME in rel.split("/")[:-1]


@dataclass(frozen=True)
class Exclusion:
    """One excluded name, its reason, and the state it was last observed in.

    `reason` is why a walker skips it. `state` is `"present"` or `"absent"`,
    `since` is the ISO date that state was OBSERVED on rather than the date it
    was written down, and `note` is why the entry is kept in the list while in
    that state.
    """

    reason: str
    state: str
    since: str
    note: str


#: The names that are a fact about the operator's tree. Read by every walker
#: that used to spell one of them itself. Ordered most-costly-to-lose first,
#: which today is a list of one.
SHARED: dict[str, Exclusion] = {
    "pre-migration-junk": Exclusion(
        reason=(
            "the operator's own holding pen for pre-move leftovers, deleted by "
            "his hand; no record in it, nothing tracked, and it holds the "
            "previous incarnations of this tree's own indexes, so a walk that "
            "read it would index the index"),
        state="absent",
        since="2026-09-20",
        note=(
            "the operator deleted it himself on 2026-09-20, the day after he "
            "gathered it. The name stays because it costs nothing while it "
            "matches nothing and a re-gathered folder is not cheap; removing it "
            "needs his word, like every other record here."),
    ),
}

#: The bare names, for composing into a walker's own skip set.
NAMES: frozenset[str] = frozenset(SHARED)

#: name -> reason, for composing into a walker that keeps reasons beside names.
REASONS: dict[str, str] = {name: e.reason for name, e in SHARED.items()}


@dataclass(frozen=True)
class Drift:
    """One name whose declaration disagrees with the tree it was checked against."""

    name: str
    declared: str
    found: str
    since: str

    def __str__(self) -> str:  # pragma: no cover - exercised through report()
        return (f"{self.name}: declared {self.declared} since {self.since}, "
                f"found {self.found}")


def prune(dirnames: list[str], names=NAMES) -> None:
    """Drop `names` from an `os.walk(topdown=True)` directory list, IN PLACE.

    In place and returning None on purpose: `os.walk` reads the list object it
    handed out, so `dirnames = [...]` prunes nothing while reading as though it
    did. Top-level names only is the CALLER's business; this touches whatever
    list it is given.
    """
    dirnames[:] = [d for d in dirnames if d not in names]


def overlay_root() -> Path | None:
    """The operator's overlay, or None when this clone has none.

    Deferred import because this module is imported by walkers that must not
    pull the write guard in, and the guard is where the structural resolution
    already lives. NOT `get_data_root()`: that honours `HEADING_OS_DATA`, and
    the question here is about the operator's real tree, which is the one thing
    an environment variable must not be able to answer for.
    """
    from scripts.utils.overlay_write_guard import _structural_overlay_root
    return _structural_overlay_root()


def drift(root: Path, declared: dict[str, Exclusion] = None) -> list[Drift]:
    """Names whose declared state disagrees with `root`, both directions.

    `root` is a tree, not an environment: a caller that wants the operator's
    overlay asks `overlay_root()` and passes what it gets. Returns an empty list
    when every declaration matches, which is the only passing state.
    """
    table = SHARED if declared is None else declared
    out = []
    for name, entry in sorted(table.items()):
        found = "present" if (root / name).exists() else "absent"
        if found != entry.state:
            out.append(Drift(name=name, declared=entry.state, found=found,
                             since=entry.since))
    return out


def report(root: Path | None) -> tuple[str, int]:
    """The console text and the exit code, so the CLI holds no logic of its own."""
    lines = [f"{len(SHARED)} shared exclusion name(s):"]
    for name, entry in sorted(SHARED.items()):
        lines.append(f"  {name}  [declared {entry.state} since {entry.since}]")
        lines.append(f"      {entry.reason}")
    if root is None:
        lines.append("")
        lines.append("no overlay on this clone, so no tree to check the "
                     "declarations against; nothing was verified")
        return "\n".join(lines), 0
    lines.append("")
    lines.append(f"checked against {root}")
    drifted = drift(root)
    if not drifted:
        lines.append("every declaration matches the tree")
        return "\n".join(lines), 0
    for d in drifted:
        lines.append(f"DRIFT  {d}")
    lines.append("")
    lines.append("record the new state and its date in scripts/utils/"
                 "excluded_dirs.py, with the operator's word before removing "
                 "any name")
    return "\n".join(lines), 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=None,
                        help="tree to check the declarations against "
                             "(default: the operator's overlay, if this clone "
                             "has one)")
    args = parser.parse_args(argv)
    root = args.root if args.root is not None else overlay_root()
    text, code = report(root)
    print(text)
    return code


if __name__ == "__main__":  # pragma: no cover - the console entry point
    # Run by PATH rather than by `-m`, so the repo root has to be put on the
    # path here. `-m scripts.utils.excluded_dirs` also works and emits a runpy
    # warning about the package already being in `sys.modules`, which is why the
    # docstring names the path form.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    raise SystemExit(main())
