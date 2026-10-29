---
name: crm
x-heading-requires: ["email"]   # F-7.1: optional-dependency extras this skill needs
description: "Personal CRM - add, log, radar, find, update contacts, and back-fill them from Exchange mail"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.4"
argument-hint: "[add|log|radar|find|update|next|backfill] [contact] [details]"
allowed-tools: "Read, Write, Edit, Glob, Bash(.venv/bin/python:*)"
model: sonnet
x-heading-capability:
  what: >
    The personal CRM - add contacts, log interactions, update records, find
    people, and surface a health radar of who has gone overdue across your
    relationship base.
  how: >
    Run /crm <add|log|radar|find|update|next|backfill> <contact> <details>. Writes to
    the contact cards under crm-contacts and keeps the people record in sync.
  when: >
    Use to record or query a relationship. For Google address book use
    /google-contacts.
x-heading-routing:
  category: CRM
  triggers:
    - crm add
    - crm log
    - crm radar
    - crm find
    - crm update
    - check CRM
    - contact health
  exclusions:
    - N/A
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

# Personal CRM

Manage contacts, log interactions, track relationship health, and surface follow-up priorities.

## Resource Names

Every resource this skill touches is a LOGICAL name, not a path. Resolve it with `.venv/bin/python scripts/data-path.py path <name>` and use what that prints.

| Resource | Name |
|----------|------|
| Contacts, team members included | `crm-contacts` |
| CRM config | `crm-config` |
| People index | `people` |

There is one set of contacts and no second tier to check (operator decision, 2026-09-24). A team member is a contact of type `tribe*` inside `crm-contacts`.

## Variables

- `$ARGUMENTS` — Subcommand and parameters. Format: `[action] [details]`

## Actions

Parse the first word of `$ARGUMENTS` to determine the action:

**Read `references/actions.md` before you run any action below.** It holds the
full step sequence, the canonical-owner lookup, and the record templates. The
table names only the command lines and the guards.

| Action | Commands and guards | What it does |
|---|---|---|
| `radar` (default, and the no-argument case) | `.venv/bin/python scripts/crm-health.py`. On request, `.venv/bin/python scripts/generate-crm-dashboard.py` for the full HTML dashboard. | Relationship health dashboard, ordered RED, then YELLOW, then commitments due within 7 days, then GREEN. |
| `next` | `.venv/bin/python scripts/crm_next.py` to build today's queue. Present all 3 drafts and WAIT. Send only what the CEO names, one at a time, with `.venv/bin/python scripts/send-email.py --to <addr> --subject "<subject>" --body-stdin` and the body in a heredoc, the form `crm_next.py` prints (`references/actions.md`), never `--body`, which shows the text to every local account through `ps`. | Top-3 priority follow-ups with drafts ready for per-draft CEO review. |
| `add` | Warn when `email` sits on the configured corporate mail domain and the type is neither `tribe` nor `tribe-leadership` (resolution: `references/actions.md`). Only when `odin-brain` exists, and only for a deal-bearing external type: `.venv/bin/python scripts/odin-principles.py --type {relationship_type} [--stage {stage}] --json`. | Create the address book entity plus the relationship record, then index the contact in `people`. |
| `log` | No script. Ask which contact when several match. Suggest `/crm add` when none match. | Add a dated interaction entry at the TOP of the Interaction Log, then bump `last_touch`. |
| `find` | Grep over the top-level `*.md` in `crm-contacts` only, never its subdirectories. No writes. | Search name, company, title, region, type, and interaction history. |
| `backfill` | `.venv/bin/python scripts/sync-exchange.py --export-metadata <file> --since YYYY-MM-DD` (read only, run in HELM), then `.venv/bin/python scripts/crm-backfill.py --mail <file>`. Report first and WAIT; `--apply` only on the CEO's yes, new contacts and second addresses only from the `--approved` list he confirmed. | Move `last_touch` forward from mail, add one summary line per card, flag AWAITING REPLY, list missing correspondents and second addresses. |
| `update` | Only when `odin-brain` exists, and only for a deal-bearing external type: `.venv/bin/python scripts/odin-principles.py --type {relationship_type} [--stage {stage}] --json`. | Change one profile field, then recalculate cadence when `type` changed. |

After surfacing radar output, suggest: `Run /crm next to see the top 3 follow-ups with drafts ready for review.`

`next` presents drafts only. The CEO decides per draft and sends manually.

## Context Loading

Resolve each name against the registry, then load:
- `crm-config` — cadence defaults and health thresholds
- `people` — quick-reference index
- `pipeline` — deal context (when logging deal-related interactions)

## Rules

- Always use today's date (YYYY-MM-DD format) when logging interactions
- Interaction log entries are reverse-chronological (newest first)
- When creating contacts, match the voice and format of existing contact files in `crm-contacts`
- If a logged interaction changes the relationship dynamic, note it in the profile section
- Keep interaction summaries concise — 1-3 sentences max
- Use hyphens (-), never em-dashes
- When adding or editing any contact, always ask where they live (for timezone). Use IANA timezone names (e.g., `America/New_York`, `America/Winnipeg`, `Asia/Jerusalem`). If the user does not explicitly define a different operating timezone, omit `operating_timezone` from the YAML - it defaults to `timezone`.
