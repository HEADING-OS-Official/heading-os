---
name: ceo-intel
x-heading-requires: ["research"]   # F-7.1: optional-dependency extras this skill needs
description: Global, geopolitical, and cross-domain intelligence brief for the CEO - world events, technology and regulatory shifts, macro and threat signals relevant to the user's own company, rendered as a dated HTML/PDF brief. Use for world-scale situational awareness, not a single entity. Trigger when the user says "world intel", "geopolitical brief", "what's happening globally", "global threats", or "CEO intelligence brief". Do NOT use for a specific company or person (use /osint) or an externally published newsletter (use /intel-briefing-newsletter).
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "2.0"
argument-hint: "[focus]"
context: fork
allowed-tools: "WebSearch, WebFetch, Read, Bash(.venv/bin/python:*)"
model: sonnet
x-heading-capability:
  what: >
    A confidential CEO-eyes-only world intelligence brief - geopolitics, cybersecurity, markets, and the company's priority regions, each section framed through the company's own lens (read from its records) with direct business implications and action items.
  how: >
    Run /ceo-intel [focus]. Forked-context research pass that writes an HTML + PDF brief named world-intel-YYYY-MM-DD.html under the registry name `ceo-intel-briefs`. Names partners, deals, and investors - never shared externally.
  when: >
    Use for internal world/geopolitical situational awareness with company action items. For the public-safe version use /intel-briefing-newsletter; for a specific company or person use /osint.
x-heading-routing:
  category: Intel
  triggers:
    - world intel
    - geopolitical brief
    - what's happening globally
    - global threats
    - CEO intelligence brief
  exclusions:
    - Specific company or person -> /osint
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

# CEO Intelligence Brief

Generate a confidential world intelligence brief for CEO eyes only. Covers geopolitics, cybersecurity, markets, and regional developments - framed through the company's strategic lens with direct business implications and action items.

## Variables

focus: [Optional - specific topic or region to emphasize, e.g., "a regional conflict", "regional defense", "regional telecom"]

---

## Phase 0: Company context

This skill frames everything through the user's own company, read at run time and never written into this file. Resolve each name with `read <name>` and read what is there:

- `business-info` - the company, its products and how it writes their names, its sector, partners and customers
- `strategy` - priorities, positioning, target markets and priority regions
- `current-data` - live metrics, deployments and proof points
- `pipeline` - active deals and investor conversations
- `voice-guide` - voice and tone
- `skill-data skill=ceo-intel` - the operator's own frame for this skill, if one exists: company lens, section and callout names, region list, proof points, voice notes. Where it is present, take its frame over; the procedure below stays this skill's.

Take the company name, products, sector, priority regions and position from these records. Use them wherever this file says "the company", "the product" or "Priority Region N". A record can be missing or empty. Then name it in the output and say how to fill it: run `/setup-wizard`, or write the file at the address `.venv/bin/python scripts/data-path.py path <name>` prints. Continue with a neutral frame that claims nothing the records do not hold. Never invent a company, a product, a customer or a figure.

Also read:
- `market-intelligence` - the company's market data, if the operator keeps one
- `search-domains` - Domain filtering for research
- `people` - Key contacts and relationship context

The priority regions are the ones `strategy` names (up to three). With none named, use the regions the focus implies. Failing that, use the three regions with the most activity in this week's research, and say which you chose.

---

## Instructions

**Classification:** Internal - CEO Eyes Only. This document contains company-specific strategic analysis, business implications, and action recommendations. NEVER share externally.

---

## Phase 1: Research (Parallel)

Before searching, read `search-domains` for domain filtering configuration.

Run these searches in parallel to gather live intelligence. Use `allowed_domains` from the matching topic group(s):

1. **Geopolitics & Security** - WebSearch for major world events, conflicts, policy changes
   - `allowed_domains`: Geopolitics & Defense group
2. **Cybersecurity** - WebSearch for cyber incidents, threat actors, vulnerabilities, breaches
   - `allowed_domains`: Cybersecurity + General Tech groups
3. **[Priority Region 1]** - WebSearch for the developments in that region that touch the company's sector
   - `allowed_domains`: [Priority Region 1] + Geopolitics & Defense groups
4. **[Priority Region 2]** - WebSearch for the priority region and regional developments
   - `allowed_domains`: [Priority Region 2] + Geopolitics & Defense groups
5. **[Priority Region 3]** - WebSearch for sector, infrastructure and investment developments
   - `allowed_domains`: [Priority Region 3] + the group matching the company's sector
6. **Markets & Sector** - WebSearch for capital markets and the company's own industry trends
   - `allowed_domains`: Markets & Finance + the group matching the company's sector
7. **Perplexity Deep Research** - Run with domain filtering:
   ```bash
   .venv/bin/python scripts/perplexity-research.py --domains "reuters.com,bbc.com,ft.com,bleepingcomputer.com,darkreading.com,therecord.media" "current world events [focus area] cybersecurity [company sector] markets [current month] [current year]"
   ```

A topic group that `search-domains` does not have is skipped for that stream, with `blocked_domains` still applied.

Also fetch World Monitor for aggregated intelligence:
- https://worldmonitor.app/
- https://tech.worldmonitor.app/
- https://finance.worldmonitor.app/

After WebSearch returns article URLs, batch-scrape top articles via `.venv/bin/python scripts/firecrawl.py batch` for full content instead of relying on search snippets. Before the first firecrawl call, run the VPN pre-flight gate in `reference/vpn-preflight.md` and wait for an explicit answer.

---

## Phase 2: Write the Brief

Generate a single-page HTML intelligence brief with these sections. `[Company]` below is the company's short name from `business-info`; with no record it is "Company".

### Header
- Title: "World Intelligence Brief"
- Subtitle: "[Company] - Strategic Intelligence"
- Date: Current date
- Classification: "Internal - CEO Eyes Only"

### Threat Level Bar
- Set to one of: LOW, GUARDED, ELEVATED, HIGH, CRITICAL
- Color: green (LOW/GUARDED), orange (ELEVATED), red (HIGH/CRITICAL)

### Key Metrics Strip
- 5 metrics with large values, small labels, and color coding (up/danger/neutral)
- Focus on: the company's own market, threat counts, commodity prices, regional indicators

### Sections (each with a "[Company] Relevance" callout)

Each section gets:
- **Section title** (uppercase, bordered)
- **Body text** - 1-2 paragraphs of factual intelligence
- **[Company] Relevance callout** - Orange left-border box with direct business implications

Write these sections:

1. **Geopolitical** - The dominant world events affecting the company's markets. What's happening and why it matters for the company's buyers.

2. **Economic / Sector Market** - The company's market data, adoption trends, investment climate. Reference `market-intelligence` for baseline figures when it exists. Include fresh data from research.

3. **Markets Are Pricing** (conditional, Polymarket) - if the focus topic matches the Polymarket coverage whitelist (`polymarket-coverage`), include this section. Run:

   ```bash
   .venv/bin/python scripts/polymarket.py "$FOCUS_OR_TOP_THEME" --output markdown
   # Disambiguation rule (P4): for one-or-two-word topics that could match multiple entities,
   # pass --keywords with 2-3 disambiguators (e.g., "Apple" --keywords "company,stock,iphone").
   ```

   Include the rendered markdown table verbatim in the brief, INCLUDING the trailing internal-use footer line. If the command prints nothing, omit the section silently. It prints nothing on `outside_whitelist` and `no_matches`; on `fetch_error` it exits 1 with the reason on stderr.

   **External-use boundary (CRITICAL):** Polymarket data is internal signal only. NEVER quote in proposals, letters, partnership documents, RFP responses, LinkedIn posts, or any external company communication. Boundary pinned in `polymarket-coverage`.

4. **Cybersecurity** - Specific incidents, threat actors, attack statistics. Focus on threats relevant to the company's target verticals as `strategy` names them.

5. **Regional Intelligence** - Three-column layout, one column per priority region:
   - **[Priority Region 1]**, **[Priority Region 2]**, **[Priority Region 3]**: the developments in each that touch the company's sector, buyers and partners.
   - Each region gets its own "[Company] Relevance" callout.

6. **Strategic Implications** - Numbered list of 3-5 specific action items for the operator. Be direct:
   - Reference specific pipeline deals or partners where relevant
   - Suggest timing ("push now", "re-engage this week", "prepare for Q2")
   - Connect intelligence to fundraising narrative, deal positioning, or partner enablement
   - Flag risks or opportunities that need immediate attention

With no `strategy` or `pipeline` record, the callouts and implications stay at the level of the company's sector and say that the records were missing.

### Footer
- "[Company] - Confidential Intelligence Brief"
- Source attribution line

---

## Phase 3: Generate Output

1. **Create the HTML file** at `$BRIEFS_DIR/world-intel-YYYY-MM-DD.html` (resolver below)

   The logical name is `ceo-intel-briefs`, and every block below resolves it.

   Use this design system:
   - Font: Segoe UI / Helvetica Neue / Arial
   - Body: 10pt, line-height 1.55
   - Page: A4 with 20mm margins
   - Colors: #1a1a1a (ink), #c77b30 (accent/relevance), #c0392b (danger), #1a7a3a (up)
   - Relevance callouts: #f7f3ed background, 3px left border in #c77b30
   - Metrics strip: bordered boxes, large values (14pt bold), small uppercase labels (7pt)
   - Regional columns: flex layout, 3 equal columns
   - Section titles: 11pt, 800 weight, uppercase, letter-spacing 1.5px

   Bash gets no path redirect, so each block below resolves the name first.

2. **Convert to PDF:**
   ```bash
   BRIEFS_DIR="$(.venv/bin/python scripts/data-path.py path ceo-intel-briefs)"
   .venv/bin/python scripts/html-to-pdf.py "$BRIEFS_DIR/world-intel-YYYY-MM-DD.html"
   ```

3. **Validate:**
   ```bash
   BRIEFS_DIR="$(.venv/bin/python scripts/data-path.py path ceo-intel-briefs)"
   .venv/bin/python scripts/sanitize-text.py "$BRIEFS_DIR/world-intel-YYYY-MM-DD.html" --scan
   ```

4. Report both file paths (HTML + PDF), hidden character status, and any company record Phase 0 found missing.

5. **Post-synthesis audit:** run `/brain-audit --sources <the brief source files>` (omit `--entity` — this is a multi-region brief, not entity-scoped) and append the returned footer. Per development-standards, any synthesis-over-sources skill composes /brain-audit.

---

## Voice Rules

- **Intelligence sections**: Third-person, objective, factual. Authoritative.
- **Relevance callouts**: Direct, tactical, second-person ("you", "your"). Speak to the operator as the captain.
- **Strategic Implications**: Action-oriented. Specific. No hedging.
- **Overall**: Hyphens only (never em-dashes). Product names styled exactly as `business-info` writes them.
- **Length**: Total brief should fit on 1-2 A4 pages. Concise is better.

## What Makes This Different from /intel-briefing-newsletter

This brief is the OPPOSITE of the public newsletter:
- It names partners, clients, pipeline deals, and investors where relevant
- It contains direct business advice and action items
- It references internal strategy, pricing, and competitive positioning
- It frames every piece of intelligence through "what should the operator do about this?"
- It is NEVER shared externally

## NEVER
- Share this document outside CEO context
- Publish any content from this brief without sanitizing confidential information
- Use this format for external communications
- Invent a company, partner, deal or figure the records do not hold

## Knowledge Base

After delivering the brief, offer this: "Want me to capture the lasting signals? The `/odin log` command records them as an episode in Odin's brain (CEO-only). The `/zk distill` command adds durable intelligence to the knowledge base."
