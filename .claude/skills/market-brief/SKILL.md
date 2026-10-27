---
name: market-brief
description: Market or regional intelligence brief for a sector or geography - TAM/sizing, competitive landscape, regulatory environment, entry timing, and the user's own company positioning, framed through the company's lens as its records state it. Use for sector- or region-level analysis, not a single company. Trigger when the user says "market intel", "market for [sector]", "regional analysis", "sector overview", or "TAM for [sector]". Do NOT use for a specific named company (use /competitor-intel or /osint) or global geopolitical briefs (use /ceo-intel).
argument-hint: "[topic]"
context: fork
allowed-tools: "WebSearch, WebFetch, Read, Bash(.venv/bin/python:*)"
model: sonnet
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "2.0"
x-heading-capability:
  what: >
    Produces a fast market intelligence brief on a region, country, sector, or
    technology trend - market size and drivers, competitive landscape, and 2-3
    actionable insights framed through the company's own positioning, read
    from its records.
  how: >
    Run /market-brief [topic]. Forked-context web research; default depth is
    surface (3-5 points), pass detailed for full analysis.
  when: >
    Use for a sector, region, or TAM question. For a specific named company use
    /osint or /competitor-intel; for global geopolitics use /ceo-intel.
x-heading-routing:
  category: Intel
  triggers:
    - market intel
    - market for [sector]
    - regional analysis
    - sector overview
    - TAM for [sector]
    - market size for [sector]
  exclusions:
    - Specific named company -> /competitor-intel or /osint
    - global/geopolitical -> /ceo-intel
  compound: 'No'
  router: auto
---

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Ask the workspace's data-address resolver for
the real address before the file operation, and use what it answers. An unknown
name refuses rather than resolving to a plausible directory, and a path written
out here goes stale the day the directory moves.

The resolver, `scripts/data-path.py`, ships in the `heading-core` plugin bundle (HOS-48).

# Market Brief

Produce an instant market intelligence brief on a region, country, or sector. For a named competitor use /competitor-intel.

## Variables

topic: [Region / country / sector / technology trend — e.g., "[target-region] telco market", "[region] data-residency regulations", "5G security spending in [region]"]

depth: surface | detailed — default: surface (3-5 key points); detailed (full analysis)

purpose: [What decision or action this brief supports — optional]

---

## Phase 0: Company context

This skill frames everything through the user's own company, read at run time and never written into this file. Resolve each name with `read <name>` and read what is there:

- `business-info` - the company, its products and how it writes their names, its sector, partners and customers
- `strategy` - priorities, positioning, target markets and priority regions
- `current-data` - live metrics, deployments and proof points
- `pipeline` - active deals and investor conversations
- `voice-guide` - voice and tone
- `skill-data skill=market-brief` - the operator's own frame for this skill, if one exists: company lens, section and callout names, region list, proof points, voice notes. Where it is present, take its frame over; the procedure below stays this skill's.

Take the company name, products, sector, priority regions and position from these records. Use them wherever this file says "the company", "the product" or "Priority Region N". A record can be missing or empty. Then name it in the output and say how to fill it: run `/setup-wizard`, or write the file at the address `.venv/bin/python scripts/data-path.py path <name>` prints. Continue with a neutral frame that claims nothing the records do not hold. Never invent a company, a product, a customer or a figure.

Also read:
- `market-intelligence` — the company's market data and competitor landscape, if the operator keeps one
- `geopolitical-landscape` — Regional and regulatory context, if present
- `search-domains` — Domain filtering for web searches

---

## Instructions

When searching the web for market intelligence, apply domain filtering from `search-domains`:
- Use `allowed_domains` from the topic groups most relevant to the query. The group matching the company's sector serves its market. The relevant regional group serves regional demand
- Always apply Blocked Domains as `blocked_domains`
- For broad market overview queries, use `blocked_domains` only

Produce a brief covering:

**Surface (3-5 key points):**
- Market size and growth rate (if applicable)
- Key dynamics and drivers
- The company's position and opportunity
- 2-3 actionable insights for the operator

**Detailed (full analysis):**
- All of the above plus:
- Competitive landscape in this region/sector
- Regulatory environment
- Entry timing assessment
- Recommended positioning approach
- Specific risks and mitigations

Frame everything through the company's lens as `strategy` and `business-info` state it: its category, its differentiation, and the openings it targets. With neither record, write the brief as a neutral market view. Keep the position and opportunity points to what the topic itself supports, and say that the records were missing.

---

## Markets Are Pricing (conditional, Polymarket)

The Polymarket coverage whitelist is AI, big tech, elections, geopolitics, crypto, macro, global sports, and corporate events. See `polymarket-coverage`. If the topic matches it, include a "Markets Are Pricing" section in the brief output.

**Disambiguation rule (P4):** a topic of one or two words can match multiple entities. "Apple" could match Apple Inc or apple-fruit markets. In that case, pass `--keywords` with 2-3 disambiguators.

```bash
.venv/bin/python scripts/polymarket.py "$TOPIC" --output markdown
# OR with disambiguation:
.venv/bin/python scripts/polymarket.py "Apple" --keywords "company,stock,iphone" --output markdown
```

Include the rendered markdown table verbatim in the brief, including the trailing internal-use footer line. If the command prints nothing, omit the section silently and do not flag the absence. It prints nothing on `outside_whitelist` and `no_matches`; on `fetch_error` it exits 1 with the reason on stderr.

**External-use boundary (CRITICAL):** Polymarket data is internal signal only. NEVER quote in proposals, letters, partnership documents, OnePagers, RFP responses, LinkedIn posts, or any external company communication. Boundary pinned in `polymarket-coverage`.

---

## Post-synthesis audit (required)

Per development-standards, this skill synthesizes over a source set. After writing the brief, run `/brain-audit --sources <the brief, plus every DataStore record and reference file it cites> --entity "<market or sector>"` (omit `--entity` for a broad multi-region brief) and append the returned footer.

## Voice Rules

- Direct and specific; every figure sourced. Product names styled exactly as `business-info` writes them.
- Hyphens only (never em-dashes).

## NEVER
- Invent a market figure, a company position or a customer
- Quote Polymarket data in anything external
