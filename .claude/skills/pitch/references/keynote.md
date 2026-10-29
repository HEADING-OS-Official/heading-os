# Keynote Deck

Consumed by: `.claude/skills/pitch/SKILL.md`, mode `keynote` (the alias `/keynote-deck` runs the same mode).

Last Updated: 2026-10-30

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Resolve the name before the file operation and
use what it prints:

    .venv/bin/python scripts/data-path.py path <name>

Use `read <name>` for a source that may legitimately be absent, and
`read --required <name>` for one whose absence is a defect. An unknown name
refuses; it never resolves to a plausible directory, and a path written out
here goes stale the day the directory moves.

Produce a presentation deck for an event, VIP meeting, or government engagement.

## Variables

event: [Event name — industry conference, regional expo, security summit, government meeting, investor presentation, etc.]

audience: [Who is in the room — executives, government officials, investors, technical decision-makers]

topic: [Core message or presentation theme]

duration: [Presentation length in minutes — this determines slide count]

context: [Any specific context, asks, or goals for this presentation]

---

## Phase 0: Company context

This skill frames everything through the user's own company, read at run time and never written into this file. Resolve each name with `read <name>` and read what is there:

- `business-info` - the company, its products and how it writes their names, its sector, partners and customers
- `strategy` - priorities, positioning, target markets and priority regions
- `current-data` - live metrics, deployments and proof points
- `pipeline` - active deals and investor conversations
- `voice-guide` - voice and tone
- `skill-data skill=keynote-deck` - the operator's own frame for this skill, if one exists: company lens, section and callout names, region list, proof points, voice notes. Where it is present, take its frame over; the procedure below stays this skill's.

Take the company name, products, sector, priority regions and position from these records. Use them wherever this file says "the company", "the product" or "Priority Region N". A record can be missing or empty. Then name it in the output and say how to fill it: run `/setup-wizard`, or write the file at the address `.venv/bin/python scripts/data-path.py path <name>` prints. Continue with a neutral frame that claims nothing the records do not hold. Never invent a company, a product, a customer or a figure.

Also read:
- `market-intelligence` — Market data for relevant sections, if present
- `geopolitical-landscape` — Regional context for the audience, if present
- `datastore-index` — If the document contains specific facts or numbers, validate against source documents

**Slide count guideline:** 1 minute per slide for technical/detailed content; 2 minutes per slide for storytelling/high-level. Calculate accordingly.

---

## Phase 1: Opening Hook (2-3 slides)

- Slide 1: A single powerful statement, striking statistic, or story opening
- Slide 2: The tension / the problem (why this audience should care)
- Slide 3: The promise of what they'll leave knowing

**Voice note:** open with a story or a counterintuitive observation, as `voice-guide` describes the operator's style — never "Today I'm going to talk to you about..."

---

## Phase 2: The Problem (2-3 slides)

Frame the problem in terms this specific audience experiences, using the buyer pains `business-info` and `strategy` record:
- For operators and enterprises: the operational and regulatory pain the product removes
- For government: control, dependency and compliance risk
- For investors: market opening, timing, structural opportunity

---

## Phase 3: The Solution (3-4 slides)

- The company's platform narrative from `business-info` (not a product tour — a capability story)
- The company's one-line promise, as it writes it
- What changes for the audience when they have it: what they can see, what they can do
- The architectural principle that sets the company apart

---

## Phase 4: Proof (2-3 slides)

Pull the current proof points at runtime from `current-data` (deployments, alliances, partner-network and team figures) rather than hardcoding them — those numbers drift. Typical shape:
- Lead production deployment(s) and their status
- Commercial deployments in progress
- Strategic alliances
- Partner network breadth (named partners + reseller count)
- The team: headcount and hiring rigor

With no `current-data`, leave each proof slide as a bracketed placeholder and say so.

---

## Phase 5: The Future (1-2 slides)

- The expansion sequence `strategy` sets (home market → adjacent → expansion)
- The category the company is building
- What the world looks like when the company's promise is the standard

---

## Phase 6: Close + Call to Action (1-2 slides)

- The one thing you want the audience to do or think after leaving
- Clear, specific ask or invitation (meeting, follow-up, partnership conversation)
- The presenter's contact / next step

---

## Deliverables

For each slide provide:
- **Slide title**
- **Key message** (one sentence — what the audience should take from this slide)
- **Content** (bullets, data points, visual suggestion)
- **Speaker notes** (what the presenter says — written in the operator's voice, not formal notes)

**Output:** Complete presentation script and slide structure, with any missing company record named at the top. The operator reviews, refines, and presents.

## Voice Rules

- The operator's voice from `voice-guide`. Product names styled exactly as `business-info` writes them.
- Hyphens only (never em-dashes).

## NEVER
- Invent a deployment, a partner, a customer or a figure
- Present a placeholder as a fact
