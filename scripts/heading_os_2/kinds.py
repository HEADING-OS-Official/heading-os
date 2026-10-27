#!/usr/bin/env python3
"""Which kinds hold the records of one legacy directory.

The predicate that `outputs_kinds` in `scripts/next-signal.py` and
`_datastore_kinds` in the since-deleted `scripts/compression-candidates.py`
each carried lives here once (MEASURED 2026-09-16: the two were identical).
What differed between them stayed with the callers.

`homes_under` is imported by `scripts/utils/runtime_junk_moves.py`, which the
Action Queue readers use. Whether a timer or hook reaches this module decides
whether a half-finished edit to it is live, so check that before editing it
in HELM.
"""
from __future__ import annotations

import sys
from pathlib import Path

from scripts.heading_os_2.resolve import NoManifestOnThisTree, load_per_file
from scripts.utils.paths import get_data_root

__all__ = ["homes_under", "kinds_under"]


def kinds_under(prefix: str,
                data_root: Path | None = None) -> tuple[tuple[str, str], ...]:
    """Every kind holding a record whose LEGACY path is under `prefix`.

    `prefix` is a data-root-relative directory spelled as it was BEFORE the
    move (`outputs`, `datastore`); a trailing slash is optional and is
    normalised here, because both original copies appended one by hand and a
    caller that forgot would have matched `outputs-archive/` as well.

    Returns `()` on a tree with no table, which is the honest answer: nothing
    has moved there, so the legacy directory is the whole story. A caller that
    has a second source of kinds unions it afterwards -- that is `outputs_kinds`
    and its rules table, and it is why this returns the empty tuple rather than
    deciding for everyone what "no manifest" should mean. Any OTHER manifest
    error propagates: a table that is present and malformed, or named through
    the environment and absent, is a refusal and stays one.

    The moved form is matched by stripping exactly the kind's own two leading
    segments, never by looking for `/<prefix>` anywhere in the path. The
    manifest's `path` column carries the legacy spelling until that kind flips
    and the DESTINATION afterwards (`flip_tables` rewrites it to
    `<family>/<leaf>/<the whole legacy path>`), so testing the raw value alone
    answers NO for every kind that has already moved. Matching the substring
    instead would adopt an unrelated kind whose legacy path merely contains the
    word, which is how `docs/superpowers/plans/` reads as the plans tree.
    """
    prefix = prefix.rstrip("/") + "/"
    try:
        index = load_per_file(data_root=data_root)
    except NoManifestOnThisTree:
        return ()
    kinds: list[tuple[str, str]] = []
    for kind, rels in sorted(index.files_by_kind.items()):
        moved_prefix = f"{kind[0]}/{kind[1]}/"
        if any(rel.startswith(prefix)
               or (rel.startswith(moved_prefix)
                   and rel[len(moved_prefix):].startswith(prefix))
               for rel in rels):
            kinds.append(kind)
    return tuple(kinds)


def homes_under(legacy: Path, *, data_root: Path | None = None) -> tuple[Path, ...]:
    """Every directory `legacy` became, for a legacy directory that FANNED OUT.

    A legacy directory whose records were classified into several kinds is
    answered with every home it became: `legacy` itself while it is still a
    directory (a kind that is `not-moved`, `moving` or `not-for-migration`
    keeps records there), then `<family>/<leaf>/<tail>` for each kind the
    MANIFEST says took a share of the prefix (through `kinds_under`) and whose
    directory exists. The answer is never empty: on a tree with no family roots
    it is exactly `(legacy,)`. A `legacy` outside the data root is returned
    unchanged, because only the overlay moves.

    The manifest is a generated file and can lag the tree, so a home created
    after the last generation is missed here;
    `scripts/workspace-health.py::check_home_reconciliation` reports that
    disagreement in both directions and resolves nothing.

    ANY manifest failure degrades to `(legacy,)` and logs, unlike `kinds_under`,
    which propagates every error except `NoManifestOnThisTree` (ruling
    2026-09-24: match the directory scan this replaced, whose hook callers
    turn a raise into a default anyway). A tree with family roots and no
    manifest also answers `(legacy,)`, and
    `_say_the_tree_outran_its_manifest` prints when that shape is met.
    """
    root = data_root or get_data_root()
    try:
        tail = legacy.relative_to(root)
    except ValueError:
        return (legacy,)          # not under the overlay; it cannot have moved
    if tail == Path("."):
        # The data root itself has no tail to carry, and every leaf directory
        # would match. Refusing beats answering with the whole new tree.
        return (legacy,)
    found: list[Path] = [legacy] if legacy.is_dir() else []
    try:
        kinds = kinds_under(tail.as_posix(), data_root=root)
    except Exception as exc:  # noqa: BLE001 - the incumbent's degradation, by ruling
        print(f"kinds: the manifest could not be asked which kinds hold "
              f"{tail}/ ({type(exc).__name__}: {exc}); the legacy directory "
              f"answered alone.", file=sys.stderr)
        return tuple(found) or (legacy,)
    if not kinds:
        _say_the_tree_outran_its_manifest(root, tail)
    for family, leaf in _in_incumbent_order(kinds):
        candidate = root / family / leaf / tail
        if candidate.is_dir() and candidate not in found:
            found.append(candidate)
    return tuple(found) or (legacy,)


def _say_the_tree_outran_its_manifest(root: Path, tail: Path) -> None:
    """Print when the tree has fanned out and no manifest declares it.

    Called only where `kinds_under` answered with NO kinds, which has two
    causes that look identical to a caller and are not the same event. Either
    the table is there and simply places nothing under this prefix, which is
    ordinary and silent, or there is no table at all. The second is only
    interesting when the tree HAS family roots: on a public clone, a fresh
    overlay and every scratch fixture there is nothing to declare and
    `(legacy,)` is the whole truth, so printing there would put a warning on
    stderr on every run of a correct tree.

    It prints and does not raise, because the answer is settled by the ruling
    this function's caller records, and because a print on stderr is what the
    removed `moved_homes` did for the one failure it swallowed.
    """
    from scripts.heading_os_2.families import family_roots
    from scripts.heading_os_2.resolve import NoManifestOnThisTree, manifest_dir

    try:
        manifest_dir(data_root=root)
    except NoManifestOnThisTree:
        pass
    except Exception:  # noqa: BLE001 - the caller's handler owns every other one
        return
    else:
        return
    if not any((root / family).is_dir() for family in family_roots()):
        return
    print(f"kinds: {root} carries family roots and no HEADING OS 2 manifest, so "
          f"nothing declares which kinds hold {tail}/; the legacy directory "
          f"answered alone and any home the move created was NOT searched. "
          f"scripts/workspace-health.py --section homes reports this.",
          file=sys.stderr)


def _in_incumbent_order(
        kinds: tuple[tuple[str, str], ...]) -> list[tuple[str, str]]:
    """`kinds` re-sorted into the order the removed `moved_homes` emitted its homes in.

    This exists because of a MEASUREMENT, not a preference, and the measurement
    is one the design that proposed `homes_under` could not have taken. Its
    equivalence script compared `tuple(sorted(...))` on both sides, so it
    established that the two answer the same SETS and said nothing about order.
    Re-derived 2026-09-24 on this branch with the sorting removed: the sets
    agree at nine prefixes of nine, and the ORDER disagreed at two of them,
    `datastore` and `outputs`.

    The cause is that the two iterated different things. `moved_homes` walked
    `family_roots()`, whose order is `FAMILIES`' declaration order and is NOT
    alphabetical (`canon, ledger, status, research, work, inbound, build,
    trace, asset`), and within each family it walks `sorted(base.iterdir())`.
    `kinds_under` returns `sorted(index.files_by_kind.items())`, which is
    alphabetical over the whole `(family, leaf)` pair. So `outputs` came back
    starting `ledger/interaction` from one and `asset/bundle` from the other. (leak-guard: ok, history)

    Nothing measured at the ten call sites depends on that order -- the two
    that could, `runtime_junk_moves.py::resolve_store` and
    `workspace-health.py::check_datastore`, decide by content and by count
    respectively. It is corrected anyway, because "the same tuples in the same
    order" is a cheaper thing to verify than "the same tuples, and here is the
    argument for why order cannot matter at any of ten callers", and the second
    one has to be re-made every time a caller is edited.

    A kind whose family is not in `family_roots()` cannot arise from the
    manifest and would be an unbuildable address anyway; it sorts last rather
    than raising, so this ordering helper can never be the thing that refuses.
    """
    from scripts.heading_os_2.families import family_roots
    order = {family: i for i, family in enumerate(family_roots())}
    return sorted(kinds, key=lambda k: (order.get(k[0], len(order)), k[1]))
