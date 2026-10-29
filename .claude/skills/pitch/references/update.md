# Investor Update

Consumed by: `.claude/skills/pitch/SKILL.md`, mode `update` (the alias `/investor-update` runs the same mode).

Last Updated: 2026-10-30

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Resolve the name before the file operation and
use what it prints:

    .venv/bin/python scripts/data-path.py path <name>

Use `read <name>` for a source that may legitimately be absent, and
`read --required <name>` for one whose absence is a defect. An unknown name
refuses; it never resolves to a plausible directory, and a path written out
here goes stale the day the directory moves.

Draft a progress update for existing investors — quarterly or milestone-triggered.

## Variables

period: [Q1 2026 / Q2 2026 / Post-Event / Post-Milestone / etc.]

investor: [Investor name or "all investors" — use `people` for context]

highlights: [Key developments to feature]

challenges: [Honest challenges to acknowledge — an update that hides them loses trust]

ask: [Any specific asks or decisions needed from investors — if none, leave blank]

---

## Phase 0: Company context

This skill frames everything through the user's own company, read at run time and never written into this file. Resolve each name with `read <name>` and read what is there:

- `business-info` - the company, its products and how it writes their names, its sector, partners and customers
- `strategy` - priorities, positioning, target markets and priority regions
- `current-data` - live metrics, deployments and proof points
- `pipeline` - active deals and investor conversations
- `voice-guide` - voice and tone
- `skill-data skill=investor-update` - the operator's own frame for this skill, if one exists: company lens, section and callout names, region list, proof points, voice notes. Where it is present, take its frame over; the procedure below stays this skill's.

Take the company name, products, sector, priority regions and position from these records. Use them wherever this file says "the company", "the product" or "Priority Region N". A record can be missing or empty. Then name it in the output and say how to fill it: run `/setup-wizard`, or write the file at the address `.venv/bin/python scripts/data-path.py path <name>` prints. Continue with a neutral frame that claims nothing the records do not hold. Never invent a company, a product, a customer or a figure.

Also read:
- `people` — Investor relationship context
- `growth-playbook` — Metrics that matter for valuation story (ARR, NRR, GM, CAC), if present
- 3-5 varied sent emails from `exchange-email-cache`, loaded into `<examples>` per `reference/voice-examples.md`, when that cache exists
- `datastore-index` — If the document contains specific facts or numbers, validate against source documents

With no `current-data`, the metrics table keeps its rows with empty cells, and the draft says at the top that the record is missing. Never fill a cell from memory.

---

## Structure

**Subject: [Company] Update — [Period] | [One-line headline]**

**Opening (1 paragraph):**
The operator's personal note — where the company is, the read of the conditions it operates in. Honest, direct, warm.

**Key Metrics:**
| Metric | This Period | vs. Last Period | Target |
|--------|------------|----------------|--------|
| Revenue / ARR | | | |
| Active deployments or customers | | | |
| Pipeline (qualified) | | | |
| Team size | | | |
| Markets active | | | |

Use the metrics `current-data` and `growth-playbook` track; these rows are the fallback.

**The Milestones:**
What we said we'd do → What we did. No spin. Real numbers.

**What's Working:**
2-3 specific wins with evidence.

**What's Challenging:**
1-2 honest challenges. Investors are partners and need the real picture.

**Next 90 Days:**
3 specific commitments with measurable outcomes.

**Ask (if any):**
Specific, direct. CEOs make specific asks, not vague updates.

**Closing:**
Brief, confident, forward-leaning.

---

**Output:** A draft investor update for the operator's review, 1-2 pages: honest, metric-driven, in the operator's voice. It is never sent from this skill.

## Voice Rules

- The operator's voice from `voice-guide`. Precise numbers. Product names styled exactly as `business-info` writes them.
- Hyphens only (never em-dashes).

## NEVER
- Invent a metric, a milestone or a win
- Send the update: the operator sends it after review
