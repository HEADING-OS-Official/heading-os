# What's new in HEADING OS Official

This page lists what the Official distribution gained since its first release, newest first. Each entry says what changed for you and, where it matters, what you do about it.

The current version is HEADING OS 3.4. From 3.1 on, one number names a release: the tag on the `HEADING-OS-Official/heading-os` repository (`v3.4.0`) and the version the README shows under "Status" are the same, and they follow Semantic Versioning. The first three releases were tagged by date, `v2026.10.1` to `v2026.10.3`; those tags stay as they are.

To move an existing installation to a newer release, follow "Update" in `INSTALL.md`.

## 3.4.0 (2026-11-25)

Tagged `v3.4.0`; `install.sh` installs it. Your sessions can talk to you on Telegram, telemetry is now refused by code, the engine watches its own services and timers, a new mod shows where Claude's work goes, and yards work under both versions of Herdr. Every command from 3.3.0 still works.

What you do:

- Run `uv sync --all-extras --group dev` in the engine folder, for the new library versions.
- Run `bash scripts/setup-platform.sh` once, so the new yard hook is registered. `bash scripts/setup-platform.sh --check` confirms it.
- Reinstall your timers and services the way you first installed them, so each unit picks up the failure alert.
- If you want the Telegram bridge, run `bash scripts/install-daemon-service.sh telegram-bridge`. It is off until you do.
- If you want the lot-map, install the `lot-map` mod from the engine's mod marketplace, `heading-os-mods`, as you did `context-bar`.

### New: your sessions on Telegram

Each Claude session, the main one and every yard, gets its own topic in your private chat with your notification bot. When a session stops and waits for you, its last answer appears in its topic. When it shows a menu or a permission prompt, the topic gets buttons; a button presses that option once, and only while the screen still shows the same prompt. What you write in a topic is typed into that session, as if you had typed it at the terminal.

Only you can use it. A message from anyone else, from outside your private chat, forwarded, edited or older than 15 minutes is ignored. A message for a session that is busy waits until it is free. When a yard is removed, its topic is renamed and then deleted, so a phone does not keep showing it.

### New: telemetry stays off, and a machine checks it

An update check is fine. Telemetry is not, unless you say so. Three changes hold that rule in code.

Every Claude Code process the engine starts for itself, for tests, reviews or setup, now carries the four no-telemetry switches. Before, a process started with project settings only never read the user settings where those switches live; under the nightly timer it ran with none of them. That is closed. A commit check refuses any new launch that skips the helper.

The update manager now knows, for each component it updates, whether that component sends telemetry and how to prove it is off. It refuses to update a component whose telemetry it cannot verify as off, and the daily radar says so. Chromium and the Hindsight server and console now have verified switches. Each probe only reads.

### New: the engine watches its own services and timers

The watchdog used to look only at heartbeat files. Not enough. It now checks that every enabled service is running and every enabled timer has fired on time, with room after a boot and after the machine wakes. When a timer job, a service or an update fails, you get one message on your own Telegram with what failed and its last five log lines, secrets removed. A crash loop sends at most one message an hour, not one a minute.

The daily radar has seven new checks: disk space, certificate expiry, pending security updates, a weekly dependency audit, timers that should be on and are not, MCP servers that stopped answering, and tool paths still pointing at an old Node.js version after an upgrade. That last one is also a row in `doctor`.

### New: a map of where the work goes

The lot-map mod draws a live map of what Claude is doing. Every tool call, model step, hook and message lights one of ten stations: you, the model, memory, files, shell, network, browser, agents, skills and hooks. Labels show names. Never content. `/lot-map on` opens it and `/lot-map off` closes it; nothing opens by itself. Under herdr-core the map is a picture; in other terminals it is drawn in braille. `scripts/lot-map.py <transcript>` replays the same map for a past session, including a yard that no longer exists.

### Changed: yards under both versions of Herdr

Herdr now comes in two versions, the classic TUI and herdr-core. Both work. HEADING OS picks the right one from the session's own environment. `scripts/yard.py create <name>` and `scripts/yard.py remove <name>` build and tear down a yard under either. Under herdr-core each yard gets a short code, `w<N>-<name>`, and appears as its own space in the window.

A session started in a yard without its yard marker is now refused every tool. Before, it got a warning and could carry on regardless of what the warning said. If you start yards through `scripts/yard.py` or `herdr worktree create`, the marker is set for you.

### Changed: transcription stays on your machine

Audio and video transcription now goes through your local Lemonade server by default, with Whisper Large v3. When the server does not answer, it falls back to the local engine and says so in one line. Nothing leaves the machine. `--engine local` keeps the old behaviour.

### Changed: libraries brought up to date

Every engine dependency is on its current release. Among them: anthropic 2.12.0, openai 3.57.0 and fastapi 0.165.0. `uv sync --all-extras --group dev` brings them in.

### Fixed

- Security: one relayed instruction could authorise a second, identical action. Each one now authorises exactly one.
- Security: the MCP check follows no redirect, so the headers you configured for a server never travel to another address.
- Removing a yard that had never committed deleted its scratch folder, with any notes and evidence in it. Scratch is now archived, never deleted.
- After `/clear`, a session in unattended mode stopped at its resume banner. It now carries on, and names both paths of the file it is working on.
- `/second-wind` checks the terminal before saving the handoff and retries a reset that did not go through.
- When the Hindsight server does not answer, the memory sync ends with one plain line instead of an error trace.
- Under herdr-core, a pane with only background shells reads as idle, and yard agents find the same programs you do.

## 3.3.0 (2026-11-17)

Tagged `v3.3.0`; `install.sh` installs it. Your morning dashboard can now arrive on Telegram by itself, the context bar sits under the prompt, every saved prompt carries a checked spec block, and every background job runs on the engine's own Python. Every command from 3.2.0 still works.

What you do:

- Run `uv sync --all-extras` in the engine folder before you install or reinstall any timer or service. The installers now refuse when `.venv/bin/python` is missing, instead of falling back to whatever Python is on the machine.
- If you want the daily dashboard on Telegram, run `bash scripts/install-dashboard-telegram-timer.sh` once. It is off until you do.

### New: the morning dashboard on Telegram

Every day at 08:00 in your configured time zone (`HEADING_OS_TZ`), a timer builds the morning dashboard and sends it as an HTML file to your own Telegram notification chat. The caption names the contacts that need attention, the deals and today's meetings, so you can read the essentials without opening the file. It sends only to your own chat, the same one the other notifiers use. If a build fails, you get one short text line instead, and the timer reads failed. `.venv/bin/python scripts/dashboard-telegram-notify.py --dry-run` builds the file into the checkout scratch and sends nothing.

### New: a checked spec block in every saved prompt

When Claude expands a request before acting, the expansion now ends with one fenced `yaml` block: the objective, what is in and out of scope, the deliverables, each of your asks quoting your own words, and any open questions. `scripts/prompt-archive.py save` checks the block's form and writes `Spec: valid`, `Spec: none` or `Spec: invalid` with the reasons directly under the `Task:` line of the saved prompt; `check` runs the same test before anything is saved. Two presets cover the routine cases (a backup push, and merging and deleting a yard). An invalid block is reported, never a reason to lose the prompt: it is saved anyway, and the command exits 3 so you notice. The block is read as data only; nothing in it is ever executed.

### Changed: the context bar sits under the prompt

The bar showing what fills Claude's working memory now sits directly under the prompt, above the status line, separated from it by a faint dotted line. It is more compact: about 118 columns wide it takes 3 rows, at 100 columns 4, and no label or marker is ever dropped to save space. Labels stay in their columns and use your theme's colours. In the first moments of a session, before the status line has run, the bar is drawn above the prompt as before.

### Changed: one Python for everything the engine starts

Background services, timers, hooks and every helper process now run the engine's own `.venv/bin/python`. Before, some of them used whichever Python started them, or the first `python3` on the machine, which could lack the pinned libraries and fail in ways that were hard to trace. Now a missing venv is named in one line: "no <path>: run uv sync in <engine folder>".

### Changed: models that think get room to answer

The engine now reads from the Anthropic Models API which models think by default and which accept an effort setting, and caches it beside the model list. A model such as Haiku 5.5, which thinks by default, now gets enough output room to finish its answer after thinking.

### Fixed

- Security: the status line ran git in whatever folder the session was opened in. git reads that repository's own settings, and some settings name a program git starts, so opening a session inside a repository someone else controls could make the status line run their program. It now runs git only in the engine folder and its own worktrees, and reads worktree paths in a form an unusual character cannot break.
- On macOS, Sentinel's background agent file could be refused by launchd when an argument held a character such as `&` or `<`, and an interrupted write could leave it half-written. It is now written safely and in one step.
- A session started with its data folder pointed at an empty scratch directory could copy your memory files into it, and later copy a stray file from it back into your real memory. Memory now syncs only with a real data repository.
- A spec block with an impossible value, such as a month 99, stopped the prompt archive with an error before the prompt was saved. It is now reported as invalid and the prompt is saved.
- The turn check's tests step refused to start on every run because it filtered on a test marker that no longer exists. It runs again.

## 3.2.0 (2026-11-16)

Tagged `v3.2.0`; `install.sh` installs it. New tools for design files and for watching a push, a new status line and context bar, two Hindsight parts that update themselves, and every dependency brought up to date. Every command from 3.1.4 still works.

What you do:

- Check your Python: 3.11.5 or newer is now required (`python3 --version`). Before, 3.11.0 was enough.
- After updating, run `uv sync --all-extras` and `.venv/bin/playwright install chromium` in the engine folder, so the new library versions and the matching browser are in place. On Linux, Playwright 1.65 needs the system library `libatomic.so.1` (the `libatomic1` package on Debian and Ubuntu).
- Run `bash scripts/setup-platform.sh` once, so the status line picks up its new refresh setting.

### New: read and render Figma design files

`scripts/fig-design.py <name> <command>` opens a Figma `.fig` design-system file kept in your data repository (`<name>` is its data registry name) and works with it from the terminal. `info` prints its counts and fonts; `render` turns frames into SVG and PNG pictures; `shells` draws empty app screens; `kit` writes the design tokens as `tokens.css` and a plain-text list of components; `shot` turns SVG or HTML files into PNG. A `.fig` file is treated as untrusted: nothing from it is written to your disk, every size is capped, and a damaged or hostile file ends with one plain line and exit 2 instead of a crash.

### New: see a push while it runs

`scripts/push-all.py` now records each step as it goes (checks, commit, push and tests, per repository). The status line under the prompt shows a short bar: which step of how many, how long it has run, and how long is left, estimated from your last five pushes. When it ends, the result stays there for ten minutes. `scripts/push-progress.py` prints the same line in any terminal. Each run also sends you two Telegram messages, at the start and at the end, to your own notification chat only; `--no-notify` turns them off. If anything about the progress record fails, the push itself carries on exactly as before.

### New: a status line that tells you where you are

The line under the prompt now has two rows. The first shows whether this session is HELM or a yard, the folder and branch when they are not the obvious ones, whether unattended mode is on with two counters (rounds it continued on its own, and context resets it sent), the Lific issue in work, how many yards are open and how many wait for you, the state of each repository and the model. When unattended work is paused for your answer, the badge turns yellow. The second row names the yards waiting for you, and the push bar sits under it while a push runs. Nothing is cut off: a long row wraps. Drawing the line never waits on git, Lific or Herdr; a helper reads them in the background at most every 30 seconds.

### New: find broken headers in your notes

`scripts/check-frontmatter.py` lists every Markdown file in your data repository, and every skill file, whose header (the block between `---` lines at the top) is not valid YAML. It only reads; it changes nothing. Until now such a header was read partly and silently, so a field such as `metadata.type` could vanish without a word. Now the reader also writes a warning naming the file, and a bad date or an unknown tag in a header no longer stops it with an error.

### New: Hindsight keeps two of its parts up to date

The update manager now installs new releases of the Hindsight command-line tool and the Hindsight coding-agents plugin by itself. Each update checks the download's checksum, keeps a backup, checks the result, and puts the backup back if anything fails. The CLI waits until your Hindsight server already runs the matching version. The Hindsight server and its web console are still only watched: their updaters ship too, but they work on a separate server reached over ssh (`HINDSIGHT_ADMIN_SSH_HOST` in `.env`), so on a standard install you update those two by hand as before.

### Changed: the context bar

The band above the prompt shows what fills Claude's working memory, by kind: the fixed part, skills, memory files and messages. It now measures against the whole window and marks four points on it: where to pay attention, where `/second-wind` saves and resets, where Claude Code would compact on its own, and the end of the window. On the right it says how many turns are left before `/second-wind` at your current pace. Labels sit on their own rows and never cover the bar, and the bar never runs past the edge of a narrow terminal.

### Changed: libraries brought up to date

Every library the engine uses is on its current release, including the big jumps: anthropic 2.8, openai 3.49, langfuse 5.2, ruff 0.17, playwright 1.65, fastapi 0.162 and pymdown-extensions 13.1. With the new Anthropic library, requests to Claude models no longer send a `temperature` setting, which that version of the API refuses. This is why Python 3.11.5 is now the minimum.

### Fixed

- Exchange calendar sync failed completely when one meeting had an attendee without an email address, such as a room. It now uses the attendee's name, or leaves the attendee out.
- Langfuse traces lost their trace id and their vendor, model, skill and fallback tags after the langfuse 5 update, with no visible error. They carry them again.
- The hourly memory index refresh could fail because it walked into the engine's runtime store. It now skips that store at every level.
- A session started in a subfolder of the engine wrote its checkpoint file inside the engine folder, where the commit guard then refused it. It now keeps it where the engine root keeps it.
- The unit drift check told you to re-run the installer for services the installer deliberately leaves alone. It now marks them `[adopted]`.

## 3.1.4 (2026-11-04)

The installer asks for your consent before its first change, can be paused and stopped, and reports what a removal took away. macOS support: Hindsight runs under launchd, the time zone is set before the first timer, and a free-space check runs first.

## 3.1.3 (2026-11-03)

A new installer. `install.sh` starts one resumable installer that sets up and wires every module (Lific, Herdr, CLIProxyAPI, Hindsight, the local search index, Exchange and Google mail and calendar), with a progress window and the setup wizard inside Herdr. One service layer installs every background job on systemd or launchd, `doctor` checks every component end to end, and the `heading` command opens Claude in the engine. Secrets are typed into a hidden terminal prompt, never into the chat. Also new: the HEADING OS Admin Console, one page linking every web interface and the guides, and the context bar above the prompt.

## 3.1.2 (2026-11-01)

Your own Telegram chat can receive files, through the same allowlist as notifications. The outbound wall also covers MCP sends, shares and deletes, Exchange invites and mail deletion. Branch review uses a voting panel of reviewers. Several status reports and background waiters were fixed.

## 3.1.1 (2026-10-30)

Tagged `v3.1.1`; `install.sh` installs it. Mostly a tidy-up of how existing things work, plus three new tools. Every command from 3.1.0 still works.

What you do: nothing, for most of it. Three habits are worth a look:

- `/investor-pitch`, `/linkedin-post`, `/flux-image` and their five siblings still work when you type them. The new short forms are `/pitch investor`, `/linkedin post` and `/image generate`.
- Eight tools such as `/radar` and `/playwright` no longer start on their own when you describe a task. Type their name.
- If you installed a second copy of HEADING OS on a machine that already had one, run `.venv/bin/python scripts/install-git-hooks.py` once in the first copy's engine folder.

### New: three tools

- **`/teach`** runs a course over several sessions: a goal, trusted sources, short interactive lessons, reference sheets, a glossary and notes that decide the next lesson. Every course file goes into your data repository, never into the engine folder. Type `/teach <topic>` to start; it never starts on its own. It is adapted from Matt Pocock's `teach` skill ([mattpocock/skills](https://github.com/mattpocock/skills)). Its MIT License travels with it, in `.claude/skills/teach/LICENSE-MIT.txt`, and `NOTICE` credits him.
- **`scripts/rules-ab.py`** tests a change to your instructions in real sessions before you adopt it. It runs the old and the new version side by side on the same tasks, and a separate judge scores them without knowing which is which. It can also show which rules loaded on which tool call. Test sessions run in a sealed copy and cannot write into your data or your engine.
- **`scripts/context-floor-audit.py sessions [--days N]`** reads your recent session records and reports where Claude's working memory went: which rules were loaded, how much memory recall added, which tools returned the most text, and how full sessions got (median, 90th percentile, maximum). It prints totals only, never message text or session names.

### Shorter instructions in every conversation

HEADING OS gives Claude a set of rules at the start of every conversation: how to write, what never to send without your approval, where files go. They are read before your first word, every time, and they take room in Claude's working memory (its "context window").

The always-on rules were rewritten shorter, with the same meaning; the rule files in the release went from 195,510 to 173,399 bytes. Every actor, trigger, prohibition, exception, file name and command is still there. Three kinds of text left the always-on set: step-by-step procedures needed only at one moment (they now live in the `reference/` file the rule points to), history about how a rule came about, and duplicates (a rule written in two or three places now has one owner). More room is left for your documents and answers, and two copies of a rule can no longer drift apart.

### One skill with modes instead of three similar skills

- `/pitch` covers `investor` (a new fundraising deck), `update` (a progress report to existing investors) and `keynote` (a conference or meeting deck). It replaces `/investor-pitch`, `/investor-update` and `/keynote-deck`.
- `/image` covers `prompt` (write a text-to-image prompt) and `generate` (make the picture). It replaces `/image-prompt` and `/flux-image`.
- `/linkedin` covers `post`, `series` (several connected posts) and `archive` (file a post you have already published). It replaces `/linkedin-post`, `/linkedin-series` and `/linkedin-archive`.

The eight old names stay as typed shortcuts: `/investor-pitch` runs `/pitch` in its investor mode. Each old procedure moved unchanged into the new skill's `references/` folder. Claude reads a short description of every skill it may choose from in every conversation, so fewer, clearer choices mean less reading and fewer wrong picks between near-twins.

### Eight skills start only when you type them

`/context7`, `/docparse`, `/google-contacts`, `/memory-hygiene`, `/pencil-export`, `/playwright`, `/radar` and `/setup-browser-cookies` now run only on their typed name; what each does is unchanged. These are tools you reach for on purpose. Claude's automatic choice list went from 56 skills in 3.1.0 to 43 in 3.1.1.

### Choosing the right skill

The router rule that helps Claude pick a skill from your plain words is now a short policy plus a "collision map" for skills that are easy to confuse; the full table moved to `reference/skill-router/<category>.md`, which Claude opens only when it hesitates. A new test, `scripts/skill-trigger-test.py --competitive`, checks routing the way a real conversation works: Claude sees every skill at once and picks one or none. It checks the exact bytes it sends for private names, and refuses uncommitted files unless you allow them. A request to "compare us with a competitor" with no competitor named now goes to `/competitor-intel`, which asks which one you mean.

### Memory recall answers are about half the size

The Hindsight bridge (`scripts/hindsight-mcp-bridge.py`) now returns compact answers: the facts, without most of the bookkeeping fields, which measured about 62% of a typical answer. The answer also arrived twice inside one reply; both copies are compact now. Add `verbose` to a recall for the full record.

### Safer releases and pushes

- Two checks, the rule inventory and the secret baseline, used to run only on GitHub after a push, so a break showed up as a failure email. Both now run in `scripts/push-all.py` and `scripts/check-push-ratchets.py`, and a push stops on your machine first. The secret check is one script, `scripts/check-secrets-baseline-drift.py`, which CI calls too.
- `scripts/review-branch.py` used to cut a large change at 120,000 characters; on one measured branch no line of code was reviewed, because documentation came first. It now reviews code first and splits a large change into parts.
- `scripts/herdr-brief.py` sends a work brief to a yard only when a `/goldfish ready` review covers that exact text; the one escape, `--no-goldfish "<why>"`, is recorded with the brief.
- A commit message ending in a "Co-Authored-By: Claude" line is refused.

### Fixed

- **The installer touches only your new installation.** If another HEADING OS installation had set `HEADING_OS_DATA` in your shell, `install.sh` used to arm that installation's data repository with the new engine's hooks. It now points the variable at the data repository it creates before any step, warns you when an inherited value differs, and checks the result the way a new shell will find it.
- **`/dream` records its last step again.** A safety guard refused it, because it wrote through a throwaway script; `scripts/retire-memory.py` now writes that record itself.
- **Memory sync no longer gets stuck.** When a safety limit blocked a large batch of old-version cleanups, nothing could release it. `scripts/hindsight-sync.py blocked list` shows the blocked set, and a confirmed command runs it.
- **Deleting a Telegram message is its own action.** The approval gate treated a delete as a send; it now needs a delete instruction.
- The status line in a yard names the yard once instead of "branch on branch", and the automatic continuation message after a long session is a few lines instead of a page.

## 3.1.0 (2026-10-27)

Tagged `v3.1.0` on 2026-10-27 and re-cut on 2026-10-28. The release ships 70 skills.

### New guides for everyday use

Five new pages join the install guide and the six tool guides in `docs/`:

- `WORKING-WITH-HEADING-OS.md`: the first day, an ordinary day, how a request becomes work, drafts and sending, handoffs, backups.
- `SKILLS.md`: the index of every skill that ships, with one page per group (`SKILLS-INTEL.md`, `SKILLS-COMMUNICATION.md`, `SKILLS-CONTENT.md`, `SKILLS-STRATEGY.md`, `SKILLS-DAILY.md`, `SKILLS-SYSTEM.md`). Each skill has what it does, how to call it, an example, what it needs, and when to use another one instead.
- `RULES-AND-PROTECTIONS.md`: the always-on rules and the checks that refuse an action, what you see when one refuses, and what to do.
- `WHATS-NEW.md`: this page.

### A cold read by a reviewer who never saw the work

`/goldfish` hands one document or change to a reviewer that starts without your conversation, your instruction files or memory, and reads only the document and a written standard. Use it before you approve a plan, send a brief, submit a proposal or tender answer, or trust that a document still describes what the code does. You answer each finding as FIX, CONTEST or ESCALATE. The full description is in `SKILLS-SYSTEM.md`.

### Seven more skills ship

With these seven and `/goldfish`, the Official tree ships 70 skills, up from 62. These seven were left out of the first three releases because their text was written around one company. Each now reads your company from your own records instead:

| Skill | What it gives you |
|---|---|
| `/ceo-intel` | A dated global and geopolitical brief, framed for your company. |
| `/competitor-intel` | Your company against one named competitor: gaps, overlap, strengths, a win strategy. |
| `/market-brief` | A sector or region brief: size, players, regulation, timing, your position. |
| `/intel-briefing-newsletter` | An intelligence newsletter for outside readers, rendered to HTML and PDF under your brand. It never sends itself. |
| `/investor-pitch` | Pitch deck content, slide by slide, with speaker notes. |
| `/investor-update` | A progress update for your current investors. |
| `/keynote-deck` | A deck for a conference, an important meeting or a government engagement. |

What you do: each of these starts by reading your business records (`business-info`, `strategy`, `current-data`, `pipeline`, `voice-guide`). If one is missing, the skill names it and tells you how to fill it, then continues with a neutral frame and invents nothing about your company, your numbers or your market. `/setup-wizard` writes most of them. Run it first. `SKILLS-INTEL.md`, `SKILLS-STRATEGY.md` and `SKILLS-CONTENT.md` describe each skill in full.

The newsletter's look comes from a settings file, `newsletter-brand.json`, beside `newsletter.css` in your brand kit: name, colours, fonts, logo, regions and section titles. Without either file it renders in a plain neutral style and says so on screen, so you always know which look you got. Nothing breaks.

### Nothing in the tree belongs to anyone

A full review of the published tree found business framing and machine names from the maintainer's own installation inside shipped skills, rules, scripts and comments. They are gone. The build that makes each release now refuses any such term, in a file's text or in its path, so a later change cannot quietly bring one back into a release. Skills that used to carry one company's framing now read yours from your records or write neutrally.

What you do: nothing. If a skill seems to know nothing about your business, fill in your records with `/setup-wizard`.

### The repository's own checks pass

From v2026.10.1 to v2026.10.3, the CI checks of the published repository failed on every run. The cause was simple. The engine's checks were looking for files the Official tree does not ship, and missed the files it adds, such as `install.sh` and the guides. The build now adapts those checks to what it actually ships, so they can pass. No check was weakened: an unlisted page or a dangling path still refuses, exactly as it did before, and the build itself verifies that in both directions.

Smaller changes in the same release:

- The README gained a "Status" section with the version, 3.1.0, and names the seven enforcement layers that `SECURITY.md` describes.
- A CI step that checked hashes of vendored skills is gone, because no vendored skill ships.
- The pinned versions of the GitHub Actions used by CI were brought up to date.
- `install.sh` now picks the newest versioned tag (`v3.1.0` and later) and falls back to the dated tags only while none exists. Before, it ranked `v2026.10.3` above `v3.1.0`, because 2026 is a larger number than 3.
- The README has a section on the VIP licence and links the guides online at headingos.com/docs. It no longer calls HEADING OS "source-available"; `LICENSE` and `NOTICE` are unchanged.
- Security reports go to `admin@headingos.com` with the subject line `HEADING OS security`, as the site says.
- New at the top of the repository: `CHANGELOG.md`, `SUPPORT.md` and `CONTRIBUTING.md`. Bug reports are welcome as issues; pull requests are not accepted.

## v2026.10.3 (2026-10-26)

### The six tool guides rewritten

`HINDSIGHT.md`, `LIFIC.md`, `HERDR.md`, `CLIPROXY.md`, `AGENTVIEW.md` and `YARDS.md` were rewritten from short notes into full guides for a new user on Linux, WSL or macOS: what the tool is, how to install and connect it, daily use with examples, troubleshooting, updating and removing it. Together they grew by about 2,000 lines. Start with `YARDS.md`.

Two small engine fixes came with them. One is a wording fix in the Lific rule, and the other is a fix in the yard preparation script that Herdr runs each time it creates a yard.

## v2026.10.2 (2026-10-26)

### A README for executives

The README was rewritten for an executive with no technical background: what HEADING OS does in one sentence, eight before-and-after scenarios with the command for each, a day with HEADING OS, security in plain words, install in three steps, the optional tools and an FAQ. Three illustrations sit in `docs/assets/`. No jargon comes before the install section.

### Install fixes

- `INSTALL.md` now gives the macOS commands for `git` and `gh` (`brew install git gh`, after Homebrew).
- `INSTALL.md` now explains how `/backup` and `scripts/push-all.py` behave with a data repository created with `--local-data`, which has no remote, and how to give it one: `gh repo create <name> --private --source <parent>/.heading-os-data --remote origin --push`.
- `install.sh` now picks the newest release only among tags of the form `vYEAR.MONTH.N`, ignoring any other tag, and sorts them in a way that also works with the `sort` that ships on macOS.

### License and notice

The contact address in `LICENSE` and `NOTICE` moved to the `headingos.com` domain. `NOTICE` now says plainly that the `*.example.*` files are empty templates, with field names and no values, and that every name of a person or an organisation in the documentation and the test fixtures is invented. Write in if one is not.

## v2026.10.1 (2026-10-26)

The first public release of the Official distribution:

- the engine without its private history, as one snapshot commit;
- 62 skills;
- `install.sh` and `docs/INSTALL.md`;
- the first versions of the six tool guides: Hindsight, Lific, Herdr, CLIProxyAPI, AgentView, and the HELM and yards method;
- the empty data template, `heading-os-data-template`, from which `install.sh` builds your private data repository;
- CI workflows for tests, a dependency audit and an OpenSSF Scorecard.
