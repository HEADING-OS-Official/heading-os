# Skills

A skill is a ready-made procedure HEADING OS follows for one kind of job: preparing a meeting, drafting a reply, researching a company, writing a weekly review. HEADING OS Official ships 70. This page explains how to call them and lists every one, grouped by what it is for. Each group has its own page with the full description of each skill: what it does, how to call it, an example, what it needs, and when to use another skill instead.

## How to call a skill

There are two ways.

**Type the command.** Every skill has a slash command, such as `/meeting-prep`, and many take arguments after the command, written in plain words in the same line, the way you would brief a person. Two examples:

```text
/meeting-prep Dana Whitfield, CFO of Northwind Logistics, contract renewal
/devil 3 we should open a second office in Singapore this year
```

**Ask in your own words.** "Prepare me for my call with Dana at Northwind" reaches `/meeting-prep` without the command. No syntax to learn. HEADING OS matches your message against the phrases each skill lists, announces the skill it picked ("Using /meeting-prep for this"), and runs it. When two or three skills fit equally well, it shows a short numbered menu with one line about each skill and waits for you to choose, rather than guessing which one you meant. The matching table is `.claude/rules/skill-router.md` in the engine; it is generated from each skill's own file by `scripts/generate-skill-router.py`, so do not edit it by hand.

**Some skills start only from the typed command.** Plain words never reach them, because each one commits, pushes, clears the session, or runs a long and costly job that you should decide to start yourself, on purpose, every single time. That is deliberate. They are marked "typed only" in the tables below. At the time of writing they are `/backup`, `/brain-audit`, `/burst`, `/calibrate`, `/canopus`, `/clear-handover`, `/devil`, `/dream`, `/handover`, `/osint-advanced`, `/prime`, `/scrutinize`, `/second-wind`, `/sentinel`, `/setup-wizard`, `/sync` and `/workspace-deep-audit`.

## What skills need

- **Your records.** Most skills read your own documents first: who you are, your business, your strategy, your contacts, your voice. `/setup-wizard` writes the first versions of these documents from your answers, and you can edit any of them later in a text editor, since each is a plain file in your data repository. A skill that finds a record missing says which one and continues without inventing anything.
- **Keys.** A skill that calls an outside service reads its key from `.env` in the engine, for example `FIRECRAWL_API_KEY` or `REPLICATE_API_TOKEN`. `.env.example` lists every key with a comment. Leave a key empty if you do not use that service; the skill that needs it says so when you call it.
- **Optional extras.** Heavy integrations are installed separately: `uv sync --extra <name>` in the engine, or `uv sync --all-extras` for all of them. A script that needs a missing extra prints the exact command.
- **Optional tools.** Some skills work better, or only, with one of the optional tools: Hindsight (`HINDSIGHT.md`), Lific (`LIFIC.md`), Herdr (`HERDR.md`), CLIProxyAPI (`CLIPROXY.md`).

## Where the results go

Every skill saves its result in your data repository, at the place the registry names for that kind of output, and tells you the full path. It never writes into the engine. Skills that write to another person produce a draft and stop: nothing is sent until you approve it (see "Drafts, approval and sending" in `WORKING-WITH-HEADING-OS.md`).

## Intelligence and research

Full descriptions: [SKILLS-INTEL.md](SKILLS-INTEL.md)

| Skill | What it does |
|---|---|
| `/ceo-intel` | A confidential world and geopolitical brief, framed for your company. |
| `/competitor-intel` | A named competitor compared with your company and product. |
| `/market-brief` | A brief on a region, country, sector or technology trend, from your company's position. |
| `/osint` | The base investigation skill: a dossier on a company, person, market or technology, with sources and confidence. |
| `/osint-advanced` | Specialised databases `/osint` does not reach: sanctions lists, registries, breach data, threat intelligence. Typed only. |
| `/deep-research-advance` | A deep, fact-checked report on a public question. |
| `/docparse` | Answers questions about a PDF, DOCX, PPTX or XLSX with citations to the exact place on the page. |
| `/notebooklm` | Controls Google NotebookLM from the terminal: notebooks, sources, audio overviews. |
| `/yt-pulse` | What YouTube creators are saying about a topic right now. |
| `/intel-briefing-newsletter` | A branded intelligence newsletter for readers outside the company. |

## Communication and contacts

Full descriptions: [SKILLS-COMMUNICATION.md](SKILLS-COMMUNICATION.md)

| Skill | What it does |
|---|---|
| `/email-draft` | A new email from you to a named person. |
| `/email-respond` | A reply to an email you received. |
| `/follow-up` | A follow-up email after a meeting or event. |
| `/ceo-to-ceo` | A letter to another company's CEO, written as one peer to another. |
| `/telegram` | Reads and writes Telegram as your own account. Sends only on your order. |
| `/mattermost` | Reads and writes Mattermost as your own account. Sends only on your order. |
| `/crm` | Your personal CRM: one card per person, open commitments, an interaction log, and who is due a call. |
| `/google-contacts` | Searches and edits your Google Contacts. |

## Content, presentations and design

Full descriptions: [SKILLS-CONTENT.md](SKILLS-CONTENT.md)

| Skill | What it does |
|---|---|
| `/linkedin-post` | One LinkedIn post in your voice. |
| `/linkedin-series` | A plan for several connected LinkedIn posts. |
| `/linkedin-archive` | Files a piece you have just published on LinkedIn. |
| `/keynote-deck` | The slide structure and script of a talk. |
| `/image-prompt` | A prompt for an AI image tool, to go with text you just wrote. |
| `/flux-image` | Real image files from a prompt, through Replicate. |
| `/design` | Branded visuals: social graphics, infographics, mockups, logos. |
| `/marp` | Slides from Markdown, rendered to PDF and HTML. |
| `/pptx-generator` | PowerPoint decks and LinkedIn carousels in your brand. |
| `/pencil-export` | Exports a Pencil `.pen` deck to PNG, PDF, PowerPoint and HTML. |

## Strategy, meetings and decisions

Full descriptions: [SKILLS-STRATEGY.md](SKILLS-STRATEGY.md)

| Skill | What it does |
|---|---|
| `/meeting-prep` | A brief before a meeting with a named outside person or company. |
| `/voss` | A negotiation playbook for one difficult conversation. |
| `/deep-think` | A decision paper for a hard choice. |
| `/council` | Independent opinions from other AI models, beside Claude's own. |
| `/odin` | A private advisor with his own knowledge base, built from what you give him to learn. |
| `/recall` | What your workspace already knows about something, with the file each answer came from. |
| `/investor-pitch` | The content of an investor pitch deck, from your records. |
| `/investor-update` | A progress update for your current investors. |

## Daily operations: sessions, planning and records

Full descriptions: [SKILLS-DAILY.md](SKILLS-DAILY.md)

| Skill | What it does |
|---|---|
| `/prime` | Starts a working session with your context loaded. Typed only. |
| `/next` | The logical next step and the command to type. |
| `/dashboard` | The morning one-page dashboard. |
| `/radar` | Which important manual actions are overdue, and which health checks have degraded. |
| `/weekly-review` | The weekly review, compared with last week. |
| `/event-debrief` | A structured debrief from your notes after an event. |
| `/thread` | The running record of a matter that spans weeks. |
| `/zk` | Your knowledge base of small linked notes. |
| `/census` | Counting questions across many files, such as "how many threads have not moved in 30 days". |
| `/dossier` | Everything about one Lific issue in one place. |
| `/pin` | Keeps one item word for word through compaction and `/clear`. |
| `/handover` | Saves a handoff of the session and keeps going. Typed only. |
| `/clear-handover` | Saves a handoff, then starts clean. Typed only. |
| `/second-wind` | Saves a handoff and frees the context the safe way. Typed only. |
| `/create-plan` | An implementation plan for a non-trivial change, before any change. |
| `/implement` | Carries out a plan from `/create-plan`, step by step. |
| `/context7` | Current documentation for a software library. |
| `/playwright` | A scripted job in a hidden browser: a screenshot, a page's text, a form, a PDF. |
| `/setup-browser-cookies` | Copies your logged-in browser sessions so `/playwright` can open pages that need a login. |

## System, quality and maintenance

Full descriptions: [SKILLS-SYSTEM.md](SKILLS-SYSTEM.md)

| Skill | What it does |
|---|---|
| `/setup-wizard` | Personalises a fresh installation with 18 questions. Typed only. |
| `/calibrate` | Learns from the current session and proposes improvements. Typed only. |
| `/backup` | Commits and pushes both repositories to their private GitHub remotes. Typed only. |
| `/sync` | Pulls both repositories, then backs them up. Typed only. |
| `/devil` | Argues against a claim or decision on purpose. Typed only. |
| `/burst` | The same content written several ways, to compare. Typed only. |
| `/editorial-review` | A structural edit of a long draft: order, gaps, repetition. |
| `/scrutinize` | A strict review gate for a plan, document or change. Typed only. |
| `/goldfish` | A cold read of one document or change by a reviewer that never saw how it was made. |
| `/canopus` | The seven-step method for a change worth a plan. Typed only. |
| `/sentinel` | Starts, stops and checks Sentinel, which watches your mail and Telegram for urgent messages. Typed only. |
| `/brain-audit` | A short check under an answer built from several sources: how fresh, what is missing, where they disagree. Typed only. |
| `/memory-hygiene` | Finds provable defects in the memory store and fixes none of them. |
| `/dream` | A careful tidy-up of the memory store, with your approval. Typed only. |
| `/workspace-deep-audit` | A full audit of your installation in an eight-section report. Typed only. |

## Skills from plugins

Claude Code plugins add skills of their own, named `plugin:skill`, such as `superpowers:brainstorming`. They are not listed here. HEADING OS never picks a plugin skill from plain words, even when your message matches its description closely, so call one by its full name when you want it. `INSTALL.md`, "Plugins", shows how to install the plugins the engine enables.

## Adding your own skill

A skill is one folder under `.claude/skills/` in the engine, holding a `SKILL.md` file. The `skill-creator` plugin, enabled in `.claude/settings.json`, writes and tests one with you, step by step, from a description of the job in your own words. Then regenerate the matching table, so that plain-words requests can reach the new skill and the router knows which phrases belong to it:

```bash
.venv/bin/python scripts/generate-skill-router.py
```

A skill you add is engine code. Write it in a yard (`YARDS.md`) and keep your data out of it: examples in a skill are invented, and anything personal, such as a client's name, a price or a private address, belongs in your data repository.
