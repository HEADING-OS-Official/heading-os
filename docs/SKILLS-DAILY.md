# Skills: Daily operations: sessions, planning and records

These skills run the working day: starting a session, choosing the next step, seeing what is overdue, keeping the records of ongoing matters and notes, saving a session so the next one can pick it up, planning and building a change, and driving a browser. `SKILLS.md` has the full index of skills and explains how a skill is called, by its slash command or in your own words.

## /prime

Starts a working session. It reads your core records (who you are, your company, strategy, current figures, the top of your contacts list, the pipeline, the workspace overview and the document index), runs the health checks in one pass with `scripts/prime-health-parallel.py`, and gives you a session brief. The brief covers your pipeline, the handoff from your previous session in this checkout (if there is one), contact health, knowledge base health, memory health, open threads, stale records and stale mail or calendar syncs (anything older than 48 hours is flagged `SYNC STALE`), and two or three skills that fit the day. It ends by asking what you are working on. It saves nothing.

On a fresh public install it first runs `scripts/apply-wizard-answers.py --status`, and if setup is not finished the brief opens with the percentage done and a pointer to `/setup-wizard`.

**How to call it.** `/prime` (no arguments). Only the typed command starts it (or the single word "prime"); other plain words do not.

**Example.**

```text
You:        /prime
Assistant:  Previous session handoff, generated 2026-11-03 18:40. Next: finish the Northwind Logistics pricing note. Resume this work, or start fresh?
            Relationship radar: 2 RED, 3 YELLOW. Active threads (4): Acme Corp renewal, last 2026-11-02 [2 follow-ups open] ...
            Sync data: fresh (all <48h). Suggested today: /meeting-prep, /follow-up.
            What are we working on today?
```

**What it needs.** Your records in the data repository. Panels for optional parts (Hindsight, Lific, the mail and calendar sync) stay quiet or report a problem when those parts are not set up; see `HINDSIGHT.md` and `LIFIC.md`. The session hooks must be armed by `bash scripts/setup-platform.sh`, as `INSTALL.md` shows; the first panel tells you if they are not.

**When not to use it.** In the middle of a session, to ask what comes next, use `/next`. For the daily one-page briefing alone, use `/dashboard`.

## /next

Tells you the logical next step and the exact command to type, based on what just happened. It runs `scripts/next-signal.py --json`, which reads this checkout's last handoff, the newest files your skills produced, recent git commit subjects and your active business threads, and then looks up which skills usually follow the one you just used. You get two to four one-line recommendations, optional steps first. If nothing is clear, it says so and asks for an area instead of guessing. It is read-only: it names commands and never runs them. It saves nothing.

**How to call it.** `/next [area]`, where the area is optional, for example `deals` or `content`. Or say it in your own words, for example: "what next", "where were we", "what should I do now".

**Example.**

```text
You:        /next
Assistant:  You debriefed the Acme Corp expo yesterday -> run /follow-up (three warm leads have no follow-up yet).
            The plan for the weekly summary is written -> run /implement (the plan is marked Ready).
```

**What it needs.** Nothing beyond the core install. Personal threads are never read for this signal.

**When not to use it.** For a full context load at the start of a session, use `/prime`. For the morning briefing, use `/dashboard`. For the end-of-week review, use `/weekly-review`. For what is overdue, use `/radar`.

## /dashboard

Builds your morning dashboard: one page covering contact health and commitments, the pipeline, today's meetings, the latest inbox, strategy and how fresh your records are. It runs `scripts/generate-dashboard.py --pdf`. Then it checks the result for hidden characters and reports three to five bullets: urgent items, today's meeting count, a pipeline snapshot and any stale files. The HTML and PDF are saved in a dated folder under the registry name `dashboard`, as `morning-dashboard.html` and `morning-dashboard.pdf`. If a brand font or logo from your `brand-kit` records is missing, it says so rather than falling back silently. If you ask for a live "sea state" update, it also runs a short web search on your sector and adds two or three sentences.

**How to call it.** `/dashboard` (no arguments). Or say it in your own words, for example: "morning dashboard", "daily brief".

**Example.**

```text
You:        /dashboard
Assistant:  - 2 urgent items: Dana Whitfield is RED (last touch 41 days ago), one commitment to Acme Corp due today.
            - 3 meetings today.
            - Pipeline: 6 active deals, 1 won this month.
            - Stale: the pipeline record was last verified 33 days ago.
            Saved: morning-dashboard.html and .pdf under `dashboard`, in today's folder.
```

**What it needs.** Your contacts in the CRM, the pipeline, strategy and current-data records. The calendar and inbox panels read the Exchange sync caches, which need `EXCHANGE_EMAIL`, `EXCHANGE_PASSWORD` and `EXCHANGE_SERVER` in `.env` and a running sync. Without them those panels have nothing to show.

**When not to use it.** For a full session start, use `/prime`. For overdue manual actions, use `/radar`. For the end-of-week view, use `/weekly-review`.

## /radar

Shows which important manual actions are overdue and which background health checks have degraded. It runs `scripts/ops-radar.py`, which is read-only in this mode, and presents the result by severity. Each overdue item comes with the one command that clears it, for example `/backup`, `/weekly-review`, or `/odin collect` and `/odin reflect`. If nothing is due, it says "all clear". It never runs the action for you. It saves nothing, except an acknowledgement or the crunch setting when you ask for one.

You can silence one known item with `ack`, until its time limit passes or it gets worse. You can switch crunch mode on, which hides every nudge except the critical ones: a backup debt that risks data loss, and repeated self-repair failures.

**How to call it.** `/radar`, `/radar ack <key>`, or `/radar crunch on|off`. Or say it in your own words, for example: "what's overdue", "what am I forgetting to run", "crunch mode on". The script behind it takes `ack <key> [--ttl 24h|7d]`; an unknown key is refused. The script runs in HELM only; in a yard it stops with a message.

**Example.**

```text
You:        /radar
Assistant:  2 items due.
            - backup: last run 9 days ago -> run /backup
            - weekly_review: last review 12 days ago -> run /weekly-review
You:        /radar ack backup
Assistant:  Acked backup. It stays silent until the time limit passes or it gets worse.
```

**What it needs.** Nothing beyond the core install. Optional: set `OPS_RADAR_TELEGRAM_TARGET` (and the notifications bot token, `TELEGRAM_NOTIFY_BOT_TOKEN`) in `.env` if you want the same counts-only line sent to your own Telegram channel. That line carries labels and numbers only, never names or text.

**When not to use it.** For the morning brief, use `/dashboard` or `/prime`. For the single next step, use `/next`.

## /weekly-review

Writes your weekly review: what happened to last week's top three priorities and course corrections, the outside conditions ("sea state"), whether you are on heading, pipeline movement (advanced, stalled for more than 14 days, new, won or lost, and a stage-weighted value), the state of your team, content cadence against targets of three LinkedIn posts and one newsletter a week, this week's top three priorities, and which context records are more than 14 days old. Unclear deal status is stated as unclear. It keeps the review to two or three pages. The review is saved as `YYYY-MM-DD-weekly-review.md` under the registry name `weekly-reviews`, dated by the week you name.

Two optional steps follow. Each runs only when its part exists. If you keep an Odin brain, it may ask whether to run `/odin reflect`, and runs it only on your yes. If Hindsight is connected, it asks Hindsight for standing rules that came up that week and saves them beside the review as `YYYY-MM-DD-rule-proposals.md`, marked as a proposal; nothing becomes a rule until you approve it.

**How to call it.** `/weekly-review [week date]`. Or say it in your own words, for example: "weekly review", "friday review".

**Example.**

```text
You:        /weekly-review 2026-11-02
Assistant:  Continuity: 2 of 3 priorities done; the Northwind Logistics proposal is not addressed. Grade: Mixed.
            Heading: slight drift. Pipeline: 1 advance, 2 stalled over 14 days. Content: 2 of 3 posts, newsletter on track.
            Saved: 2026-11-02-weekly-review.md under `weekly-reviews`. 3 context files need refreshing; want me to draft updates?
```

**What it needs.** Your current-data, pipeline, strategy, people and operational-state records, plus the LinkedIn and newsletter archives if you keep them. The `weekly-reviews` location must exist; if it does not, the skill stops and says so. Hindsight is optional (`HINDSIGHT.md`).

**When not to use it.** For the daily briefing, use `/dashboard`. For overdue actions only, use `/radar`.

## /event-debrief

Turns your raw notes from a conference, summit or important visit into a structured debrief. It reads your pipeline, people and strategy records first, then produces ten sections: a summary, a leads table with interest level, owner and deadline, the meetings held, what competitors showed, market signals, what worked, what to improve, action items, and suggested updates to your pipeline and people records. Afterwards it offers to log each meeting in the CRM, add new commitments to the contact, and create cards for hot or warm contacts with `/crm add`. It then offers to capture the takeaways with `/odin log` or `/zk distill`. The skill sets no save location.

**How to call it.** `/event-debrief [event name]`, then paste your notes, as messy as they are. Or say it in your own words, for example: "event debrief", "debrief the Acme Corp partner summit".

**Example.**

```text
You:        /event-debrief Northwind Logistics Partner Summit
            (pastes notes: met Dana Whitfield, wants a demo in December; competitor booth next to ours ...)
Assistant:  Summary: strong presence, 7 leads, 2 hot. Top outcome: Dana Whitfield asked for a December demo.
            Leads table and 6 action items below. Shall I log the 4 meetings in the CRM and add a card for Dana Whitfield?
```

**What it needs.** Your pipeline, people and strategy records, and your contacts in the CRM for the logging step.

**When not to use it.** To write the follow-up emails to the people you met, use `/follow-up` for each one. To prepare for a meeting before it happens, use `/meeting-prep`.

## /thread

Keeps the running record of a situation that spans several sessions: a negotiation, a vendor dispute, a recurring project, or a personal matter. Each thread is a file with its events, decisions, open follow-ups and linked documents. Business threads are saved under the registry name `threads-business`, or under an account's own threads when you name the account; personal threads are kept separately and never appear in anything that leaves the workspace. It never opens or logs silently.

It drives `scripts/thread.py`, whose commands are: `open business|personal "<title>" [--account <slug>] [--new-account]`, `log <thread-id> "<event>"` with `--artifact`, `--decision`, `--follow-up` and `--done <index>`, `close` and `hold` with `--reason`, `reopen`, `list [--type ...] [--status active|on-hold|closed]`, `find "<query>"`, `show <thread-id>`, `quiet <thread-id>` with `--until YYYY-MM-DD`, `--indefinite` or `--clear`, and `archive-scan [--apply]`. A quiet thread is never raised by any skill until the date passes or you raise it yourself.

**How to call it.** `/thread <command> [args]`. Or say it in your own words, for example: "open a thread", "log to the Acme Corp thread", "what threads are active".

**Example.**

```text
You:        Log to the Acme Corp renewal thread: they accepted the 12-month term, legal review next.
Assistant:  This looks like part of the Acme Corp renewal thread. Log it, with one decision and one follow-up ("legal review")?
You:        yes
Assistant:  Logged to business/acme-corp-renewal. 2 follow-ups open.
```

**What it needs.** Nothing beyond the core install.

**When not to use it.** For a single email, use `/email-draft`. For an idea or insight, use `/zk`. For a log of contact with a person, use `/crm`. Code work and debugging are out of scope.

## /zk

Manages your knowledge base, a Zettelkasten of small notes, one idea each, linked to each other. It lives under the registry name `knowledge-base` and works with the Zettlr editor if you use it. Each note is a file named `{YYYYMMDDHHMMSS}-{slug}.md`, starts at status `seed` and grows to `growing` and `evergreen`. Every new or changed note is checked for hidden characters.

The subcommands are `add [type]`, `enrich [note]`, `find [query]`, `connect [note]`, `distill [source]`, `garden`, `stats` and `brief [topic]`. With no subcommand it runs `stats`. Note types for `add` are `fleeting` (the default), `signal`, `decision`, `meeting`, `research`, `strategy` and `technology`; `people` is redirected to `/crm add`. `connect` and `distill` show their proposals and wait for your approval before writing. `garden` and `stats` run `scripts/knowledge-health.py`; `find` and `brief` write nothing.

**How to call it.** `/zk [add|enrich|find|connect|distill|garden|stats|brief] [target]`. Or say it in your own words, for example: "add a note", "distill this", "garden the knowledge base".

**Example.**

```text
You:        /zk add signal Northwind Logistics is moving its tender to a two-stage format.
Assistant:  Created 20261104093012-northwind-two-stage-tender.md (signal, seed) under `knowledge-base`. Source: observation.
            Word count: 64. Hidden characters: none found.
```

**What it needs.** Nothing beyond the core install. `enrich` uses web search for some note types. If Hindsight is connected, its file sync mirrors the notes (`HINDSIGHT.md`).

**When not to use it.** To ask what you already know about something, use `/recall`. For learned principles and advice, use `/odin`. For an ongoing situation, use `/thread`. For a full lint of the brain, use `/odin compile`.

## /census

Answers a counting or set question that no single file can answer, such as "how many threads have not moved in 30 days" or "which pipeline rows have no CRM card". It writes a small program that walks every file in the chosen part of your records, prints the program, and runs it with `scripts/census.py` inside a sandbox with no network and read-only files. You get a count, a list of paths, or pairs of paths, always with the source files so you can check by hand. It does not wait for your approval after printing the program. Read it first.

The scopes are `threads`, `crm`, `context` (which includes the pipeline), `auto_memory` and `knowledge`; the default is the first four. If the chosen records are small enough to read directly, it refuses with exit code `4` and you should use `/recall` instead. Free text comes back only with `--free-text`, and is then marked untrusted.

**How to call it.** `/census "<question>" [--corpus <scope>]`, with `--corpus` repeatable. Or say it in your own words, for example: "how many contacts have no email", "which threads have not moved in 30 days".

**Example.**

```text
You:        /census "which pipeline rows have no CRM card" --corpus context --corpus crm
Assistant:  (prints the traversal program, then runs it)
            3 rows: Acme Corp, Northwind Logistics, Example Freight. Sources: the pipeline record and the CRM contacts folder.
```

**What it needs.** `bubblewrap` (the `bwrap` sandbox tool) on the machine. Without it the run is refused with exit code `5`.

**When not to use it.** For "what do we know about X" or "where did we decide Y", use `/recall`. To add a note, use `/zk`. For advice from the Odin brain, use `/odin`. For a person or company outside your records, use `/osint`.

## /dossier

Gathers everything about one issue in the Lific tracker: its current state, what waits for your approval, the saved prompts, handoffs and yard briefs that name the issue, its comments and journal in Lific, the commits in both repositories that mention it, and older files that only mention it in passing. It runs `scripts/lific-board.py dossier <ISSUE-ID>`, then opens the newest handoff and the newest prompt it lists. It prints paths, never full texts. It writes nothing. If Lific does not answer, it says so and builds the dossier from files and git alone.

**How to call it.** `/dossier <ISSUE-ID>`. The id is two to five capital letters, a dash and a number, for example `ACME-14`; anything else is refused. Or say it in your own words, for example: "show the dossier of ACME-14". Without an id it asks you for one.

**Example.**

```text
You:        /dossier ACME-14
Assistant:  ACME-14, "Weekly summary export", active. Now: yard add-weekly-summary has staged its work.
            Awaiting approval: the commit in that yard.
            Chronology: prompt saved 2026-11-01, brief sent 2026-11-01, handoff 2026-11-02 (paths below). 2 commits mention it.
```

**What it needs.** Lific, the local issue tracker, set up as `LIFIC.md` shows. Without it, only the file and git parts are available.

**When not to use it.** For the whole board, use `/prime`, which shows the Lific panel. For a memory search without an issue id, use `/recall`. To create or move issues, use Lific itself.

## /pin

Keeps one item word for word for the rest of the work, so that compaction and `/clear` cannot lose or reword it. `scripts/pin.py` appends the text exactly as you wrote it to this session's pins file. From there it goes into every handoff this session writes (`/handover`, `/clear-handover`, `/second-wind` and the handoff after a compaction) and into what the next session receives after `/clear`, compaction or resume. Bare `/pin` lists this session's pins. An item over 500 characters is refused (split it), and so is anything that looks like a password or key.

**How to call it.** `/pin <text>`. Or start your message with the Russian word for "pin" followed by a colon; `reference/skill-router/operations.md` in the engine shows the exact trigger.

**Example.**

```text
You:        /pin All prices to Acme Corp stay in euros, never dollars.
Assistant:  Pinned: "All prices to Acme Corp stay in euros, never dollars."
```

**What it needs.** Nothing beyond the core install.

**When not to use it.** For a fact that should outlive this piece of work, write a memory file instead. For a task to do later, create an issue in Lific (`LIFIC.md`). For something Odin should remember, use `/odin`.

## /handover

Saves a handoff of the current session without clearing or compacting it: the objective, current state, files touched, next steps, a line for each of your messages with its status, and a prompt to continue. It hands the text to one command, `scripts/checkpoint-paths.py --write`, which saves the file under the registry name `handoff-archive` and updates the pointers that `/prime`, `/next` and the next session read. The script adds your pins, the master instruction line and the list of saved prompts itself. Then it stops and waits.

The same command also carries a switch. `/handover unattended on` makes the session carry on by itself at a pause after a short countdown instead of waiting for you, and at the context threshold menu it runs `/second-wind` by itself. It is off by default in HELM and on by default in a yard. Only you turn it off. `/unattended on|off|status` is a shorter way to the same switch.

**How to call it.** `/handover [note]` or `/handover unattended on|off|status`. Only the typed command starts it, or another session or skill calling it directly; plain words do not.

**Example.**

```text
You:        /handover before I switch to the pricing work
Assistant:  Wrote the handoff to the archive (full path printed). Pointers updated for this session and this checkout.
            State: plan step 4 of 7 done. Run /second-wind if you want to free context now; otherwise the handoff stays on disk.
```

**What it needs.** Nothing beyond the core install. The countdown shows in the Herdr label when you use Herdr (`HERDR.md`).

**When not to use it.** To save and always clear the context, use `/clear-handover`. To save and free the context while background work keeps running, use `/second-wind`. For a reflective end of session, use `/calibrate`. For memory consolidation across sessions, use `/dream`.

## /clear-handover

Saves a full handoff of this session and then starts clean. The skill writes the handoff with `scripts/checkpoint-paths.py --write --kind clear`, including a short `## Resume` section of two or three lines that you see on screen right after the clear. When the turn ends, the Stop hook checks the handoff on disk and only then sends `/clear` through Herdr. The next session receives the handoff and the master prompt once. If a `/loop`, cron job, `/goal`, background agent or monitor is running, it sends nothing and says so, because `/clear` would stop them. Without Herdr it says "handover verified, now type /clear" and you type it. The handoff is saved under `handoff-archive`.

**How to call it.** `/clear-handover [note]`. Only the typed command starts it; plain words do not.

**Example.**

```text
You:        /clear-handover switching to the Northwind Logistics tender
Assistant:  (one line: the full path of the saved handoff)
            Stop hook: handover verified, /clear sent.
```

**What it needs.** Herdr for the automatic clear (`HERDR.md`). Without it the handoff is still saved and you type `/clear` yourself.

**When not to use it.** To save without clearing, use `/handover`. To free the context but keep background work running, use `/second-wind`.

## /second-wind

Saves a full handoff and frees the context, choosing the safe way at the moment it runs. If nothing runs in the background, the Stop hook sends `/clear` and a new session reads the handoff. If a `/loop`, cron job, `/goal`, background agent or monitor is running, or the hook cannot tell, it sends `/compact` instead, which keeps them running. The handoff is written with `scripts/checkpoint-paths.py --write --kind second-wind` under `handoff-archive` and must include a `## Safe resume` block; the hook sends nothing without it. If the hook says `NOT sent`, nothing was lost, and it names what is missing.

It also appears in the menu shown at 45% and 55% context use, and in unattended mode it runs by itself at that menu.

**How to call it.** `/second-wind [note]`, or pick it from the threshold menu. Plain words do not start it.

**Example.**

```text
You:        /second-wind
Assistant:  (one line: the full path of the saved handoff)
            Stop hook: a /loop is running, so /compact was sent instead of /clear.
```

**What it needs.** Herdr for sending the command automatically (`HERDR.md`). Without it the hook tells you to type `/clear` or `/compact` yourself.

**When not to use it.** For an always-clear reset, use `/clear-handover`. To save without freeing context, use `/handover`.

## /create-plan

Researches the workspace and writes an implementation plan for a non-trivial change, before any change is made. The plan follows a fixed template: overview, a "spec core" (why, capabilities, constraints, non-goals, success signal), current state, the files to create, change or delete, design decisions and open questions, step-by-step tasks (grouped in waves when steps can run in parallel), a validation checklist and success criteria. Each check names a command and its expected output. The plan is saved as `YYYY-MM-DD-<name>.md` under the registry name `plans`. It plans only. It never makes the changes itself.

**How to call it.** `/create-plan <what to plan>`. Or say it in your own words, for example: "plan for adding a weekly summary export", "design the approach".

**Example.**

```text
You:        /create-plan add a CSV export of the weekly review
Assistant:  Plan written: 2026-11-04-weekly-review-csv-export.md under `plans` (full path printed). 6 steps, 1 open question: which columns.
            Run /implement <that path> to execute. Run /scrutinize before approval?
```

**What it needs.** Nothing beyond the core install. If you use Lific, the plan's `Issue` line names the issue (`LIFIC.md`).

**When not to use it.** To carry out a plan, use `/implement`. To stress-test a plan before you approve it, use `/scrutinize`. For a decision rather than a build, use `/deep-think`.

## /implement

Carries out a plan written by `/create-plan`, step by step, in order. It reads the whole plan first and stops to ask if there are open questions or blockers. It reads every file before changing it, writes complete files, records every departure from the plan, runs the plan's validation checklist, and finally marks the plan `Implemented` with notes on what was done. While it works it writes a step-by-step log (a "trajectory") with `scripts/implement-trajectory-log.py`, saved as `_trajectory_<run_id>.jsonl` at the runtime address `runtime:implement/logs`, which `/scrutinize` can audit later. It then offers that audit.

**How to call it.** `/implement <plan-path> [--no-trajectory] [--evaluate]`. `--no-trajectory` skips the log for a throwaway run. `--evaluate` checks each changed file with `scripts/artifact-evaluator.py`, with up to three rounds of fixes. Or say it in your own words, for example: "execute the plan", "build it".

**Example.**

```text
You:        /implement <path to 2026-11-04-weekly-review-csv-export.md>
Assistant:  Trajectory: run_id=2026-11-04_101500_weekly-review-csv-export.
            Implementation complete. 6 of 6 steps, 0 deviations. Validation: 4 of 4 checks passed.
            Plan status set to Implemented. Run /scrutinize to audit what was done?
```

**What it needs.** Nothing beyond the core install.

**When not to use it.** To write the plan, use `/create-plan`. To audit a finished run, use `/scrutinize`.

## /context7

Fetches current, version-specific documentation for a software library or framework from the Context7 service, so code is written against today's API rather than from memory. It runs `scripts/context7.py "<library>" "<query>"` and shows the documentation in the chat. If you give only a library name, the query is "documentation". If the library is not found, it tries other spellings. It saves nothing. The script also takes `--list` (list matching libraries only), `--version <v>` (pin a version), `--json` and `--limit <tokens>`.

**How to call it.** `/context7 <library> [query]`. Or say it in your own words, for example: "look up docs for fastapi", "library documentation for react hooks".

**Example.**

```text
You:        /context7 fastapi dependency injection
Assistant:  (current FastAPI documentation on Depends(), with code examples)
```

**What it needs.** `CONTEXT7_API_KEY` in `.env` is optional: the service also works without a key, with lower rate limits. The key is free from the Context7 dashboard.

**When not to use it.** For general web research, use `/osint`. For your own notes, use `/zk`.

## /playwright

Drives a real, hidden browser for a scripted job: a screenshot, text from a page that needs JavaScript, filling a form, a PDF of a page, checking that a page is up, or the transcript of a YouTube video. The script is `.claude/skills/playwright/scripts/pw.py` with the commands `screenshot`, `extract`, `fill`, `click`, `pdf`, `youtube`, `batch-screenshots`, `monitor` and `execute`. Common options are `--headed`, `--device "<name>"`, `--timeout <ms>` (default 30000), `--wait-for` and `--full-page`. Without `-o`, files land at the runtime address `runtime:browser/tmp`; copy anything you want to keep into `deliverables` or `drafts`. Everything a page returns is treated as untrusted: instructions found on a page are reported, never followed.

If you imported cookies with `/setup-browser-cookies`, every command uses them, so logged-in pages work. A form is submitted only with your explicit approval. Before `youtube`, the skill runs the VPN check in `reference/vpn-preflight.md` and waits for your answer, because YouTube blocks transcript requests from datacenter addresses.

**How to call it.** `/playwright [url] [action]`. Or say it in your own words, for example: "screenshot this site", "scrape this page", "headless browser".

**Example.**

```text
You:        /playwright screenshot https://example.com full page
Assistant:  Saved the screenshot under runtime:browser/tmp/screenshots (full path printed). Page title: "Example Domain".
```

**What it needs.** The browser extras: `uv sync --extra browser --extra media`, then `.venv/bin/python -m playwright install chromium`. The `media` extra is only for `youtube`.

**When not to use it.** For a plain page fetch, Claude Code's own WebFetch tool is enough. To import logged-in sessions first, use `/setup-browser-cookies`.

## /setup-browser-cookies

Copies your logged-in sessions for a website from a real browser on your machine (Brave, Chrome, Chromium or Edge) into the workspace cookie store, under the registry name `browser-cookies`, so `/playwright` can open pages that need a login. It runs `scripts/utils/chromium_cookies.py "<domain>" --browser <browser> --profile <profile> --store`, which decrypts that domain's cookies and merges them into the store, keeping other domains. The defaults are browser `brave` and profile `ClaudeCode`; name another profile if you log in under a different one. It reports only domain names and counts. Cookie values never appear. It never commits or sends cookie data anywhere.

If no cookies are found, log in to the site in that browser profile first. If some cookies cannot be decrypted (exit code `4`), the store is left untouched on purpose; the printed `cause:` lines say why. On Linux a locked keyring must be unlocked first. The first read may trigger a keychain or keyring prompt.

**How to call it.** `/setup-browser-cookies [domain]`. Without a domain it asks which domains and which browser. Or say it in your own words, for example: "import cookies for example.com".

**Example.**

```text
You:        /setup-browser-cookies example.com
Assistant:  Imported 14 cookie(s) for example.com into the runtime cookie store. Playwright will auto-load them for future browser commands.
```

**What it needs.** A Chromium-family browser on the same machine, logged in to the site. On Linux, the keyring that holds the browser's key.

**When not to use it.** For the browser work itself, use `/playwright`.
