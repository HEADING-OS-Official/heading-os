# /crm - action procedures

Consumed by: `.claude/skills/crm/SKILL.md`, section "Actions". Holds the full
step sequence for each of the seven actions, the canonical-owner lookup, the
interaction-log entry template, and the brain-gated `relevant_principles` rules.
Read the section for the action you are about to run, before you run it.

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Resolve the name before the file operation and
use what it prints:

    .venv/bin/python scripts/data-path.py path <name>

Use `read <name>` for a source that may legitimately be absent, and
`read --required <name>` for one whose absence is a defect. An unknown name
refuses; it never resolves to a plausible directory, and a path written out
here goes stale the day the directory moves.

Resolve every path below through the Resource Names table in SKILL.md.

Last Updated: 2026-10-16

---

### `radar` (default — also runs when no arguments provided)

Display the relationship health dashboard.

1. Run `.venv/bin/python scripts/crm-health.py` to get the health report
2. Present the output organized by urgency:
   - **RED** contacts first (overdue — need attention today)
   - **YELLOW** contacts (approaching — plan a touch this week)
   - **Active commitments** due in the next 7 days
   - **GREEN** contacts (on track — for reference)
3. For each RED contact, suggest a specific action (email, call, meeting)
4. **Full CRM View:** If the user says "full CRM view" or "CRM dashboard":
   a. Run `.venv/bin/python scripts/generate-crm-dashboard.py` to produce the HTML dashboard
   b. Present the output path and a summary of what's in it

After surfacing radar output, suggest: `Run /crm next to see the top 3 follow-ups with drafts ready for review.`

### `next`

Surface the top-3 priority follow-ups with checking-in drafts ready for manual review and send. Version 0 of this subcommand presents drafts only. The CEO decides per-draft and sends manually via `send-email.py`.

1. Run `.venv/bin/python scripts/crm_next.py` to generate today's queue. (Note: filename is snake_case for Python importability — `crm_next.py`, not `crm-next.py`.)
2. Read the output path printed by the script (e.g., `next-{TODAY}.md` under `crm-operations`).
3. Present the 3 candidates inline:
   - Candidate index + name + company
   - Stage + days overdue
   - Last interaction excerpt (if any)
   - Draft body (ready for manual send)
4. For each candidate the CEO wants to send, pass the draft body on stdin, the form `crm_next.py` prints. Never `--body`: an argv element is visible to every local account through `ps`. The auto-log hook in send-email.py handles the last_touch bump + interaction log entry automatically.

   ```bash
   .venv/bin/python scripts/send-email.py --to <addr> --subject "<subject>" --body-stdin <<'BODY'
   <the draft body>
   BODY
   ```
5. For drafts the CEO wants to revise: discuss the requested change inline, regenerate the draft body, repeat step 4.
6. For drafts the CEO wants to skip: do nothing. The contact stays in the radar and reappears in tomorrow's queue.

### `add`

Create a new contact. Two-tier model: address book entity + relationship record.

1. Parse arguments for: name, company, relationship type, region, timezone, email (REQUIRED for auto-log entity resolution to work).
2. Generate slug: `firstname-lastname` (kebab-case).
3. **Check if entity exists.** Look for `{slug}.md` under `crm-address-book`.
   - If exists: skip address book creation, proceed to relationship record only.
   - If not exists: create the address book entry with a Profile section. Its YAML frontmatter carries slug, name, canonical_email, employer, canonical_owner per the owner lookup in step 4, and today's created date.
4. **Canonical owner lookup** (only when creating an address book entry): read the table `## Canonical Owner by Relationship Type` in `crm-config` and take the owner slug for this contact's type; a row ending in `*` is a prefix (`investor*` covers `investor-active`). `parse_owner_map()` and `owner_for_type()` in `scripts/utils/crm.py` read the same table. The engine carries no owner names. If `crm-config` holds no such map, or the type is not in it, ask the operator who owns the contact; never stamp a placeholder slug.
5. Create the relationship record at `crm-contact` (person={slug}). The record carries:
   - YAML frontmatter: `entity_ref: {slug}`, `relationship_type`, `last_touch: today`, `created: today`. Optional fields per user input: `cadence`, `source`, `pipeline_company`.
   - **Relevant principles (brain-gated):** two conditions must hold. `odin-brain` exists, and `relationship_type` is a deal-bearing external type, NOT `tribe`, `tribe-leadership`, or `inactive`. Then stamp an optional `relevant_principles:` YAML list. Run `.venv/bin/python scripts/odin-principles.py --type {relationship_type} [--stage {stage}] --json` and take the top slugs. For internal types, or when no brain exists, skip silently, never write the field, and never error.
   - Empty Active Commitments + Interaction Log sections.
6. **Corporate-domain warning:** resolve the instance's corporate mail domain first. It is configuration, never a literal: `corporate_email_domain()` in `scripts/utils/operator_identity.py`, backed by `corporate_email_domain` in the operator config.

   ```bash
   CORP="$(.venv/bin/python -c "import sys; sys.path.insert(0,'.'); from scripts.utils.operator_identity import corporate_email_domain; print(corporate_email_domain())")"
   ```

   An empty `$CORP` means this clone has no corporate domain. Skip the warning entirely. Never test for a bare `@`, which matches every address ever written. Otherwise, if `email` ends with `@$CORP` AND type is NOT tribe or tribe-leadership, surface a warning. The text is "This contact has a @$CORP email but is not classified as tribe. Update type to 'tribe' if they are a team member."
7. Add to `people` radar table.
8. Confirm creation with summary including the entity_ref slug.

### `log`

Log an interaction for an existing contact.

1. Parse arguments for: contact name (fuzzy match) and interaction details
2. Search the top-level `*.md` in `crm-contacts` only (its subdirectories hold the address book and the people indexes, not cards) for the matching contact file (match against filename or frontmatter name field)
3. If multiple matches, list them and ask which one
4. If no match, suggest creating a new contact with `/crm add`
5. Determine interaction type from context (Meeting, Call, Email, Event, Note)
6. Add a new entry at the TOP of the Interaction Log section:
   ```
   ### YYYY-MM-DD | Type | Brief Title
   Description of the interaction.
   **Next:** What happens next (if applicable).
   ```
7. Update `last_touch` in the YAML frontmatter to today's date
8. Check Active Commitments — if this interaction addresses any, mark them done
9. Confirm the log entry

### `find`

Search across all CRM contact files.

1. Parse the query from arguments
2. Search using Grep (glob `*.md`, no recursion) across the top-level `*.md` in `crm-contacts` only (its subdirectories hold the address book and the people indexes, not cards) for matches in:
   - Name, company, title (frontmatter)
   - Region, type (frontmatter)
   - Interaction history (body text)
   - Any keyword match
3. Present results as a compact list: Name | Company | Type | Last Touch
4. If no results in CRM files, also check `people` for contacts not yet migrated

### `update`

Update a contact's profile information.

1. Parse arguments for: contact name, field to update, new value
2. Find the contact file (same fuzzy match as `log`, top-level `*.md` in `crm-contacts` only)
3. Update the specified field in the YAML frontmatter or profile section
4. If updating `type`, recalculate cadence from `crm-config` defaults (unless cadence was manually set)
5. **Relevant principles refresh (brain-gated):** three conditions must hold. `relationship_type` or `pipeline_company` changed, and `odin-brain` exists. The type is also deal-bearing and external, NOT `tribe`, `tribe-leadership`, or `inactive`. Then re-derive `relevant_principles` via `.venv/bin/python scripts/odin-principles.py --type {relationship_type} [--stage {stage}] --json`. Skip silently when no brain exists or the type is internal.
6. Confirm the update

### `backfill`

Reconcile the cards with Exchange mail over a date range. Rules decided by the CEO on 2026-10-16.

1. **Export, read only, in HELM.** `.venv/bin/python scripts/sync-exchange.py --export-metadata <file> --since YYYY-MM-DD [--until YYYY-MM-DD]` writes one JSON line per message from Inbox with its subfolders, Sent Items and Archive: date in the configured timezone, direction, sender, To, CC, subject, conversation id. No bodies. Put the file in the checkout scratch (`python scripts/data-path.py scratch`).
2. **Report.** `.venv/bin/python scripts/crm-backfill.py --mail <file>`. Nothing is written. It shows:
   - `last_touch` bumps, forward only;
   - AWAITING REPLY: the newest inbound mail is newer than the newest outbound one and not older than the card's `last_touch` (a later touch on another channel answers it);
   - possible second addresses of existing contacts (an unknown address whose display name is a contact's name);
   - frequent correspondents missing from the CRM, external and corporate-domain separately, bulk and no-reply senders filtered.
   Matching is exact on the card's `email` and `secondary_email` and the entity's `canonical_email` and `other_emails`. Inbound counts only when the CEO is in To; CC never counts.
3. **Present and WAIT.** Show the report. Ask which second addresses to attach and which correspondents to add, with type for each.
4. **Write the approved list** to a JSON file in the same scratch directory: `{"aliases": {"<address>": "<slug>"}, "new": [{"slug", "name", "email", "employer", "type"}]}`. The owner comes from `crm-config`; give `"owner"` only for a type with no row there, and only the owner the CEO named.
5. **Apply on his yes.** `.venv/bin/python scripts/crm-backfill.py --mail <file> --approved <list> --apply`. It refuses the whole run while any approved entry is refused. Per card it moves `last_touch`, adds one line at the top of the Interaction Log (`- YYYY-MM-DD | Email | Exchange back-fill <from>..<to>: N out, M in; last <date> "<subject>".`, plus the AWAITING REPLY clause), appends approved addresses to the entity's `other_emails`, and creates each approved contact (entity, card, `people` row). A second run with the same files writes nothing.
