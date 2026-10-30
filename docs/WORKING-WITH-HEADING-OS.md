# Working with HEADING OS

This guide explains how to use HEADING OS from day to day, after `INSTALL.md` is done. It covers the first day, an ordinary day, how a request becomes work, where your files go, memory and the task tracker, drafts and sending, handoffs between sessions, and backups.

Each section is short and points to the guide that holds the detail. Read this one first.

## A few words used in this guide

- **Engine**: the `.heading-os` folder. It holds the code: skills, rules, hooks and scripts. It never holds your data.
- **Data repository**: the `.heading-os-data` folder next to it. Everything you create lives there: briefs, drafts, notes, contacts, reviews.
- **Session**: one running Claude Code conversation, started with `claude` in the engine folder.
- **Skill**: a ready-made procedure that starts with a slash command, such as `/meeting-prep`. You can also ask in plain words and HEADING OS picks the skill. `SKILLS.md` lists every skill.
- **Rule**: an instruction Claude follows in every session, such as "never send without approval". `RULES-AND-PROTECTIONS.md` lists them.
- **Hook**: a small program Claude Code runs before or after each action. Some hooks refuse an action outright, whatever Claude decided.
- **Registry name**: a short name for a place in your data repository, such as `prompt-archive`. You never type a folder path; the engine looks the name up. `.venv/bin/python scripts/data-path.py path <name>` prints where a name points.

## The first day

You have run the installer and filled in `.env`. Now introduce yourself.

1. Go to the engine and start Claude Code:

   ```bash
   cd <parent>/.heading-os
   claude
   ```

   `<parent>` is the folder that holds `.heading-os` and `.heading-os-data`. The installer's default is `~/ai/claude-workspaces`.
2. Run `/setup-wizard`. It asks about you, your business and how you write, one question at a time, and saves the answers as documents in your data repository. Type `skip` for a question you want to leave for later.
3. Open `.claude/rules/terminology.md` in the engine. It ships empty. Fill in your company name, your product names and any words your company uses in place of generic ones. Until you do, Claude writes in plain neutral English.
4. Run `/prime`. It loads your context and reports anything that needs attention. On a fresh data repository it looks sparse. That is expected.
5. Try one real task, for example:

   ```text
   /meeting-prep Dana Whitfield, CFO of Northwind Logistics, contract renewal
   ```

At the end of each working session, type `/calibrate`. It looks back over the session for your corrections, stated preferences and repeated friction, and proposes numbered changes. Reply `apply all`, `apply all except 3, 5`, or `cancel`. An approved memory change is saved at once; every other change is filed as a proposal and never applied by itself.

## An ordinary day

There is no fixed routine. A typical day looks like this:

| When | What you type | What you get |
|---|---|---|
| Start of the session | `/prime` | Your context loaded, and a short list of what needs attention. |
| Morning | `/dashboard` | One page: today's meetings, recent mail, relationships due a call, deals in motion, stale records. |
| Before a meeting | `/meeting-prep <person and company>` | A brief with history, likely positions and three questions to ask. |
| When mail arrives | `/email-respond` with the message | A reply drafted in your voice, for you to edit and approve. |
| A new name appears | `/osint <company or person>` | A dossier with a source and a confidence rating on each finding. |
| After a meeting | `/follow-up <person>` | A follow-up email, drafted, never sent on its own. |
| Any time you are unsure | `/next` | The sensible next step, with the command to run. |
| End of the session | `/calibrate` | Proposed improvements drawn from today's corrections and friction. |
| End of the week | `/weekly-review` | How the week went, compared with last week, and next week's priorities. |

You can ask for each of these in your own words. "Prepare me for tomorrow's call with Dana at Northwind" reaches `/meeting-prep` just as the command does.

## How a request becomes work

HEADING OS treats your request in one of two ways.

**A direct, clear request runs at once.** "What's on my calendar today", "draft a thank-you to the panel organisers": Claude does it, with no extra step.

**A request that is ambiguous, has several steps, cannot be undone, or changes the shape of a task gets an approval stop first.** Claude replies with a line that starts `It looks like you want me to do the following:` and then sets out:

- the objective;
- what is in scope and what is out;
- what it will produce and where;
- the assumptions it made, marked as such;
- open questions, if any are left;
- a short `Measure:` block: how "done" will be checked.

Then it stops and waits. Answer with `approved`, `proceed`, `go`, `execute` or `yes` to start. Answer with a correction to change the plan.

Three ways to skip the stop:

1. Start your message with `!`, for example `!fix the typo in the Northwind brief`.
2. Reply directly to a question Claude asked you.
3. Ask for a small correction to something it just produced: a typo, a rename, one line.

**Your instruction is saved before any work starts.** When a message sets or changes what a task is, Claude saves it with `scripts/prompt-archive.py save`: your words exactly as typed, plus the expanded version. It tells you the file's full path. That file is now the master instruction: if Claude's memory of the conversation and the file disagree, the file wins. The files live under the registry name `prompt-archive`. To list this session's saved prompts:

```bash
.venv/bin/python scripts/prompt-archive.py list
```

Why: a long session gets compacted, and compaction can lose the turn where you explained what you wanted. A saved file survives it.

## Where your files go

Every brief, draft, report and note lands in your data repository, never in the engine. Each kind of file has its own place, named in the registry. When a skill saves something, it tells you the full path.

To see where a kind of file lives:

```bash
.venv/bin/python scripts/data-path.py path meeting-prep-briefs
```

To see every name the registry knows:

```bash
.venv/bin/python scripts/data-path.py names
```

Everything in the data repository is a plain file: Markdown, JSON, YAML, CSV. Open it, edit it, delete it. HEADING OS uses files and SQLite for its own state and runs no database server.

## Memory

HEADING OS remembers in layers:

- **Your records.** Contacts, threads, notes, briefs and reviews are files in the data repository. Skills read them before they write anything. `/recall <question>` searches them and answers only from what it finds, with the file it came from, or says that nothing was found.
- **Memories.** A fact a later session will need (a decision, a preference, a correction) is saved as one small file in the registry name `auto-memory`. When an answer settles something like that, Claude ends it with one line, `Remember: <the memory in one sentence>?`. Say yes and it writes the file; say no and the offer is dropped. You can also ask it to remember something at any time.
- **Long-term memory server (optional).** Hindsight keeps every conversation and every record searchable by meaning, so the assistant remembers what you decided weeks ago. Setup and daily use: `HINDSIGHT.md`.

One rule governs all three: a recalled snippet is a pointer, not a fact. Before Claude acts on a number, a date, a promise or a decision, it opens the record the snippet came from.

## Threads and the task tracker

**Threads** follow one ongoing matter over weeks: a negotiation, a hire, a partnership. `/thread` opens, logs, holds and closes them. To list the active ones from a terminal:

```bash
.venv/bin/python scripts/thread.py list
```

**Lific (optional)** is a local issue tracker. When you say "don't forget to do this" or "we'll do this after the Northwind renewal", Claude records it as an issue in your inbox status, `backlog`. Only you move an issue to `todo`, which means "approved, ready to work on". `/prime` shows the board. Setup, the five statuses and daily use: `LIFIC.md`.

`/dossier <ID>` gathers everything about one issue: the saved prompts, handoffs, briefs, comments and commits that mention it.

## Drafts, approval and sending

HEADING OS drafts. You send.

Every skill that writes to another person (`/email-draft`, `/email-respond`, `/follow-up`, `/ceo-to-ceo`, `/telegram`, `/mattermost`, `/linkedin` and the rest) produces a draft and stops. Nothing goes to another person until you approve it.

How approval works:

1. Read the draft. Ask for changes until it is right.
2. Tell Claude to send, in an order, not a question: `send it`, `ok send`, `yes, send this`.
3. Claude runs the send command.

A hook checks step 3. Before any command that sends an email or a Telegram or Mattermost message runs, the release gate reads your most recent typed message. It lets the send through only when that message asks for it as an order. It refuses when:

- your message was a question, such as `can you send it?`;
- your message also says not to send, anywhere in it;
- the request came from another session or a brief, not from you.

When it refuses, you see a message that starts `RELEASE GATE:` and nothing is sent. Type the order yourself and Claude runs the command again.

The only messages HEADING OS sends without asking are notifications to you, on your own configured channel.

Some cards wait in the Action Queue instead of in the chat. To see and approve them from a terminal:

```bash
.venv/bin/python scripts/action-queue.py list
.venv/bin/python scripts/action-queue.py show <id>
.venv/bin/python scripts/action-queue.py approve <id>
```

`approve` sends at once, in the same command. `dismiss <id>` drops a card, and `edit` changes an email card's recipient, subject or body before you approve it.

`RULES-AND-PROTECTIONS.md` lists every check of this kind, what it refuses, and what to do.

## Long sessions: pins, handoffs and a fresh start

A long session fills Claude's context. When it is full, Claude Code compacts it: it replaces the conversation with a summary. Details can get lost. HEADING OS gives you four ways to keep what matters.

| Command | What it does |
|---|---|
| `/pin <text>` | Saves one item word for word for this session. It reaches every later handoff and the fresh start after `/clear`. |
| `/handover [note]` | Saves a full handoff of the session (what was done, what is next, which files matter) under the registry name `handoff-archive`. The session continues. |
| `/clear-handover [note]` | Saves the handoff, then clears the context. The next session starts with the handoff and the master instruction. Needs Herdr (`HERDR.md`). If something runs in the background, such as `/loop` or a background agent, it sends nothing and says so, because a clear would stop it. |
| `/second-wind [note]` | Saves the handoff and frees the context in the safest way: a clear when nothing runs in the background, a compaction when something does, so it keeps running. Needs Herdr. |

When the context reaches 45% full, and once more at 55%, HEADING OS shows a menu that offers `/second-wind` or to continue as is. You pick one.

### Unattended mode

`/unattended on` lets the session carry on by itself at a pause, after a short silent wait, instead of waiting for you. `/unattended off` turns it off and `/unattended status` reports it. It applies to the current session only, and only you can turn it off. It never grants an approval: sends and commits still need your word.

## HELM and yards, in short

You can run several Claude sessions at once, each on its own task.

- **HELM** is your main session in the engine folder. Everything live happens there: background services, sending, pushing, merging.
- **A yard** is a separate copy of the engine on its own git branch, made by Herdr, where a second Claude works without touching what is running. When it finishes, you review its branch in HELM and merge it.

You do not need yards for ordinary use. You need them when you want a second assistant changing something while you keep working. The full method, step by step: `YARDS.md`. Herdr itself: `HERDR.md`.

## Second opinions

`/council <question>` asks other AI models for an independent view and shows their answers beside Claude's own. `/devil` argues against a plan you are about to adopt. `/deep-think` writes a decision paper. The outside models need CLIProxyAPI (`CLIPROXY.md`), and they are third-party services: never send them anything private.

## Backups

Your data repository is the one thing that cannot be rebuilt. Back it up often.

1. In HELM, type `/backup`.
2. It commits the changes in both repositories, then pushes each to its private GitHub repository: the data repository first, then the engine.

Your typed `/backup` is the authorisation. Claude cannot start it by itself.

The same from a terminal, in HELM:

```bash
.venv/bin/python scripts/push-all.py              # commit changes, then push both
.venv/bin/python scripts/push-all.py --no-commit  # push existing commits only
.venv/bin/python scripts/push-all.py --dry-run    # show what would happen, change nothing
```

`push-all.py` needs `GH_TOKEN` in `.env` (a `--dry-run` runs without it). Before anything leaves the machine, it scans for secrets, private data in the engine, and files that must never be pushed. If the scan finds one, the push is refused and nothing is sent.

What the result means:

| Result | Meaning |
|---|---|
| Exit `0` | Both repositories pushed and verified. |
| Exit `3`, `Partial: N of M repo(s) not pushed.` | Some repositories were pushed. The ones listed were not, with the reason beside each. They are still committed locally. |
| Exit `3`, `NOTHING PUSHED: all M repo(s) skipped.` | Nothing reached GitHub. There is no new off-machine copy. Read the reasons. |
| Exit `1` or `2` | A real failure: a security refusal, a missing `GH_TOKEN`, or a push that did not verify. Read the message before you try again. |

`/sync` pulls both repositories first and then backs up, for when you work on more than one machine.

A data repository created with `--local-data` has no remote. `INSTALL.md`, "Back up your data", shows how to give it one.

## When something goes wrong

1. Read the message. Most refusals say what to do.
2. Check the setup: `bash scripts/setup-platform.sh --check` reports whether every session hook is registered.
3. Run `/prime`. It reports the health of the installation.
4. Look up the refusal in `RULES-AND-PROTECTIONS.md`, and install problems in `INSTALL.md`, "Troubleshooting".

## Where to go next

- `SKILLS.md`: every skill, with examples.
- `RULES-AND-PROTECTIONS.md`: the rules and the checks that refuse an action.
- `WHATS-NEW.md`: what changed in each release.
- `INSTALL.md`: installing and updating.
- The optional tools: `HINDSIGHT.md`, `LIFIC.md`, `HERDR.md`, `YARDS.md`, `CLIPROXY.md`, `AGENTVIEW.md`.
