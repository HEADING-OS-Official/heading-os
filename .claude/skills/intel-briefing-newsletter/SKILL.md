---
name: intel-briefing-newsletter
x-heading-requires: ["research"]   # F-7.1: optional-dependency extras this skill needs
description: Produce an external-facing intelligence briefing newsletter - curated, publishable intel framed for an audience beyond the CEO, rendered to HTML and PDF under the user's own brand. Use when the output is meant to be shared/published, not a private desk brief. Trigger when the user says "newsletter", "intel briefing", "publish intelligence brief", or "external intel brief". Do NOT use for an internal-only situational brief (use /ceo-intel).
argument-hint: "[focus]"
context: fork
allowed-tools: "WebSearch, WebFetch, Read, Bash(.venv/bin/python:*)"
model: sonnet
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "2.0"
x-heading-capability:
  what: >
    A branded, public-safe Intelligence Briefing newsletter with live world-intel research - hero, indicators, Sea State, Cyber Front, regional Navigation Chart, Market Depth and the company's own perspective - carrying zero confidential information, rendered under the company's brand from its brand-kit.
  how: >
    Run /intel-briefing-newsletter [focus]. Forked-context research, then a confidentiality scrub, then HTML + PDF generated into YYYY-MM-DD/ under newsletters. It renders the issue and stops; it never sends or distributes it.
  when: >
    Use to produce the external newsletter. For the CEO-only version that names partners and deals and adds action items use /ceo-intel.
x-heading-routing:
  category: Intel
  triggers:
    - newsletter
    - intel briefing
    - publish intelligence brief
    - external intel brief
  exclusions:
    - Internal-only brief -> /ceo-intel
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

# Intel Briefing Newsletter

Generate a branded Intelligence Briefing newsletter with live world intelligence research. This is the PUBLIC-SAFE newsletter: its content may be shared beyond the company, so it contains zero confidential information. For CEO-only intelligence with business implications and action items, use `/ceo-intel` instead.

This skill renders the issue to HTML and PDF and stops there. It never sends, mails, posts or otherwise distributes it; sharing the files is the operator's own step.

## Variables

focus: [Optional - specific topic or region to emphasize, e.g., "regional cybersecurity", "emerging-market telecom", "global markets"]

issue_number: [Optional - issue number, defaults to auto-detect from existing folders in `newsletters`]

---

## Phase 0: Company context

This skill frames everything through the user's own company, read at run time and never written into this file. Resolve each name with `read <name>` and read what is there:

- `business-info` - the company, its products and how it writes their names, its sector, partners and customers
- `strategy` - priorities, positioning, target markets and priority regions
- `current-data` - live metrics, deployments and proof points
- `pipeline` - active deals and investor conversations
- `voice-guide` - voice and tone
- `skill-data skill=intel-briefing-newsletter` - the operator's own frame for this skill, if one exists: company lens, section and callout names, region list, proof points, voice notes. Where it is present, take its frame over; the procedure below stays this skill's.

Take the company name, products, sector, priority regions and position from these records. Use them wherever this file says "the company", "the product" or "Priority Region N". A record can be missing or empty. Then name it in the output and say how to fill it: run `/setup-wizard`, or write the file at the address `.venv/bin/python scripts/data-path.py path <name>` prints. Continue with a neutral frame that claims nothing the records do not hold. Never invent a company, a product, a customer or a figure.

Also read:
- `newsletter-guide.md` under `company-reference` in the data repository (`.venv/bin/python scripts/data-path.py path company-reference` prints the directory) - Content structure, design system, confidentiality rules, image guidelines. A public clone does not have it: say so, and draft from the section list below without it
- `market-intelligence` - the company's market data for the Market Depth section, if present
- `people` - To know which names must NEVER appear in the newsletter

The priority regions are the ones `strategy` names (up to three). With none named, use the three regions this issue's research makes most relevant, and say which you chose.

---

## Instructions

**Classification:** Semi-Public. Safe for external sharing. NEVER include confidential information.

---

## Phase 1: Research (Parallel)

Before searching, read `search-domains` for domain filtering configuration and the Stream-to-Topic Mapping table.

Run these searches in parallel to gather live intelligence. For each stream, use `allowed_domains` from the matching topic group(s) and always apply the Blocked Domains list as `blocked_domains`. A group that `search-domains` does not have is skipped for that stream:

1. **Geopolitics & Security** - WebSearch for major world events, conflicts, policy changes
   - `allowed_domains`: Geopolitics & Defense group
2. **Cybersecurity** - WebSearch for cyber incidents, threat actors, vulnerabilities, breaches
   - `allowed_domains`: Cybersecurity + General Tech groups
3. **[Priority Region 1]** - WebSearch for the developments there that touch the company's sector
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

Also fetch World Monitor for aggregated intelligence:
- https://worldmonitor.app/
- https://tech.worldmonitor.app/
- https://finance.worldmonitor.app/

After WebSearch returns article URLs, batch-scrape top articles via `.venv/bin/python scripts/firecrawl.py batch` for full content instead of relying on search snippets. Before the first firecrawl call, run the VPN pre-flight gate in `reference/vpn-preflight.md` and wait for an explicit answer.

---

## Phase 2: Synthesize Content

Write each section following the newsletter guide when it exists:

1. **Hero** - a dramatic 2-4 word title, with line breaks for stacking and one accent word. Add a 1-2 sentence deck that summarizes the dominant narrative.
2. **Indicators** - Select 5 key metrics with value, label, and style (up/danger/neutral). Examples: commodity prices, infrastructure status, incident counts, market sizes.
3. **Sea State** - The dominant global events. Lead with what matters most. 2-4 paragraphs. Include `banner_title`, `banner_detail`, and `caption` for the radar banner.
4. **The Cyber Front** - Specific incidents, threat actors, attack patterns. 2-3 paragraphs. Include `badge` (APT identity), `big_stat` (one standout statistic), `banner_title`, `banner_detail`, and `caption`.
5. **Navigation Chart** - Regional intel, one entry per priority region. Each region gets `code`, `name` (with line breaks), and `body` (1-2 paragraphs).
6. **Market Depth** - The company's market, adoption, investment trends. 2-3 paragraphs. Include `bars` (15 values 0-100 for bar chart), `stats` (3 key metrics), `caption`, and optional `pullquote`.
7. **The Heading** - The company's own strategic read. Take it from `strategy` and `business-info`: what these events mean for its field. 2-3 paragraphs. Plain markdown text. With neither record, write it as a neutral editorial read and say the records were missing.
8. **Signal Watch** - 3-5 forward-looking items with **bold** keywords.
9. **Recommended Reading** - 2-3 curated links with title, url, source, and description.
10. **Threat Level** - Set to one of: ELEVATED, HIGH, CRITICAL, GUARDED, LOW.

---

## Phase 2.5: Structural pass (optional)

For a long deliverable, you may run the `/editorial-review` structural checklist over the assembled draft before you finalize it. It verifies the argument arc, claim-to-evidence linkage, and section hierarchy. Reference: `reference/editorial-review.md`. The prose-level voice pass (`humanization.md`) runs as usual after. Skip when the draft is short or already tight.

---

## Phase 3: Confidentiality Review

**CRITICAL.** Before generating HTML, scan ALL content for:

- Partner names (check `people` Partner section)
- Client names (check `pipeline` Active Deals and Won/Closed)
- Investor names (check `people` Investors section)
- Reseller names (check `people` Resellers section)
- Deal values, deployment locations, contract terms
- Internal metrics, team size, revenue figures
- Shareholder structure, corporate entities

If ANY confidential information is found, remove it and replace with generic language. With no `people` or `pipeline` record, say that this check ran without them.

---

## Phase 4: Generate HTML

**Resolve the DATA-overlay output dir first.** The newsletter folder, its `input.json`, the generated
images, and the HTML/PDF are all DATA artifacts. They must land in the data overlay, never the
engine tree. A bare data-relative path passed to a Bash script (or `mkdir`) resolves against the engine
git root. Resolve once from the workspace root and reuse `$NEWS_DIR` for every path below:

```bash
cd "$(git rev-parse --show-toplevel)"
NEWSLETTERS_DIR="$(.venv/bin/python scripts/data-path.py path newsletters)"
NEWS_DIR="$NEWSLETTERS_DIR/YYYY-MM-DD"   # substitute today's date
```

**The brand comes from the brand kit, never from this file.** The generator reads two files from the generator templates of `brand-kit`: the stylesheet `newsletter.css` and the brand settings `newsletter-brand.json`. The settings carry the name, subtitle, tagline, website, colours, fonts, logo, default regions and section titles, and the generator's own docstring documents every field. With neither file present it renders in a plain neutral style and says so on stderr. Report that line, and point the operator at the docstring to brand the issue.

1. **Determine issue number:** Count existing folders in `$NEWSLETTERS_DIR` and add 1, or use the provided issue_number variable.

2. **Create newsletter folder** at `$NEWS_DIR` (`mkdir -p "$NEWS_DIR"`).

3. **Generate images (optional).** If the content warrants a visual (especially for Sea State or Hero), generate via Nano Banana 2. Take the palette words from the `image_palette` brand setting when it is set. Otherwise use "muted neutral palette, dark ink accents":

```bash
.venv/bin/python ".claude/skills/flux-image/scripts/generate_image.py" \
  --prompt "Minimalist editorial illustration, [palette], light textured background, abstract data visualization aesthetic, no text, no words, no letters, no typography, clean negative space, newspaper editorial style, sophisticated and restrained, [section-specific suffix]" \
  --output "$NEWS_DIR/[section]-visual.png" \
  --aspect-ratio 16:9 --model banana --format png
```

Section-specific suffixes:
- Sea State: `concentric radar circles, geopolitical threat mapping, surveillance aesthetic`
- Cyber: `digital grid pattern, network node breach visualization, scanline interference`
- Markets: `abstract financial chart lines, market flow visualization`

4. **Create JSON input file** at `$NEWS_DIR/input.json` with this structure:

```json
{
  "date": "YYYY-MM-DD",
  "issue_number": N,
  "threat_level": "ELEVATED",
  "regions": ["[Region 1]", "[Region 2]", "[Region 3]"],
  "hero": {
    "kicker": "Intelligence Briefing - [Month] [Year]",
    "title": "Title\nWith\nBreaks",
    "accent_word": "OneWord",
    "deck": "1-2 sentence summary of dominant narrative."
  },
  "indicators": [
    {"value": "[value]", "label": "[indicator]", "style": "up"},
    {"value": "[value]", "label": "[indicator]", "style": "up"},
    {"value": "[value]", "label": "[indicator]", "style": "danger"},
    {"value": "[value]", "label": "[indicator]", "style": "danger"},
    {"value": "[value]", "label": "[indicator]", "style": "neutral"}
  ],
  "sea_state": {
    "body": "Markdown content with **bold** supported...",
    "banner_title": "Banner Title",
    "banner_detail": "Detail line",
    "caption": "Caption text"
  },
  "cyber_front": {
    "body": "Markdown content...",
    "badge": {"top": "APT Active", "name": "ThreatActorName", "bottom": "Attribution"},
    "banner_title": "Banner Title",
    "banner_detail": "Detail line",
    "caption": "Caption text",
    "big_stat": {
      "value": "[value]",
      "title": "Stat title",
      "description": "Stat description"
    }
  },
  "navigation_chart": {
    "r1": {"code": "[CODE1]", "name": "[Region\nOne]", "body": "Content..."},
    "r2": {"code": "[CODE2]", "name": "[Region\nTwo]", "body": "Content..."},
    "r3": {"code": "[CODE3]", "name": "[Region\nThree]", "body": "Content..."}
  },
  "market_depth": {
    "body": "Markdown content...",
    "bars": [35, 42, 38, 50, 44, 55, 48, 63, 70, 66, 78, 85, 91, 88, 95],
    "stats": [
      {"value": "[value]", "label": "[stat]", "style": "up"},
      {"value": "[value]", "label": "[stat]", "style": "dn"},
      {"value": "[value]", "label": "[stat]", "style": "gold"}
    ],
    "caption": "Caption text",
    "market_caption": "Chart annotation",
    "pullquote": {"text": "Quote text", "attribution": "Source"}
  },
  "the_heading": "Markdown content - the company's perspective...",
  "signal_watch": ["Item 1 with **bold** keywords...", "Item 2..."],
  "recommended_reading": [
    {"title": "Title", "url": "https://...", "source": "Source", "description": "Brief description"}
  ]
}
```

5. **Run the generator:**

Pass `--output-dir` every time, pointing at `$NEWS_DIR`. The generator's own default
resolves the same `newsletters` name, one dated folder beneath it.

```bash
.venv/bin/python scripts/generate-newsletter-html.py "$NEWS_DIR/input.json" --output-dir "$NEWS_DIR"
```

To include AI-generated images:

```bash
.venv/bin/python scripts/generate-newsletter-html.py "$NEWS_DIR/input.json" \
  --output-dir "$NEWS_DIR" \
  --images sea_state="$NEWS_DIR/sea-state-visual.png"
```

---

## Phase 5: Validate

1. The generator automatically creates both HTML and PDF (single continuous page) in the newsletter folder.
2. Run `.venv/bin/python scripts/sanitize-text.py "$NEWS_DIR/intelligence-briefing.html" --scan` to check for hidden characters.
3. Report both output file paths (HTML + PDF), the word count and the hidden character status. Also name each missing company record and say whether the brand kit was found.
4. Remind the operator to open the HTML file in a browser and the PDF to verify both outputs. Stop there: sending or publishing the issue is the operator's own step.

---

## Voice Rules

- **Intelligence sections** (Sea State, Cyber Front, Navigation Chart, Market Depth): Third-person, objective, factual. No "we" or "our."
- **The Heading section**: First-person plural ("we"). This is the company's strategic interpretation.
- **Signal Watch**: Brief, forward-looking. One line per item.
- **Overall**: Hyphens only (never em-dashes). Product names styled exactly as `business-info` writes them. Vocabulary register as `voice-guide` sets it.
- **Length**: Total content 800-1200 words across all sections.

## NEVER
- Partner, client, investor, or reseller names
- Internal financial data or deal details
- "Excited to share" / "thrilled" / corporate filler
- Em-dashes (use hyphens)
- Unverified statistics without sourcing
- Send, mail, post or distribute the issue: this skill only renders it
