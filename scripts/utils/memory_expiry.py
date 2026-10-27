#!/usr/bin/env python3
"""memory_expiry.py - the one link grammar of the memory index (importable, pure).

Every reader of ``MEMORY.md`` that needs its ``[label](target)`` pointers
composes the pattern below or calls ``index_link_targets`` / ``index_links``,
never a regex of its own. Reads text; never writes.

The module name is historical: it also held the ``expires:`` retirement path
until that path and its CLI were deleted on the operator's word, 2026-10-16.

Consumed by:
  - scripts/utils/memory_health.py
  - scripts/memory-index-probe.py
"""
from __future__ import annotations

import re

# One LINK in the index: `[title](name.md)`.
#
# The title admits ONE level of balanced brackets. It used to be `[^\]]*?`,
# forbidding `]` outright, and that is wrong for this index: a memory hook is
# often a fragment of code, and code carries brackets. MEASURED 2026-09-02
# against the live index, this line
#
#     - Code scanners: [the mode is args[0] on Path.open, args[1] on open](a-scanner-....md)
#
# matched nothing at all. `[^\]]*?` cannot cross the `]` of `args[0]`, so the
# engine tried to end the title there, found ` on Path.open` instead of `(`, and
# no amount of backtracking could recover: the whole pointer was invisible.
#
# It cost two things.
#
#   * `memory_health.compute_memory_defects` reported that memory as an ORPHAN,
#     one line below the pointer that names it. A false orphan in a report whose
#     whole job is "which memories has the index lost" is a report the operator
#     stops believing.
#   * The retirement path (deleted 2026-10-16) removed pointers with a pattern
#     built from this one, so it would have left a dangling pointer.
#
# Two links on one line still cannot be read as one, which is the property the
# old comment credited to the `]` ban. It survives for a different reason: both
# alternatives below stop dead at `]`, so the title can never reach across the
# `](` that closes the first link. One level of nesting is deliberate rather
# than a general balanced-bracket matcher; `args[0]` is the shape that occurs,
# and the two branches are disjoint on their first character, so this cannot
# backtrack catastrophically.
#
# THIS IS THE SHARED ROOT, and saying so is the point of this line. The same
# flat `\[[^\]]*\]` was still living in two other readers on 2026-09-10, eight
# days after the measurement above: `memory_health._VH_POINTER_RE` and
# `memory-index-probe.POINTER` each carried their own copy and each reported 339
# pointers against this pattern's 340. Any new reader of the index composes this
# pattern or calls `index_links`; it does not write a third one.
#
# Two spellings of one grammar. `LINK_PATTERN` names no group, so a caller may
# embed it any number of times - inside a lookahead, say - without `re` refusing
# the redefinition of `label`. `_LINK_RE` is the one that captures.
LINK_PATTERN = r"\[(?:[^\[\]]|\[[^\[\]]*\])*\]\([^)]+\)"
_LINK_RE = re.compile(
    r"\[(?P<label>(?:[^\[\]]|\[[^\[\]]*\])*)\]\((?P<target>[^)]+)\)")


def index_link_targets(index_text: str) -> set[str]:
    """The exact ``](<target>)`` link targets the index points at.

    Exposed because ``memory_health.compute_memory_defects`` was answering "is this file
    referenced?" with ``name in index_text`` - a substring of the whole index.
    Two ordinary inputs defeated that, and reproducing them took one file each:

      * A name nested in a longer one. With ``harbour-lantern-ledger.md`` linked
        and ``lantern-ledger.md`` not, the shorter file was reported as
        referenced forever, because its name sits inside its neighbour's.
      * A path-qualified pointer. ``](threads/business/drop.md)`` names a thread  # leak-guard: ok (an invented example of a link target)
        record, and a bare memory file ``drop.md`` was counted as referenced by
        it.

    Both are silent in the direction that matters: the orphan report said zero
    and the operator believed it. Targets are returned verbatim, so a
    subdirectory pointer stays distinct from the bare filename it ends with.
    """
    return {m.group("target") for m in _LINK_RE.finditer(index_text)}


def index_links(index_text: str) -> list[tuple[str, str]]:
    """Every ``[label](target)`` in file ORDER, labels included.

    `index_link_targets` answers "is this record referenced" and returns a set,
    which is the right shape for the orphan report and the wrong one for anything
    comparing two versions of the index: a set cannot see that a pointer moved,
    and it drops the label entirely, so a candidate that rewrote every label
    reads as identical.

    Exposed because both callers that needed the ordered, labelled form wrote
    their own flat-bracket regex instead and both went blind on the one pointer
    whose label carries `args[0]`. See `_LINK_RE`.
    """
    return [(m.group("label"), m.group("target"))
            for m in _LINK_RE.finditer(index_text)]
