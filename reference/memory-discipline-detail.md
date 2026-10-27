# Memory discipline — the Economy half, written out

Last Updated: 2026-10-04

Companion to the always-on `.claude/rules/memory-discipline.md`, which keeps the
Fidelity half resident. This file carries the Economy half: the convention that
keeps the pointer layer lean, what the mechanical check enforces, and how the
rule composes with the rest of the memory ecosystem.

**Consumed by:** `.claude/rules/memory-discipline.md`, `/dream`, and anything
else that writes or consolidates `MEMORY.md`. Open it before touching the index.

It moved out of the resident rule on 2026-09-04 because it fires at one moment,
writing or consolidating a memory hook, and the skills that do that are named
above. The Fidelity half stayed resident because it fires on a judgement about
consequence that no path and no skill can signal.

## 2026-09-20: the rules left the index

The rules of conduct live in `CLAUDE.private.md` in the private overlay,
imported through `CLAUDE.operational.md` from the USER-level
`~/.claude/CLAUDE.md`, because the 200-line / 25,000-unit truncation belongs to
the memory index alone and the import chain has no such cap (MEASURED
2026-09-20). The index is now a router plus the rules whose violation cannot be
reversed, and the budget arithmetic below still binds on it. The full account is
`memory-discipline-history.md` under registry name `reference-docs`; what the
index used to hold is `2026-09-20-index-to-policy/` under registry name `memory-
archive`, checked by `python scripts/check-memory-preservation.py`.

## 2026-10-04: Hindsight answers for reachability

Operator decision that day (HOS-198), in his words, translated: "Let us remove Cognee
from this workspace. Let Hindsight be the main memory". The graph memory
the section below describes was removed with everything that maintained it,
and reachability is now asked of Hindsight: a record is an orphan only when
`MEMORY.md` does not link it AND Hindsight's bank `main` holds no document for
it (`scripts/utils/memory_health.py`). When Hindsight does not answer, the
check is NOT EVALUATED and the gate says so; it never reads as a pass. A record
reaches the bank through the file sync (`scripts/hindsight-sync.py push`, on
`hindsight-sync.timer`), so the repair when the gate names one is the next sync
pass, or a `push --path` naming it.

## 2026-09-11 and 2026-09-12: what the graph memory taught

Habits stay resident and facts go to memory because, MEASURED 2026-09-11 over
338 records, a question that NAMES its subject retrieves at 0.909 and a rule
needed in the moment of work at 0.533 on vectors and 0.200 on the graph. Two
lessons from the graph memory that held reachability then carry over to
Hindsight unchanged: ask the store, not a list of what was sent, and never read
"no answer" as "nothing missing".

## Economy — what a line in the index costs

- **A line never quotes volatile state.** It does NOT carry a live value — a
  price, a ceiling, an offer, a live count, a live deadline, a current status.
  Those live in the record, read on demand. Stable descriptors ARE fine: a
  version pin, a fixed threshold, a capacity, a historical date, a hardware spec.
  Since the rewrite this is nearly automatic: a conduct rule rarely has a live
  number in it, and one that does is a fact wearing a rule's clothes.
- **A line earns its place only by being a rule that must fire when it would not
  occur to anyone to search.** Anything else is a record and nothing more.
- **Sync the line when the record changes**, in the same change.

`scripts/memory-hygiene.py` enforces the volatile-hook convention mechanically
(advisory). Consolidate the index actively (`/memory-hygiene`, `/dream`) so
"remember everything" stays a lean index plus disciplined retrieval rather than a
warehouse held resident. Consolidation means merging near-duplicates into one
survivor carrying both facts, and keeping hooks short. It never means removing a
fact: auto-memory is never pruned, and deletion happens only on an explicit
instruction from the operator.

## The index budget has two axes, and neither one refuses

`MEMORY.md` is loaded under TWO caps: 200 lines and 25000 UTF-16 units. On
exceeding either, the harness does not refuse and does not ask. It TRUNCATES:
lines past the 200th go first, then the remainder is cut at the last newline
before 25000. What disappears is the END of the file. A warning is appended to
what did load, and by then the content is gone, so a check that waits for the
breach reports a loss instead of preventing one. `/prime` and `/memory-hygiene`
therefore report the byte axis from 80% of the cap, and gate at 100%.

Both numbers are VENDOR constants, read on 2026-09-09 out of one build (Claude
Code 2.1.266). Nothing promises they hold across versions;
`scripts/utils/memory_health.py` carries the loader source they were read from,
so re-read them there rather than treating this paragraph as the authority.

**The byte axis is the one that binds first** on an index grouped by subject, and
it went unmeasured here until 2026-09-09. MEASURED that day on the operator's
index: 125 of 200 lines (0.63) against 22732 of 25000 units (0.91). It is counted
in UTF-16 code units, which is neither `len(text)` (code points) nor
`len(text.encode())` (UTF-8, which over-counts every `·` and `—` the index is
built from). `claude_md_chain.utf16_units` (`scripts/utils/claude_md_chain.py`) is the one
implementation.

**The remedy, and the one thing it is never.** Reorder the sections so the end
truncation would eat is the recoverable part, or move a section into its own
file. Do NOT shorten the index labels to buy room (operator decision,
2026-09-09): the label is the retrieval signal, and past rounds of trimming cost
recall quality. This does not contradict "keep hooks short" above. Short is how a
hook is WRITTEN; it is not a lever to pull on an index that is already full,
because pulling it trades the thing the index exists for against bytes that
reordering can free without cost. The header of the live index says the same in
one line: ORDERED, NEVER TRIMMED.

## How this composes

`/recall` and the memory index are the ENTRY to a record; the resident rule says
read the record before acting on it. Auto-memory recalled inside a
`<system-reminder>` is background context and point-in-time — verify it against
the current record before asserting it as fact. Applies to auto-memory, business
threads, CRM, outputs, and any pointer-to-record structure, not only the memory
system.
