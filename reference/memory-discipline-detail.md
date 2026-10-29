# Memory discipline — the Economy half, written out

Last Updated: 2026-10-29

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

**Why conduct stays resident.** A rule needed in the moment of work retrieves
far worse than a fact asked for by name, so habits stay resident and facts go to
memory. A new line in a rule file earns its place only by being a rule
that must fire when it would not occur to anyone to search; anything else is a
record.

**Read live figures from the tool.** Any count or budget written in prose is a
snapshot; take the current one from `scripts/utils/memory_health.py`.

**Orphans.** A record is an orphan only when `MEMORY.md` does not link it AND
Hindsight's bank `main` holds no document for it, asked of the bank itself
(`scripts/utils/memory_health.py`), never of the directory, which lists what is
eligible rather than what was sent. Since the index became a router, orphan
status rests on Hindsight alone, and a record written between two passes of the
file sync reads as an orphan until the next one. Leave the router unlinked to
records.

**The weekly gate.** When Hindsight does not answer, the orphan check returns
`None`, and the gate then drops the orphan category and says so on stdout as NOT
EVALUATED rather than passing silently. Its `policy_layer` category asks `check_policy_layer` whether any root
still reaches the conduct rules, and refuses to credit a hook route over the cap.
Read the category names in its output, never the evaluated fraction.

The convention written out, what `scripts/memory-hygiene.py` checks, the index's
two budgets, what consolidation does and does not mean, and how this composes
with `/recall` and auto-memory: `reference/memory-discipline-detail.md`. Read it
before writing or consolidating the index; `/memory-hygiene` and `/dream` open it
as a matter of course.

### What a line in the index costs

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

## Deletion protection: how the derived copies behave

- **A file's Hindsight copy is derived.** On the next sync pass a changed file replaces its document; a file deleted, renamed or excluded from sync has its document removed, in portions, each removal written to the sync ledger and recoverable from git and the backups.
- **Hindsight-only content** (observations, mental models, directives, knowledge pages, documents with no file behind them, `memory:` memories) is removed only on the operator's word: a path-shaped id through the cleanup command's apply, any other id by that id.
- **The daily snapshot** under `runtime:hindsight-snapshot/cache` is a derived copy of the Hindsight-only content: each run replaces it whole, a failed run keeps the previous one, and nobody edits or prunes it by hand.
- **MCP tools.** `delete_mental_model`, `clear_mental_model`, `delete_knowledge_node`, `delete_directive`, `invalidate_memory` and `clear_memories` may be used; `delete_document` and `delete_bank` stay off, because a document removed past the sync ledger desyncs it silently.
