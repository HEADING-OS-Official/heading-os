#!/usr/bin/env python3
"""Path-deny predicate. Every deny it applies is one the CALLER passed in.

Operator directive, 2026-09-12: nothing on this machine is withheld from the
index or from memory. So the two unconditional denies this module used to carry
(`HARDCODED_DENY_PREFIXES`, `HARDCODED_DENY_SEGMENTS`) were removed, not emptied:
an importer that still reaches for one fails loudly at import rather than
receiving a tuple that silently denies nothing. A `personal` path is an ordinary
indexed path; `config/memory-index.yaml` gives it layers of its own
(`context-personal`, `chronicle-personal`).

What this module still does, and it is the whole of it: apply the prefixes and
segments a caller hands over. `scripts/memory-index.py`,
`scripts/utils/symbol_source.py` and `scripts/utils/commit_source.py` pass the
`deny_prefixes` / `deny_segments` lists out of `config/memory-index.yaml`;
`scripts/utils/sandbox.py` passes its own pair as literals, because the /census
box is a different boundary from the index (`.claude/rules/generated-code-execution.md`
condition 2, whose change control is the operator's) and it now names what it
denies instead of inheriting it from here.

Case-folded: a path whose segment is `Personal` is denied exactly as one whose
segment is `personal`, when the caller denies that segment at all. Letter-case
is never a boundary.

Usage:
    from scripts.utils.air_gap import is_denied
    if is_denied(rel):                    # no caller denies -> nothing is denied
        continue
    is_denied(rel, deny_prefixes=cfg["deny_prefixes"],
              deny_segments=cfg["deny_segments"])
"""

import os


def is_denied(rel_path: str, deny_prefixes=(), deny_segments=()) -> bool:
    """True if rel_path matches one of the CALLER's denies.

    A path is denied if (case-insensitively) it starts with any deny prefix, OR
    if any of its `/`-separated segments equals a denied segment. With neither
    argument given nothing is denied except a path that escapes its own root,
    for which see the traversal paragraph below.

    Comparison is case-folded, and case-folded means `str.casefold()`, not
    `str.lower()`. The two are not the same function: a character whose fold
    mapping differs from its lowercase mapping slips a `.lower()` comparison
    entirely. MEASURED 2026-08-30 against the then hard-coded `personal`
    segment: `"perſonal".lower()` is `"perſonal"` (unchanged, U+017F LATIN
    SMALL LETTER LONG S is already lowercase) while `.casefold()` is
    `"personal"`, so `is_denied("perſonal/todo.md")` returned False while
    `is_denied("personal/todo.md")` returned True. That segment is no longer  # leak-guard: ok (an invented example input, not an address)
    denied here, but the folding is kept because the same one-character bypass
    applies to every deny a caller passes, and `_secure` in
    `config/memory-index.yaml` is one of them today.

    Traversal-safe: `..` is collapsed lexically BEFORE any check, so a path like
    `threads/business/../../_secure/x` resolves to `_secure/x` and still trips a  # leak-guard: ok (an invented example input, not an address)
    caller's `_secure/` prefix. A path that still escapes its root after
    collapse (`../x`) is not a legitimate workspace-relative ingest path and is
    denied fail-closed, with no caller deny required. This is a pure string
    transform, with no filesystem access.
    """
    prefixes = tuple(deny_prefixes)
    segments = set(deny_segments)
    raw = rel_path.replace("\\", "/").lstrip("/")
    norm = os.path.normpath(raw).replace("\\", "/").lstrip("/").casefold()
    if norm == ".":
        return False
    if norm == ".." or norm.startswith("../"):
        return True
    if any(norm.startswith(p.lstrip("/").casefold()) for p in prefixes):
        return True
    seg_folded = {s.casefold() for s in segments}
    return any(seg in seg_folded for seg in norm.split("/"))
