# /zk - subcommand procedures

Consumed by: `.claude/skills/zk/SKILL.md`, section "Subcommand dispatch". Holds
the full step-by-step procedure for each of the eight subcommands, plus the note
file template used by `add` and `distill`. Read the section for the subcommand
you are about to run, before you run it.

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Resolve the name before the file operation and
use what it prints:

    .venv/bin/python scripts/data-path.py path <name>

Use `read <name>` for a source that may legitimately be absent, and
`read --required <name>` for one whose absence is a defect. An unknown name
refuses; it never resolves to a plausible directory, and a path written out
here goes stale the day the directory moves.

Last Updated: 2026-10-16

---

### `add [type]`

Create a new note in the knowledge directory.

1. Parse remaining arguments for note type. Valid types: `fleeting` (default), `signal`, `decision`, `meeting`, `research`, `strategy`, `people`, `technology`
2. If the user provided content inline, use it. Otherwise ask: "What's the idea?" (one question only)
3. Generate the note ID: current timestamp as `YYYYMMDDHHMMSS`. **The ID must be
   unique across the knowledge root.** A second-resolution stamp is not unique on
   its own: `distill` creates three to seven notes in one batch, inside the same
   second, and `connect` and `garden` address notes by `[[ID|Title]]`, so a
   repeated ID silently points two links at one note. Before writing, glob
   `{knowledge_root}**/{ID}-*.md`; if anything matches, add one second and check
   again until the ID is free.
4. Generate slug from the core idea (kebab-case, max 6 words)
5. The knowledge root is the registry name `knowledge-base`
   (`python scripts/data-path.py path knowledge-base`). Every `/zk` subcommand
   writes, searches, links and summarises under that one name, and the
   Hindsight file sync mirrors every note written there.
6. The target subdirectory is the note type, RELATIVE to the knowledge root:
   - fleeting -> `fleeting/`
   - signal -> `signal/`
   - decision -> `decision/`
   - meeting -> `meeting/`
   - research -> `research-notes/` (`research/` belongs to the registry name
     `research-surveys`, so a note there is never read as a `/zk` note)
   - strategy -> `strategy/`
   - people -> Redirect: "People intel belongs in CRM. Use `/crm add`."
   - technology -> `technology/`

   A destination must be one `python scripts/knowledge-health.py --json`
   reads: never a name in its `excluded_subdirs` (the Odin brain, `shared`, and
   every child another registry name owns). A new type directory is read
   automatically once it exists. Learned knowledge for the Odin brain goes
   through `/odin`, not `/zk`.
7. Create the note file `{knowledge_root}/{subdir}/{ID}-{slug}.md` with the
   template below. Its required fields are `REQUIRED_FIELDS` in
   `scripts/knowledge-health.py`, and `type` takes the `/zk` type named in
   step 1.

```markdown
---
id: "{ID}"
title: "{Title}"
type: {type}
keywords: [{keywords}]
status: seed
created: {YYYY-MM-DD}
updated: {YYYY-MM-DD}
source: ""
confidence: medium
---

# {Title}

{Core idea - one paragraph max. Atomic: one idea per note.}

## Context

{Why this matters. What prompted it. Connection to current state.}

## Connections

{Links to related notes and workspace files - add as discovered.}

## Open Questions

- {What this note raises but doesn't answer}

---
*Origin: manual*
```

8. Run `.venv/bin/python scripts/sanitize-text.py` on the created file to validate
9. Confirm with: "Note created: `{path}`. Status: seed." followed by the
   confirmation line from `.claude/rules/hidden-chars.md`, carrying what the
   scan actually reported.

---

### `enrich [note]`

Read a seed or growing note, research it, add connections, and upgrade its status.

1. Find the target note:
   - If a path is given, read it directly
   - If a keyword/title is given, search the `/zk` note directories (the `scanned_subdirs` of `.venv/bin/python scripts/knowledge-health.py --json`: every child of `knowledge-base` that no other registry name owns, so never the Odin brain, `intelligence`, `book-library` or `research-surveys`) with Grep
   - If multiple matches, list them and ask which one
2. Read the note content and frontmatter
3. Based on note type, perform enrichment:
   - **signal/research/technology**: Run 2-3 targeted WebSearch queries related to the note's core idea. Add findings under Context.
   - **people**: Check `crm-contacts` for matching contact files. Cross-reference with `people`. Add relationship context.
   - **strategy/decision**: Read `strategy` and `pipeline`. Add strategic alignment notes.
   - **meeting**: Check for related CRM interactions. Add follow-up context.
   - **fleeting**: Determine if it should be reclassified to a more specific type. Suggest reclassification.
4. Search the `/zk` note directories for related notes sharing 2+ keywords.
5. Add `[[ID|Title]]` wiki-links under Connections.
6. Search workspace files (the five context homes, `reference-docs`, `crm-contacts`) for relevant cross-references. Add backtick paths under Connections.
7. Update frontmatter:
   - `status`: seed -> growing (or growing -> evergreen if sufficiently enriched)
   - `updated`: today's date
   - `confidence`: adjust if research confirms or weakens the idea
8. Run `.venv/bin/python scripts/sanitize-text.py` on the file
9. Present changes summary: "Enriched: {title}. Status: {old} -> {new}. Added {N} connections."
   plus the confirmation line from `.claude/rules/hidden-chars.md`.

---

### `find [query]`

Search the knowledge base by keyword, tag, type, or content.

1. Parse the query from arguments
2. Search the `/zk` note directories (see `enrich` step 1) with Grep.
3. Match against:
   - Title (frontmatter)
   - Keywords (frontmatter)
   - Type (frontmatter)
   - Body text content
4. Present results as a compact list:
   ```
   | Title | Type | Status | Keywords | Path |
   ```
5. If no results, say so and suggest `/zk add` to capture the idea

---

### `connect [note]`

Analyze a note and suggest links to other notes and workspace files.

1. Find and read the target note (same matching as `enrich`)
2. Extract the note's keywords and core idea
3. Search the `/zk` note directories (see `enrich` step 1) for notes sharing
   2+ keywords or containing related terms.
4. Search workspace files for relevant cross-references:
   - `crm-contacts` for people mentions
   - `pipeline` for deal references
   - `strategy` for strategic themes
   - `intelligence` (the intelligence reports and dossiers; `competitor-dossiers` for
     the competitors) for competitive intel
5. Present suggested connections as a numbered list
6. Ask which connections to add
7. Edit the note's Connections section with approved links
8. For each connected note, add a reciprocal link back if not already present
9. Update `updated` date in frontmatter

---

### `distill [source]`

Extract atomic insights from an output file into knowledge notes. This is the bridge from skill outputs to durable knowledge.

1. Read the source file (e.g., `.../brief.md` under `osint-dossiers`, a note under `thinking-notes`, a draft under `linkedin-drafts`)
2. Identify 3-7 atomic insights worth preserving. Each insight must be:
   - A single, self-contained idea
   - Worth revisiting in 6+ months
   - Not purely operational (that belongs in a context file or in memory)
3. For each insight, determine:
   - Best type (signal, research, strategy, technology, decision, people)
   - Relevant keywords
   - Confidence level based on source quality
4. Present the proposed notes as a numbered list with titles and types
5. Wait for the operator's approval (they may modify, add, or remove)
6. Create approved notes using the `add` template
7. Link the new notes to each other where relevant
8. Link back to the source file in each note's Context section
9. Set origin footer to: `*Origin: skill-output ({skill name})*`
10. Run `.venv/bin/python scripts/sanitize-text.py` on each created file
11. Report: "Distilled {N} notes from {source}." plus the confirmation line from
    `.claude/rules/hidden-chars.md`.

---

### `garden`

Maintenance pass - find orphans, stale seeds, broken links, and suggest connections.

1. Run `.venv/bin/python scripts/knowledge-health.py` for the `/zk` notes. Run
   `.venv/bin/python scripts/odin-brain-health.py` as well only for the Odin half, and
   report its findings separately.

> **Note:** For full brain linting (contradictions, position candidates, gap analysis), use `/odin compile`.

2. Report findings organized by urgency:

   **Stale Seeds** (status=seed, created > 7 days ago):
   - List each with title, age, and path
   - For each, suggest: enrich, reclassify, or archive

   **Orphan Notes** (no incoming or outgoing links):
   - List each with title and path
   - For each, search for potential connections and suggest links

   **Broken Links** (wiki-links pointing to non-existent IDs):
   - List each broken link with the note it appears in

   **Connection Opportunities** (notes sharing 2+ keywords but not linked):
   - List pairs with shared keywords

3. Ask which actions to take
4. Execute approved actions (enrich, connect, archive, delete)
5. Run `.venv/bin/python scripts/knowledge-health.py --update-index` to regenerate `knowledge-index`

---

### `stats`

Regenerate `knowledge-index` with current knowledge base statistics.

1. Run `.venv/bin/python scripts/knowledge-health.py --update-index`
2. Read and display the updated `knowledge-index`. For the Odin half, run
   `.venv/bin/python scripts/odin-brain-health.py --update-index` and show `odin-index`.
3. Highlight any health concerns (stale seeds, orphans, schema issues)

---

### `brief [topic]`

Synthesize all notes related to a topic into a narrative summary.

1. Parse the topic from arguments
2. Search the `/zk` note directories (see `enrich` step 1) for notes matching the topic by keyword, title, or content. ALSO search `odin-principles` and `odin-positions` for files matching the topic. Match on content: the note template in `add` step 7 defines no `domain` field, so nothing this skill creates has one, and a `domain`-only match can never hit.
3. Read each matching note
4. Synthesize into a narrative summary structured as:
   - **What we know** - confirmed insights (evergreen + high confidence). Include matching Odin principles/positions tagged with `[Odin]`.
   - **What we think** - working hypotheses (growing + medium confidence)
   - **What we're watching** - early signals (seeds + low/unverified confidence)
   - **Open questions** - aggregated from all matching notes
   - **Sources** - list of all brain files contributing to this brief
5. Present the brief inline (do not create a file unless asked)

---
