"""The four states a manifest row may be in, and the two questions asked of one.

ONE definition, imported by everything. It sits in its own module rather than in
`resolve.py` for a reason that was measured, and the reason is now HISTORY while
the arrangement is not. `scripts/utils/ho2_tree_guard.py` was loaded by a
PreToolUse wall on EVERY tool call of every session, and importing `resolve`
costs 24-27 ms of import time against 2.1-2.6 ms for that module itself
(MEASURED 2026-09-19, `python -X importtime`, three runs each). That cost is why
it first carried its own copies of the literals, with a test pinning them equal.
A test that catches a drift is not the same thing as a tree that cannot drift,
and the operator's standing rule is the second one: two identical copies of one
thing may not live in one workspace. The guard was retired on 2026-09-20 when
its migration ended, so nothing on a per-tool-call path imports these words
today. This module stays split out on the standing rule alone, which is the half
that never depended on the measurement.

This module imports nothing at all, so the copy is unnecessary rather than
merely guarded. `resolve.py` re-exports every name below, so `resolve.STATE_MOVED`
and its siblings keep working for the readers that already ask for them there.

The vocabulary is spelled out here and nowhere else because the difference
between two of these words is load-bearing and has already been got wrong:
`scripts/check-data-path-references.py` asked `state != "moved"` and so treated
a destination RECEIVING records as one that had received none, calling eleven
populated directories empty (MEASURED 2026-09-18).
"""
from __future__ import annotations

#: The copy has not started. The target holds nothing.
STATE_NOT_MOVED = "not-moved"

#: The copy is under way, so records may be on BOTH sides and a read has to
#: cover both.
STATE_MOVING = "moving"

#: Every record of the kind arrived. The target is the only home.
STATE_MOVED = "moved"

#: The fourth state, added 2026-09-19 on the operator's instruction and with his
#: name for it: "It is wrong to leave anything 'in progress'." (translated)
#:
#: It reads EXACTLY like `moving` (records on both sides, so both homes are
#: searched) and it means the opposite thing about the future: the migration of
#: this kind is OVER, and the records outside the target stay where they are
#: because the operator ruled those trees out of the move. `moving` says work is
#: still expected; for these kinds none is, and a row that says otherwise is a
#: standing instruction to finish something nobody intends to finish.
#:
#: Relayed by HELM from its own measurement of the live storage, 2026-09-19: five
#: rows read `moving` and none of the five can ever reach `moved`, because
#: between them they leave 246 records outside their target on purpose --
#: `canon/reference` 56 (45 under `auto-memory/`, 11 under
#: `knowledge/odin-brain/`), `ledger/episode` 90 and `research/survey` 58 (all  # leak-guard: ok (history: measured split of records left in place)
#: under `knowledge/odin-brain/`), `trace/report` 8 and `trace/scratch` 34 (all  # leak-guard: ok (history: measured split of records left in place)
#: inside the migration's own journal). `auto-memory/` and `knowledge/` are ruled
#: STAYS for the whole folder by the operator's decisions table of 2026-09-13;
#: the journal by his decision of 2026-09-19.
STATE_NOT_FOR_MIGRATION = "not-for-migration"

STATES: tuple[str, ...] = (STATE_NOT_MOVED, STATE_MOVING, STATE_MOVED,
                           STATE_NOT_FOR_MIGRATION)

#: The states whose records are on BOTH sides of the move, so a reader has to
#: cover the legacy homes AND the target. ONE predicate rather than a comparison
#: repeated per call site: the dominant defect in this repository is a fix that
#: landed in one of N copies, and a fourth state that reads like `moving` is
#: precisely the change that finds every copy somebody forgot.
STATES_BOTH_HOMES: frozenset[str] = frozenset({STATE_MOVING,
                                               STATE_NOT_FOR_MIGRATION})

#: The states in which no further move is expected, for either of the two
#: reasons. Anything reporting progress asks this, never `== STATE_MOVED`.
STATES_MIGRATION_OVER: frozenset[str] = frozenset({STATE_MOVED,
                                                   STATE_NOT_FOR_MIGRATION})


def reads_both_homes(state: str) -> bool:
    """True when a read of this state must cover the legacy homes AND the target.

    False for `moved`, whose target is the only home, and for `not-moved`, whose
    target holds nothing yet. The target is added for both members anyway in the
    `not-moved` case by `read_scope`'s write-home rule; this predicate is the
    narrower question of whether the TARGET is a place records are already.
    """
    return state in STATES_BOTH_HOMES


def migration_is_over(state: str) -> bool:
    """True when no further move of this kind is expected, for either reason.

    `moved` finished by arriving; `not-for-migration` finished by decision.
    Anything that reports progress asks THIS, never `state == STATE_MOVED`,
    which counts a finished kind as outstanding forever.
    """
    return state in STATES_MIGRATION_OVER
