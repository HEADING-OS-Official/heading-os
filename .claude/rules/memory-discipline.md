<!-- version: 3.3.0 | last-updated: 2026-10-29 -->

# Memory Discipline: Source of Truth, Fetched on Demand

Last Updated: 2026-10-29
Last Verified: 2026-09-04

Always-active rule. The workspace's files are its memory; the context window is only a cache of pointers into it.

**A pointer is not a fact.** A `MEMORY.md` index hook, a recalled snippet, a skill's cached summary, a `CLAUDE.md` bullet: each is an ENTRY POINT to a record. Pointers lag their file and the file can lag the primary source, so acting on the stale layer gives an answer that is confident and wrong.

## Fidelity: open the record before acting

Before any CONSEQUENTIAL action, open and read the authoritative record, not the pointer that surfaced it. Consequential means the action touches a commitment, promise or agreement (what was owed, to whom, by when); a decision or its rationale; a fact, number, price, quantity or measurement; a deadline, date-as-state or schedule; a relationship, or a person's stated position; the live state of an ongoing matter (a negotiation, deal, plan, task).

Freshness order when layers disagree: **primary source > full record file > index hook / recalled snippet.** When a pointer contradicts its record, flag the drift; never paper over it. When a task touches a domain you hold records for, FETCH them first, and answer a consequential question from the record, never from the hook alone. Trivial, no-stakes conversation is exempt: the trigger is consequence, not volume, never "re-read everything every turn".

**Where memory lives.** Hindsight is the main memory: facts, decisions, conversation history, synthesized knowledge. Rules of conduct stay resident files loaded every session; work documents stay files in git, which Hindsight mirrors and searches; engine code stays with the code graph.

**Ask Hindsight first, for records**, before the directory: in a session through the `hindsight-main` MCP server's query tools (`recall`, `reflect`, `list_documents`, `get_document`) or the plugin's `hindsight_search_knowledge_pages`; from a terminal, `hindsight memory recall main "<what you are after>"`. Engine code is the code graph's (`codegraph_explore`), never Hindsight's. The local memory index stays beside it: `scripts/memory-index.py`, `/recall` and the per-prompt pointers are unchanged.

A Hindsight hit is still a POINTER: open the document a recalled fact names before acting on it. `check_hindsight_first` in `.claude/hooks/_dispatch.py` refuses the session's first lookup aimed at the records until one Hindsight query has been ATTEMPTED; an attempt that errors unlocks, so nothing can wedge, and a machine with no Hindsight key file gets no wall. A write tool (`retain`, `create_*`, `delete_*`) never unlocks it.

**When Hindsight does not answer, answer from its snapshot, and say so.** `scripts/hindsight-snapshot.py take` copies what lives ONLY in Hindsight (observations, mental models, directives, the knowledge-base export, documents with no file behind them, conversations and remembered memories among them) daily into `runtime:hindsight-snapshot/cache`, which `hindsight-sync.py` never sends back; the local memory index holds it as the collection `hindsight-snapshot`, outside `content`. When Hindsight does not answer (refused connection, timeout, an HTTP error such as a refused key), the per-prompt hook falls back by itself; in a session or a terminal:

    python3 scripts/hindsight-snapshot.py query "<what you are after>"
    python3 scripts/hindsight-snapshot.py lookup "<what you are after>"   # Hindsight first, then the snapshot

Mark every answer from it "from snapshot of <date>": nothing newer is in it, and a gap there is not a gap in memory. Documents mirrored from files are not in it, because the files are.

**The WRITE half is automatic.** Hindsight's own Claude Code plugin retains every session on this machine (HELM, the data repository, every yard) as one conversation document on each Stop; the engine captures nothing, and NOTHING is filtered out, by decision (2026-09-11). No machine writes records; files under `auto-memory/` with `written_by: memory-distil` are older machine-written ones.

**A memory to keep is a FILE in `auto-memory/`** (registry name `auto-memory`), its frontmatter `written_by` naming who typed it, `operator` or `helm`; the file sync mirrors it into Hindsight, so the file is the record. Only a memory with no file behind it uses

    .venv/bin/python scripts/hindsight-remember.py --id <meaningful-slug> "<the memory, as plain factual sentences>" [--tag T]

which writes the document `memory:<slug>`, reads it back and reports how many facts Hindsight extracted; it exits 2 when none were, because recall could never surface that memory, and refuses an existing id unless `--replace` is given. The sync's mirror and cleanup never reach a `memory:` id; the daily snapshot copies it.

**Offer to remember**: end an answer that settles something a later session will need with one line, "Remember: <the memory in one sentence>?".

## Economy: conduct stays resident, Hindsight holds everything else

The rules of conduct are the files in `.claude/rules`, loaded every session. Add rules there, never into `MEMORY.md`, and never deliver them through a hook. Auto-memory is never pruned; deletion happens only on an explicit instruction from the operator. Before editing the instruction chain or `MEMORY.md`, read § Economy of `reference/memory-discipline-detail.md` (the orphan rule, the weekly gate, the index's two budgets, what consolidation means); `/memory-hygiene` and `/dream` open it as a matter of course.
