# OSINT Research Streams - MODE: MARKET

Consumed by: `.claude/skills/osint/SKILL.md` Phase 1 when target mode resolves to `market`.

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Resolve the name before the file operation and
use what it prints:

    .venv/bin/python scripts/data-path.py path <name>

Use `read <name>` for a source that may legitimately be absent, and
`read --required <name>` for one whose absence is a defect. An unknown name
refuses; it never resolves to a plausible directory, and a path written out
here goes stale the day the directory moves.

Last Updated: 2026-06-10

**Use resolved plan from Phase 0.5** - substitute `canonical.name`, `canonical.region`, `canonical.key_terms`, and `key_players[].name` into the queries below where `[market/region]` appears. If a field is unresolved, fall back to the literal target string.

`[our sector]` below is the company's product category and sector, in the words `business-info` uses (read in Phase 0); with no `business-info`, drop it from the query. "The sector domain group" is the topic group in `search-domains` that matches it.

8 streams. Execute in parallel.

**Stream 1 - Market Size & Dynamics**
- WebSearch with Markets & Finance domains: "[market/region] market size growth forecast CAGR"
- Perplexity: `.venv/bin/python scripts/perplexity-research.py --domains "bloomberg.com,ft.com,reuters.com,tradingeconomics.com" "[market/region] economic outlook investment trends"`

**Stream 2 - Regulatory Landscape**
- WebSearch with Geopolitics & Defense domains: "[market/region] regulation policy [our sector] data protection"
- WebSearch: "[market/region] government procurement tender ICT"

**Stream 3 - Competitive Dynamics**
- WebSearch with the sector domain group: "[market/region] [our sector] vendors"
- Cross-reference against `market-intelligence`

**Stream 4 - Digital Infrastructure**
- WebSearch with the sector domain group: "[market/region] 5G deployment broadband internet penetration"
- WebSearch: "[market/region] data center cloud infrastructure data residency"

**Stream 5 - Investment & Funding Flows**
- WebSearch with Markets & Finance domains: "[market/region] investment technology venture capital funding"
- WebSearch: "[market/region] FDI foreign direct investment technology"

**Stream 6 - Key Decision Makers**
- WebSearch: "[market/region] ministry telecom communication digital transformation"
- WebSearch: "[market/region] telecom operator tier 1 carrier ISP"
- Cross-reference against `people` and `crm-contacts`

**Stream 7 - Geopolitical Context**
- WebSearch with Geopolitics & Defense domains: "[market/region] geopolitics alliance sovereignty"
- Use the region-specific domain groups `search-domains` carries for the target region
- Read `geopolitical-landscape` for baseline context

**Stream 8 - Business Opportunity Assessment**
- WebSearch: "[market/region] [our sector] incumbent vendor replacement"
- Cross-reference `pipeline` for existing deals
- Cross-reference `strategy` for GTM phasing
