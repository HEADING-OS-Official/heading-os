---
name: create-plan
description: Design a structured implementation plan for a non-trivial change before any code is written - objective, scope, critical files, step sequence, success criteria, and risks. Use before significant additions or multi-file work. Trigger when the user says "create plan", "plan for [change]", or "design the approach". Do NOT use to execute an existing plan (use /implement).
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.2"
argument-hint: "[describe what to plan]"
allowed-tools: "Read, Glob, Grep, Bash(.venv/bin/python:*)"
x-heading-capability:
  what: >
    Researches the workspace and produces a thorough implementation plan
    document - Spec Core, current state, proposed changes, step-by-step tasks,
    and success criteria - ready for /implement to execute without ambiguity.
  how: >
    Run /create-plan [describe what to plan]. Writes the plan to
    YYYY-MM-DD-{name}.md under plans; it plans only and never makes the changes.
  when: >
    Use before any significant or structural workspace change. To carry the
    plan out use /implement; to stress-test it before approval use /scrutinize.
x-heading-routing:
  category: Operations
  triggers:
    - create plan
    - plan for [change]
    - design the approach
  exclusions:
    - Execute plan -> /implement
  compound: 'No'
  router: auto
---
# Plan

Create a detailed implementation plan for changes to this workspace. Plans are thorough documents that capture the full context, rationale, and step-by-step tasks needed to execute a change with complete alignment across the project.

## Variables

request: $ARGUMENTS (describe what you want to plan — new command, new workflow, structural change, template update, etc.)

---

## Instructions

- **IMPORTANT:** You are creating a PLAN, not implementing changes. Research thoroughly, then output a comprehensive plan document.
- Research the workspace to understand existing patterns, conventions, and how this change fits.
- Create the plan in the `plans` directory (resolve with `.venv/bin/python scripts/data-path.py path plans`) with filename: `YYYY-MM-DD-{descriptive-name}.md`
  - Use today's date
  - Replace `{descriptive-name}` with a short, kebab-case name (e.g., "add-guest-research-command", "restructure-outputs", "create-outreach-workflow")
- Fill out every section of the Plan Format below. Replace all `<placeholders>` with specific, actionable content.
- Be thorough — this plan will be executed by `/implement` and needs enough detail to execute without ambiguity.
- Follow existing patterns. Study similar files in the workspace before proposing new structures.

---

## Research Phase

Before writing the plan, investigate:

1. **Read core reference files:**
   - `CLAUDE.md` — workspace overview
   - The five context homes: `context-position`, `context-reference`, `context-entity`, `context-matter`, `context-measurement` — background on the user and project

2. **Explore relevant areas:**
   - If creating a skill: read existing skills in `.claude/skills/`
   - If modifying outputs: explore the output homes (`.venv/bin/python scripts/data-path.py group output-dirs`) and examples
   - If updating templates: check `reference/` for existing patterns
   - If adding scripts: review `scripts/` for conventions

3. **Understand connections:**
   - How does this change relate to existing workflows?
   - What files reference or depend on areas being changed?
   - Are there naming conventions to follow?

---

## Plan Format

Write the plan using the **exact structure in `references/plan-template.md`**, a fill-in template. Its top-level sections come in this order. First the title with the `Created`/`Status`/`Request`/`Master instruction`/`Issue` header, then Overview (What This Plan Accomplishes and Why This Matters). Then **Spec Core**: Why, Capabilities, Constraints, Non-Goals, and Success Signal, defined in `reference/templates/spec-core.md`. Then Current State, then Proposed Changes with its New/Modify/Delete file tables. Then Design Decisions, which includes Open Questions, and Step-by-Step Tasks. It closes with Connections & Dependencies, Validation Checklist, Success Criteria, Deviations from Plan, and Notes.

The two header lines tie the plan to the rest of the chain. A reader then reaches the request, the plan, the issue and the merge commit from any one of them. `Master instruction` is the absolute path `.venv/bin/python scripts/prompt-archive.py latest` prints for this session, the prompt the plan executes. `Issue` is the Lific identifier (`lific search "<the gist>"`). When either does not exist, write `none, <why>` rather than leaving the line out. `Deviations from Plan` reads "none yet" until execution departs from the plan text.

The reference also carries the **wave-grouping rules** for parallel execution, plus a wave example. Those rules cover `### Wave N (parallel)` against `### Wave N`, `####` step headings, global step numbering, and the ban on mixing wave and bare-step formats. Read it before you write the plan. Reproduce the structure verbatim, and replace every `<placeholder>` with specific content.

---

## Quality Standards

- **Completeness:** Every section filled out with specific content, no generic placeholders left
- **Verification follows the test policy:** the Validation Checklist names a reproduction run (command and expected output) for each change. A plan adds a test only for failure A or B of Phase 5 in `.claude/rules/development-standards.md`. Every other change ships with a `Fix-Note:` line, and the rule wins over any habit of planning a test step.
- **Spec Core complete:** Non-Goals lists at least one item; the Success Signal is a single testable observable, not a vague aspiration (see `reference/templates/spec-core.md`)
- **Actionability:** Steps are detailed enough that `/implement` can execute without asking questions
- **Consistency:** Follows existing workspace patterns and naming conventions
- **Clarity:** Someone unfamiliar with the project could understand and execute the plan
- **Traceability:** Changes are connected back to goals and rationale

---

## Report

After creating the plan:

1. Provide a brief summary of what the plan covers
2. List any open questions that need user input before implementation
3. Provide the full absolute path to the plan file: `YYYY-MM-DD-{name}.md` under `plans`
4. Remind user to run `/implement <that absolute path>` to execute
5. Offer a pre-approval scrutiny: `"Plan ready. Run /scrutinize before approval? (recommended for high-stakes or structural plans)"`

---

## Voice

- The plan is a working document, not a pitch. Concrete and specific over polished.
- Every step traces to a Spec Core capability; no speculative scope.
- Use hyphens (`-`), never double dashes (`--`); product names styled per `.claude/rules/terminology.md`.

## NEVER

- Never implement changes from this skill — it plans only; execution is `/implement`.
- Never leave a `<placeholder>` unfilled or a section generic; an ambiguous plan blocks `/implement`.
- Never write the plan with an empty Non-Goals or a vague (non-testable) Success Signal.
- Never invent files, paths, or conventions — research the workspace first and follow what exists.
