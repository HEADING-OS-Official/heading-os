# Skills: System, quality and maintenance

These skills look after HEADING OS itself rather than your business. They set up a fresh install, back it up and keep it in step with GitHub, give a second, harder look at a claim, a draft, a plan or a piece of code, watch your mail and Telegram in the background, and keep the memory store in order. Most of them start only when you type the command, because each one either writes to your records, runs something live, or costs real time. For the full list of skills and how a skill is called, see `SKILLS.md`.

## /setup-wizard

Personalises a fresh HEADING OS clone by asking you 18 questions, one at a time, and writing your answers into the right files. For short questions it saves what you type. For the bigger ones (your voice, who you are, your business) it drafts a 200 to 400 word document from your short answer and shows it to you before saving. API keys are written into `.env`, and the wizard shows them back to you masked. When you run it again, it opens a dashboard with your completion percentage and lets you edit any answer, walk through all of them again, or re-apply the saved answers to the files. Your answers are kept in `.setup/answers.json`.

**How to call it.** `/setup-wizard`, with no arguments. Only the typed command starts it; plain words do not. Inside the wizard you can type `skip`, `example` or `help` at any question. On a draft you answer `y` (save), `t` (tell it what to tweak), `r` (redraft from a new short answer) or `s` (skip). On the dashboard you type a row number, `all`, `apply` or `done`.

**Example.**

```text
You:        /setup-wizard
Assistant:  Welcome. I'll ask you ~18 questions to set up your workspace.
            Q3 of 18 - Your voice
            How would you describe the way you write?
            Your answer: _
You:        Direct, short sentences, no jargon, a bit dry
Assistant:  Drafting your voice guide... [draft shown]
            Does this sound like you?  [y] [t] [r] [s]
```

**What it needs.** Nothing beyond the core install (`INSTALL.md`). Every write goes through `scripts/apply-wizard-answers.py`, which is the only thing allowed to touch `.setup/answers.json`. For a key, the wizard can optionally check that it works with one call through `scripts/wizard-verify-key.py`; the default answer is no, and that check is the wizard's only network call. It refuses to run, and stops at once, if it detects the original master workspace instead of a public clone.

**When not to use it.** To change your settings or records after setup, edit them directly or run the wizard again. To load your context at the start of a session, use `/prime`.

## /calibrate

Looks back over the current session for things worth learning from: your corrections, preferences you stated, patterns that repeated, errors and friction, things that went well, and slips in voice. It turns them into a numbered list of proposed changes, grouped as Memory, Settings, Skills and Rules. A memory change you approve is written to your memory store (registry name `auto-memory`). Every settings, skill or rule change goes into a review file under the registry name `calibrate-reviews` as a proposal and is never applied by the skill. Nothing is committed.

**How to call it.** `/calibrate` for the full pass, which parses the saved session log. `/calibrate light` scans only the conversation still in context and skips the repeated-pattern and error checks. Only the typed command starts it; plain words do not. You choose with `apply all`, `apply all except 3, 5`, `apply 1, 3, 5` or `cancel`.

**Example.**

```text
You:        /calibrate light
Assistant:  Light mode active. Scanning only in-context conversation.
            Memory
              1. Write dates as YYYY-MM-DD in every Northwind report
            Skills
              2. /email-draft: offer three subject lines, not two (goes to the review queue)
            Reply: apply all | apply 1, 2 | apply all except 2 | cancel
You:        apply 1
```

**What it needs.** Nothing beyond the core install. The full mode reads the session log through `scripts/calibrate.py`; if that script is missing, use light mode.

**When not to use it.** To tidy memory across many sessions, use `/dream`. To check memory for broken links and orphans, use `/memory-hygiene`. To save where you are before clearing the context, use `/handover`.

## /backup

Commits everything in the engine and in the data repository and pushes both to their private GitHub remotes, in one step. It runs `scripts/push-all.py`, which scans for secrets before pushing, refuses to push secret-like files such as `.env`, pushes the data repository first, and then checks that each repository is level with its remote. It reports what each repository pushed.

**How to call it.** `/backup`, with no arguments. Only the typed command starts it; plain words do not. Typing it is your permission for that one commit and push.

**Example.**

```text
You:        /backup
Assistant:  Data repository: committed 6 files, pushed, verified [0 0].
            Engine: nothing to commit, pushed, verified [0 0].
            Backup complete.
```

**What it needs.** `GH_TOKEN` in `.env`, and a private GitHub remote for each repository. It runs only from HELM, your main copy of the engine; in a yard the script refuses (see `YARDS.md`). If it ends with `Partial: N of M repo(s) not pushed`, the others were pushed and the skipped one is still committed locally. If it ends with `NOTHING PUSHED`, there is no new copy off the machine. Either way the skill names the repository and the reason. If the script cannot run at all, the skill stops and reports the error; it never falls back to a plain `git push`.

**When not to use it.** To pull updates first and then back up, use `/sync`.

## /sync

Brings your copy up to date and backs it up. The pull step runs `git pull --ff-only` on the engine and on the data repository, so a local history that has drifted apart shows up as a plain error instead of a silent merge. The backup step runs the same `scripts/push-all.py` as `/backup`. It never deletes files and never forces a push.

**How to call it.** `/sync` pulls and then backs up. `/sync pull` only pulls. `/sync backup` only backs up. Only the typed command starts it; plain words do not.

**Example.**

```text
You:        /sync pull
Assistant:  Engine: Already up to date.
            Data repository: Fast-forward, 3 files changed.
```

**What it needs.** `GH_TOKEN` in `.env` for the backup half. The data repository is found next to the engine, or at `HEADING_OS_DATA` when that is set in `.env`. If a pull is refused because the histories have diverged, the skill tells you to look at `git status` and sort it out yourself; it will not merge for you.

**When not to use it.** To back up without pulling, use `/backup`.

## /devil

Argues against a claim or decision on purpose. You get exactly N points, default 5, each tagged BLOCKER, HIGH, MEDIUM or LOW and each attacking from a different angle: correctness, scope, cost, timing, alternatives, side effects, people and politics, technical debt, security, upkeep. It offers no fixes and does not hedge. If it cannot find N honest points, it gives fewer and says so instead of padding. Nothing is saved.

**How to call it.** `/devil` attacks the latest real claim or decision in the conversation with 5 points. `/devil 3` sets the number (1 to 10). `/devil 4: <your claim>` attacks a claim you type after the colon. Only the typed command starts it; plain words do not. If it stopped short of N, reply `force N` to see the weaker cuts, marked `weak:`.

**Example.**

```text
You:        /devil 3: We should move Northwind Logistics to annual billing next month
Assistant:  Adversarial pass on: "Move Northwind Logistics to annual billing next month"
            ## Point 1: [HIGH] Renewal timing
            **Wrong month.** Their budget year closes in six weeks, so the ask lands
            when nothing new can be approved.
            ...
            Reply with point numbers to address, or any other message to continue normally.
```

**What it needs.** Nothing beyond the core install.

**When not to use it.** For the views of other AI models, use `/council`. For a full review of a plan, file or piece of work with proposed fixes, use `/scrutinize`. To see different versions of a draft instead of attacks on it, use `/burst`.

## /burst

Rewrites the latest piece of content in several different ways so you can compare directions. You get N variants, default 3, each changing one thing such as the opener, tone, structure, lens or length, while the core message stays the same. The last variant always swings the other way and inverts something defining about the original. It ends with its own pick and one line of reasoning. Nothing is saved. Where it lacks a figure, it writes `[NEEDS FIGURE: ...]` instead of inventing one.

**How to call it.** `/burst` varies the latest content the assistant produced, 3 ways. `/burst 4` sets the number (2 to 5). `/burst 3: <text>` varies text you paste after the colon. Only the typed command starts it; plain words do not. Reply `use variant 2` to take one as the new draft; the next `/burst` then starts from it. If the content would cost money per variant, such as image generation, or would take a long time, it quotes the cost or time first and waits for your "go".

**Example.**

```text
You:        /burst 3: Acme Corp cut its shipping errors by a third in one quarter.
Assistant:  Varying: "a one-line customer result for a post"
            ## Variant 1 - Number first
            One third fewer shipping errors. One quarter. That was Acme Corp.
            ## Variant 2 - Customer voice
            ...
            ## Variant 3 - Swing: the problem, not the win
            ...
            My pick: Variant 1 - the number leads, so a scrolling reader stops.
```

**What it needs.** Nothing beyond the core install.

**When not to use it.** To attack a position instead of rewording it, use `/devil`. For a fresh single post, use `/linkedin post`. To fix the structure of a long document, use `/editorial-review`.

## /editorial-review

A structural editor for a long draft that is complete but badly ordered. It picks the shape the document should follow (for example a proposal arc or a decision note), lists each section with a rough word count, and checks for a buried lead, claims without evidence, evidence without a claim, repeated paragraphs, missing sections and scope creep. Each finding is tagged with one operation (CUT, MERGE, MOVE, CONDENSE, SPLIT, ADD, PROMOTE, DEMOTE, PRESERVE or QUESTION) and an estimate of the words it saves. It changes nothing until you approve, then applies only the approved moves. It never rewrites sentences and never argues with your ideas.

**How to call it.** `/editorial-review file:<path>`, or paste the text. Or say it in your own words, for example: "do an editorial pass on this proposal", "review the structure of this brief", "restructure this draft".

**Example.**

```text
You:        /editorial-review file:<draft folder>/acme-renewal-brief.md
Assistant:  This deliverable should follow the decision-note arc; its primary rule
            is the recommendation comes first.
            1. MOVE  "Our recommendation" from section 5 to the top (0 words)
            2. MERGE sections 2 and 3, same evidence twice (-180 words)
            3. PRESERVE the summary table, it carries the comparison
            Estimated reduction: 240 words (14%). Approve which?
```

**What it needs.** Nothing beyond the core install. After applying changes it runs `scripts/sanitize-text.py --scan` and `scripts/humanization-check.py` on the file and reports the word count, hidden characters and the humanisation result.

**When not to use it.** For a short note in the knowledge base, use `/zk`. For a harder review that tests the claims themselves, use `/scrutinize` or `/devil`. Sentence-level polish and typo fixes are not this skill's job.

## /scrutinize

A strict review gate. It checks a target against a fixed list of checks, then lists findings, each with a severity, a confidence score from 0 to 100, the evidence and a concrete fix. Before showing them, it tries to knock its own findings down: other models argue each serious finding, and the ones that do not hold up are dropped. Findings below confidence 75 are hidden unless you ask. Nothing is changed until you approve. For anything other than a plan, the report is saved as `YYYY-MM-DD-<slug>.md` under the registry name `scrutiny-sheets`.

**How to call it.** `/scrutinize [target] [flags]`. The target is `plan`, `execution` (your uncommitted changes and this session's commits), `file:<path>`, `dir:<path>`, `workspace`, or `trajectory:<run_id>`; leave it out and the skill works out the target and asks you to confirm. Flags: `--relentless` (apply every fix and loop, up to 10 rounds; not for plans), `--no-refute` (skip the knock-down step), `--include-low-confidence`, `--include-ambiguous`, `--no-code-review`. Only the typed command starts it; plain words do not. You answer with `approve all`, `approve <ids>`, `reject <ids>`, `revise <id>: <note>`, `skip <ids>` or `flag-as-fp <ids>`; anything else means it waits.

**Example.**

```text
You:        /scrutinize file:<project folder>/northwind-export.py
Assistant:  Grade: NEEDS-REWORK. Refutation: 4 findings argued, 1 dropped.
            H1 [HIGH, 88] Line 47 writes the export without a temporary file;
               a crash leaves half a file. Fix: write to .tmp, then os.replace.
            M1 [MEDIUM, 79] ...
You:        approve H1, skip M1
```

**What it needs.** The core install for a basic pass. The other models that argue findings (Kimi and ChatGPT) and the extra code reviewer (Kimi) are reached through CLIProxyAPI (`CLIPROXY.md`). Without the proxy the pass still runs: the code reviewer is skipped and noted, and the report says which model judged what and why.

**When not to use it.** To reason through a decision, use `/deep-think`. For a quick contrarian take, use `/devil`. For a full audit report of the whole setup, use `/workspace-deep-audit`. It is not meant for checking a post or an email.

## /goldfish

A cold read. It hands one document or change to a reviewer that starts with none of your conversation, none of the instruction files and no memory, and that reads only the document and a written standard for the kind of review you ask for. A reviewer who watched the work tends to repeat the author's reasoning; this one cannot, so it finds the gap between what you meant and what the page says. You then answer each finding as FIX (change it), CONTEST (the reviewer missed something it could have read) or ESCALATE (only you can decide). The report is saved as `YYYY-MM-DD-goldfish-<mode>-<slug>.md` under the registry name `scrutiny-sheets`.

**How to call it.** `/goldfish <mode> <path or this>`. The modes: `plan` (a plan before you approve it), `reader` (would a stranger act on it correctly: a brief, a handoff, a report, a proposal or a tender answer), `ready` (could someone carry it out without asking a single question), `proposal` (an initiative or investment, against the Heilmeier questions), `drift` (does a document still describe what the code does), `code` (a change, read without knowing what it was meant to do). Add `--as <audience>` to have it read as a given reader, and `--context <path>` to give it a file it may consult. Or say it in your own words: "goldfish review this", "cold read this", "would a stranger understand this".

**Example.**

```text
You:        /goldfish reader <draft folder>/northwind-tender-answer.md --as "procurement officer"
Assistant:  ESCALATE 1: section 4 promises delivery "in Q3"; only you can say
            whether that is a commitment or an estimate.
            FIX 3, CONTEST 1, ESCALATE 1. Verdict: needs changes before it goes.
```

**What it needs.** Nothing beyond the core install. The reviewer is the `goldfish` agent in `.claude/agents/`, so a session started before that file existed has to be restarted first; the skill says so and stops rather than fall back to an ordinary agent.

**When not to use it.** For the full review gate, where other models argue each finding, use `/scrutinize`. For an opinion from other model families, use `/council`. To reorder a draft, use `/editorial-review`.

## /canopus

The engineering method HEADING OS uses for any change worth a plan: seven steps, two of which are yours alone. The steps are: define the value, brainstorm a scope document, write the plan, run `/scrutinize` on the plan and apply every finding (then you commit the plan, which is the approval), build it with a separate implementer, run `/scrutinize --relentless` on the result, and ship. The bare command runs nothing; it shows the seven steps and names the step your work is at. The subcommands keep the record and check it.

**How to call it.** `/canopus` shows the steps. `/canopus note` writes the record for a finished slice under the registry name `slice-notes` (`scripts/canopus.py note <slug>` with `--value`, `--approval-sha`, `--contract`, `--plan-digest`, `--scrutinize-plan`, `--scrutinize-built` and `--undo`, all required). `/canopus check` checks every record: the contract did not change after approval, and the code descends from the approval commit. `/canopus probe` runs a test contract against fake code to see whether it can fail at all; `probe --after-build` asks the same of tests over code that already shipped. Only the typed command starts it; plain words do not.

**Example.**

```text
You:        /canopus
Assistant:  1. Define the Value
            2. superpowers:brainstorming  -> scope document
            3. superpowers:writing-plans  -> plan
            4. /scrutinize the plan, apply every finding -> your commit
            5. superpowers:subagent-driven-development -> implementation
            6. /scrutinize --relentless, apply every finding
            7. Production
            You are at step 3: the plan for the Acme export is drafted, not yet reviewed.
```

**What it needs.** The `superpowers` Claude Code plugin for steps 2, 3 and 5 (install it as described in `INSTALL.md`), and git. Steps 4 and 7 are always yours; the skill never takes them for you.

**When not to use it.** For a typo or a config-only edit, skip it. To review finished work, use `/scrutinize`. To plan a smaller change, use `/create-plan`, then `/implement`.

## /sentinel

Starts, stops and checks Sentinel, a background program that watches your Exchange mail and your Telegram for urgent messages. It scores each message from 1 to 10, uses Claude to suggest an action (reply, forward, schedule, decide, FYI or escalate), and sends you an alert on Telegram when a score reaches the threshold, plus morning and evening digests. It also checks meeting invites against your calendar policy. Its log is kept under `runtime:sentinel/logs`.

**How to call it.** `/sentinel` checks whether it runs and starts it in the background if it does not, then shows the status. `/sentinel start`, `/sentinel stop`, `/sentinel status`, `/sentinel logs` and `/sentinel config` do what they say. Only the typed command starts it; plain words do not. For a safe trial, `scripts/sentinel.py --test` runs one cycle in which nothing is sent, no invite is answered and nothing is saved.

**Example.**

```text
You:        /sentinel status
Assistant:  Sentinel is running (PID 48213), last check 09:45.
            Today: 37 emails, 12 Telegram messages, 2 urgent alerts,
            1 invite escalated to you. No errors in the log.
```

**What it needs.** `ANTHROPIC_API_KEY`, `EXCHANGE_EMAIL`, `EXCHANGE_PASSWORD` and `EXCHANGE_SERVER` in `.env`. A signed-in Telegram session for reading (run the `setup` command of `/telegram` once). `TELEGRAM_NOTIFY_BOT_TOKEN` and `SENTINEL_TELEGRAM_TARGET` (or `ODIN_CADENCE_TELEGRAM_TARGET`) in `.env` for alerts; without them nothing is delivered. Settings live in `sentinel_config.yaml` under the registry name `data-config`, copied from `scripts/sentinel_config.example.yaml`. Be aware that the example file sets `calendar.auto_accept` and `calendar.auto_decline` to `true`, so Sentinel answers invites by itself: it accepts the ones that fit your policy and declines internal ones that do not, proposing another time. Invites from VIP and external senders are passed to you. Set both keys to `false` if you want to answer every invite yourself. It runs only from HELM, never from a yard (see `YARDS.md`).

**When not to use it.** To read or send Telegram messages yourself, use `/telegram`. For the morning overview, use `/dashboard`.

## /brain-audit

Adds a short check under any answer built from several source files. It reports three things in a fixed `## Brain audit` footer: how recently each source was changed (anything over 90 days is marked `[STALE]`), where a named person or company appears across your records (email cache, OSINT dossiers, CRM, calendar cache), and where the sources contradict each other on numbers, status or relationships. It writes nothing and never stops the skill that called it; if a check fails, the footer says so.

**How to call it.** `/brain-audit --sources <path1>,<path2> [--entity <name>] [--modalities <list>]`. Only the typed command or another skill starts it; plain words do not. `/meeting-prep` and `/odin consult` call it themselves at the end of their answers.

**Example.**

```text
You:        /brain-audit --sources <crm path>/dana-whitfield.md,<threads path>/acme-pilot.md --entity "Dana Whitfield"
Assistant:  ## Brain audit
            **Newest source per claim:**
            - .../dana-whitfield.md: modified 2026-09-30 (12 days ago)
            - .../acme-pilot.md: modified 2026-06-02 (132 days ago) [STALE]
            **Modality coverage for Dana Whitfield:**
            - crm-log: last_touch 2026-09-30   - telegram: not indexed
            **Disagreements among sources:**
            - Pilot start: one file says 3 March, the other says 17 March
```

**What it needs.** Nothing beyond the core install. The coverage check only finds what your other skills have already saved; Telegram is always reported as not indexed, because nothing keeps a searchable copy of it.

**When not to use it.** To ask what your records say about something, use `/recall`. To count across many files, use `/census`.

## /memory-hygiene

Checks the memory store for defects that can be proved mechanically and fixes none of them. The defects it counts are: `superseded_by` links that point nowhere or in a circle, orphan memory files that neither `MEMORY.md` nor Hindsight knows about, and a `MEMORY.md` over its budget of 200 lines or 25,000 UTF-16 units. It also lists softer signals that do not count as failures: files older than 45 days, near-duplicates, dead `[[wikilinks]]`, and stale items in the Odin brain. It writes a dated report under the registry name `memory-hygiene-reports` and ends with "clean" or the number of defects.

**How to call it.** `/memory-hygiene`. Only the typed command starts it; plain words do not. From a terminal, `scripts/memory-hygiene.py` takes `--json`, `--quiet` and `--no-report`.

**Example.**

```text
You:        /memory-hygiene
Assistant:  2 objective defects:
            - dangling superseded_by in northwind-billing-terms.md
            - 1 orphan: acme-contact-preferences.md
            Advisory: 4 files older than 45 days, 1 near-duplicate pair.
            Recommend /dream to resolve the two defects.
```

**What it needs.** The core install. The orphan check asks Hindsight (`HINDSIGHT.md`); when Hindsight does not answer, the report says `not evaluated this run: orphans`, which means unknown, not clean. The Odin half is reported as unavailable if you have no Odin brain.

**When not to use it.** To actually merge, rewrite or retire memories, use `/dream`. To recall a fact, use `/recall`.

## /dream

A careful tidy-up of your memory store (registry name `auto-memory`). It lists the memory files, finds orphans, reads recent git activity and your pipeline and current-data records for facts that contradict or extend what memory says, checks technical claims against live library documentation through Context7, and then rewrites out-of-date memories in place, merging duplicates into one file. It never writes into `MEMORY.md` and never deletes a memory on its own initiative. It ends with a consolidation report and appends one line to a trace file under the registry name `dream-reports`.

**How to call it.** `/dream`, or `/dream <focus area>` to look harder at one topic. Only the typed command starts it; plain words do not.

**Example.**

```text
You:        /dream Northwind contract
Assistant:  ## Memory Dream - Consolidation Report
            **Consolidated**
            - northwind-billing-terms.md - updated: billing moved to quarterly on 2026-09-14
            **Context7 Flagged**
            - exchangelib - unvalidated, marked for manual review
            **Security Gate** - Status: PASS
            **Index** - Lines: 61 of 200. Unchanged by this pass.
```

**What it needs.** The core install. Context7 lookups go through `scripts/context7.py`; `CONTEXT7_API_KEY` in `.env` is optional. The orphan step uses `scripts/memory-hygiene.py`, so Hindsight (`HINDSIGHT.md`) makes it complete. Before any write it passes a security gate that keeps secrets and credentials out of memory and keeps every write inside the memory folder. Merging two near-duplicate memories needs your yes for each pair, and a memory is only removed when you ask, through `scripts/retire-memory.py`.

**When not to use it.** To only find defects without changing anything, use `/memory-hygiene`. To learn from the current session, use `/calibrate`. For notes in the knowledge base, use `/zk`. To save a session snapshot, use `/handover`.

## /workspace-deep-audit

The heaviest skill in HEADING OS: a full audit of your installation in an 8-section report. It takes stock of skills, rules, hooks, scripts, dependencies, records and security with up to 7 read-only agents, judges the architecture, checks every dependency pin against current versions, scores the setup against a 34-point best-practice list, compares it with other platforms, and ends with at most 25 prioritised recommendations, each with effort, impact and acceptance criteria. It changes no file in the workspace. It writes the report as Markdown plus HTML under the registry name `workspace-deep-audit` and adds an entry to an audit thread under `threads-business`.

**How to call it.** `/workspace-deep-audit` for the full audit, which asks you to confirm first. `--mode=quick` skips the best-practice and competitor sections. `--mode=focus --focus=<area>` looks at one area: `skills`, `rules`, `deps`, `security`, `architecture` or `observability`. `--vs=<previous audit file>` adds before and after tables against an earlier run. Only the typed command starts it; plain words such as "run a full audit" do not.

**Example.**

```text
You:        /workspace-deep-audit --mode=focus --focus=security
Assistant:  Running workspace deep audit. Mode: focus. Focus: security.
            Delta vs: no baseline.
            ...
            Audit complete. Output: <workspace-deep-audit>/2026-10-27_audit_workspace-deep-overview.md
            (2,140 words). HTML beside it. Top P0 items: none.
```

**What it needs.** The core install. Dependency checks use Context7; the `context7` plugin ships switched off in `.claude/settings.json`, and when Context7 cannot be reached the report marks those pins as unvalidated and carries on. The competitor section needs a baseline file that a public clone does not have, so it is skipped and the report says so. Expect a full run to take 45 to 90 minutes and use a large amount of tokens; `--mode=quick` is about 20 minutes.

**When not to use it.** To review and fix one file, plan or change, use `/scrutinize`. For overdue manual tasks, use `/radar`. For the morning overview, use `/dashboard`. For the end-of-week review, use `/weekly-review`.
