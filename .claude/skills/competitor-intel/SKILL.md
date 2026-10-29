---
name: competitor-intel
x-heading-requires: ["research"]   # F-7.1: optional-dependency extras this skill needs
description: Competitive analysis of a named vendor versus the user's own company and product - product gaps, geographic overlap, honest strengths, exploitable weaknesses, win strategy, and recommended actions. Use when comparing the company against a competitor or sizing a competitive landscape, including when no competitor is named yet ("how do we stack up against a competitor"), in which case the skill asks which one first. Trigger when the user says "competitor analysis", "how does [company] compare to [competitor]", "competitive advantage vs [competitor]", or "competitive landscape for [sector]". Do NOT use when the target is a person (use /osint), or for market sizing (use /market-brief).
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "2.2"
argument-hint: "[company]"
context: fork
allowed-tools: "WebSearch, WebFetch, Read, Bash(.venv/bin/python:*)"
model: sonnet
x-heading-capability:
  what: >
    Deep competitive analysis of a named vendor against the company's own
    product (read from its records) - product comparison table, geographic
    overlap, honest strengths, exploitable weaknesses, and a win strategy,
    with each claim evidence-graded.
  how: >
    Run /competitor-intel <company>. Forked-context web research pass that
    maintains a persistent case file under intel-cases and outputs a
    competitor brief.
  when: >
    Use for head-to-head against a named competitor. For a person or company
    recon use /osint; for market sizing use /market-brief.
x-heading-routing:
  category: Intel
  triggers:
    - competitor analysis
    - competing vendor
    - how does [company] compare to [competitor]
    - competitive advantage vs [named competitor]
    - competitive landscape for [sector]
  exclusions:
    - Target is a person -> /osint
    - market sizing -> /market-brief
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

# Competitor Intelligence

Deep competitive analysis on a specific company or vendor, against the user's own company and product.

## Variables

company: [Company name — e.g., "a competing vendor", "a state-aligned vendor", "a regional challenger"]

trigger: [Why this analysis now — deal competition, market move, partnership evaluation, investor question]

depth: quick | deep — default: deep

---

## Phase 0: Company context

This skill frames everything through the user's own company, read at run time and never written into this file. Resolve each name with `read <name>` and read what is there:

- `business-info` - the company, its products and how it writes their names, its sector, partners and customers
- `strategy` - priorities, positioning, target markets and priority regions
- `current-data` - live metrics, deployments and proof points
- `pipeline` - active deals and investor conversations
- `voice-guide` - voice and tone
- `skill-data skill=competitor-intel` - the operator's own frame for this skill, if one exists: company lens, section and callout names, region list, proof points, voice notes. Where it is present, take its frame over; the procedure below stays this skill's.

Take the company name, products, sector, priority regions and position from these records. Use them wherever this file says "the company", "the product" or "Priority Region N". A record can be missing or empty. Then name it in the output and say how to fill it: run `/setup-wizard`, or write the file at the address `.venv/bin/python scripts/data-path.py path <name>` prints. Continue with a neutral frame that claims nothing the records do not hold. Never invent a company, a product, a customer or a figure.

Also read:
- `market-intelligence` — Existing competitive data, if the operator keeps one
- `competitor-dossiers` and `deliverable-documents` — any earlier profile or vendor landscape of this company
- `search-domains` — Domain filtering configuration

`[Our product]` below is the company's product as `business-info` names it. With no `business-info`, the comparison columns stay empty for our side and the brief says so; it never fills them from memory.

---

## Instructions

If the request names no competitor, ask one question first: "Which company should I compare us against?" Wait for the answer, then continue.

Search the web for the latest information on this company using two search passes:

**Pass 1 -- Industry sources** (filtered):
- WebSearch with `allowed_domains` from the group matching the company's sector + General Tech groups
- `blocked_domains`: Blocked Domains list
- Query: "[company name] [sector] product launch partnership [current year]"

**Pass 2 -- Broad company search** (blocklist only):
- WebSearch with `blocked_domains` only (Blocked Domains list)
- Query: "[company name] earnings revenue leadership acquisition market moves"

**Pass 3 -- Website crawl** (for comprehensive product intelligence):
- Use `.venv/bin/python scripts/firecrawl.py crawl "[company website]" --limit 10 --include "/product|/pricing|/solution"` to get full product and pricing pages. Before the first firecrawl call, run the VPN pre-flight gate in `reference/vpn-preflight.md` and wait for an explicit answer.

---

## Evidence Grading & Case File

Before writing the analysis, grade the evidence and maintain the competitor's persistent case file. Full spec: `reference/forensic-evidence-grading.md`.

- **Grade each material claim** Confirmed, Deduced, or Hypothesized. Confirmed needs >=2 independent sources or an official filing. Deduced needs a single source or a defensible inference, with the chain shown. Hypothesized is unconfirmed: state what would confirm or refute it. Surface the grade next to the claim, especially in Product Comparison, Their Weaknesses, and Financial health signals. A competitor weakness graded Hypothesized must not be sold as Confirmed.
- **Case file** at `[competitor-slug].md` under `intel-cases` (ceo-only). If it exists, read it first and update hypothesis statuses. That is append-only: never delete a refuted theory, flip its `Status` to `Refuted` with a `Resolution`. If it does not exist, create it from the case-file template, `intel-case-file.md`. The template is in the directory that `.venv/bin/python scripts/data-path.py path templates` prints. A public clone does not have it. Then say so and skip the case file rather than invent its layout. This protects the head-to-head from narrative lock-in across quarters.

---

## Quick Analysis (1 page)

- Who they are (one paragraph)
- What they offer vs. what [Our product] offers
- Their weakness that matters most for our current situation
- Recommended positioning in 2-3 sentences
- One thing the operator should know right now

---

## Deep Analysis

### Company Profile
- HQ, ownership, revenue, market cap (if public)
- Employee count, leadership
- Recent strategic moves (last 12 months)
- Financial health signals

### Product Comparison

The rows are the capabilities that buyers in the company's sector compare. Take them from `business-info` and `strategy`. Fall back to these rows when the records name none:

| Capability | [Competitor] | [Our product] | Advantage |
|------------|-------------|----------|-----------|
| Architecture | | | |
| Core capability | | | |
| AI/ML capabilities | | | |
| Deployment model | | | |
| Scalability | | | |
| Data control and compliance | | | |
| API/Integration | | | |

### Geographic Overlap
- Where they operate vs. where the company operates or plans to operate (`strategy`)
- Regions where we'll compete directly
- Regions where they can't operate (geopolitical or regulatory barriers)

### Their Strengths (Honest Assessment)
- What they do well
- Where they have advantages we need to respect
- Their installed base / switching cost moat

### Their Weaknesses (Exploitable)
- Technical gaps
- Strategic missteps
- Geopolitical liabilities
- Financial vulnerabilities
- Market positioning gaps

### Win Strategy
- How to position against them in a head-to-head evaluation
- Key messages for each buyer audience that `strategy` names (for example a technical buyer, an executive buyer, an investor)
- Objection handling: what they'll say about us, and our response
- The killer argument (one sentence that ends the comparison)

### Recommended Actions
- Immediate: what to do right now
- Medium-term: how to position over the next 6 months
- What to watch: signals that indicate their next move

---

**Output:** Competitor intelligence brief. Actionable, honest, and specific to the company's current situation. Name any company record Phase 0 found missing.

**Save the brief** as `[competitor-slug]/YYYY-MM-DD_competitor-intel-[quick|deep].md` under `competitor-dossiers`. Resolve the directory with `.venv/bin/python scripts/data-path.py path competitor-dossiers`, create the `[competitor-slug]` folder when it does not exist, and give the operator the full path of the saved file. The brief is internal: it stays in the data repository.

**Post-synthesis audit (required).** Per development-standards, this skill synthesizes over a source set. After writing the brief, run `/brain-audit --sources <the brief + any cited intel/datastore files> --entity "<competitor>"`. Append the returned footer, which carries newest-source dates, modality coverage, and source disagreements.

## Voice Rules

- Honest, direct, specific. Product names styled exactly as `business-info` and the competitor write them.
- Hyphens only (never em-dashes).

## NEVER
- Invent a capability, customer or figure for either side
- Sell a Hypothesized weakness as Confirmed
- Share the brief outside the company: it is internal
