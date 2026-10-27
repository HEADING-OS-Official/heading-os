#!/usr/bin/env python3
"""Family and kind labels for a data-repository path, in four tiers.

`resolve_labels` answers what a file IS: from the migration manifest when a row
names it, from the vocabulary when the path sits under a family root, from the
registry for a name the C3 move took out of the family roots, and from the
legacy top-segment map otherwise. Anything else raises `UnknownRoot`.

Read by `scripts/datastore-map.py`. It moved here from the retired HEADING OS 2
ingest walker on 2026-10-16, when the rest of that stack was deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# HOS-240 (2026-10-13): kept, class (d): family and kind are LABELS on a path here, never an address a file is found or placed at
from scripts.heading_os_2.families import FAMILIES, address_kind, vocabulary_family

__all__ = ["WalkConfig", "resolve_labels", "load_manifest", "UnknownRoot",
           "LabelConflict", "PATH_FAMILY", "NAME_FAMILY"]

# The legacy top-level directories mapped onto the families. Third tier only,
# after the manifest and the vocabulary, and every file labelled this way
# carries `label_src='path'`. No key here may be a family root: a family root
# is read from the vocabulary, and a key that shadows one does so in silence.
PATH_FAMILY = {
    "auto-memory": ("canon", "principle"),
    "knowledge": ("canon", "reference"),
    "reference": ("canon", "reference"),
    "context": ("status", "entity"),
    "crm": ("status", "entity"),
    "threads": ("status", "matter"),
    "admin": ("status", "machine"),
    "state": ("status", "machine"),
    "plans": ("build", "spec"),
    "docs": ("build", "spec"),
    "config": ("build", "config"),
    "deploy": ("build", "config"),
    "scripts": ("build", "code"),
    "tests": ("build", "code"),
    "templates": ("build", "spec"),
    "datastore": ("inbound", "document"),
    "corporate": ("work", "deliverable"),
    "outputs": ("work", "deliverable"),
    "chronicle": ("trace", "chronicle"),
    "journal": ("trace", "handoff"),
    "_archive": ("trace", "report"),
}

# The kind each registry name's files ARE, for a name the C3 subject-tree move
# repoints out of every family root (HOS-234 part 9). After the move the path no
# longer carries the family, the manifest still keys the old address, and the
# registry entry is a bare `path:`, so the kind would otherwise be lost: an
# intelligence report under `knowledge/` read as `("canon", "reference")` through the
# legacy map, and a file under `content/` was refused as an unknown root. The
# key is a NAME, so its address is asked of the registry and follows every
# repoint; the longest address holding a file answers. Labelled `registry`.
NAME_FAMILY = {
    "intelligence": ("research", "digest"),
    "competitor-dossiers": ("research", "dossier"),
    "research-surveys": ("research", "survey"),
    "book-library": ("inbound", "document"),
    "books": ("work", "manuscript"),
    "linkedin-drafts": ("work", "deliverable"),
    "linkedin-archive": ("work", "deliverable"),
    "generated-images": ("work", "deliverable"),
    "notebooklm-output": ("work", "deliverable"),
    "partner-enablement": ("work", "deliverable"),
    "xpager-reference": ("work", "deliverable"),
}


@dataclass
class WalkConfig:
    root: Path
    manifest: dict[str, tuple[str, str]] | None = None


def load_manifest(path: Path) -> dict[str, tuple[str, str]] | None:
    """`_manifest/families.tsv` -> {relative path: (family, kind)}, or None.

    The migration's phase-0 output. It is the authority on what a file IS, and
    the design's accepted migration variant keys family rules off the LABEL
    rather than the disk location, so an ingestion that reads the path instead
    of this file is reading the wrong thing the moment the first record moves.
    """
    if not path.is_file():
        return None
    out: dict[str, tuple[str, str]] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        # The generator writes `leaf`; an earlier spelling wrote `kind`. Both
        # are read, because a reader that asked for `kind` alone once returned
        # None on the real table and every caller fell through to the path map.
        try:
            i_path = header.index("path")
            i_family = header.index("family")
        except ValueError:
            return None
        i_kind = next((header.index(name) for name in ("leaf", "kind")
                       if name in header), None)
        if i_kind is None:
            return None
        for line in fh:
            cols = line.rstrip("\n").split("\t")
            if len(cols) <= max(i_path, i_family, i_kind):
                continue
            out[cols[i_path]] = (cols[i_family], cols[i_kind])
    return out or None


class LabelConflict(ValueError):
    """Two authorities name two families for one file.

    The path under a family root IS the family after the move; a manifest row
    that says otherwise is a stale label, and a stale label silently winning is
    how one file gets two citation rules (outside review, 2026-09-14).
    """


class UnknownRoot(ValueError):
    """A path whose family nothing can name: not the manifest, not the
    vocabulary, not the registry, not the legacy map.

    Raised rather than answered with blanks, so a caller counts the file as
    unlabelled instead of filing it under an empty family (2026-09-14).
    """


_NAMED_ADDRESSES: dict[str, list[tuple[str, tuple[str, str]]]] = {}


def _named_addresses(root: Path) -> list[tuple[str, tuple[str, str]]]:
    """`NAME_FAMILY` as (address relative to `root`, kind), longest first.

    Resolved once per data root. A name the registry cannot answer is said on
    stderr and labels nothing; the legacy map then answers as before."""
    key = str(root)
    if key not in _NAMED_ADDRESSES:
        from scripts.utils.data_homes import resolve
        out = []
        for name, kind in NAME_FAMILY.items():
            path = resolve(name, root)
            if path is None:
                continue
            try:
                out.append((path.relative_to(root).as_posix(), kind))
            except ValueError:
                continue
        _NAMED_ADDRESSES[key] = sorted(out, key=lambda t: -len(t[0]))
    return _NAMED_ADDRESSES[key]


def _named_kind(rel: str, root: Path) -> tuple[str, str] | None:
    for address, kind in _named_addresses(Path(root)):
        if rel == address or rel.startswith(address + "/"):
            return kind
    return None


def resolve_labels(rel: str, cfg: WalkConfig) -> tuple[str, str, str]:
    """(family, kind, label_src), in four tiers, first answer wins.

    manifest    - the migration manifest names the file. Its family is read in
                  either spelling (`state` arrives from the 2026-09-12 rows and
                  means `status`) and must be a vocabulary family.
    vocabulary  - the top segment is a family root: after the move the path IS
                  `<family>/<leaf>/...`, so the family is the first segment and
                  the kind the second when the vocabulary declares it. A second
                  segment the vocabulary does not declare (the family README, a
                  directory that predates the scheme) keeps the family and an
                  empty kind, which is the honest answer.
    registry    - `NAME_FAMILY`: a file under the address of a registry name
                  the C3 move took out of the family roots.
    path        - the legacy map, for what stays outside the family roots.

    Anything else raises `UnknownRoot`.
    """
    parts = rel.split("/")
    top = parts[0]
    if cfg.manifest is not None:
        hit = cfg.manifest.get(rel)
        if hit:
            family = vocabulary_family(hit[0])
            if family not in FAMILIES:
                raise UnknownRoot(
                    f"{rel}: the manifest names family {hit[0]!r}, which the "
                    f"vocabulary does not define")
            if hit[1] and hit[1] not in FAMILIES[family]:
                raise LabelConflict(
                    f"{rel}: the manifest names kind {hit[1]!r}, which is not "
                    f"a leaf of {family}")
            if top in FAMILIES and top != family:
                # Under a family root the path is the authority: the manifest
                # is what PUT the file there, so a row that now disagrees is
                # stale, and a stale row must not reclassify a record.
                raise LabelConflict(
                    f"{rel}: sits under the {top!r} root and the manifest "
                    f"names {family!r}; the path decides and the row is stale")
            return family, hit[1], "manifest"
    if top in FAMILIES:
        # `address_kind` owns the reading of `<family>/<leaf>/...`, because the
        # migration's own classifier has to read it the same way and this
        # repository's dominant defect is a rule that exists in two copies.
        addressed = address_kind(rel)
        return top, (addressed[1] if addressed else ""), "vocabulary"
    named = _named_kind(rel, cfg.root)
    if named is not None:
        return named[0], named[1], "registry"
    if top in PATH_FAMILY:
        family, kind = PATH_FAMILY[top]
        return family, kind, "path"
    raise UnknownRoot(
        f"{rel}: top segment {top!r} is not a family root, not a legacy "
        f"directory the walker knows, and no manifest row names the file")

