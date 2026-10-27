---
name: memory-hygiene
description: Objective-defect detector for the memory ecosystem (auto-memory + Odin brain). Runs scripts/memory-hygiene.py, which flags ONLY mechanically-verifiable rot - dangling/circular superseded_by refs, orphan memory files (reachable through neither MEMORY.md nor the Hindsight bank `main`), and an over-budget MEMORY.md - into a dated report, and exits non-zero when any are present. It never mutates memory. Use when the user says "memory hygiene", "check memory health", "memory rot", "scan memory for defects", or wants to know whether the memory store has accumulated objective defects. Do NOT use to consolidate/merge/delete/reword memory (that is judgement - use /dream); or to recall a past fact (use /recall).
argument-hint: ""
allowed-tools: "Read, Bash(.venv/bin/python:*)"
model: haiku
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-capability:
  what: >
    Surfaces only the objective, deterministically-verifiable defects across
    auto-memory and the Odin brain (dangling/circular superseded_by, orphan
    memory files, over-budget MEMORY.md) into a dated report; never mutates
    memory.
  how: >
    Run /memory-hygiene; it executes scripts/memory-hygiene.py, which writes
    YYYY-MM-DD_memory-hygiene_report.md into the registry name
    memory-hygiene-reports, and presents the gate defects plus advisory
    signals. Resolution stays with /dream.
  when: >
    Use to check whether memory has accumulated objective rot, on demand or on a
    weekly cadence. For consolidation/merge/delete use /dream; to recall a fact
    use /recall.
x-heading-routing:
  category: Operations
  triggers:
    - memory hygiene
    - check memory health
    - memory rot
    - scan memory for defects
  exclusions:
    - Consolidate/merge/delete memory -> /dream
    - recall a fact -> /recall
  compound: 'No'
  router: auto
---
# Memory Hygiene

Detect objective memory defects. This skill is a detector, not a fixer: it surfaces deterministically-verifiable rot and hands resolution to `/dream`. It NEVER merges, deletes, or rewrites memory.

## Phase 0 - Run the detector

```bash
.venv/bin/python scripts/memory-hygiene.py
```

The script:
- Reads the canonical auto-memory dir (`<data-root>/auto-memory/`) and computes orphans, BOTH index-budget breaches (200 lines and 25000 UTF-16 units), and stale files. An orphan is a record that NEITHER MEMORY.md links NOR the Hindsight bank `main` holds as a document (id `auto-memory/<name>.md`, or a `#part-NN` of it). Since 2026-09-20 the index links no record at all. The bank is therefore the only source that can still say no. When the bank does not answer, or names under half the corpus, the script drops the orphan category from the gate. The script only reads the bank, never writes to it. The run then prints `not evaluated this run: orphans`. That is an UNKNOWN, never a clean result. Do not read it as a reason to put pointers back into MEMORY.md.
- Runs `odin-brain-health.py --compile` and reads its temporal-validity errors.
- Writes `YYYY-MM-DD_memory-hygiene_report.md` to the registry name `memory-hygiene-reports`. The run prints the destination. To open it by hand, resolve the name first: `.venv/bin/python scripts/data-path.py path memory-hygiene-reports`.
- Prints a one-line summary and exits `0` (clean) or `1` (objective defects present).

If the Odin brain is unavailable, the script degrades clearly and still reports the auto-memory half.

## Phase 1 - Present the result

Read the report it wrote and present, grouped:

1. **Objective defects (the gate)** - dangling/circular `superseded_by`, orphan memory files (in neither MEMORY.md nor the Hindsight bank), `MEMORY.md` over either budget. These are the items that need action. Read the `not evaluated this run:` note beside the count. An unevaluated category is an unknown. Report it as unknown, never as clean.
2. **Advisory (non-gating)** - stale files (>45 days), Odin stale seeds/positions, orphan principles, near-duplicate pairs, volatile pointers, and dangling `[[wikilinks]]`. Mention counts; do not treat as failures.

One more advisory sits with them: the index past 80% of its unit budget. Act on that one before it becomes a gate. The harness truncates an oversized index rather than refusing it. By the time that category gates, entries have already stopped loading.

Carry the remedy the script prints. Do not improvise a different one: reorder the sections, or move one into its own file. Never propose shortening the labels. The label is the retrieval signal, and past trims cost recall quality (operator, 2026-09-09).

Read the dangling-link list for breadth, not for count. One unresolved link marks a memory worth writing later, which the convention allows. A target cited by several files is different: it usually means those files share a premise that died with the pointer. Name such a target to `/dream` explicitly.

State the exit posture plainly: "clean" or "N objective defect(s)".

## Phase 2 - Hand resolution to /dream

If objective defects exist, recommend `/dream` to resolve them (consolidation is judgement). Do NOT fix them here. Name the specific defects so `/dream` has a worklist.

## NEVER

- NEVER merge, delete, reword, or otherwise mutate any memory file - resolution is `/dream`, human-gated.
- NEVER include judgement defects (semantic contradictions, near-duplicate facts, stale-but-maybe-valid positions) in the gate; those are advisory only.
- NEVER claim the brain half ran when the script reported it unavailable - surface the degradation.
