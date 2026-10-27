---
name: zk
description: >
  Zettelkasten knowledge base - add, enrich, find, connect, distill, garden,
  stats, and brief across the knowledge-base second brain shared with Zettlr.
  Use when the user says "zk", "add a note", "knowledge base", "distill this",
  "garden", "connect this to", or asks to capture
  an idea, insight, signal, or decision. Also use when processing outputs
  from other skills into durable knowledge.
argument-hint: "[add|enrich|find|connect|distill|garden|stats|brief] [target]"
allowed-tools: "Read, Write, Edit, Glob, Grep, Bash(.venv/bin/python:*), WebSearch, WebFetch"
model: sonnet
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.1"
x-heading-capability:
  what: >
    Zettelkasten second-brain manager over the knowledge base - add atomic notes, enrich, find, connect, distill skill outputs into durable notes, garden for orphans/stale seeds, stats, and topic briefs.
  how: >
    Run /zk [add|enrich|find|connect|distill|garden|stats|brief] [target].
  when: >
    Use to capture or work an atomic note in the knowledge base. Prefer /odin for learned knowledge, /thread for situations, auto-memory for cross-session facts.
x-heading-routing:
  category: Operations
  triggers:
    - zk
    - add a note
    - knowledge base
    - distill
    - garden
  exclusions:
    - "\"what do we know about\" -> `/recall`"
    - Learned knowledge -> `/odin`; situations -> `/thread`; cross-session facts -> auto-memory.
  compound: 'No'
  router: auto
---

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Resolve the name before the file operation and
use what it prints:

    .venv/bin/python scripts/data-path.py path <name>

Use `read <name>` for a source that may legitimately be absent, and
`read --required <name>` for one whose absence is a defect. An unknown name
refuses; it never resolves to a plausible directory, and a path written out
here goes stale the day the directory moves.

# Zettelkasten Knowledge Base

Manage the second brain under the registry name `knowledge-base`, shared between Claude Code and Zettlr. Every subcommand writes, searches, links and summarises under that one name, and the Hindsight file sync mirrors the notes. Atomic notes, wiki-links, status lifecycle, and anti-dumping discipline.

## Variables

- `$ARGUMENTS` - Subcommand and parameters. Format: `[action] [target]`
- Actions: `add`, `enrich`, `find`, `connect`, `distill`, `garden`, `stats`, `brief`

## Context Loading

Each entry is a LOGICAL name, not a path. Resolve it with `.venv/bin/python scripts/data-path.py path <name>` and read what that prints.

- `knowledge-index` - the `/zk` note inventory, written by `scripts/knowledge-health.py --update-index`
- `odin-index` - the Odin brain inventory, read only by `brief`, which also searches the Odin principles and positions
- `strategy` - for strategic relevance assessment
- `pipeline` - for deal/prospect context
- `current-data` - for timeline and milestone context

## Subcommand dispatch

Parse the first word of `$ARGUMENTS` to determine the action. If no subcommand
is given, default to `stats`.

**Read `references/subcommands.md` before you run any subcommand below.** It
holds the full step sequence, the note file template, and the per-type
enrichment rules. The table names only the command lines and the approval gates.

| Subcommand | Commands and gates | What it does |
|---|---|---|
| `add [type]` | Ends with `.venv/bin/python scripts/sanitize-text.py {path}`. Type `people` is a redirect: "People intel belongs in CRM. Use `/crm add`." | Create one atomic note under `knowledge-base`, in the directory of its type, status `seed`. |
| `enrich [note]` | Ends with `.venv/bin/python scripts/sanitize-text.py {path}`. Ask which note when several match. | Research a seed or growing note, add connections, raise its status. |
| `find [query]` | Grep only. No writes. | Search the knowledge base by title, keyword, type, or body. |
| `connect [note]` | Present suggested links as a numbered list, then ask which to add. Never add a link unasked. | Suggest links to other notes and to workspace files, then write the approved ones both ways. |
| `distill [source]` | Present the proposed notes, then WAIT for the operator's approval. Ends with `.venv/bin/python scripts/sanitize-text.py` on each created file. | Extract 3-7 atomic insights from a skill output file into durable notes. |
| `garden` | `.venv/bin/python scripts/knowledge-health.py`, then ask which actions to take, then `.venv/bin/python scripts/knowledge-health.py --update-index`. `odin-brain-health.py` only for the Odin half. | Maintenance pass over stale seeds, orphans, broken links, and connection opportunities. |
| `stats` | `.venv/bin/python scripts/knowledge-health.py --update-index` | Regenerate `knowledge-index` and display it with any health concerns. |
| `brief [topic]` | Read-only. Present inline; create no file unless asked. | Synthesize every note on a topic into a narrative summary. |

> For full brain linting (contradictions, position candidates, gap analysis),
> use `/odin compile` rather than `garden`.

## Output Conventions

- **File naming**: `{YYYYMMDDHHMMSS}-{slug}.md` - timestamp ID + human-readable slug
- **Subdirectory mapping**: type determines subdirectory (see `add` in `references/subcommands.md`)
- **Wiki-links**: `[[ID|Label]]` format for Zettlr graph compatibility
- **Cross-references**: cite a data-repository file by its logical name in backticks (e.g., `crm-contact` with person=victor-stein), and an engine file by its path in backticks
- **Validation**: Run `.venv/bin/python scripts/sanitize-text.py <note> --scan` on every generated note and report what it found, per `.claude/rules/hidden-chars.md`. Never pre-write the word "clean".
- **Hyphens only** (-) never em-dashes

## Rules

1. **Atomic discipline**: One idea per note. If a note grows beyond ~4 paragraphs, split it into multiple notes and link them.
2. **Status lifecycle**: seed -> growing -> evergreen -> archived. Never skip stages. Gardening surfaces stuck notes.
3. **Anti-dumping**: The knowledge base is not a filing cabinet. Every note must have a clear reason to exist and be worth revisiting. If it's purely operational, it belongs in a context file or in memory.
4. **Keyword consistency**: Before adding a new keyword, check existing keywords in the knowledge base. Prefer existing terms over synonyms to maintain a coherent tag cloud.
5. **Bidirectional linking**: after a link goes from note A to note B, add the reciprocal link from B to A.
6. **Source attribution**: Always fill the `source` field. Use URLs, meeting names, "observation", or skill names (e.g., "/osint", "/ceo-intel").
7. **Confidence honesty**: Set confidence based on source quality, not conviction. Unverified signals stay `unverified` until corroborated.
8. **Zettlr compatibility**: Use `keywords` (not `tags`) in frontmatter. Use `[[ID|Label]]` wiki-links. These are Zettlr conventions.
