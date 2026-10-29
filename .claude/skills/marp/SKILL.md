---
name: marp
description: "Markdown-driven presentation pipeline producing PDF and HTML from MARP source with a branded theme. Four subcommands: new (prompt-to-deck), render (existing source), from (workspace markdown transform), watch (live preview). Internal decks, runbooks, state checks, knowledge renders. Do NOT use for editable PPTX client decks (use /pptx-generator) or LinkedIn carousels (use /pptx-generator)."
argument-hint: "[new <topic> | render <path> | from <path> | watch <path>]"
allowed-tools: "Read, Write, Edit, Bash(.venv/bin/python:*), Bash(marp:*), Glob, Grep"
context: fork
model: haiku
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-capability:
  what: >
    Renders branded presentations (PDF + HTML, not editable PPTX) from
    Markdown source using the brand's dual-mode theme from the data repository.
  how: >
    Type /marp with one of four subcommands - new <topic> (draft a deck from a
    prompt), render <path> (render existing source), from <workspace-path>
    (transform any workspace markdown into slides), or watch <path> (live
    preview). Output lands beside the source or in
    marp-source/ under presentations.
  when: >
    Use for internal decks, runbooks, state checks, and knowledge renders. For an
    editable brand-heavy client deck or a LinkedIn carousel use /pptx-generator.
x-heading-routing:
  category: Design
  triggers:
    - marp
    - render as slides
    - turn this into slides
    - slides from this doc
    - render this as a deck
    - internal deck about
    - runbook deck
    - quick slides
    - md to slides
  exclusions:
    - Brand-heavy client deck -> /pptx-generator
    - Carousel -> /pptx-generator
  router: auto
---
# /marp - Markdown Presentation Pipeline

Render branded presentations from Markdown source. Outputs PDF and HTML. Not editable PPTX.

## Step 0: Read the visual design rule

Before you generate any visual, read `.claude/rules/visual-design-discipline.md` by path. Its path globs do not load it for this skill. Read the sections "The vocabulary of AI tells" and "Three reflexes no detector catches". Check the finished artifact against that table before you present it.

Patterns to avoid in every deck, named in that rule's presentation row:

- Inter, Roboto or another default face alone on every slide. The theme sets GT Standard; do not override it.
- Purple-to-blue or purple-to-pink gradient backgrounds.
- A three-card feature row as the default slide shape.
- Generic titles such as "Our solution" or "Why us". A title states the slide's claim.
- Title Case headings. Use sentence case.
- Invented testimonials, quotes or attributions.
- Emoji or Unicode glyphs standing in for icons.

## Subcommands

### `/marp new <topic>`

Draft a full deck from a topic prompt.

1. Parse topic into purpose and audience. Ask one clarifying question only if truly ambiguous.
2. Generate outline:
   - Cover slide (title layout, dark)
   - One context or "why" slide
   - Three to five main-point slides (one idea per slide, under 50 words each)
   - One key-takeaway slide
   - Closing slide
3. Default mode: `mixed` (dark cover, dark section-breaks, dark closing; light content slides).
4. Write source to `marp-source/YYYY-MM-DD-<slug>.md` under `presentations` (resolve with `.venv/bin/python scripts/data-path.py path presentations`).
5. Run `.venv/bin/python scripts/marp_render.py render <source>` to produce PDF and HTML.
6. Report: file paths, slide count, sanitizer status.

**Flags:** `--slides N`, `--mode dark|light|mixed`, `--no-render`, `--title "override"`, `--images png`

If target filename exists, refuse to overwrite. Prompt for rename or `--force`.

### `/marp render <path>`

Pure render of an existing .md source. No authoring.

```bash
.venv/bin/python scripts/marp_render.py render <path> [--pdf-only] [--html-only] [--images png] [--output <dir>] [--verbose]
```

Renders PDF and HTML to the same directory as source, or to `--output <dir>`.

### `/marp from <workspace-path>`

Transform any workspace markdown into slides without modifying the source.

```bash
.venv/bin/python scripts/marp_render.py from <path> [--break-at h2|h3|manual] [--mode dark|light|mixed] [--title "..."] [--subtitle "..."] [--no-auto-cover] [--no-auto-closing] [--output <dir>] [--paginate-heavy] [--verbose]
```

Pre-processing (in-memory, source never mutated):
- Strip existing YAML frontmatter from rendered body
- Strip wiki-links: `[[id]]` becomes `id`, `[[id|Display]]` becomes `Display`
- Insert slide breaks at configured heading level (default h2)
- Auto-generate cover slide with title, date, source path
- Auto-generate closing slide
- Inject MARP frontmatter

**Workspace-aware defaults:**

| Source location (registry name) | Default mode | Cover subtitle |
|---|---|---|
| `context-matter`, `context-entity` | light | Operating Context - `company_name` |
| `knowledge-base` | light | From the brain - <date> |
| `reference-docs` | light | Reference - `company_name` |
| `intel-outputs`, `intelligence`, every account's `account-research` | dark | Intelligence - Classified |
| `operations-handoff`, `operations-reports`, `operations-scratch`, every account's `account-reports` and `account-work` | dark | Operations - `company_name` |
| `proposals`, every account's `account-proposals` | mixed | <filename> - Proposal |
| anywhere else | mixed | <filename> |

Each name's home is a prefix key, not a destination. The LONGEST key that
matches wins, so a report under `intelligence` inside `knowledge-base` stays
dark. `workspace_defaults()` in `scripts/marp_render.py` builds the live set
and also keeps the pre-move prefix keys, so a source at a pre-move address
matches as before.

### `/marp watch <path>`

Live preview with hot reload.

- **Start:** `.venv/bin/python scripts/marp_render.py watch <path>` - spawns detached marp-cli server, opens browser
- **Stop:** `.venv/bin/python scripts/marp_render.py watch stop` - kills process, removes state
- **Status:** `.venv/bin/python scripts/marp_render.py watch status` - reports running/stopped, URL, source

One watch session at a time (v1).

## Subcommand Dispatch

When invoked via natural language (not a direct subcommand):

| Signal | Subcommand |
|---|---|
| Message contains a workspace path outside `marp-source/` | `from` |
| Message contains a path inside `marp-source/` or any `.md` with MARP frontmatter | `render` |
| Message has a topic but no file path | `new` |
| "watch", "preview", "live" plus a path | `watch` |
| Ambiguous "this doc" | Resolve from conversation context (last-read file). If none, ask. |

## Theme

Single-file dual-mode brand theme with 7 layouts. The themes, their `fonts/` directory and the sample decks are brand content, never engine content. They live in the private data repository at `$(.venv/bin/python scripts/data-path.py path skill-brand-assets skill=marp)`. Each `themes/<name>.css.tmpl` there is a theme variant `<name>`; the default is the alphabetically first. With no data repository there is no theme, and `render` says so instead of producing an unbranded deck.

**Layouts:** title, content, two-column, quote, stats, section-break, closing

### Investor theme variant (`--theme-variant <name>-investor`)

A variant whose name ends in `-investor` is a brand-forward investor master. Its layouts use inline HTML, so it renders with `--html` enabled automatically. List the variants the brand half carries with `ls "$(.venv/bin/python scripts/data-path.py path skill-brand-assets skill=marp)/themes"`.

```bash
.venv/bin/python scripts/marp_render.py --theme-variant <name>-investor render <deck.md>
```

Set `theme: <name>-investor` in the deck frontmatter to match. A sample deck for it, when the brand half has one, sits in the `examples` directory under `skill-brand-assets` (`.venv/bin/python scripts/data-path.py path skill-brand-assets skill=marp`), and its design spec under `brand-kit`. The default variant is unchanged for internal decks.

**Per-slide directives:**
```markdown
<!-- _class: title | content | two-column | quote | stats | section-break | closing -->
<!-- _class: light -->
<!-- _class: dark -->
<!-- _class: "content no-corner" -->
<!-- _footer: "" -->
<!-- _paginate: false -->
```

## Frontmatter Convention

```yaml
---
marp: true
theme: <variant>  # the default variant's name; the renderer fills it in when absent
paginate: true
size: 16:9
class: dark
title: "Deck Title"
author: "Author Name"
date: "2026-04-16"
classification: "ceo-only"
footer: "(C) 2025-2026 - Acme Ltd - acme.example - Proprietary & Confidential"
---
```

## Self-Test

```bash
.venv/bin/python scripts/marp_render.py --self-test
```

Renders the sample deck (`sample-deck.md` in the `examples` directory under `skill-brand-assets`, `skill=marp`), validates output sizes, checks hidden characters, reports pass/fail. With no data repository the render checks are skipped and the run says so.

## NEVER

- Never modify the source file during render. All transformations happen in-memory on a copy.
- Never render externally sourced .md files without review (--allow-local-files security risk).
- Never skip hidden-character sanitization. Every render reports sanitizer status.
- Never output editable PPTX. That is `/pptx-generator` territory.
