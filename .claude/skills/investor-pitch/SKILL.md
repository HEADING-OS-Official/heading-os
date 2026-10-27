---
name: investor-pitch
description: Produce full investor pitch deck content with narrative arc, slide-by-slide structure, and speaker notes, built from the user's own company records. Supports strategic, venture, sovereign fund, angel, and corporate VC audiences. Use when preparing for investor meetings, fundraising presentations, or pitch deck creation. Trigger when the user says "investor pitch", "pitch deck", "fundraising deck", "investor presentation", or asks to prepare materials for an investor meeting.
argument-hint: "[investor_type]"
allowed-tools: "Read, Bash(.venv/bin/python:*)"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "2.0"
x-heading-capability:
  what: >
    Produces full investor pitch deck content - the investment narrative arc,
    a 15-slide structure with key messages and speaker notes, and a data
    appendix - tuned to the investor type (strategic, venture, sovereign fund,
    angel, corporate VC), with every fact taken from the company's records.
  how: >
    Type /investor-pitch [investor_type]. It reads the business, metrics,
    strategy, and growth-playbook records and returns deck content for
    the operator to adapt and present (text content, not a rendered file).
  when: >
    Use to prepare materials for an investor meeting or fundraise. For a progress
    update to existing investors use /investor-update; to render slides as PPTX
    use /pptx-generator.
x-heading-routing:
  category: Strategy
  triggers:
    - investor pitch
    - pitch deck
    - fundraising deck
  exclusions:
    - Existing investor update -> /investor-update
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

# Investor Pitch

Produce full investor pitch deck content — narrative, slide structure, and speaker notes.

## Variables

investor_type: strategic | venture | sovereign_fund | angel | corporate_vc

round_context: [What stage, what amount, what use of funds]

audience: [Specific investor or investor type — any known context]
format: deck_outline | full_content | speaker_notes_only — default: full_content

---

## Phase 0: Company context

This skill frames everything through the user's own company, read at run time and never written into this file. Resolve each name with `read <name>` and read what is there:

- `business-info` - the company, its products and how it writes their names, its sector, partners and customers
- `strategy` - priorities, positioning, target markets and priority regions
- `current-data` - live metrics, deployments and proof points
- `pipeline` - active deals and investor conversations
- `voice-guide` - voice and tone
- `skill-data skill=investor-pitch` - the operator's own frame for this skill, if one exists: company lens, section and callout names, region list, proof points, voice notes. Where it is present, take its frame over; the procedure below stays this skill's.

Take the company name, products, sector, priority regions and position from these records. Use them wherever this file says "the company", "the product" or "Priority Region N". A record can be missing or empty. Then name it in the output and say how to fill it: run `/setup-wizard`, or write the file at the address `.venv/bin/python scripts/data-path.py path <name>` prints. Continue with a neutral frame that claims nothing the records do not hold. Never invent a company, a product, a customer or a figure.

Also read:
- `growth-playbook` — valuation mechanics + growth model (CRITICAL for investor narrative), if present
- `market-intelligence` — Market size, growth, competitive shifts, if present
- `geopolitical-landscape` — Regional and regulatory opportunity, if present
- `datastore-index` — If the document contains specific facts or numbers, validate against source documents

A pitch is only as good as its facts. With no `business-info` and no `current-data`, deliver the structure with every fact as a bracketed placeholder. Say at the top which records are missing.

---

## Phase 1: The Investment Narrative (Story First)

The best pitches tell a story that makes the investment feel inevitable. Draft the core narrative arc from the records:

**[Problem]** → **[Structural Moment]** → **[Why Us / Why Now]** → **[Proof]** → **[Scale Path]** → **[The Ask]**

- **Problem:** the pain the company's buyers live with, as `business-info` and `strategy` describe it, in one or two sharp sentences.
- **Structural Moment:** the shift that opens the window now (a market exit, a regulation, a technology change), from `strategy` and `market-intelligence`. [$X]B addressable across [N] markets.
- **Why Us / Why Now:** the company's differentiation, in its own words from `business-info`. Why no one else can take this window.
- **Proof:** [Lead deployment or customer] live [date]. [Second] in progress. [Alliances]. [Team size].
- **Scale Path:** [Region or segment sequence]. Land-and-expand through [entry product] → full platform. [Channel model]. [$X]M ARR → [$Y]B valuation at [Z]x.
- **The Ask:** [Amount] for [use of funds] to execute [specific milestones].

---

## Phase 2: Slide-by-Slide Structure

For each slide provide: slide title, key message (one sentence), content bullets (3-5), and speaker note.

1. **Cover** — Company name, tagline, presenter
2. **The Problem** — the buyers' pain, made concrete
3. **The Structural Moment** — why the window opens now; the [$X]B opportunity
4. **Market Opportunity** — market size, growth and the fastest region. Read them from `$(.venv/bin/python scripts/data-path.py path skill-data skill=investor-pitch)` or `market-intelligence`. When neither holds them, say so plainly and stop that part. Leave the figure out or as a placeholder; never invent a number.
5. **Our Solution** — the product and its one-line promise, as `business-info` states them
6. **Product** — the modules or product lines `business-info` names, the use cases, the architecture
7. **Traction** — the deployments, customers, alliances and partners `current-data` records
8. **Go-to-Market** — the phased region or segment sequence `strategy` sets; the channel model
9. **Competitive Landscape** — the company against the alternatives `strategy` and `market-intelligence` name
10. **Our Position** — the one place in the landscape only this company holds
11. **Business Model** — [pricing unit] license or subscription + recurring support; land-and-expand NRR; ARR path
12. **The Milestones** — Year 1 is [$X]M ARR / [$Y]M. Year 2 is [$X]M / [$Y]M. Year 3 is [$X]M / [$Y]B
13. **The Team** — the principals and their tenure, from `business-info` or `people`
14. **The Ask** — Amount, use of funds, what this milestone achieves
15. **Why Now, Why Us** — Closing: the moment is now, the team is built, the product is live

---

## Phase 2.5: Structural pass (optional)

For a long deliverable, you may run the `/editorial-review` structural checklist over the assembled narrative before you finalize it. It verifies the investor-narrative arc, claim-to-evidence linkage, and that the ask is set up rather than sprung. Reference: `reference/editorial-review.md`. The prose-level voice pass (`humanization.md`) runs as usual after. Skip when the draft is short or already tight.

---

## Phase 3: Data Appendix

Prepare supporting data slides:
- Full market breakdown (global, regional, by segment)
- Detailed competitive matrix
- Financial model assumptions
- Technology architecture (for technically sophisticated investors)

---

**Output:** Full pitch deck content, with any missing company record named at the top. The operator reviews, adapts tone for the specific investor, and presents.

## Voice Rules

- Story first, numbers precise (per `voss.md`: $347,850, not $350,000). Product names styled exactly as `business-info` writes them.
- Hyphens only (never em-dashes).

## NEVER
- Invent a figure, a customer, a deployment or a team member
- Present a placeholder as a fact
