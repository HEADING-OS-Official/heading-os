<!-- version: 3.2.0 | last-updated: 2026-10-14 -->

# Memory Discipline: Source of Truth, Fetched on Demand

Last Updated: 2026-10-14
Last Verified: 2026-09-04

Always-active rule. The workspace's files are its memory; the context window is a
cache of pointers into that memory, never the memory itself.

**A pointer is not a fact.** A `MEMORY.md` index hook, a recalled snippet, a
skill's cached summary, a `CLAUDE.md` bullet: each is an ENTRY POINT to a
record, not the record. Pointers lag the file they point at, and the file can lag
the primary source. Acting on the stale layer produces an answer that is
confident and wrong. This has happened; it must not recur silently.

## Fidelity: open the record before acting

Before any CONSEQUENTIAL action, open and read the authoritative record, not the
pointer that surfaced it. Consequential means the action touches any of:

- a commitment, promise or agreement (what was owed, to whom, by when);
- a decision or its rationale;
- a fact, number, price, quantity or measurement;
- a deadline, date-as-state or schedule;
- a relationship, or a person's stated position;
- the live state of an ongoing matter (a negotiation, deal, plan, task).

Freshness order when layers disagree: **primary source > full record file > index
hook / recalled snippet.** When a pointer contradicts its record, flag the drift;
never paper over it. When a task touches a domain you hold records for, FETCH
those records first, and answer a consequential question from the record, never from the hook alone.

Bound, against defensive over-fetching: this is not "re-read everything every
turn". Trivial, no-stakes conversation is exempt. The trigger is consequence, per
the list above, not volume.

**Where memory lives, since 2026-10-05 (HOS-203).** Hindsight is the main
memory: facts, decisions, conversation history and synthesized knowledge. The
rules of conduct stay resident files, loaded every session. Work documents stay
files in git, on disk where they are or where they are created, and Hindsight
mirrors and searches them. Engine code stays with the code graph.

**Ask Hindsight first, for records.** Every lookup into the records goes to
Hindsight before the directory: in a session through the query tools of the
`hindsight-main` MCP server (`recall`, `reflect`, `list_documents`,
`get_document`) or the plugin's `hindsight_search_knowledge_pages`; from a
terminal through `hindsight memory recall main "<what you are after>"`. Engine
code is the code graph's, through `codegraph_explore`, and never Hindsight's
job. The local memory index stays beside Hindsight: `scripts/memory-index.py`,
`/recall` and the per-prompt pointers are unchanged.

A Hindsight hit is still a POINTER: a recalled fact names the document it came
from, so open that file before acting on it. `check_hindsight_first` in
`.claude/hooks/_dispatch.py` refuses the session's first lookup aimed at the
records until one Hindsight query has been ATTEMPTED; an attempt that errors
unlocks, so nothing can wedge, and a machine with no Hindsight key file gets no
wall at all. A write tool (`retain`, `create_*`, `delete_*`) never unlocks it.

**When Hindsight does not answer, answer from its snapshot, and say so.** What
lives ONLY in Hindsight (observations, mental models, directives, the
knowledge-base export, and documents with no file behind them, conversations
and remembered memories among them) is copied to disk daily by
`scripts/hindsight-snapshot.py take` into `runtime:hindsight-snapshot/cache`,
which `hindsight-sync.py` never sends back. The local memory index indexes it as
the collection `hindsight-snapshot`, outside `content`. A lookup that Hindsight
does not answer (refused connection, timeout, an HTTP error such as a refused
key) answers from it: the per-prompt hook does this by itself, and in a session
or a terminal it is

    python3 scripts/hindsight-snapshot.py query "<what you are after>"
    python3 scripts/hindsight-snapshot.py lookup "<what you are after>"   # Hindsight first, then the snapshot

Every answer from it is marked "from snapshot of <date>": anything newer than
that date is not in it, and a gap there is not a gap in memory. The documents
mirrored from files are not in the snapshot, because the files are.

**The WRITE half is automatic.** Hindsight's own Claude Code plugin retains
every session on this machine, HELM, the data repository and every yard, as one
conversation document on each Stop; nothing in the engine captures anything,
and NOTHING is filtered out, by decision (2026-09-11). No machine writes records
any more: the hourly record writer (`memory-distil`) was switched off on
2026-10-14 and deleted on 2026-10-16 (HOS-202); its records stay under
`auto-memory/` with `written_by: memory-distil`.

**A memory to keep is a FILE in `auto-memory/`** (registry name
`auto-memory`), its frontmatter `written_by` naming who typed it, `operator` or
`helm`; the file sync mirrors it into Hindsight, so the file is the record and
Hindsight is the copy that recall searches.

    .venv/bin/python scripts/hindsight-remember.py --id <meaningful-slug> "<the memory, as plain factual sentences>" [--tag T]

That command is ONLY for a memory that has no file behind it. It writes the
document `memory:<slug>`, waits for the server, reads it back and reports how
many facts Hindsight extracted; it exits 2 when the answer is none, because a
memory with no extracted fact is one recall can never surface, and refuses an
existing id unless `--replace` is given. The `memory:` id is never reached by
the sync's mirror or its cleanup, and the daily snapshot copies it.

**Offer to remember**: end an answer that settles something a later
session will need with one line, "Remember: <the memory in one sentence>?".

## Economy: conduct stays resident, Hindsight holds everything else

The history behind every line in this section, with its dates and measurements,
is `memory-discipline-history.md` in the data repository under registry name
`reference-docs`.

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

Auto-memory is never pruned; deletion happens only on an explicit instruction
from the operator.

The convention written out, what `scripts/memory-hygiene.py` checks, the index's
two budgets, what consolidation does and does not mean, and how this composes
with `/recall` and auto-memory: `reference/memory-discipline-detail.md`. Read it
before writing or consolidating the index; `/memory-hygiene` and `/dream` open it
as a matter of course. The Fidelity half above stays resident because it fires on
a judgement about consequence, which no path and no skill invocation can signal.
