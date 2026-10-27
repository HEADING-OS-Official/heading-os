"""The seam on a tree that has no manifest: answer from the legacy directory.

## Why this module exists, and why it is not in `resolve.py`

`resolve.py` refuses without a manifest, on purpose, and its docstring names
the refusal it replaces: "a resolver that answers without the manifest is the
failure this seam was built to prevent". That sentence is about a tree that IS
migrating, where a directory chosen without the manifest is a plausible wrong
answer, written into, and found empty months later.

A converted caller also runs on trees that have no manifest (a public clone,
the pull-only data clone on a service host); there `read_records()` would raise
before a single file is read (MEASURED 2026-09-14).

A tree with no manifest at all has NOT ENTERED the migration: a move writes
its outcome into the manifest (`moved`, or `moving` while a copy is under way),
so the legacy directory is the only place the records can be. That is why this
module may answer from the legacy directory for exactly this case:

  * only on `NoManifestOnThisTree`, the subclass `manifest_dir()` raises when
    the DEFAULT search finds nothing. A manifest named through
    ``HEADING_OS_2_MANIFEST`` and not there is still a refusal; a manifest that
    is present but malformed is still a refusal; a kind the manifest does not
    place is still a refusal.
  * logged once per process per kind on stderr, so a tree that should have
    had a manifest is noticed rather than silently served.
  * from directories the CALLER names; no table of legacy homes is kept here.

Generating a `not-moved` manifest instead was rejected: a generated file is a
write from a read path, and one write wedges the pull-only clone.

## A valid kind the manifest does not place reads as no records

The second thing the READ helpers absorb, and only they. `row_for()` raises
`UnknownKind` both for a pair the vocabulary does not define and for a pair it
defines that the manifest has no row for, and `resolve.unplaced_kinds()` says
what the second means: 33 of 45 pairs are in use and "the rest have no
records yet". A reader that unions thirteen kinds, or a daemon's classifier
asking for one optional signal, must report nothing for such a kind, exactly
as it does for a directory that does not exist; it must not go down over a
row the generator had no reason to write. A pair the vocabulary does NOT
define is re-raised: that is a typo in the caller, not a fact about the tree.
`write_dir_or_legacy` absorbs neither: a new record needs a home, and a kind
with no row has none to give.

This rule lived as one private helper in each of three converted callers on
2026-09-14 and was folded here the same day: one copy in one place.

`resolve.py` itself still has no fallback anywhere, so its docstring stays
true; this module is where the one exception lives, and this docstring is why.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

from scripts.heading_os_2.families import family_roots, is_valid
from scripts.heading_os_2.resolve import (
    MANIFEST_ENV, PERMANENT_MANIFEST_DIR, ManifestMissing, NoManifestOnThisTree,
    UnknownKind, read_dirs, read_records, write_dir,
)
from scripts.utils.paths import get_data_root

#: Written by the mover into `<data>/_manifest/` when a move's manifest set is
#: published; says the tree has migrated whatever else that directory holds.
MIGRATED_MARKER = "MIGRATED"
#: The mover's lock, in the same directory; present for the whole move window.
MIGRATION_LOCK_NAME = "MIGRATION-IN-PROGRESS"


class TreeHasMigrated(ManifestMissing):
    """A tree that has entered the migration and has no manifest to read.

    Outside review, 2026-09-14 (Astra a2 #1, b2 #2): "no manifest" is not
    evidence that a tree never migrated; a manifest can be lost, left out of a
    restore, or made undiscoverable by a wrong root. On such a tree the legacy
    fallback would read empty old directories and let a writer RECREATE a
    vacated one. So the fallback is vetoed by evidence in the tree itself,
    checked before any legacy answer is given (`migration_evidence`).
    """

#: Kinds this process has already answered from a legacy directory. One line
#: per kind per process, not per call: a page refreshing every ten seconds
#: must not fill a log with the same sentence.
_ANNOUNCED: set[str] = set()


def _announce(kind: str, where: Iterable[Path], what: str,
              data_root: Path | None = None) -> None:
    """Say the fallback happened. Unconditional, and that is the point.

    A conditional version was written on 2026-09-16 and REVERTED the same
    hour. It suppressed the line on a tree with no `<data>/_manifest/`, to
    keep a public clone's stderr empty, and it opened a hole in the exact
    failure this sentence exists for: a tree that HAS migrated and lost that
    directory, with family roots holding nothing yet, would have fallen back
    to the vacated legacy directory in silence. `_refuse_if_migrated` catches
    a migrated tree by the marker, the lock, or a family root with content in
    it, so that particular tree slips between the two checks. Seven test files
    assert this line independently; that is not accidental duplication, it is
    seven callers each requiring the fallback to be audible.

    `data_root` is accepted and unused, so the call sites can pass what they
    know without a second signature to keep in step.
    """
    if kind in _ANNOUNCED:
        return
    _ANNOUNCED.add(kind)
    shown = ", ".join(str(p) for p in where)
    print(f"[heading-os-2] no family manifest on this tree; {what} {kind} "
          f"from the legacy location: {shown}", file=sys.stderr)


def _announce_unplaced(kind: str, where: Iterable[Path],
                       data_root: Path | None = None) -> None:
    """Say that the VOCABULARY defines this kind and the manifest does not place it.

    A different sentence from `_announce`, because it is a different fact and
    merging them would hide the one that is currently wrong. `_announce` says
    "this tree has no manifest", which is the normal state of a public clone.
    This one says "this tree HAS a manifest and that manifest has no row for
    this kind", which is a gap in the manifest and never normal.

    MEASURED 2026-09-18 on the live overlay: ten kinds `families.py` defines
    have no row in `_manifest/family-locations.tsv` -- `canon/definition`,
    `ledger/measurement`, `ledger/incident`, `ledger/interaction`, (leak-guard: ok, history)
    `status/commitment`, `status/machine`, `status/queue`, `research/market`,
    `research/competitor`, `research/evaluation`. `resolve.unplaced_kinds()`
    names them and `write_dir` refuses them outright, which is correct. The
    hole was here: this arm returned an empty tuple with nothing on stderr,
    `workspace.moved_home` then iterated zero records and fell through to the
    legacy path, and `scripts/generate-dashboard.py:149` read `current-data.md`
    at its pre-move place in the context directory, which does not exist, while
    the record sat at the `current-data` address, which does.
    No warning, no exception, a dashboard reporting over a file it never found.

    The empty return is KEPT rather than turned into a raise. A raise here
    reaches roughly ninety call sites through `moved_home`/`moved_file` and
    would take the workspace down over a manifest gap, which is worse than the
    defect; and the answer "this kind has no placement" is genuinely the
    truthful one. What was wrong is that nobody was told. The fifth instance of
    this defect shape was found by an AST sweep rather than by a symptom, which
    is what silence buys.
    """
    if kind in _ANNOUNCED:
        return
    _ANNOUNCED.add(kind)
    shown = ", ".join(str(p) for p in where)
    print(f"[heading-os-2] {kind} is a valid kind with NO ROW in the family "
          f"manifest, so the seam placed nothing and the legacy location "
          f"answered alone: {shown}. Add the row, or the caller reads an "
          f"address the move may have emptied.", file=sys.stderr)


def migration_evidence(data_root: Path | None = None) -> str | None:
    """Why this tree cannot be treated as never-migrated, or None.

    Three signs, each of which the mover leaves and none of which a public
    clone has: the `MIGRATED` marker the
    mover publishes with the manifest set; the migration lock, which means a
    move is under way or died; and a family root holding anything but its
    README, which is a record written or moved into the new tree. A tree
    showing any of them and no manifest is damaged, not legacy, and refuses.
    """
    base = Path(data_root) if data_root is not None else get_data_root()
    permanent = base / PERMANENT_MANIFEST_DIR
    if (permanent / MIGRATED_MARKER).exists():
        return f"{permanent / MIGRATED_MARKER} exists: the mover published a manifest here"
    if (permanent / MIGRATION_LOCK_NAME).exists():
        return f"{permanent / MIGRATION_LOCK_NAME} exists: a move is in progress or died"
    for root in family_roots():
        directory = base / root
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if entry.name == "README.md":
                continue
            return f"{directory} holds {entry.name}: the new tree is in use"
    return None


def _refuse_if_migrated(data_root: Path | None) -> None:
    why = migration_evidence(data_root)
    if why is not None:
        raise TreeHasMigrated(
            f"no family manifest was found, but {why}. This tree has entered "
            f"the migration and lost its manifest; refusing to answer from the "
            f"legacy directories. Restore <data>/{PERMANENT_MANIFEST_DIR}/ or "
            f"set {MANIFEST_ENV}.")


def legacy_relative(path: Path, data_root: Path | None = None) -> Path:
    """`path` relative to the data root, spelled as it was BEFORE the move.

    A moved record sits at `<family>/<leaf>/<legacy path>`; a record that has
    not moved sits at `<legacy path>`. Callers select a sub-location of a kind
    by the legacy spelling, root-relative and prefix-exact, so that neither an
    ancestor of the data root named `business` nor a record under
    a `business` directory inside the personal threads matches the legacy
    business-threads prefix (Astra b2 #1).
    """
    base = Path(data_root) if data_root is not None else get_data_root()
    rel = Path(path).resolve().relative_to(Path(base).resolve())
    parts = rel.parts
    if len(parts) > 2 and parts[0] in family_roots():
        return Path(*parts[2:])
    return rel


def under_legacy(path: Path, prefix: str, *, depth: int | None = None,
                 data_root: Path | None = None) -> bool:
    """Is `path` under the legacy directory `prefix` (`<legacy>/<sub>/`)?

    Root-relative and prefix-exact on the legacy spelling; `depth` limits how
    many components below the prefix are allowed (1 reproduces a one-level
    glob, so a file one directory below the prefix is not `<prefix>*.md`).
    """
    try:
        rel = legacy_relative(path, data_root)
    except ValueError:
        return False
    want = Path(prefix.rstrip("/")).parts
    if rel.parts[:len(want)] != want:
        return False
    below = len(rel.parts) - len(want)
    return below >= 1 and (depth is None or below == depth)


def _legacy_files(dirs: Iterable[Path], suffixes, recursive: bool) -> tuple[Path, ...]:
    found: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            continue
        it = d.rglob("*") if recursive else d.iterdir()
        for p in sorted(it):
            if p.is_symlink():
                continue                       # never a record; see resolve.read_records
            if p.is_file() and (suffixes is None or p.suffix in suffixes):
                found.append(p)
    return tuple(found)


def records_or_legacy(family: str, leaf: str, legacy: Path | Iterable[Path], *,
                      suffixes: tuple[str, ...] | None = None,
                      recursive: bool = True,
                      manifest: Path | None = None,
                      data_root: Path | None = None) -> tuple[Path, ...]:
    """`read_records()`, or the files under `legacy` when this tree has no manifest.

    With a manifest, `legacy` is not consulted at all, on either side of the
    move. Without one, the legacy directories are listed (recursively unless
    told otherwise) and filtered by `suffixes`, and the substitution is said
    once on stderr.
    """
    dirs = [Path(legacy)] if isinstance(legacy, (str, Path)) else [Path(p) for p in legacy]
    try:
        return tuple(read_records(family, leaf, suffixes=suffixes,
                                  manifest=manifest, data_root=data_root))
    except NoManifestOnThisTree:
        _refuse_if_migrated(data_root)
        _announce(f"{family}/{leaf}", dirs, "reading", data_root)
        return _legacy_files(dirs, suffixes, recursive)
    except UnknownKind:
        if not is_valid(family, leaf):
            raise
        _announce_unplaced(f"{family}/{leaf}", dirs, data_root)
        return ()


def dirs_or_legacy(family: str, leaf: str, legacy: Path | Iterable[Path], *,
                   manifest: Path | None = None,
                   data_root: Path | None = None) -> list[Path]:
    """`read_dirs()`, or `legacy` when this tree has no manifest."""
    dirs = [Path(legacy)] if isinstance(legacy, (str, Path)) else [Path(p) for p in legacy]
    try:
        return read_dirs(family, leaf, manifest=manifest, data_root=data_root)
    except NoManifestOnThisTree:
        _refuse_if_migrated(data_root)
        _announce(f"{family}/{leaf}", dirs, "reading", data_root)
        return dirs
    except UnknownKind:
        if not is_valid(family, leaf):
            raise
        _announce_unplaced(f"{family}/{leaf}", dirs, data_root)
        return []


def write_dir_or_legacy(family: str, leaf: str, legacy: Path, *,
                        sublocation: str | None = None,
                        manifest: Path | None = None,
                        data_root: Path | None = None) -> Path:
    """`write_dir()`, or `legacy` when this tree has no manifest.

    `sublocation` is a directory name a caller keeps under the kind so its set
    stays self-delimiting (`council`, `memory-hygiene`, `presentations`). It is
    joined ONLY when the resolved directory does not already end in it:
    `write_dir()` returns the kind's evidenced home when the records agree on
    one directory, and for such a kind that home IS the sub-location, so a
    blind join wrote `.../presentations/presentations/` (Astra b2 #5,
    MEASURED 2026-09-14 on a two-record fixture). One rule here, for every
    caller, rather than a copy in each.

    Does not create the directory; that is the caller's business at the moment
    it writes, as with `write_dir()` itself.
    """
    try:
        base = write_dir(family, leaf, manifest=manifest, data_root=data_root)
    except NoManifestOnThisTree:
        _refuse_if_migrated(data_root)
        _announce(f"{family}/{leaf}", [Path(legacy)], "writing", data_root)
        base = Path(legacy)
    if sublocation and base.name != sublocation:
        return base / sublocation
    return base
