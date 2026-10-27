#!/usr/bin/env python3
"""The groups of data directories a reader walks together, by registry name.

Several readers walk a SET of directories rather than one: every output
directory (`/next`'s recent-outputs signal, the `outputs` memory-index layer),
every DataStore home (the extractor, the change log, the health check), every
context home. Each used to find its set from a legacy spelling, `outputs`,
`datastore`, `context/`, through the HEADING OS 2 manifest. The C3 subject-tree
move takes directories out of that scheme and repoints their registry names, so
a set found by legacy spelling silently shrinks. Here each set is a named GROUP of
registry names, and the group itself lives in the data registry's `groups:`
block (HOS-234, 2026-10-13; until then the four lists were constants in this
module), resolved on every call: moving a directory, or adding one to a set, is
an edit of the registry and nothing else. `python scripts/data-path.py group
<group>` prints the same addresses from a shell.

A name that does not resolve is said on stderr and left out, so one bad registry
line costs one directory, never the whole walk. A name spelled with `{account}`
is walked once per account (HOS-234 part 10), so a group may list one.

    from scripts.utils.data_homes import homes
    for home in homes("datastore-homes"):
        ...
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils.data_path import DataPathError, data_path, group_names
from scripts.utils.workspace import get_data_root

def resolve(name: str, data_root: Path | None = None) -> Path | None:
    """One registry name's address, or None, said on stderr, when it has none."""
    try:
        return data_path(name, data_root=data_root)  # leak-guard: ok (the one resolver for the registry's groups and for the registry-named layers of config/memory-index.yaml; a name that does not resolve is said on stderr at run time)
    except DataPathError as exc:
        sys.stderr.write(f"[data-homes] registry name {name!r} does not resolve "
                         f"({type(exc).__name__}: {exc}); left out\n")
        return None


def resolve_all(name: str, data_root: Path | None = None) -> list[Path]:
    """Every address of one registry name: one per account for an `{account}`
    name (`scripts/utils/accounts.py`), else its one. `[]`, said on stderr, when
    it has none."""
    from scripts.utils.accounts import PARAM, addresses, name_params
    try:
        if name_params(name, data_root) == (PARAM,):
            return addresses(name, data_root)
    except DataPathError as exc:
        sys.stderr.write(f"[data-homes] registry name {name!r} does not resolve "
                         f"per account ({type(exc).__name__}: {exc}); left out\n")
        return []
    one = resolve(name, data_root)
    return [one] if one is not None else []


def homes(group: str, *, data_root: Path | None = None, existing: bool = True,
          outermost: bool = False) -> tuple[Path, ...]:
    """The addresses of one group, in group order, duplicates removed.

    `existing` keeps only what is on disk. `outermost` drops an address that
    lies inside another one of the group, for a reader that walks each tree
    recursively and would otherwise read a child twice.
    """
    root = Path(data_root) if data_root is not None else get_data_root()
    out: list[Path] = []
    for name in group_names(group, data_root=root):
        for path in resolve_all(name, root):
            if existing and not path.exists():
                continue
            out.append(path)
    out = list(dict.fromkeys(out))
    if outermost:
        out = [p for p in out if not any(o != p and p.is_relative_to(o) for o in out)]
    return tuple(out)


def claimed_children(base: Path, *, data_root: Path | None = None,
                     ignore: tuple[str, ...] = ()) -> frozenset[str]:
    """The immediate child directories of `base` that a registry name places.

    For a reader that treats every child of a directory as its own kind of
    record (the `/zk` note directories under `knowledge-base`): a child some
    other registry name owns, the intelligence reports or the books the C3 move
    puts under `knowledge/`, is that name's, and the answer follows a registry
    repoint with no engine edit (HOS-234 part 9). A name whose address is a FILE
    directly in `base` (an index) claims nothing. Names in `ignore`, and names
    that need a parameter, are not asked; one that cannot be resolved is said
    on stderr and claims nothing.
    """
    from scripts.utils.data_path import load_registry

    root = Path(data_root) if data_root is not None else get_data_root()
    base = Path(base)
    out: set[str] = set()
    for name, entry in load_registry(root).items():
        if name in ignore or entry.get("params"):
            continue
        path = resolve(name, root)
        if path is None:
            continue
        try:
            rel = path.relative_to(base)
        except ValueError:
            continue
        if not rel.parts or (len(rel.parts) == 1 and path.is_file()):
            continue
        out.add(rel.parts[0])
    return frozenset(out)

