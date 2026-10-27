"""Which files are in scope for the document version registry, and their twins.

Two questions, kept in one module because the second one's answer (a twin's
validity) is what lets a non-Markdown document in scope ever produce a diff at
all: `datastore-extract.py` gives every non-Markdown document an `-extract.md`
companion the registry can diff, but only when the companion's own header says
it still matches the original's current bytes.

## Scope

`in_scope(rel)` is true for a file under one of three roots (DataStore, the
deliverable store and the draft store), carrying one of the extensions the
version registry tracks, and false for a code file, an archived file, a
service record, or an `-extract.md` twin naming itself as a document (the spec
is explicit: "a twin is never a family member on its own").

Every root is read from a logical name, never from a literal path.
The registry names `deliverables` and `drafts` resolve directly. DataStore
has no single logical name of its own: its homes are the registry group
`datastore-homes` (HOS-240, 2026-10-13; until then every family/leaf home a
routing rule in `families.RULES` named, joined with the literal `datastore`),
so a home moved or added is a registry edit and nothing here.

The roots also include the existing subject roots of the registry group
`version-scope-roots`, so the version registry follows a file the Part C move
(2026-10-12) carried out of the kind tree (`relocated`) instead of losing it.

`in_scope(rel, archive_ok=True)` (2026-10-12, HOS-178) drops the archive test
and nothing else. It answers one narrower question, asked only for a member of
a CONFIRMED family: the operator files a superseded version into an
`_archive/` folder beside the document, and its copy in memory still has to be
checked and removed. The path must still sit under a scope root, so the data
root's own top-level `_archive/` stays out, and every other exclusion holds.
The detector never asks it, so nothing is ever PROPOSED from an archive.

A self-test never resolves any of this: `in_scope` takes an optional `roots`
argument, an iterable of data-root-relative POSIX prefixes, and a caller that
passes one skips `data_path()` and the real data repository entirely.

## Twins

`twin_of` names the companion `datastore-extract.py` would give a document,
reusing that script's own naming function rather than re-deriving the
`-extract.md` convention a second time (this repository's dominant defect
shape is a fix that lands in one of several copies). The function is loaded by
`importlib`, because a hyphenated filename is not an importable module name.

`twin_valid` is true only when that companion exists and its
`> Source SHA-256: <sha>; converter: ...` header names the SHA-256 the
original carries right now; an edited original invalidates every twin that
predates the edit, which is the whole reason a twin has a hash in it at all.

`text_for` is the one place `doc-versions.py`'s `show` asks for comparable
text: the file's own text for a `.md` document, a valid twin's text for
anything else, or `None` ("comparison unavailable") when neither exists.
"""
from __future__ import annotations

import functools
import importlib.util
import logging
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Iterable

_LOGGER = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils.data_path import DataPathError, data_path  # noqa: E402
from scripts.utils.excluded_dirs import NAMES as SHARED_EXCLUDED_NAMES  # noqa: E402
from scripts.utils.excluded_dirs import WORK_DIRNAME  # noqa: E402
from scripts.utils.paths import get_data_root, get_workspace_root  # noqa: E402

__all__ = ["DOC_EXT", "in_scope", "twin_of", "twin_source_sha", "twin_valid", "text_for"]

#: The extensions the registry tracks. Lower-cased; compared against a
#: lower-cased suffix, so `Report.PDF` is in scope exactly like `report.pdf`.
DOC_EXT: frozenset[str] = frozenset({
    ".docx", ".doc", ".odt", ".pptx", ".key", ".pdf",
    ".xlsx", ".xls", ".csv", ".html", ".md",
})

#: Directory names this scope excludes on top of the shared exclusions of
#: `scripts/utils/excluded_dirs.py`, anything archive-shaped (below) and the
#: `_work/` folder beside a document (HOS-195, `WORK_DIRNAME` there): code,
#: service records and CRM carry no document version worth tracking, per the
#: plan's global constraints.
_EXCLUDED_DIRS = frozenset({
    "scripts", "trace", "runtime-junk", "auto-memory", "threads", "crm",
})

#: The path to `datastore-extract.py`, resolved the established way this
#: engine already loads a hyphenated sibling script by path: joined off
#: `get_workspace_root()`, the same seam `scripts/night-repair.py`'s
#: `_rotation()` and `scripts/dev/derive-memory-index-globs.py`'s
#: `_memory_index_module()` use for the identical problem (a script whose name
#: is not a legal Python module identifier).
_EXTRACTOR_PATH = get_workspace_root() / "scripts" / "datastore-extract.py"


@functools.lru_cache(maxsize=1)
def _extractor():
    """The `datastore-extract` module, loaded by path and cached.

    `scripts/datastore-extract.py` cannot be `import`-ed by name: a hyphen in
    a filename is not legal in a Python module name. Loading it once and
    caching the result means a hot caller (`in_scope`/`twin_valid` over a scan)
    pays the load cost a single time per process rather than once per file.
    """
    spec = importlib.util.spec_from_file_location(
        "scripts._doc_scope_datastore_extract", _EXTRACTOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _safe_rel(rel: str) -> str | None:
    """`rel` as a POSIX string, or `None` when it escapes the data root.

    Refuses an absolute path and any `..` component. This is the one gate
    every function in this module runs a `rel` through before touching disk,
    so a caller that hands in an attacker-shaped path gets `False`/`None`
    rather than a read or a stat outside the root.
    """
    if not rel:
        return None
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts:
        return None
    return pure.as_posix()


def _is_archive_component(name: str) -> bool:
    """Same test as `datastore-extract.py`'s `_is_archive_name`: substring, case-insensitive."""
    return "archive" in name.lower()


def _is_twin_name(posix: str) -> bool:
    """True for anything named like a twin, suffixed or not.

    Matches `get_companion_path`'s plain `<stem>-extract.md` and the
    ambiguous-stem form `_companion_for` falls back to
    (`<stem>-<suffix>-extract.md`); both end in `-extract.md`. The spec is
    explicit that a twin is never a family member on its own, so this excludes
    it from scope regardless of which root it sits under.
    """
    return posix.endswith("-extract.md")


def _excluded_by_directory(posix: str, archive_ok: bool = False) -> bool:
    """True when any directory component of `posix` (not the filename) is excluded."""
    parts = PurePosixPath(posix).parts[:-1]
    for part in parts:
        if part.startswith("."):
            return True
        if _is_archive_component(part) and not archive_ok:
            return True
        if part == WORK_DIRNAME:
            return True
        if part in _EXCLUDED_DIRS or part in SHARED_EXCLUDED_NAMES:
            return True
    return False


def _under_any_root(posix: str, roots: tuple[str, ...]) -> bool:
    parts = PurePosixPath(posix).parts
    for root in roots:
        root_parts = PurePosixPath(root).parts
        if len(parts) > len(root_parts) and parts[:len(root_parts)] == root_parts:
            return True
    return False


def _dedupe_nested(roots: list[str]) -> tuple[str, ...]:
    """Drop a root that sits strictly inside another root already in the list.

    Exact duplicates collapse to one first (so two identical roots do not
    each read as "nested inside the other" and both get dropped), then a root
    whose path parts are a proper prefix of another root's parts is removed:
    the ancestor already covers every file under it. This is what keeps a
    `datastore-homes` member that sits inside `deliverables` (or inside a
    subject root) from being a second, redundant root.
    """
    unique: list[str] = []
    for r in roots:
        if r not in unique:
            unique.append(r)
    parts = [PurePosixPath(r).parts for r in unique]
    kept: list[str] = []
    for i, pi in enumerate(parts):
        nested_in_another = any(
            j != i and len(pi) > len(pj) and pi[:len(pj)] == pj
            for j, pj in enumerate(parts))
        if not nested_in_another:
            kept.append(unique[i])
    return tuple(kept)


def _resolved_without_symlink_swap(raw: Path) -> Path:
    """`raw.resolve()`, refused when a symlink silently swapped it for its target.

    `Path.resolve()` normalises `.`/`..`/`//` AND follows every symlink on the
    path in the same step, so there is no built-in way to ask "the normalised
    form, but not resolved through a symlink" directly. This compares the
    fully-resolved form against `os.path.normpath` of the SAME raw path, which
    normalises without ever touching the filesystem or following a link: if
    the two disagree, some component along `raw` is a symlink, and this
    workspace forbids symlinks entirely, so the call is refused rather than
    silently handed the symlink's target.
    """
    resolved = raw.resolve()
    normalized = Path(os.path.normpath(str(raw)))
    if resolved != normalized:
        raise DataPathError(
            f"{raw} resolves through a symlink to {resolved}; refused")
    return resolved


@functools.lru_cache(maxsize=1)
def _default_roots() -> tuple[str, ...]:
    """The scope roots, resolved by logical name, relative to the data root.

    The registry names `deliverables` and `drafts` (rule K006: where a
    finished document and a draft go) resolve directly; the DataStore roots are
    every home of the registry group `datastore-homes` (HOS-240, 2026-10-13);
    the existing homes of the group `version-scope-roots` are added after them. A
    root nested inside another is dropped by `_dedupe_nested`; the outer root
    already covers it.

    Raises `DataPathError` (directly, or as whatever `get_data_root`/
    `data_path` themselves raise when a logical name will not resolve) rather
    than degrading to an empty tuple. An empty tuple here used to read, to
    `in_scope` and to every caller of it, exactly like a tree with nothing to
    track, which is indistinguishable from a tree that is actually broken; a
    scope resolver that cannot tell those two apart is a scope resolver a
    scan cannot trust to mean "there is nothing here" versus "I could not
    look." A resolved candidate that does not sit under the data root at all
    is the same kind of failure and raises the same way, rather than being
    silently dropped from the result. `functools.lru_cache` never caches a
    raised exception, so a transient failure is retried on the next call
    rather than sticking for the rest of the process.

    Every raw candidate goes through `_resolved_without_symlink_swap` rather
    than a bare `.resolve()`: `Path.resolve()` both normalises a path AND
    follows every symlink on it in one step, so a symlink anywhere along a
    logical name's real address would be silently swapped for wherever it
    points, and the caller would never know the two differ. This workspace
    forbids symlinks entirely, so that swap is refused outright.
    """
    root = _resolved_without_symlink_swap(get_data_root())
    candidates = [
        _resolved_without_symlink_swap(data_path("deliverables")),
        _resolved_without_symlink_swap(data_path("drafts")),
    ]
    # `existing=False`: a home is a root whether or not it holds a file.
    from scripts.utils.data_homes import homes
    for home in homes("datastore-homes", existing=False):
        candidates.append(_resolved_without_symlink_swap(home))
    # A subject root that does not exist yet is no root (`existing=True`): a
    # member recorded there is then out of scope, which gets no event at all.
    for subject in homes("version-scope-roots", existing=True):
        if subject.is_dir():
            candidates.append(_resolved_without_symlink_swap(subject))

    resolved: list[str] = []
    for candidate in candidates:
        try:
            resolved.append(PurePosixPath(candidate.relative_to(root)).as_posix())
        except ValueError as exc:
            raise DataPathError(
                f"scope candidate {candidate} does not sit under the data root {root}"
            ) from exc
    return _dedupe_nested(resolved)


def _resolve_roots(roots: Iterable[str] | None) -> tuple[str, ...]:
    """The roots a caller's `in_scope`/`scan` call actually uses, deduped.

    `roots=None` resolves `_default_roots()`. An explicit `roots` (always a
    self-test's own invented tuple) is deduped and nesting-collapsed through
    the same `_dedupe_nested` the real defaults go through, not just
    normalised to POSIX form: two callers naming the same root twice, or a
    root that is a strict subdirectory of another root already in the list,
    must not make `scan` walk the same files more than once.
    """
    if roots is None:
        return _default_roots()
    return _dedupe_nested([PurePosixPath(r).as_posix() for r in roots])


def in_scope(rel: str, roots: Iterable[str] | None = None, *, archive_ok: bool = False) -> bool:
    """True when `rel` is a document this registry tracks.

    `roots` is an iterable of data-root-relative POSIX prefixes; omitted, the
    three real logical names are resolved. A self-test always passes invented
    roots, so it never depends on, or can corrupt, the real data repository.
    `archive_ok` admits an archive-shaped folder below a scope root; only the
    scan of confirmed members passes it (module docstring).
    """
    posix = _safe_rel(rel)
    if posix is None:
        return False
    if PurePosixPath(posix).suffix.lower() not in DOC_EXT:
        return False
    if _is_twin_name(posix):
        return False
    if PurePosixPath(posix).name.startswith("~$"):
        return False  # an Office owner/lock file beside an open document, never a document
    if _excluded_by_directory(posix, archive_ok):
        return False
    return _under_any_root(posix, _resolve_roots(roots))


def twin_of(root: Path, rel: str) -> Path | None:
    """The `-extract.md` companion `datastore-extract.py` would give `rel`, or `None`.

    Names the companion; does not require it to exist, be current, or even be
    a real companion of this source (`twin_valid` answers that). `None` only
    when `rel` itself is unsafe.
    """
    posix = _safe_rel(rel)
    if posix is None:
        return None
    return _extractor().get_companion_path(root / posix)


def twin_source_sha(twin: Path) -> str | None:
    """The original's SHA-256 a twin's provenance header names, or None."""
    extractor = _extractor()
    try:
        with open(twin, encoding="utf-8", errors="replace") as fh:
            head = fh.read(extractor._HEAD_CHARS)
    except OSError as exc:
        _LOGGER.debug("doc_scope: could not read twin header %s: %s", twin, exc)
        return None
    match = extractor._PROVENANCE.search(head)
    return match["sha"] if match else None


def twin_valid(root: Path, rel: str) -> bool:
    """True only when `rel`'s twin's recorded SHA-256 matches its current bytes.

    Reads the twin's `> Source SHA-256: <sha>; converter: ...` header through
    `datastore-extract.py`'s own provenance pattern and hashing function,
    rather than a second implementation of either. False when `rel` is
    unsafe, the original is missing, the twin is missing, or the twin's
    header names a SHA-256 the original no longer carries, which is exactly
    what happens the moment the original is edited after the twin was made.
    """
    posix = _safe_rel(rel)
    if posix is None:
        return False
    extractor = _extractor()
    original = root / posix
    twin = extractor.get_companion_path(original)
    if not original.is_file() or not twin.is_file():
        return False
    sha = twin_source_sha(twin)
    return sha is not None and sha == extractor.source_sha256(original)


def text_for(root: Path, rel: str) -> str | None:
    """Text usable for a diff: the file itself for `.md`, else a valid twin's text.

    `None` ("comparison unavailable") for an unsafe `rel`, a non-Markdown
    document with no valid twin, or a read that fails.
    """
    posix = _safe_rel(rel)
    if posix is None:
        return None
    if PurePosixPath(posix).suffix.lower() == ".md":
        try:
            return (root / posix).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _LOGGER.debug("doc_scope: could not read %s: %s", root / posix, exc)
            return None
    if not twin_valid(root, rel):
        return None
    twin = twin_of(root, rel)
    try:
        return twin.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _LOGGER.debug("doc_scope: could not read twin %s: %s", twin, exc)
        return None
