# Skills: Intelligence and research

These ten skills gather outside information (world events, competitors, markets, people and companies, long documents, NotebookLM notebooks, YouTube) and weigh it against your own company records: `business-info`, `strategy`, `current-data`, `pipeline` and `voice-guide`. When a record is missing, the skill says so, suggests `/setup-wizard`, and carries on with a neutral frame rather than inventing anything. For the full index of skills and how a skill is called, see `SKILLS.md`. Sources come first.

## /ceo-intel

Writes a confidential world intelligence brief for you alone. It covers geopolitics, cybersecurity, markets and up to three priority regions, and each section ends with a callout on what it means for your company. The brief closes with three to five numbered action items, which may name your own deals and partners. You get an HTML file and a PDF, named `world-intel-YYYY-MM-DD`, saved under the registry name `ceo-intel-briefs`. The skill also reports a hidden-character check and any company record it found missing.

The priority regions come from your `strategy` record. If it names none, the skill picks regions from your focus or from the week's research and tells you which it chose. When the focus is a topic prediction markets cover, it may add a "Markets Are Pricing" section from Polymarket. That section is for internal use only and never goes into anything external.

**How to call it.** `/ceo-intel [focus]`, where the focus is an optional topic or region to emphasise. Or say it in your own words, for example: "world intel", "geopolitical brief", "what's happening globally".

**Example.**

```text
You:        /ceo-intel shipping disruption in the Baltic
Assistant:  World Intelligence Brief, threat level ELEVATED. Five sections, three
            regional columns, four action items. The first: re-engage Northwind
            Logistics this week while port delays dominate the news. Saved
            world-intel-2026-11-03.html and .pdf under ceo-intel-briefs.
            Hidden characters: clean. Missing record: current-data.
```

**What it needs.** Your company records listed above, plus `search-domains`, `people` and, if you keep it, `market-intelligence`. `PERPLEXITY_API_KEY` for the deep research pass and `FIRECRAWL_API_KEY` for fetching full articles, both in `.env`. Firecrawl also needs the `research` extra (`uv sync --extra research`, see `INSTALL.md`). Before the first Firecrawl call the skill stops and asks you to confirm your VPN exit.

**When not to use it.** For one company or person, use `/osint`. For a version you can share outside, use `/intel-briefing-newsletter`. For one sector or region, use `/market-brief`.

## /competitor-intel

Compares a named competitor with your own company and product. A deep run gives a company profile, a product comparison table, geographic overlap, an honest list of their strengths, their exploitable weaknesses, a win strategy and recommended actions. A quick run fits on one page. Pick by the stakes. Every material claim is graded Confirmed, Deduced or Hypothesized, and a Hypothesized weakness is never presented as fact.

The skill also keeps a running case file for each competitor under the registry name `intel-cases`. On later runs it reads that file first and updates each theory's status. Nothing is erased. A disproved theory is marked Refuted with a one-line reason. The case file is built from a template in your data repository; on a fresh install without that template the skill says so and skips the case file. The brief itself is saved as `<competitor-slug>/YYYY-MM-DD_competitor-intel-quick.md` (or `-deep.md`) under the registry name `competitor-dossiers`, and the skill gives you its full path. It is internal and is not to be shared outside the company.

**How to call it.** `/competitor-intel <company>`. The skill file also defines a depth of `quick` or `deep` (default `deep`). Or say it in your own words, for example: "competitor analysis of Acme Corp", "how does Acme Corp compare to us", "competing vendor Acme Corp".

**Example.**

```text
You:        /competitor-intel Acme Corp
Assistant:  Acme Corp vs. your product. Seven capability rows; you lead on
            deployment model and data control, they lead on installed base.
            Biggest weakness: no on-premises option (Confirmed, two sources).
            Killer argument and three immediate actions follow. Case file
            acme-corp.md created under intel-cases.
```

**What it needs.** Your company records, plus `search-domains`, `market-intelligence`, `competitor-dossiers` and `deliverable-documents` if you keep them. `FIRECRAWL_API_KEY` in `.env` and the `research` extra for the website crawl, with the VPN confirmation before it. The comparison columns for your side stay empty if `business-info` is missing.

**When not to use it.** If the target is a person, use `/osint`. For market size, use `/market-brief`. If you say "versus" without naming the second party, the skill asks you who you mean before starting.

## /market-brief

Gives a fast brief on a region, country, sector or technology trend, seen from your company's position. The default `surface` depth gives three to five points: market size and growth, key drivers, your position and opportunity, and two or three actions. The `detailed` depth adds the main competitors, regulation, entry timing, recommended positioning, and risks with their mitigations. Every figure is sourced.

If the topic falls inside what Polymarket covers (AI, big tech, elections, geopolitics, crypto, macro, global sports and corporate events), the brief adds a "Markets Are Pricing" table. That data is internal signal only. Never quote it outside. The skill file does not name a save location for this brief.

**How to call it.** `/market-brief [topic]`. The skill file also defines a depth of `surface` (default) or `detailed`, and an optional purpose. Or say it in your own words, for example: "market for cold-chain logistics in Southeast Asia", "TAM for drone inspection", "sector overview of industrial IoT".

**Example.**

```text
You:        /market-brief cold-chain logistics in Southeast Asia, detailed
Assistant:  Market Brief: cold-chain logistics, Southeast Asia. Size and growth
            with sources, four drivers, six competitors, a regulatory summary,
            entry timing "next 12 months", and three risks with mitigations.
            Your position: strongest in mid-size fleets. Missing record:
            pipeline.
```

**What it needs.** Your company records, plus `search-domains` and, if present, `market-intelligence` and `geopolitical-landscape`. Nothing beyond the core install otherwise; it uses the built-in web search.

**When not to use it.** For one named company, use `/competitor-intel` or `/osint`. For world events and geopolitics, use `/ceo-intel`. For deep, verified research on a public question, use `/deep-research-advance`.

## /osint

The base investigation skill. It researches one target, a company, a person, a market or a technology, through several parallel search streams and writes an executive brief. Each finding carries a confidence tag (HIGH, MEDIUM, LOW or UNVERIFIED), an evidence grade and its source. The brief also lists intelligence gaps, business relevance and recommended next skills.

Before searching, it turns the target name into a structured plan (official name, aliases, parent company, key people, social handles) and uses those as extra search terms. It checks your CRM and earlier dossiers for what you already hold, and keeps a case file per target under `intel-cases`, as `/competitor-intel` does. Output goes to a dated folder `YYYY-MM-DD-<target>` under the registry name `osint-dossiers`: `brief.md`, `research-notes.md` with the raw results, and a self-contained `report.html`. For a person without a CRM entry it suggests `/crm add`. You decide.

**How to call it.** `/osint <target>`. The skill file defines mode (`company`, `person`, `market` or `technology`, detected automatically), depth (`standard`, `deep` or `maximum`, default `deep`), and an optional focus and context. Or say it in your own words, for example: "dig into Northwind Logistics", "who is Dana Whitfield", "due diligence on Acme Corp".

**Example.**

```text
You:        /osint Dana Whitfield, pre-meeting
Assistant:  Person mode. Resolved: Dana Whitfield, COO at Northwind Logistics.
            Eight streams run. Career history HIGH, board seats MEDIUM, one
            conference talk LOW. Gaps: no current financial filings.
            No CRM entry; suggest /crm add. Saved brief.md, research-notes.md
            and report.html under osint-dossiers.
```

**What it needs.** `search-domains`, `strategy`, `pipeline`, `people`, `business-info` and your CRM contacts. For the name resolution step, `TAVILY_API_KEY` (with `BRAVE_API_KEY` as fallback) and `ANTHROPIC_API_KEY`; without them it falls back to the literal target name and notes the gap. `FIRECRAWL_API_KEY` and the `research` extra for fetching pages. It asks you to confirm your VPN exit before reaching public sources.

**When not to use it.** To find what you already know about a subject, use `/recall`. For a comparison with your product, use `/competitor-intel`. For a quick sector snapshot, use `/market-brief`. For world events, use `/ceo-intel`. For sanctions, breach and registry checks, follow up with `/osint-advanced`.

## /osint-advanced

Queries specialised databases that `/osint` does not reach: sanctions lists, corporate registries, breach databases, threat intelligence, infrastructure scanners, and username and image search. It is meant for after `/osint`, when one dimension needs more depth, for example compliance screening before a deal. In company and person mode the sanctions check always runs, and the brief opens with a clear CLEAR, MATCH or PARTIAL banner. A database that could not be queried is reported as not run, never as clear.

Output goes to a dated folder under the registry name `osint-advanced-reports`: `brief.md`, `tool-responses.md` with the raw answers, and `report.html` with the sanctions banner and a log of every tool tried.

**How to call it.** `/osint-advanced <target>`, where the target can be a name, company, email, username, domain or region. The skill file also defines mode, a list of streams, and a depth of `quick` or `full`. Only the typed command starts it; plain words do not.

**Example.**

```text
You:        /osint-advanced Acme Corp
Assistant:  Company mode. Sanctions: CLEAR (OpenSanctions queried). Registry,
            infrastructure, threat and breach streams run; DeHashed not run
            (no key). Two exposed subdomains, one 2024 breach mention (MEDIUM).
            Saved brief.md, tool-responses.md and report.html under
            osint-advanced-reports.
```

**What it needs.** Keys in `.env` for the paid lookups, each optional: `OPENSANCTIONS_API_KEY`, `HUNTER_API_KEY`, `HIBP_API_KEY`, `VIRUSTOTAL_API_KEY` and `DEHASHED_API_KEY`. The skill calls them through its helper script, which lists its commands with `.venv/bin/python .claude/skills/osint-advanced/scripts/osint_api.py --help`. `OPENSANCTIONS_API_KEY` is read by that script but is not listed in `.env.example`, so add the line yourself. `FIRECRAWL_API_KEY` and the `research` extra serve as a fallback, with the VPN confirmation first.

**When not to use it.** For a first look at any target, use `/osint`. For a comparison with your product, use `/competitor-intel`.

## /deep-research-advance

Produces a deep, fact-checked report on a public question. It takes a while. A script asks Perplexity for several research angles and asks a Kimi model, through CLIProxyAPI, to break down the question and check each claim. Claude then audits the result and writes the report: a short summary with a clear position, findings per angle with inline citations, a claims table with status and confidence, contradictions, and the source list. When there are more than twelve sources, or you pass `--critical`, Claude tries to refute every supported claim and downgrades any it cannot defend.

The report is saved as `report.md` in a run folder under the registry name `research-surveys`. Afterwards it proposes high-confidence claims for Odin's knowledge base and waits for your approval; nothing is written there without it.

Your question and the gathered web text go to outside clouds. Never use it on private matters: contacts, deals, pricing, mail or chat history. If the question touches private data, the skill refuses and points you elsewhere.

**How to call it.** `/deep-research-advance "<question>" [--critical] [--audio] [--depth N] [--domains a.com,b.com]`. `--depth` sets the number of angles (default 4, maximum 8). The script also accepts `--recency` (hour, day, week, month or year) and `--exclude-domains`. `--audio` offers a NotebookLM audio overview at the end, after a separate confirmation. Or say it in your own words, for example: "advanced deep research on EU battery recycling rules", "deep research with verification on solid-state batteries".

**Example.**

```text
You:        /deep-research-advance "How do EU battery passport rules affect small importers?" --critical
Assistant:  Governor: FIRED (critical flag). 4 angles, 17 sources. Two claims
            downgraded to unsupported. Report saved under research-surveys.
            Proposed Odin distillate: 3 entries. Reply "approve" to write them.
```

**What it needs.** `PERPLEXITY_API_KEY` and `CLIPROXY_API_KEY` in `.env`, and CLIProxyAPI running (see `CLIPROXY.md`). Without any Perplexity results the skill falls back to its own web search and says so in the report header.

**When not to use it.** For private or internal questions, use `/recall` or `/odin`. For a quick lookup, use `/osint`. For Odin's own knowledge, use `/odin`.

## /docparse

Reads PDF, DOCX, PPTX and XLSX files with the position of every piece of text on the page, then answers your question with citations that point to the exact place in the source. Each citation is a short verbatim quote with its file and page. For pages that are mostly charts or diagrams, it renders the page as an image and reads it; a value taken from an image is marked as such and gets no position citation. It keeps a parse cache. A second question on the same file is faster.

With no question, it summarises the documents and offers to answer one or build a report. With `--report`, it writes a visual citation report, an HTML file and a PDF, under the registry name `docparse-reports`. Documents over 50 pages prompt it to ask which pages to read, and it never reads more than 100 pages without a `--pages` range.

**How to call it.** `/docparse [file-path or directory] [question]`, with optional `--report` and `--pages 1-10`. Or say it in your own words, for example: "parse this document", "show me where it says the warranty ends", "extract from this PDF".

**Example.**

```text
You:        /docparse contracts/acme-supply.pdf "When can either side terminate?"
Assistant:  Parsed 1 file, 14 pages (fresh parse). Either party may terminate
            on 90 days' written notice [1], or at once for an uncured breach
            after 30 days [2]. Six citations, pages 9 to 11. Generate a visual
            citation report?
```

**What it needs.** Nothing beyond the core install. Its parser, LiteParse, is part of the core packages. The skill first runs `.venv/bin/python scripts/docparse.py setup --check` and stops with install steps if a check fails. For a password-protected file, the parser script reads the password from the `DOCPARSE_PASSWORD` environment variable.

**When not to use it.** For plain text extraction without positions, use `scripts/datastore-extract.py`. For web pages, use `/playwright`. For a question across all your records, use `/recall`.

## /notebooklm

Controls Google NotebookLM from the terminal. It creates notebooks, adds sources (web links, text, files), asks a notebook questions with citations, generates an audio overview (a podcast-style discussion of the sources), runs source discovery on a topic, writes briefing reports, and downloads results. Files are saved under the registry name `notebooklm-output`, sorted into `audio/`, `reports/`, `research/`, `queries/` and `downloads/`, with names like `YYYY-MM-DD-<slug>`.

It relies on Google interfaces that are not officially documented, so it can break without warning. It checks your login before every action. If the login has expired it stops and asks you to run `nlm login` yourself, because that opens a browser. After a query or report it may suggest passing the result to `/odin` or `/zk`, but it never does so on its own.

**How to call it.** `/notebooklm [status|create|add|query|audio|research|report|describe|download] [args]`. With no arguments it shows a menu of the nine modes. Or say it in your own words, for example: "create a notebook on port automation", "audio overview of that notebook", "add sources to notebook".

**Example.**

```text
You:        /notebooklm create "Port Automation"
Assistant:  Login OK. Notebook "Port Automation" created. Add sources next:
            /notebooklm add <notebook-id> https://example.com/report
```

**What it needs.** The `nlm` command, installed with `pip install notebooklm-mcp-cli`, and a Google account signed in through `nlm login`. No `.env` key. Sources you add are sent to your Google account.

**When not to use it.** For research on a company or person, use `/osint`. For notes in your own knowledge base, use `/zk`. For YouTube content, use `/yt-pulse`. For slides, use `/pptx-generator`.

## /yt-pulse

Scans YouTube for recent videos on any topic and tells you what creators are talking about. Any topic works. It ranks videos by engagement, groups them into three to five hot topics, then takes the top video, reads its transcript, and gives key takeaways, a summary, notable quotes and a watch recommendation. It scores how relevant the topic is to your company and, at MEDIUM or above, adds one to three action items. You get `yt-pulse-brief.md`, `.html` and `.pdf` in a dated folder under the registry name `yt-pulse-briefs`.

YouTube often blocks data-centre and some VPN addresses. So the skill first checks your public IP address and asks you to confirm your VPN and to close the browser it reads YouTube cookies from. It waits for your answer. If the transcript is blocked anyway, it falls back to web summaries of the video and says so in the brief.

**How to call it.** `/yt-pulse <query>`. The skill file also defines a timeframe (`24h`, `72h`, `7d` or `30d`, default `72h`) and a depth (`quick` for search and topics only, or `full`, the default). Or say it in your own words, for example: "scan YouTube for warehouse robotics", "what's trending on YouTube about drone delivery".

**Example.**

```text
You:        /yt-pulse warehouse robotics, 7d
Assistant:  41 videos found. Hot topics: humanoid pickers (12), AMR fleet
            software (9), safety rules (6). Top video: a 14-minute plant tour,
            "Watch if time permits". Relevance: MEDIUM, two actions. Brief
            saved as MD, HTML and PDF under yt-pulse-briefs.
```

**What it needs.** The `media` extra (`uv sync --extra media`) for the YouTube tools. By default it reads cookies from a Brave profile; the search script accepts `--browser` to pick another browser or `--browser none` to turn this off. It reads `strategy`, `business-info` and `pipeline` for the relevance score. No `.env` key is required.

**When not to use it.** For research outside YouTube, use `/osint`. To read one specific video's transcript, use `/playwright`.

## /intel-briefing-newsletter

Produces a branded intelligence newsletter you can share outside the company. It runs the same kind of world research as `/ceo-intel`, then writes ten sections: hero, five indicators, Sea State (world events), The Cyber Front, a regional Navigation Chart, Market Depth, The Heading (your company's own reading of events), Signal Watch, recommended reading, and a threat level. Before rendering, it scans everything for partner, client, investor and reseller names, deal values and internal figures, and replaces anything confidential with general wording. Then it renders.

It renders `intelligence-briefing.html` and a PDF into a dated folder under the registry name `newsletters`, and stops there. It never sends, mails or posts the issue; sharing it is your own step. The look comes from your brand kit (registry name `brand-kit`, files `newsletter.css` and `newsletter-brand.json`); without them it renders in a plain neutral style and tells you.

**How to call it.** `/intel-briefing-newsletter [focus]`. The skill file also defines an optional issue number, otherwise counted from earlier issues. Or say it in your own words, for example: "newsletter", "intel briefing", "publish intelligence brief".

**Example.**

```text
You:        /intel-briefing-newsletter supply-chain security
Assistant:  Issue 7, threat level HIGH. Confidentiality review removed two
            client names. Rendered intelligence-briefing.html and .pdf under
            newsletters. Brand kit found. Hidden characters: clean.
            Open both files to check them before you share.
```

**What it needs.** Your company records, plus `search-domains`, `people` (to know which names must never appear) and, if present, `market-intelligence` and a newsletter guide in your data repository. `PERPLEXITY_API_KEY` and `FIRECRAWL_API_KEY` in `.env`, the `research` extra, and the VPN confirmation before fetching. Optional section images use `REPLICATE_API_TOKEN`.

**When not to use it.** For an internal brief that names your deals and gives action items, use `/ceo-intel`. For a LinkedIn post, use `/linkedin-post`.
