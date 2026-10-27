---
name: prime
description: Initialize a working session. Loads full workspace context (personal info, business, strategy, current data, pipeline, key contacts, datastore index), runs CRM and sync health checks, surfaces what needs attention, and lists available skills with recommendations for the day. Use at the start of every working session. NEVER auto-trigger from natural language - this is an explicit "/prime" command only.
argument-hint: "(no arguments)"
disable-model-invocation: true
allowed-tools: "Read, Bash(.venv/bin/python:*)"
model: sonnet
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.3"
x-heading-capability:
  what: >
    Initializes a working session - loads full workspace context (personal, business, strategy, current data, pipeline, key contacts, datastore index), runs CRM/knowledge/sync/daemon health checks in parallel, surfaces what needs attention, and recommends the day's skills.
  how: >
    Explicit-invocation only (disable-model-invocation) - type /prime. Health block runs in-process via scripts/prime-health-parallel.py; ends by asking what to work on today.
  when: >
    Use once at the start of every working session. Mid-session, for a next-step recommendation use /next; for the daily briefing alone use /dashboard.
x-heading-routing:
  category: Operations
  triggers:
    - NEVER auto-trigger. Explicit `/prime` or "prime" only.
  exclusions:
    - All natural language
  compound: 'No'
  router: manual
---

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Ask the workspace's data-address resolver for
the real address before the file operation, and use what it answers. An unknown
name refuses rather than resolving to a plausible directory, and a path written
out here goes stale the day the directory moves.

# Prime

> Initialize the session. Load full context. Surface priorities. Recommend today's agents.

---

## Read

First, read `.workspace-identity.json` to determine workspace type. `ceo-master` is the only type there is; an absent file means a public clone.

CLAUDE.md is already auto-loaded by the Claude Code harness (visible in the system prompt's `claudeMd` block) - do NOT re-read it.

Load the following files in this order. Each entry is a LOGICAL name, not a path. Resolve it with `.venv/bin/python scripts/data-path.py path <name>` and read what that prints.

1. `personal-info` - who the operator is
2. `business-info` - the company and its products
3. `strategy` - strategic priorities and arc
4. `current-data` - current metrics, milestones, active workstreams
5. `people` - **read only the "Top 10 Active Contacts (summary)" section.** Full file remains available - Read `people` when a specific named contact requires deeper context.
6. `pipeline` - active deals and investor conversations
7. `workspace-overview` - **read only the H1 + "## Index" section.** Defer the per-section detail to lazy-read when a specific tool, script, or system needs deeper context.
8. `datastore-index` - **read only the H1 + "## Top-Level Structure (summary)" section.** Defer the per-document tables to lazy-read when a specific fact needs validating.

---

## Summary

After reading, provide a structured session brief:

## Setup-wizard status check (first line of brief when incomplete)

If `.workspace-identity.json` is absent (a public clone), run:

`.venv/bin/python scripts/apply-wizard-answers.py --status`

Parse the returned `completion_pct`. If it is `< 100`, prepend this line to the brief above every other section:

> Setup is `<completion_pct>`% complete - `<required.pending + required.skipped>` required questions open. Run /setup-wizard to finish.

Skip entirely when:
- `.workspace-identity.json` exists (the CEO master workspace; any other type makes `--status` exit non-zero anyway), OR
- `--status` exits non-zero, OR
- `completion_pct >= 100`.

### 1. Context Confirmation
Brief statement: who the user is, what the company is, where we are (post-launch, deployments in progress), current operational state. Include workspace type (CEO master or public clone) as detected from `.workspace-identity.json`.

### 2. Pipeline Pulse
Summary of active deals, investor conversations, and partnership discussions from pipeline.md. Flag anything requiring immediate attention.

### 2.1 Previous Session Handoff

Read THIS checkout's pointer: `.venv/bin/python scripts/checkpoint-paths.py` prints its
resolved path as `checkout_summary_pointer`. It is the newest handoff written
from this checkout, by the checkpoint hooks, so HELM and each YARD see only
their own. If the file is absent, skip this section and do not mention
handoffs. Never read `.latest/summary.md` under `runtime:handoff/state` instead:
that machine-wide pointer is retired, and what it holds may be another
checkout's handoff (HOS-158).

The file carries four things this section needs. A `Source:` line names the full
archive. A `Generated:` line gives the ISO timestamp. A `Trigger:` line says what
caused the save. A `## Next steps` list follows them. Present:

> ## Previous session handoff
> Generated: {Generated}
> Trigger: {Trigger}
> Next: {the first bullet under `## Next steps`}
> Full record: {Source}
>
> Resume this work, or start fresh?

On **resume**, read the archive named on the `Source:` line — that file is the
record, and the summary here is a bounded copy of it. On **fresh**, say nothing
further and move on. Archive NOTHING: the handoff already IS the archive, and
`.latest/` holds pointers that the next save overwrites on its own.

This section covers a session started cold. After a compaction, a `/clear`, or a
`--resume`, `.claude/hooks/checkpoint-inject.py` injects the same handoff at
SessionStart. The compaction summary in context is fresher than the pointer.
Do not repeat it.

### 2.4-2.19 Parallel Health Block

Run the health checks in parallel via one helper:

```bash
.venv/bin/python scripts/prime-health-parallel.py
```

**Run it from the launch directory, the engine clone; never `cd` into the data repository, which has no `scripts/`.** The scripts resolve all data themselves. If the helper errors, debug it in place. Never the bare `python`. On this machine it is the system 3.12 without the pinned packages. MEASURED 2026-10-01: it made the Memory panel print `orphans NOT checked`, because the system interpreter lacked a package the memory panel needed.

This single invocation dispatches every check in the `CHECKS` registry of `scripts/prime-health-parallel.py` to a `ThreadPoolExecutor(max_workers=8)` and prints the aggregated output in the order /prime expects. The registry is the source of truth for the list. Since the 2026-09-21 test-suite split nothing holds this page to it, so a check added there has to be written in here by hand. Today it carries sixteen. The first is Session hooks armed. Then CRM health, Knowledge health, Memory file scan, the Hindsight memory server, Email-Intel state, and Threads archive-scan. Then Team-bot daemon, Sync-Exchange daemon, Odin cadence, Ops-radar, Reminders due, Dream-shadow worklist, the component update check, the Lific board, and the document version registry. Session hooks armed, the Hindsight panel and the last seven render nothing when they have nothing to say:

- **### 2.4 Session Hooks** -- whether `.claude/settings.local.json` registers every session hook its platform template defines. It is gitignored, so a clone that never ran `bash scripts/setup-platform.sh` has none of them. Renders nothing when armed. A short clone gets the missing list and the fix command.
- **### 2.5 Relationship Radar** -- RED contacts (overdue), YELLOW contacts (approaching), Active commitments due in the next 7 days, Total contacts tracked / individual CRM files. CEO workspace also surfaces company-wide CRM from the operator's own aggregate under the registry name `crm-aggregated`.
- **### 2.7 Knowledge Base Health** -- total notes and status breakdown (seeds / growing / evergreen). Also stale seeds (>7 days old still seed status), orphan notes count, and top 5 keywords.
- **### 2.9 Memory Health** -- count of memory files, MEMORY.md against BOTH budgets (N/200 lines, U/25000 units), files >45 days flagged. Also orphan files, which sit in the memory directory and are reachable through neither MEMORY.md nor the Hindsight bank `main`. Since 2026-09-20 the index links no record. The bank is therefore the only source that can say no. If it did not answer, or names under half the corpus, the panel says `Orphans NOT checked` rather than counting zero. All clean = "Memory: N files, M/200 lines, U/25000 units. All healthy." The harness truncates the END of an oversized index rather than refusing it. So the panel warns from 80% of the unit budget. Its only remedy: reorder the sections, or move one into its own file. Never shorten the labels.
- **### 2.9a Hindsight Memory Server** -- the read-only half of the hourly check, `scripts/hindsight-health.py show`. It reads the tunnel and API, the web UI and the local verified backup copy. It also reads the server's own snapshot: services, queue movement, LLM 401/403, certificate, disk, backups. It never notifies and never writes the check's state. Renders nothing when healthy; otherwise one line per problem, or `state UNKNOWN` when the check itself failed or timed out.
- **### 2.10 Email Intelligence Status** -- last_run age vs 20-hour threshold, pending P1 task count from tasks.md. "Never run" surfaced when state.json missing.
- **### 2.11 Active Threads archive scan** -- dry-run results from `thread.py archive-scan`. Failure of this check never blocks /prime; the helper degrades the panel gracefully.
- **### 2.13 Sync-Exchange Daemon** -- daemon liveness check via `daemon.pid` under `runtime:sync-exchange/tmp` (`.venv/bin/python scripts/data-path.py path runtime:sync-exchange/tmp`). If daemon is dead, pulse spawns a detached `sync-exchange-daemon.py daemon`. If alive, reports pid + relative time of the last successful sync (parsed from `daemon.log` under `runtime:sync-exchange/logs`). The daemon runs `.venv/bin/python scripts/sync-exchange.py --calendar --emails` every 2 hours; the first run fires immediately on daemon start.
- **### 2.19 Lific Board** -- the tracker's open work, from `scripts/lific-board.py`. Per project it counts three statuses. `active` means a yard holds the issue. `todo` means the operator approved it and it is ready to hand out. `backlog` means Claude captured it and nobody has triaged it yet. Below the counts sit the issues whose work is finished where no merge will close them. A yard asks for that by opening its closing comment with the marker. This panel is the only surface that shows them. It renders nothing when every project is idle, and nothing when `lific` is absent. It only READS. Closing an issue stays the operator's, per `.claude/rules/lific.md`. Before the board it flushes the task-dossier outbox (`scripts/dossier-journal.py flush`) and adds one line when `[dossier]` records still wait for Lific. It also shows, once, each issue that the first saved prompt after /clear dropped, with its relink command (HOS-259).

After the parallel block prints, render the **Active Threads panel**. This part is not in the parallel script, because it must read the per-thread files:

1. Read the live set from disk:

   ```bash
   .venv/bin/python scripts/thread.py list --status active
   ```

   Each line is `[status] <type>/<slug> - <title> (last_touched: DATE)`. A quiet
   thread carries a suffix: `[quiet until DATE]` for a dated pause, or
   `[quiet indefinitely]` for a freeze with no end date. Never surface a thread
   that carries either suffix. If the command prints nothing, no threads are
   open: skip the panel silently and continue.

2. For each thread, read its `## Open follow-ups` section from
   `.venv/bin/python scripts/thread.py show <slug>`, where `<slug>` is the part after
   `<type>/` in the list line.
3. Count unchecked `- [ ]` items.
4. Render as:

```text
## Active Threads (N)

Business (M):
  - <title> - last <DATE> [K follow-ups open]
  ...

Personal (P) [CEO-ONLY]:
  - <title> - last <DATE> [K follow-ups open]
  ...
```

The archive-scan results from the parallel block above feed this section: if candidates exist, surface them with "Archive candidates: <list>. Run `.venv/bin/python scripts/thread.py archive-scan --apply` to archive." If no candidates and no error, render no panel (silent success).

A failing health check in the parallel block never blocks the others; one failure is reported inline and /prime continues.

### 3. Upcoming Events & Deadlines
From current-data.md: any events, meetings, or deadlines in the next 30 days.

### 4. Strategic Heading
Current strategic heading and any drift signals visible from context.

### 5. Data Freshness
Check context file headers for `> Last verified: YYYY-MM-DD` dates. Flag any context file older than 30 days as stale. Note if DataStore has key documents loaded or is still sparse.

Also check sync output files. Three of them count: `exchange-calendar-cache` and `exchange-email-cache` (`.venv/bin/python scripts/data-path.py path exchange-calendar-cache`, and the same for the other), and the email-intelligence `state.json` (`.venv/bin/python scripts/data-path.py path email-intel-state`). Flag any whose mtime is older than 48 hours RED, with the literal label "SYNC STALE". The line format is:

> SYNC STALE: exchange-calendar-cache upcoming.md last refreshed Ndays ago — run `.venv/bin/python scripts/sync-exchange.py` (or expected automation has stopped).

This catches a silent sync failure within one /prime cycle rather than 2 weeks later. The 48-hour threshold is intentional - allows a weekend gap, surfaces anything longer. If all sync files are fresh, the section reports "Sync data: fresh (all <48h)."

### 5.1 Terminal Entry Points

The operator asks "how do I see X from here". Answer with a command he can type.
Read `references/terminal-entry-points.md` first. It carries sixteen rows, each
run when it was added, each naming what the command leaves out, and the three actions
no command reaches. Do not print the table unprompted, and never run a row here.

### 6. Available Skills

The canonical skill registry lives in `.claude/rules/skill-router.md` (already loaded as an always-active rule, so no re-read needed). Surface a CEO-facing catalog by:

1. Pulling skill names + one-line triggers from the skill-router skill registry tables (Intel / Communication / Content / CRM / Design / Strategy / Operations).
2. Suggest the 2-3 most contextually relevant skills for today, drawing on the pipeline pulse and active threads loaded earlier.
3. For a state-aware next-step recommendation mid-session, point the CEO at `/next`. It reads what just happened and names the logical next command.

This section deliberately defers to the router rather than duplicating the catalog inline. The catalog lives only in the router, generated from each skill's `x-heading-routing` frontmatter; `scripts/generate-skill-router.py --check` (CI and pre-commit) verifies it matches.

### 7. Ready

Confirm ready to execute. Ask: "What are we working on today?"
