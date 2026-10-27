---
name: calibrate
disable-model-invocation: true
description: >
  Reflective end-of-session self-improvement. Scans the current Claude Code
  session for corrections, preferences, repeated patterns, errors, success
  patterns, and voice violations, then proposes numbered concrete patches to
  memory, settings, skills, and rules. Approved memory patches are written to
  the auto-memory store; every other patch is written as a proposal into the
  calibrate-reviews queue and is NEVER auto-applied. Commits nothing. Use at
  end of every working session. Light mode for low-token state or quick sweeps.
argument-hint: "[light]"
allowed-tools: "Read, Edit, Write, Bash(.venv/bin/python:*), Glob, Grep"
context: fork
model: sonnet
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.2"
x-heading-capability:
  what: >
    Reflective end-of-session self-improvement - scans the session for corrections,
    preferences, errors, and voice violations, then proposes numbered patches to memory,
    settings, skills and rules. Memory patches are applied to the auto-memory store; settings,
    skill, rule and engine patches go to a review queue and are never auto-applied. CEO-only.
  how: >
    Run /calibrate (explicit-invocation-only, never auto-triggers). Full mode parses the
    session JSONL; /calibrate light scans only the in-context conversation. Writes approved
    memory patches and puts every other proposal under calibrate-reviews; it commits nothing.
  when: >
    Use at the end of a working session to capture learnings. For cross-session memory
    consolidation use /dream; for checking a single artifact run scripts/artifact-evaluator.py.
x-heading-routing:
  category: Operations
  label: /calibrate [light]
  triggers:
    - NEVER auto-trigger. Explicit `/calibrate [light]` only.
  exclusions:
    - Cross-session memory hygiene -> /dream
    - quality check on a single artifact -> scripts/artifact-evaluator.py
  compound: 'No'
  router: manual
---

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Resolve the name before the file operation and
use what it prints:

    .venv/bin/python scripts/data-path.py path <name>

Use `read <name>` for a source that may legitimately be absent, and
`read --required <name>` for one whose absence is a defect. An unknown name
refuses; it never resolves to a plausible directory, and a path written out
here goes stale the day the directory moves.

# /calibrate - End-of-Session Self-Improvement

Scans the current session for friction and successes. Proposes numbered
patches grouped by target (Memory / Settings / Skills / Rules). Approved
memory patches are written to the auto-memory store. Every Settings, Skills
and Rules patch, and every engine-routed candidate, is written as a proposal
into the review queue under `calibrate-reviews` in the data repository. None
of them is ever auto-applied. /calibrate makes no git commit: the data repository's git
belongs to HELM. Applying a skill or rule proposal is an ordinary approved
yard change; a settings proposal is applied by HELM.

Design spec: `superpowers/specs/2026-05-13-calibrate-skill-design.md` under `spec-docs` in the data repository (resolve with `.venv/bin/python scripts/data-path.py path spec-docs`).
Detection prompts: `references/detection-prompts.md`.

## Memory store

Memory patches target the canonical auto-memory store in the DATA overlay, the
same store `/dream`, `/memory-hygiene` and the memory index read. Resolve it:

```bash
.venv/bin/python -c "from scripts.utils.workspace import get_auto_memory_dir; print(get_auto_memory_dir())"
```

Do NOT write to the native harness store at
`~/.claude/projects/<project-slug>/memory/`. That store is a per-launch runtime
cache, keyed to the directory the session started in. A patch written there is
invisible to a session that launched from any other path. The SessionStart hook
`.claude/hooks/memory-reconcile.py` bridges the two stores. It syncs both
directions, newest-wins, and never deletes, so the patch is not lost. It does
not reach the canonical store or the memory index until the next session starts.

## Phase 0 - Pre-flight + mode dispatch

Run these checks before any other work. Each failure has a specific recovery
message; never abort silently.

1. **Mode dispatch.** Parse `$ARGUMENTS`:
   - Empty / no flag -> full mode
   - `light` or `--light` -> light mode

2. **Full mode only:** verify `scripts/calibrate.py` exists. If missing,
   abort: "Parser script missing. Cannot run full mode. Try `/calibrate light`."

3. **All modes:** verify `config/routing-map.yaml` exists. If missing,
   abort with: "Routing map missing. /calibrate's safety filter
   cannot run. Aborting."

## Phase 1 - Acquire signal envelope

### Full mode

Invoke the parser:

```bash
SCRATCH="$(.venv/bin/python scripts/data-path.py path --create runtime:scratch/tmp)"
.venv/bin/python scripts/calibrate.py > "$SCRATCH/calibrate-envelope.json"
```

Read `$SCRATCH/calibrate-envelope.json` via the Read tool. Validate the envelope
has the expected keys (`user_turns`, `assistant_turns`, `tool_errors`,
`system_reminders`, `workspace`). If validation fails: report parser error
and abort.

If `envelope["truncated"]` is true, surface a one-line notice in Phase 4
preamble: "Note: session exceeded budget. Oldest user turns truncated. Recent
corrections still captured."

If `envelope["event_count"]` is 0: exit cleanly with "Session produced no
actionable events. Nothing to calibrate."

### Light mode

Skip the parser. Use the current in-context conversation directly. Announce in
the Phase 4 preamble:

> Light mode active. Scanning only in-context conversation (no JSONL parse).
> Categories 3 (repeated patterns) and 4 (errors / friction) skipped - they
> require the structured envelope. Long sessions may have auto-compacted early
> turns - those signals are not recoverable here. Run `/calibrate` (full)
> for the complete pass.

## Phase 2 - Six-category detection

Load `references/detection-prompts.md` via Read. Walk the envelope through the
six detection prompts in order. Each detected signal becomes a candidate of
the shape specified in detection-prompts.md.

| # | Category | Full mode | Light mode |
|---|---|---|---|
| 1 | Corrections | kept | kept |
| 2 | Preferences | kept | kept |
| 3 | Repeated patterns | kept | SKIPPED |
| 4 | Errors / friction | kept | SKIPPED |
| 5 | Success patterns | kept | kept |
| 6 | Voice violations | kept | kept |

For each candidate, also run the **idempotency check** from
detection-prompts.md: Grep the proposed target file for substring match of
the proposed diff body. If matched, drop the candidate silently. This
prevents duplicate patches.

Also: NEVER propose patches to `~/.claude/CLAUDE.md` (the global private
CLAUDE.md). V1 scope is per-project memory only.

## Phase 3 - Classification filter

For each candidate, resolve the target file's routing destination with
`get_routing_destination()`, the canonical classifier. Give the path relative
to its own repository root and name that repository with `tree=`:
`auto-memory/...` with `tree='data'`, `.claude/...` with `tree='engine'`. A
relative path without `tree=` leaves the resolver to guess the repository, so
always pass it.

```bash
.venv/bin/python -c "import sys; sys.path.insert(0,'.'); from scripts.utils.workspace import get_routing_destination; print(get_routing_destination('<target-path>', tree='<data|engine>'))"
```

`scripts/utils/workspace.py` is a library. It declares no argument parser and no
`__main__` block, so running it as a script reads no argument, prints nothing,
and exits 0. Call the function, as the one-liner above does.

Read the result as a table. Silence is not an answer:

| Command result | Read as | Routing |
|---|---|---|
| exit 0, stdout `private` | resolved | Phase 4 numbered list |
| exit 0, stdout `engine` | resolved | review queue (Step 5.2) |
| exit 0, stdout empty or any other text | UNRESOLVED | review queue (fail-closed) |
| exit != 0 | UNRESOLVED | review queue (fail-closed) |

Name every fail-closed candidate in the Phase 4 preamble, with the reason. A
candidate you queue in silence looks the same to the reader as a genuine
engine one.

The resolver reads the path, not the disk, so a candidate whose target file does
not exist yet resolves the same way. This needs no separate new-file rule.

Expect most `.claude/skills/` and `.claude/rules/` targets to resolve
**engine**. Engine files are the public repository and infrastructure. An edit
to one needs the operator's explicit approval, and the operator makes it in a
yard. This skill never auto-applies it. A queued skill or rule patch is the gate
working, not a fault.

## Phase 4 - Grouped numbered presentation

Sort the private candidates per the sort order in detection-prompts.md.
Group by target category (Memory / Settings / Skills / Rules). Number
sequentially across groups. Approving a Memory candidate applies it (Step
5.1). Approving a Settings, Skills or Rules candidate writes it into the review
queue as a proposal (Step 5.2).

### Auto-include: thread-log capture

Before presenting Phase 4, check whether the session edited any file under
the business or personal layer of `threads`. If yes, prepend a special
candidate to the Memory group (or Skills group if the thread skill itself
needs an entry):

```
0. [PROJECT-LOG]  Append a session-log entry to {layer}/{slug}.md under threads
   -> {layer}/{slug}.md under threads
   Session touched this thread; capture key decisions, files changed, open
   questions, and what's next so the next session resumes cleanly.
```

If multiple threads were touched, list one candidate per thread. Skip
entirely if no thread files were read or written.

This is a CEO-only auto-include. The `threads` tree is CEO-only per the
workspace classification rule.

### Numbered candidate presentation

Display format and strict input grammar: `references/presentation-format.md`.

Wait for user input after rendering. Re-prompt rather than guess on
unrecognised input.

## Phase 5 - Apply memory, queue the rest

After user approval, in this exact order:

### Step 5.1: Apply memory patches

For each approved Memory candidate (in sorted order), in the canonical
`auto-memory/` store resolved above (a `[PROJECT-LOG]` candidate appends to its
thread file instead):
- If patch is "append": Edit to add the new bullet under the body's main rule
- If patch is "update": Edit to replace the affected section
- If patch is "create new": Write a new kebab-case `<slug>.md` file. Use no
  underscore, matching every name already in the store. Give it full
  frontmatter in the store's shape:

  ```yaml
  ---
  name: <slug>
  description: "<one sentence>"
  metadata:
    node_type: memory
    type: <feedback|project|user|reference>
    written_by: <helm|operator>
  ---
  ```

  `written_by` is `operator` when the operator dictated the text and `helm`
  when this session wrote it. Write nothing into `MEMORY.md`: it links no
  record, and the file sync mirrors the new file into Hindsight, where recall
  reaches it.

If any Edit fails (old_string no longer unique, file changed since Phase 1, etc.):
stop immediately. Report: "Patch {N} failed: {reason}. Patches 1 through {N-1}
applied. Patches {N} through {M} skipped. Run /calibrate again to retry." Skip
Steps 5.2-5.4 entirely.

### Step 5.2: Write the review queue (only if any candidates go there)

Every approved Settings, Skills and Rules candidate, and every candidate
Phase 3 routed to the queue, is a proposal, never an edit. Write them into a
single file:

```
$(.venv/bin/python scripts/data-path.py path calibrate-reviews)/{YYYY-MM-DD}_engine-review.md
```

Format per the `references/detection-prompts.md` section on engine review
queue patches. The fields are source quote, category, proposed target, and the
proposed diff in a fenced code block (read-only format, NOT git-applyable). Then
the rationale, then how it is applied: a skill or rule proposal as an ordinary
approved yard change, a settings proposal (`.claude/settings.local.json`) by
HELM.

If no candidate goes to the queue: skip this step. Do not write an empty file.

### Step 5.3: Sanitisation pass

Run `.venv/bin/python scripts/sanitize-text.py <file> --scan` once per file that Steps
5.1 and 5.2 modified or created; it takes one file. If hidden Unicode is
detected, run `.venv/bin/python scripts/sanitize-text.py <file>` (without `--scan`) to
clean it, and say so in the final report.

### Step 5.4: Report final state

Final state report template: `references/patch-application-protocol.md`.
Everything this run wrote stays uncommitted in the data repository; HELM
commits it on the operator's approval.

## NEVER

1. **Never auto-apply a settings, skill, rule or engine patch.** Those only
   go to the review queue. Engine files are the public repository and
   infrastructure. An edit needs the operator's explicit approval, and the
   operator makes it in a yard. A settings proposal is applied by HELM.
2. **Never write to `~/.claude/CLAUDE.md` (global).** V1 scope is per-project
   memory only.
3. **Never rollback automatically on partial failure.** Stop on first error,
   report, let the user decide.
4. **Never run silently on ambiguous input.** Strict grammar in Phase 4 -
   re-prompt rather than guess.
5. **Never propose duplicate patches.** Idempotency check before every
   candidate goes into the numbered list.
6. **Never commit and never push.** Everything this run writes stays
   uncommitted; git in the data repository is HELM's.
7. **Never write to the native harness memory store.** Memory patches go to the
   canonical `auto-memory/` store only.
8. **Never read an empty resolver result as a classification.** Empty stdout and
   a non-zero exit both mean the review queue.

## Error handling

Specific recovery messages per failure mode. See spec section 9 for the
complete matrix. Always provide actionable recovery; never raw tracebacks.

## Voice rules

- Single hyphens `-` in prose, never `--`.
- No em-dashes in any prose Claude generates within this skill.
- Product names and terms exactly as `.claude/rules/terminology.md` gives them.
- Language matches the user's language (Russian question = Russian output).

### Output-prose plain-English mandate

Candidate summaries, bodies, and rationales must be plain-language so the
user can scan and pick `apply 1, 3, 5` in seconds. Banned-vocabulary table,
plain-form replacements, and worked good/bad examples:
`references/output-prose-style.md`.

If a 12-year-old reading the candidate out loud would not roughly
understand it, rewrite it.

## Examples

Two end-to-end runs cover light mode with 2 candidates and a clean session
with no actionable events. They live in `examples.md` in the data repository at
`$(.venv/bin/python scripts/data-path.py path skill-examples skill=calibrate)/examples.md`. A public clone does not have it.
