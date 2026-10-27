---
paths:
  - "docs/**"
  - "**/templates/**"
  - "reference/**"
  - ".claude/skills/**"
  - ".claude/rules/**"
  - "scripts/**"
  - "**/plans/**"
---

<!-- version: 3.0.0 | last-updated: 2026-09-25 -->
# Documentation Propagation Rule

Last Updated: 2026-09-25
Last Verified: 2026-07-12

**Data addresses.** Resources in the data overlay are named below by their
logical name, never by a path. Resolve one before the file operation with
`python scripts/data-path.py path <name>` and use what it prints. One line
rather than the block a skill carries, because this file loads on every edit
under the paths above and every line of it is paid for there.

## Always Update Documentation

Documentation lives in two coexisting systems, and one change may touch both:

- **Docs site** (`spec-docs`, private DATA overlay). It was the engine's public
  `docs/` until 2026-09-25, when it left the engine (HOS-131); the engine keeps
  one page, `docs/INSTALL.md` in the official tree, the install guide. The site's pages are
  authored in `spec-docs` and rendered to `.html` by the single generator
  `scripts/regenerate-docs-html.py` (full map: `DOCS-PIPELINE.md` in
  `spec-docs`). Markdown pages regenerate to `.html`. The hand-authored pages
  are the ones with no `.md` source at all: an `.html` in `spec-docs` that links
  `assets/docs.css` and has no matching `.md`. Editing one means editing the
  HTML; the generator only nav-syncs them. `spec-docs` also holds the portable
  operator guides, which carry no site stylesheet, and the generator leaves
  their shell alone.
- **Operator private templates and overview** (`templates` and
  `workspace-overview`, both in the private DATA overlay, so absent in
  a bare public engine clone but present on the operator workspace). `templates`
  holds `CLAUDE.md.template`, `GETTING-STARTED` and `EMERGENCY-PROCEDURES`; the
  active `sync-docs.py` PostToolUse hook mirrors `GETTING-STARTED` into
  `spec-docs`, and `workspace-health.py` guards that sync.
  **`EMERGENCY-PROCEDURES` is NOT mirrored** (since 2026-09-22): its page in
  `spec-docs` and the template are separate texts, required to differ.

Whenever ANY of the following change, update the matching docs:

1. **New rule created or existing rule modified** -> Add or update its row in the
   catalogue `RULES-REFERENCE.md` in `spec-docs` (right functional group), then
   run `python scripts/regenerate-docs-html.py` on that file. On the operator
   workspace, also update the rules list in `CLAUDE.md.template` under `templates`.
2. **New skill created or existing skill modified** -> Update the skills
   catalogue page `skills-<category>.html` in `spec-docs` (hand-authored cards, NOT generated
   from frontmatter), then `python scripts/regenerate-docs-html.py --nav-sync`. On
   the operator workspace, also update the skill table in
   `GETTING-STARTED.md` under `templates` and `workspace-overview`. For the
   router registry, do NOT hand-edit the generated layers
   (`.claude/rules/skill-router.md`, `reference/skill-router/<category>.md`): set the
   skill's `x-heading-routing` frontmatter and run
   `python scripts/generate-skill-router.py` (CI + pre-commit enforce `--check`).
3. **New script created or modified** -> On the operator workspace, update
   `workspace-overview`; update the page in `spec-docs` that documents the
   behaviour (`EXTENDING.md`, `INTEGRATIONS-SETUP.md`, etc.) and regenerate it.
   A change to how the engine is installed also updates the engine's
   `docs/INSTALL.md`.
4. **Workspace structure changes** -> Update `CLAUDE.md.template` under `templates` and
   `GETTING-STARTED.md` under `templates` (operator workspace) and
   `ARCHITECTURE.md` in `spec-docs`.
5. **Regenerate after editing any site page:** `python scripts/regenerate-docs-html.py --all`,
   from the repo `.venv` so its pygments matches the pinned toolchain. The
   `docs-html-drift` pre-commit hook and the CI step that diffed the engine's
   `docs/` were removed on 2026-09-25 with the site, so nothing gates the
   rendered pages now; the overlay's diff is read in HELM.

## Version Tracking

Every shared doc in `templates` and its auto-synced counterpart in `spec-docs` carries an HTML-comment version marker. For `.md` and `.template` files the marker sits in the first three lines (usually line 1). For `.html` files the marker is embedded at the top of the `<main>` body during markdown-to-HTML rendering, not on line 1 (which is `<!DOCTYPE html>`).

```
<!-- version: MAJOR.MINOR.PATCH | last-updated: YYYY-MM-DD -->
```

Bump semantics:
- **PATCH** - typo fixes, clarifications that don't change meaning
- **MINOR** - new sections, meaningful content additions, reworded guidance
- **MAJOR** - structural reorganization, removal of sections, breaking changes for anyone following the doc

When editing a template, always update both fields.

What `workspace-health.py` actually enforces, stated narrowly because the rule used to claim more than the code does. `check_doc_versions` opens exactly three files, all of them `.md`/`.template` under the directory `get_templates_dir()` resolves, which since the HEADING OS 2 move is `templates`: `GETTING-STARTED.md`, `EMERGENCY-PROCEDURES.md`, `CLAUDE.md.template`. It opens no `.html` file at all. For each of the three, a missing file, a missing or unparseable marker, and a malformed date each count as an issue and fail the check.

**Age is reported, never enforced.** A `last-updated` older than the 90-day threshold prints a warning and increments a separate `stale` counter that is deliberately excluded from the returned issue count, so a stale marker does not fail the health check. Staleness is a refresh signal here, not a gate. Anything that needs to be a gate has to be written as one; do not read the 90 days as enforcement.

The mirrored copies are covered from the other side: `check_docs_sync` byte-compares the two synced files (`GETTING-STARTED` as `.md` and `.html`) between `templates` and `spec-docs`, so a marker that drifted in a copy fails there instead; `check_doc_versions` still reads the `EMERGENCY-PROCEDURES` template's own marker. Every other page, the `EMERGENCY-PROCEDURES` page in `spec-docs` among them, carries a version marker by convention only, checked by no code.

Both checks no-op with a warning when `templates` is absent, which is the normal state of a public engine clone with no data overlay. Both run as part of the standard health check, `scripts/workspace-health.py`.

## Plans Lifecycle

Active implementation plans live at `{YYYY-MM-DD}-{slug}.md` under `plans`. A plan is active while its work is in progress; the `plans` root should hold only plans currently being executed or about to be executed.

When a plan is complete (success criteria met or work abandoned):

1. Move it to `archive/{YYYY}/` under `plans`: `P=$(python scripts/data-path.py path plans); git mv "$P/{filename}" "$P/archive/{year}/"`
2. Optionally add a `status:` line to the plan file top (e.g., `status: completed`, `status: abandoned`, `status: superseded by {other-plan}`) to make the outcome searchable.

Archived plans are permanent records - they remain git-tracked and searchable via `grep -r "$(python scripts/data-path.py path plans)/archive/"`. Never delete a plan; archive it. This preserves the decision trail for future scrutiny passes and post-mortems.

CEO triage cadence: archive any plan whose success criteria have been met or whose work has been abandoned before the next perf sprint.

## Never Deliver Barebones Documentation

All documentation presented to the operator must be:
- **Comprehensive** -- every skill, every workflow, every rule documented
- **Branded** -- HTML versions use the company design system (dark theme, GT Standard font from the brand assets under the registry name `brand-kit`, resolved with `python scripts/data-path.py path brand-kit`, with Inter fallback, gradient headers)
- **Current** -- version number and last-updated date in footer
- **Actionable** -- step-by-step with examples, not abstract descriptions
