# Skills: Strategy, meetings and decisions

These eight skills help you get ready for a meeting or a hard conversation, think a decision through, ask other models for a second opinion, grow a private advisor that learns from what you read, find what your workspace already knows, and write material for investors. None of them sends a message or an email to anyone. For the full index of skills, and for how a skill is called by command or by plain words, see `SKILLS.md`.

## /meeting-prep

Builds a brief for a meeting with a named outside person or company. It reads what your records hold on them (your people file, your pipeline, your strategy, your company description, market notes and any case file `/osint` wrote earlier), searches the web for recent news, and writes nine sections: who you are meeting, their world right now, your objective, five to seven talking points, the questions they will likely ask, competitive context, a suggested meeting flow, a negotiation plan in the Voss method, and one paragraph to read five minutes before you walk in. You get three files, `briefing.md`, `briefing.html` and `briefing.pdf`, in one folder named `YYYY-MM-<meeting-name>`. The folder goes under the account's `account-reports` when the company is one of your accounts, otherwise under `meeting-prep-briefs`. If a contact file exists in your CRM, the skill adds a note that prep was done.

**How to call it.** `/meeting-prep [with] [purpose]`. Or say it in your own words, for example: "meeting prep for Dana Whitfield from Acme Corp", "prepare for my meeting with the Northwind Logistics team".

**Example.**

```text
You:        /meeting-prep Dana Whitfield, COO of Northwind Logistics, renewal talk on Thursday
Assistant:  Brief ready: 6 talking points, 4 likely questions, an accusation audit and 3 calibrated
            questions. Northwind is an Analyst type, so lead with data. Files: briefing.md, .html, .pdf
            under meeting-prep-briefs/2026-11-northwind-renewal. Brain audit: the pipeline row is 41 days old.
```

**What it needs.** Your people, pipeline, strategy and company records (fill them with `/setup-wizard`). Web research uses `scripts/firecrawl.py`, which needs `FIRECRAWL_API_KEY` in `.env` and the optional `research` package extra; before the first crawl the skill asks a VPN question and waits for your answer. PDF output uses `scripts/html-to-pdf.py`. If you keep an Odin brain, it also cites up to three matching Odin principles.

**When not to use it.** For an internal meeting with no outside party, just ask. For a briefing with no named counterpart, use `/market-brief` or `/dashboard`. For raw background research on a person or company, use `/osint`. For a follow-up after the meeting, use `/follow-up`.

## /voss

Writes a negotiation playbook for one specific, difficult conversation, using Christopher Voss's method. The playbook has five parts: a profile of the other side (their likely negotiator type, what drives them, who needs whom more), an accusation audit (every negative thing they might think about you, worst first, each with a sentence that names it before they do), eight to twelve "how" and "what" questions, a tactical plan (opening, mirroring, labels, the path to "that's right", hidden factors, and an Ackerman price plan when money is involved), and scenario maps for when they agree, push back, go silent or get aggressive, plus your walk-away point. It is saved as `YYYY-MM-DD-<counterpart>.md` under the registry name `negotiations`, and if the person has a CRM file a note is added there.

**How to call it.** `/voss [counterpart] [situation]`. Or say it in your own words, for example: "help me prepare for a tough negotiation with Acme Corp", "run an accusation audit before I push back on their terms".

**Example.**

```text
You:        /voss Dana Whitfield, Northwind wants a 22% discount on the renewal
Assistant:  Playbook saved under negotiations. Opening: "You're probably thinking we priced this
            renewal before we knew your volumes." Ackerman plan: target $184,300, open at $119,795.
            Walk away below $171,650.
```

**What it needs.** Nothing beyond the core install. It reads your voice guide, people, pipeline and company records, and the contact's CRM file and any `/meeting-prep` brief when they exist.

**When not to use it.** For a full meeting brief with research on the counterpart, use `/meeting-prep`. For background on a person, use `/osint`. For working out the deal strategy itself, use `/deep-think`.

## /deep-think

Writes a short decision record for a hard choice, so you can check every line of it. It states the real decision (and what is not the question), lists the assumptions and marks each one TESTED with its source or UNTESTED, sets out two or more options with their upside, downside and cost, gives the evidence from your records with citations, makes a recommendation, and names the facts that would change it. It ends with a one-line course, a confidence level, the key risk, and what to do if the call turns out wrong. There are three depths: `quick` (two options, evidence cited inline), `standard` (the default) and `deep` (three or more options plus a pre-mortem). Standard and deep analyses of strategic decisions are saved as `YYYY-MM-DD-<topic>.md` under the registry name `thinking-notes`.

**How to call it.** `/deep-think [problem]`. Or say it in your own words, for example: "think through whether we sell direct or through a partner", "what are we missing on this pricing call", "break this decision down for me".

**Example.**

```text
You:        /deep-think should Acme Corp open a second warehouse this year or extend the lease, deep
Assistant:  Deep Think: second warehouse vs lease extension. 3 options, 7 assumptions (3 untested).
            Course: extend the lease for 18 months and sign a site option. Confidence: MEDIUM.
            Key risk: the Q3 volume forecast is untested.
```

**What it needs.** Nothing beyond the core install. It reads whatever records bear on the question. It can also pull named analysis methods from `scripts/elicit.py` when the standard sections are not enough.

**When not to use it.** For a simple question, just ask it. For views from other models, use `/council`. For advice from your curated knowledge brain, use `/odin`. To turn a settled decision into a build plan, use `/create-plan`.

## /council

Asks one or more other AI models for an independent opinion and shows their answers side by side with Claude's own view. It never merges them into one final answer: you decide. In independent mode (the default) the other models see only the question and the facts, never Claude's reasoning. In critique mode they stress-test a draft you give them. By default the only outside voice is Kimi on the `k3.1-pro` model. You add Gemini, Grok or ChatGPT with `--gemini`, `--grok`, `--chatgpt`, or all of them with `--all`. `--gemini-only`, `--grok-only`, `--kimi-only` and `--chatgpt-only` run exactly one voice, and `--no-gemini`, `--no-grok`, `--no-kimi` and `--no-chatgpt` drop one. A transcript is saved under the registry name `council-transcripts` unless you add `--no-log`. At the end it asks which answer landed best and records your one-word reply.

**How to call it.** `/council [question]`, or `/council --critique [draft]`. Or say it in your own words, for example: "second opinion on this go-to-market call", "stress-test this draft with ChatGPT", "consult the council on this pricing decision".

**Example.**

```text
You:        /council --chatgpt should Acme Corp drop its free tier before the Series A?
Assistant:  Kimi's view, ChatGPT's view, Claude's view, where we agree and disagree, 2 open questions.
            Transcript saved. Which answer landed best: claude, gemini, grok, kimi, chatgpt, mix,
            reject, or skip?
```

**What it needs.** CLIProxyAPI running, with `CLIPROXY_API_KEY` set in `.env`; every outside voice goes through it, and without the key each voice stops. See `CLIPROXY.md`. If the proxy does not offer `k3.1-pro`, the Kimi voice is skipped, and on the default roster the run stops rather than present Claude alone as a council. These models are outside cloud services, so do not send private data.

**When not to use it.** For Claude reasoning a decision through on its own, use `/deep-think`. For advice from your own knowledge brain, use `/odin`.

## /odin

Odin is a private advisor with his own knowledge brain, kept under the registry name `odin-brain`. You feed him books, articles, videos and documents. He pulls out principles and positions, and when you ask for advice he answers in the first person, cites the brain first, and always adds a Challenge section that names the weak spots. He has nine modes: `learn` (absorb a source, such as a YouTube link, a web page, a PDF or a file), `consult` (advice on a situation; the default), `recall` (what the brain holds on a topic), `reflect` (review the brain for gaps, stale positions and episodes ready to become principles), `teach` (you state a rule directly), `log` (record a dated event), `collect` (scan your business threads and CRM for events you forgot to log, then ask you about each one), `compile` (check the brain and your notes for broken links, contradictions and gaps), and `skill-proposal` (draft a checklist step for another skill from a principle, as a diff you apply yourself). Odin writes only inside his brain, and in `collect` mode nothing is written until you approve each episode.

**How to call it.** `/odin [mode] [source or question]`. With no arguments it shows the mode menu. Or say it in your own words, for example: "ask Odin what he thinks about our pricing", "Odin log: Acme Corp signed the pilot today", "compile the brain".

**Example.**

```text
You:        /odin consult should we give Northwind Logistics exclusivity in their region?
Assistant:  Odin's Take: I would not grant exclusivity without a volume floor... Challenge: the weak
            spot is that I hold only one episode about regional partners. Sources: 3 principles,
            1 episode, 1 workspace file.
```

**What it needs.** Nothing beyond the core install to start; the brain grows as you use it. Web pages that need a crawler use `FIRECRAWL_API_KEY` and ask the VPN question first. The brain search uses the local embedding server described for `/recall` below; if it is down, Odin falls back to text search and says so.

**When not to use it.** For structured reasoning without the brain, use `/deep-think`. To capture a general note, use `/zk`. To research a company or person, use `/osint`. For views from other models, use `/council`.

## /recall

Finds what your workspace already knows, by meaning rather than exact words, and answers only from the files it finds, with each claim cited. It searches your Odin brain, threads, CRM, context, plans, outputs, knowledge notes and saved memories. When nothing clears its match threshold it says plainly that the answer is not in memory and stops, instead of guessing. When the match is weak it lists the nearest files as possible leads only and does not answer from them. A Russian question can find English notes and the other way round. After the main answer it adds a separate, dated list of past conversations on the topic, marked as history rather than current fact. It changes no record content: it refreshes its local search index and updates the access count on saved memories it cites.

**How to call it.** `/recall <what to recall> [--collection content|code|all] [--layer NAME] [--personal]`. `--collection code` searches skills and rules, `all` searches everything, `--layer` limits it to one layer such as `crm` or `thread`, and `--personal` limits it to personal history. Or say it in your own words, for example: "what do we know about Acme Corp", "where did we decide on the pricing model", "have we touched a reseller deal before".

**Example.**

```text
You:        /recall what did we agree with Northwind about delivery windows?
Assistant:  On 2026-09-14 you agreed a 48-hour window for the northern routes (thread file).
            The CRM note from 2026-10-02 says 72 hours; the two disagree.
            Sources: 2 files, with paths.
```

**What it needs.** A local embedding server running the `bge-m3` model, either Ollama or Lemonade Server, declared in `config/inference-hosts.yaml` (template `config/inference-hosts.example.yaml`; Lemonade also needs `LEMONADE_API_KEY`). With no host file the code looks for Ollama on `localhost:11434`. If the server is down, it searches the existing index and says it was not refreshed. If you run the Hindsight memory server, it asks that first; see `HINDSIGHT.md`.

**When not to use it.** For counting or totals across many files, use `/census`. For advice from the Odin brain, use `/odin`. For outside information on a company or person, use `/osint`. To save a new note, use `/zk`.

## /investor-pitch

Writes the content of an investor pitch deck from your own company records: the story arc (problem, why the moment is now, why you, proof, path to scale, the ask), fifteen slides each with a title, a one-sentence key message, three to five bullets and a speaker note, and a data appendix. You choose the investor type: `strategic`, `venture`, `sovereign_fund`, `angel` or `corporate_vc`. Every fact comes from your records. When a figure is missing it stays as a bracketed placeholder, and any missing record is named at the top of the output with how to fill it. The result is text in the chat for you to adapt; it does not render slides.

**How to call it.** `/investor-pitch [investor_type]`. Or say it in your own words, for example: "help me build the pitch deck for our Series A", "we need a fundraising deck for angel investors".

**Example.**

```text
You:        /investor-pitch venture
Assistant:  Missing record: current-data (run /setup-wizard to fill it). Narrative arc, then 15 slides.
            Slide 7, Traction: key message "[Lead customer] live since [date]"; speaker note: lead
            with the renewal, not the logo.
```

**What it needs.** Your company records: business information, strategy, current data, pipeline and voice guide, plus a growth playbook and market notes if you keep them. Fill them with `/setup-wizard`. Nothing else beyond the core install.

**When not to use it.** For an update to investors you already have, use `/investor-update`. To turn the content into a PowerPoint file, use `/pptx-generator`. For a conference or event deck, use `/keynote-deck`.

## /investor-update

Drafts a progress update for people who have already invested, for a quarter or after a milestone. The draft has a subject line, an opening personal note, a key metrics table (this period, last period, target), what you said you would do against what you did, two or three wins with evidence, one or two honest challenges, three commitments for the next 90 days, any specific ask, and a short close. It runs one to two pages in your voice. Metrics come only from your records: if your current-data record is missing, the table keeps its rows with empty cells and the draft says so at the top. The skill never sends the update; you send it yourself after review.

**How to call it.** `/investor-update [period]`, for example `/investor-update "Q3 2026"`. Or say it in your own words, for example: "time for the board update", "write a shareholder update covering wins and challenges".

**Example.**

```text
You:        /investor-update "Q3 2026"
Assistant:  Subject: Acme Corp Update - Q3 2026 | Second region live. Metrics table filled from
            current-data; one challenge named (hiring lag). Three 90-day commitments. Draft only,
            not sent.
```

**What it needs.** Your company records (business information, strategy, current data, pipeline, people, voice guide), filled with `/setup-wizard`. If a cache of your sent mail exists, it uses a few of those emails as voice examples. Nothing else beyond the core install.

**When not to use it.** For a first pitch to new investors, use `/investor-pitch`. For preparing a single meeting with an investor, use `/meeting-prep`.
