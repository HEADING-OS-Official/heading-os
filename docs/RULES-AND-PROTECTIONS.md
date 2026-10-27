# Rules and protections

This guide explains the two kinds of safety HEADING OS ships with. It covers the always-on rules, the rules that load only for certain files, the session hooks that refuse an action, the commit gates, the push gates, the switches you control, how to check that everything is armed, and what to do when something refuses.

## The idea in one paragraph

A **rule** is a Markdown file in `.claude/rules/` that tells Claude how to behave. Claude reads it and follows it, but a rule is only words: if Claude misreads it, nothing stops the action. A **wall** is code that runs before or after an action and refuses it whatever Claude decided. HEADING OS uses both. Rules carry judgement ("ask before an irreversible step"). Walls carry the things that must never happen by mistake ("no API key in a tracked file", "no push you did not ask for"). When a wall refuses, the action did not happen, and the message says why and what to do instead.

A few words used below:

- **Engine**: the `.heading-os` folder. Code only, never your data.
- **Data repository**: the `.heading-os-data` folder next to it. Everything you create lives there.
- **Registry name**: a logical name for a place in the data repository. `.venv/bin/python scripts/data-path.py path <name>` prints where it is today.
- **HELM** and **yard**: your main engine copy, and a separate git worktree for one task. `YARDS.md` explains both.

## Always-on rules

These files load into every session. You do not call them. You notice them in how Claude answers.

### Prompt refinement and the approval stop

File: `.claude/rules/prompt-refinement.md`.

**What it asks of Claude.** When a request is ambiguous, multi-step, irreversible, or changes the shape of a task, Claude does not start at once. It restates the request as a full instruction, opening with "It looks like you want me to do the following:", and lists the objective, scope, deliverables, constraints, assumptions and open questions. The last part is a short `Measure:` block (see "Measurable execution" below). Then it stops and waits.

A direct, plain request or a question is done at once, with no stop.

**What you notice.** For bigger tasks, a structured restatement and a pause. Approve with "approved", "proceed", "go", "execute" or "yes". Before the first action, Claude saves the approved instruction with `scripts/prompt-archive.py save` and tells you the file's path. From then on, that file is the instruction. If the file and the chat disagree, the file wins.

**How to work with it.**

- Start a message with `!` to skip the stop (for example `!fix the typo in the heading`). Claude still saves the task's instruction file if the message defines a new task.
- A direct answer to a question Claude asked, or a one-step correction such as a rename, also skips the stop.
- `.venv/bin/python scripts/prompt-archive.py list` shows this session's saved instructions in order. `latest` prints the newest one's path.

### Measurable execution

File: `.claude/rules/measurable-execution.md`.

Every expanded task from the rule above ends with a `Measure:` block. It always names the check that defines "done", for example a test exit code, a count, or a file that exists and passes its audit. When the task has a clear end condition, Claude may also propose a ready-to-paste `/goal <condition>`. When the task repeats or needs polling, it may propose `/loop <interval> <prompt>`. These are proposals only. Claude never starts `/loop` or `/goal` by itself. You can accept, change or replace the metric at the approval stop.

### Human-gated sending

File: `.claude/rules/lethal-trifecta.md`.

**The risk.** An assistant that holds private data, reads untrusted content (mail, web pages), and can send messages out can be tricked by that content into sending your data away. HEADING OS keeps the third part, sending, behind a human.

**What it asks of Claude.** Every outbound message is a draft. A skill or a background job prepares it and stops. Nothing sends by itself.

**What you notice.**

- Drafts, never sends. In the Action Queue, a card that can send is in the `gated` tier. It sends only when you type `.venv/bin/python scripts/action-queue.py approve <id>`, and it sends in that same command. `list`, `show`, `edit` and `dismiss` let you review and change cards first.
- A new kind of action that can send is gated by default. An action type the risk ledger (`config/tool-risk.json`) does not know resolves to `gated`.
- A wall backs this up for Claude's own commands. The release gate (see "Session walls") refuses a command that sends through the Mattermost, Telegram or Gmail clients, or through `scripts/send-email.py`, unless your most recent message asks for it with a clear instruction such as "send it". A question like "did you send it?" does not count.

**The one exception the rule names: notifications to yourself.** A message that can only reach you, such as a timer's alert to your own Telegram chat, sends without a click. `scripts/utils/telegram_notify.py` accepts only the targets set in your environment (`HEADING_OS_SELF_TELEGRAM_TARGET`, or the per-feature `*_TELEGRAM_TARGET` variables) and refuses any other recipient. If none is set, it sends nothing.

**A known gap: Sentinel answers meeting invites by itself.** The rule says there is one exception. In the shipped code there is a second one that the rule does not name. Sentinel (`/sentinel`, `scripts/sentinel.py`) can **accept or decline calendar invites on its own**, with no approval per invite, and a decline can carry a reply message to the person who invited you. Two settings in its config turn this on, and both ship turned on:

```yaml
calendar:
  auto_accept: true
  auto_decline: true
  decline_message: ""
```

When a key is missing, the code also treats it as `true`. Sentinel reads its config from `sentinel_config.yaml` in your data repository's config folder (registry name `data-config`; `.venv/bin/python scripts/data-path.py path data-config` prints it). If that file does not exist, it reads the shipped example, `scripts/sentinel_config.example.yaml` in the engine, which has both switches on.

To turn this off:

1. Copy `scripts/sentinel_config.example.yaml` from the engine to `sentinel_config.yaml` in the `data-config` folder of your data repository, if you do not have that file yet. Do not edit the engine copy.
2. In your copy, set `auto_accept: false` and `auto_decline: false` under `calendar:`.
3. Restart Sentinel in HELM so it reads the new file. It reads the config once, when it starts.

### Humanisation and the confirmation line

File: `.claude/rules/humanization.md`.

**What it asks of Claude.** Prose should read as written by a person: concrete names, dates and numbers in every paragraph, a clear position instead of hedging, and none of the usual machine phrases. For outbound text (emails, posts, proposals) Claude writes a content draft first and then does a separate voice pass. Before it rewrites existing text, it tests that text with `scripts/humanization-check.py`. If the text already scores as human, Claude leaves it exactly as it is, byte for byte.

**What you notice.** Every prose deliverable ends with a confirmation line built from two tools: the word count and hidden-character verdict from `scripts/sanitize-text.py --scan`, and the result of `scripts/humanization-check.py`. If you give Claude your own draft, it asks before changing your voice.

### Hidden characters

File: `.claude/rules/hidden-chars.md`.

Generated text must contain only visible characters, ordinary spaces and newlines. Zero-width spaces, soft hyphens, non-breaking spaces, direction marks, byte-order marks and other invisible Unicode count as defects. The rule owns the confirmation line: `Word count: X. Hidden characters: <what the scan reported>.` Both values come from `scripts/sanitize-text.py --scan`, never from a guess. A wall backs this up: the post-write sanitize hook (see "Session walls") scans every file Claude writes.

### Voice

File: `.claude/rules/voice.md`.

Claude states only what it knows and says when it is unsure. It asks clarifying questions in one block when a gap would change the result, and goes ahead when the task is plain. It answers in the language of your message. Internal dates use `YYYY-MM-DD`, and times use the timezone in `HEADING_OS_TZ` (UTC when unset). In prose it writes, it uses no em-dash and no `--` as punctuation. Text it only edits, and quotations, keep their original punctuation.

One part of this rule is about structure: Claude must not create, change or delete workspace infrastructure (rules, skills, scripts, config, reference files, templates) without your approval. It proposes the change and waits. Output files that skills produce are exempt.

### Negotiation style (Voss)

File: `.claude/rules/voss.md`.

Any message Claude drafts for you follows a few habits from Chris Voss's negotiation method: acknowledge the other person's likely concern before making your point, ask "how" and "what" questions instead of giving demands, use precise numbers rather than round ones, and do not split the difference. `/voss` and `/meeting-prep` build a full plan for one conversation.

### Terminology

File: `.claude/rules/terminology.md`.

**This file ships empty.** It holds placeholders for your company name, your product names, your word for your team, any special vocabulary, and your stated principles. Until you fill it in, Claude writes in plain neutral English. Fill it in once, early, because skills that draft text read it.

### Memory discipline

File: `.claude/rules/memory-discipline.md`.

**What it asks of Claude.** A remembered snippet, an index entry or a summary is a pointer, not a fact. Before any consequential action (a commitment, a decision, a number, a deadline, a person's position, the state of a deal or task), Claude opens the real record and works from it. If the pointer and the record disagree, it says so.

**If you run Hindsight** (`HINDSIGHT.md`), Claude asks Hindsight first for anything in your records, and shows what Hindsight returned. Two walls back this up: "Ask Hindsight first" and the Hindsight-shown check, both below. Without Hindsight, neither wall does anything.

A memory to keep is a file in the data repository's `auto-memory` folder. Claude never deletes those files unless you tell it to.

### Persistence

File: `.claude/rules/persistence.md`.

HEADING OS keeps data in files and SQLite, and runs no database server. Any file format is fine. In-process stores such as SQLite are fine. A design that needs Postgres, MySQL, Redis, Elasticsearch or a similar server is refused, and Claude proposes the file or SQLite form instead. When Claude proposes something that stores data, it says where, and confirms that nothing new listens on a network port.

### Console-first

File: `.claude/rules/console-first.md`.

Everything must work from the terminal, a command line and Claude Code chat. A web page can be added for convenience, but nothing may depend on one. When Claude builds a capability, it builds the command-line path first and confirms it.

### Skill router

File: `.claude/rules/skill-router.md`.

Claude matches your plain-language request to a skill and announces it, for example "Using /osint for this". If two or three skills fit, it shows a short numbered menu and waits. Typing a `/command` yourself skips the matching. Skills marked `manual` in the router's table never start from a sentence; you type them. The table is generated from each skill's own metadata by `scripts/generate-skill-router.py`, and a commit gate refuses a table that drifted from the skills.

Before any skill that reaches websites which block data-centre addresses (`/yt-pulse`, `/osint`, `/playwright` on YouTube, anything that calls `scripts/firecrawl.py`), Claude asks a VPN question from `reference/vpn-preflight.md` and waits for your answer.

### Lific

File: `.claude/rules/lific.md`. Applies only if you use Lific (`LIFIC.md`).

- Claude never runs `lific connect` in any form. It rewrites your whole `~/.claude.json`.
- When you say "don't forget to do X" in passing, Claude searches for a matching issue first, then creates one at `backlog`. It never creates an issue at `todo`; deciding that a task is real is yours.
- A yard may only add comments and create issues. Status changes, links and deletions happen in HELM.
- An issue becomes `done` through the merge commit's `closes <ID>` line, fed to `lific git-hook`.
- Nothing secret goes into an issue or a comment. A deleted issue keeps its text for thirty days, and the audit trail survives deletion.

### Security

File: `.claude/rules/security.md`.

No password, key, token or session data goes into any file git tracks, in the engine or in the data repository. Keys belong in the gitignored `.env`, account passwords in a password manager, OAuth tokens and sessions in the gitignored `.sessions/` folder. Two commit-time protections can be switched off by hand, and the rule forbids both: `git commit --no-verify` (or `-n`), and setting `core.hooksPath`. If a secret is ever committed, rotate it first, then remove it from history, then record the incident under the registry name `security-incidents`.

## Path-scoped rules

These rules load only when Claude works on a file that matches their `paths:` list. A rule with a `data-scope:` list covers files in the data repository; Claude Code does not match those itself, so the data-path redirect hook names the rule to Claude the first time in a session that it touches such a file.

| Rule | What it governs | When it loads |
|---|---|---|
| `classification.md` | How a new record is classified (engine or private), and the routing map. | Work under `reference/**` or on `config/routing-map.yaml`. |
| `console-first-build.md` | The concrete checks behind console-first when building a capability. | Work under `scripts/**`, `.claude/skills/**`, `.claude/agents/**`. |
| `craft-silent-failure.md` | Eighteen rules for tests, guards, indexes and scheduled jobs whose failure looks green. | Work under `tests/`, `scripts/`, `.claude/hooks/`, `.claude/skills/`, `.claude/agents/`, `.claude/rules/`, `reference/`, `config/`. |
| `datastore.md` | The DataStore homes for original documents (contracts, decks, datasheets) and their index. | `scripts/datastore-*.py`; data scope: the `datastore-homes` group and the deliverable names. |
| `development-standards.md` | Quality gates for all engine work, including when a fix needs a test and when it carries a `Fix-Note:` line. | Skills, rules, hooks, scripts, tests, reference files, templates. |
| `documentation-style.md` | A plain-English subset of ASD-STE100 for install pages and skill bodies. | `.claude/skills/**/SKILL.md` and the install page. |
| `documentation.md` | Keeping documentation up to date when code changes. | `docs/**`, templates, `reference/**`, skills, rules, scripts, plans. |
| `generated-code-execution.md` | The one place model-written code runs on purpose (`/census`) and its sandbox conditions. | `scripts/census.py`, `scripts/utils/sandbox.py`, `scripts/utils/census_schema.py`. |
| `scope-claims.md` | A tool may claim only what its method established, and must say when it did not run. | `scripts/**`, `.claude/hooks/**`. |
| `tiered-risk.md` | The Action Queue's three tiers (`autonomous`, `notify`, `gated`) and the rule that sends always floor at `gated`. | `scripts/**`, `config/tool-risk.json`. |
| `trace-id.md` | One trace ID per process tree, so one run's logs can be found with one search. | `scripts/**`. |
| `visual-design-discipline.md` | How decks, dashboards and other visuals should look made by a person. | `docs/**`; data scope: HTML, PDF, PPTX, SVG and PNG files in the output folders, and the brand kit. |

## The walls

### Session walls (Claude Code hooks)

Hooks are registered in two files. `.claude/settings.json` is tracked and registers two. `scripts/setup-platform.sh` writes the rest into the gitignored `.claude/settings.local.json` from a template for your platform. If you skip `setup-platform.sh`, most walls are not armed and nothing tells you.

**The PreToolUse dispatcher.** `.claude/hooks/_dispatch.py` runs before every Write, Edit, MultiEdit, NotebookEdit, Bash, Read, Grep and Glob call, and before agent dispatches and codegraph and Hindsight tool calls. It runs its checks in order, and the first refusal wins. A refusal appears in Claude Code as a denied tool call with the reason below. Every refusal is counted, without its content, in a denial log; `.venv/bin/python scripts/denials.py` reports the counts, `--detail` lists each refused path, `--days N` limits the period.

| Check | What it refuses | The message starts | What to do |
|---|---|---|---|
| Secrets | A Bash command, or text written to a file, that contains a known credential pattern (Anthropic, GitHub, AWS, Slack, Google OAuth and other keys, JWTs, PEM private keys, URLs with an inline password, password assignments). `.env` files, `.sessions/` and `tests/security/` are exempt. | `BLOCKED: Detected <kind> in Bash command.` or `BLOCKED: Detected <kind> in content being written to <file>.` | Put the key in `.env` and load it from there. Never paste it into a command. |
| Release gate | A `git commit`, a git tag that writes, a `git push`, `gh pr merge`, `gh release create`, a push script such as `scripts/push-all.py`, or an outbound send, when your most recent typed message did not ask for it. | `RELEASE GATE:` | Type the word yourself in a fresh message: "commit", "push", "merge", or "send it". Avoid a "don't" in the same message, because any negation refuses. |
| Personal threads | Reading, searching, or quoting the private personal threads folder (registry name `threads-personal`), and writing its path into another file. | `Personal-threads protection` | This content is kept out of the chat by design. Search a named subfolder, such as `threads/business/`, instead of the whole tree. |
| Synced documents | A direct edit to a synced copy of a shared document under `spec-docs` that `sync-docs.py` overwrites from its template. | `BLOCKED: <path> is the synced copy` | Edit the template under the registry name `templates`. |
| Yard write guard | In a yard only: a write into HELM, any git command in the data repository, a push, a change to what runs on the machine (systemd, crontab, shell startup files, global git config), or anything aimed at another yard. | `YARD write guard` or `YARD isolation guard` | Write files into the yard or the data repository. Leave the rest to HELM, and note it in the closing note. See `YARDS.md`. |
| Yard deletion guard | Removing a worktree that has uncommitted changes or commits `main` does not have, or removing the main clone. | `YARD deletion guard` | Merge or commit first. The message lists what is unfinished and how to fix it. |
| Yard agent-start guard | `herdr agent start` into a yard's pane, which would start Claude without the `HEADING_OS_YARD` marker. | `YARD agent-start guard` | Let the yard bootstrap start the agent, or use the `herdr pane run` command the message prints. |
| Working directory | A command that runs `scripts/...` or `.claude/...` by a relative path while the shell sits in a subfolder, where the path cannot resolve. | `BLOCKED: the shell is parked in` | Run the anchored command the message prints. |
| Slow shell | Running the whole test suite in one process, or a long foreground wait (a `sleep` of 30 seconds or more, a polling loop). | `BLOCKED: this runs the suite in one process.` or `BLOCKED: this command waits` | Use `.venv/bin/python scripts/run-tests.py` (parallel), or run the long command in the background. For a deliberate exception, end the command with `# slow-shell-ok`. |
| Daily write cap | More than 1000 file writes in one day across the machine. | `BLOCKED: workspace daily write cap` | Find what produces the volume. If intentional, raise `WS_RATE_LIMIT_HARD` or delete the state file the message names. |
| Tool budget | More than 4000 tool calls in 30 minutes across the machine. | `BLOCKED: <n> tool calls in the last 30 minutes` | Check for a runaway loop. If intentional, raise `WS_TOOL_BUDGET_HARD` or delete the state file the message names. |
| Ask the graph first | Only when a `.codegraph/` folder exists: the first search or read of source code in a session, before any codegraph query. | `Ask the graph first.` | Call `codegraph_explore`, or run `codegraph explore "<names>"` in Bash. Any attempt unlocks the session, even one that fails. |
| Ask Hindsight first | Only when the Hindsight key file exists: the first lookup into your memory records in a session, before any Hindsight query. | `Ask Hindsight first.` | Query Hindsight (MCP `recall`, or `hindsight memory recall <bank> "<question>"`). Any attempt unlocks, even one that fails. |
| Crashed wall | Any action, when one of the checks above crashed. Reading, searching, and edits under `.claude/hooks/` stay allowed so you can repair it. | `HOOK WALL CRASHED:` | Fix the check the message names. There is no switch to turn this off. |

Some checks only give advice and never block:

- A notice at 200 file writes in a day, and when the same file is written six times within one session's last twenty writes.
- A notice at 75 tool calls in 30 minutes for one session, and when one tool runs four times in a row with the same arguments.
- A delegation note after a session has read more than 12 different files by hand since it last dispatched an agent. To record why the work is serial, run `.venv/bin/python scripts/fanout-note.py "<reason>"`.

**Other session hooks.**

| Hook | When | What it does |
|---|---|---|
| `session-start.py` | Session start | Prints alerts: overdue CRM contacts, stale context files, active threads, and in a yard `YARD BOOTSTRAP DID NOT COMPLETE` or `YARD MARKER MISSING`. It never blocks. |
| `memory-reconcile.py` | Session start | Keeps Claude Code's own memory folder in line with the data repository's `auto-memory`. |
| `data-path-redirect.py` | Before Read, Write, Edit, Grep, Glob | Rewrites a relative path that points at a data folder (for example `outputs/...`) to the real file in the data repository, so data never lands in the engine. Names a path-scoped rule once per session when its data scope is touched. |
| `post-write-sanitize.py` | After every write | Scans the file for hidden characters. Reports `HIDDEN CHARACTER CONTAMINATION in <file>` so Claude fixes it. If the scanner cannot load, it says `HIDDEN CHARACTER SCAN DID NOT RUN`. |
| `prompt-guard.py` | After writes to ingest folders (knowledge, CRM contacts, data family folders) | Advisory only. Reports `PROMPT INJECTION WARNING:` with the suspicious lines, so you review the file before trusting it. |
| `sync-docs.py` | After writes | Copies the shared documentation templates to their synced copies. |
| `recall-inject.py`, `hindsight-recall-inject.py` | Each message | Add pointers from the local memory index, and facts from Hindsight if it runs. |
| `codegraph-gate.py` | Each message | Adds pointers to code definitions your message names, from the codegraph index. |
| `unattended-resume.py`, `dossier-bind.py` | Each message | Resume a paused unattended stretch; tie Lific issue IDs you mention to the session. |
| `turn-check.py` | End of each turn | Runs a fast check (compile, import, matching tests) on the Python files this session changed. If it fails, it holds the turn once with the failure text, so Claude fixes it or says why not. |
| `hindsight-shown.py` | End of each turn | Only with Hindsight: when Hindsight supplied facts and the answer does not show them, it holds the turn once. The answer must contain "From Hindsight memory", a line starting "Hindsight:", or "from snapshot of". |
| `yard-watch.py` | End of each turn, HELM only | At most every five minutes, names yards that are dead, stalled, ready to merge, or unknown. It starts nothing. |
| `checkpoint-statusline.py`, `checkpoint-offer.py`, `checkpoint-save.py`, `checkpoint-inject.py` | Status line, end of turn, after compaction, after `/clear` | Track how full the context is. At 45% and 55% they offer `/second-wind`, which saves a handoff and clears the context. After a compaction or a clear they put the handoff and the saved instruction back. |

**Permission denials.** The settings template also tells Claude Code to refuse some commands outright, with no hook involved: `git push --force`, `git push -f`, `git push --force-with-lease`, `git commit --no-verify`, `git commit -n`, `rm -rf /`, and reading `.env`, `.sessions/`, `*.pem`, `*.key` and `.credentials.json`.

### Commit gates

`pre-commit install` arms these in each clone. They run on `git commit` and look at the staged files. A refusal stops the commit, and the output names the gate and the file. Never bypass them with `--no-verify`.

| Gate (pre-commit id) | What it refuses |
|---|---|
| `detect-secrets`, `secret-scanner-workspace`, `detect-private-key` | Credentials and private keys in staged files. |
| `bandit`, `ruff` (security rules), `debug-statements` | Dangerous Python patterns and left-over debugger calls. |
| `check-yaml`, `check-json`, `check-merge-conflict` | Broken YAML or JSON, and unresolved merge markers. |
| `vault-guard`, `runtime-state-guard` | Staged files under `_secure/`, `.tmp/`, or Claude Code's scheduled-task files. Message: `BLOCKED - vault files staged for commit` or `BLOCKED - runtime state staged for commit`. |
| `leak-guard-paths`, `leak-guard-staged` | Hardcoded data paths in engine code, and staged files that the routing map does not send to the engine. |
| `content-guard-workspace` | Real names and other real-entity text in engine files, from a denylist built from your data repository. Mark a genuine false positive inline with `content-guard: ok <reason>`. |
| `engine-only` | Any tracked engine file not covered by `config/engine-allowlist.yaml`. The engine holds code and what code needs to run, nothing else. |
| `data-root-bypass-guard` | Engine code that reaches the data repository without going through the data-root helpers, and data files inside the engine tree. |
| `home-paths` | A real home-directory path in the engine. |
| `skill-router-sync`, `skill-size-budget` | A skill-router table that no longer matches the skills; a skill file over its size budget. |
| `crm-schema-validate` | CRM contact files whose frontmatter does not match the schema. |
| `pip-audit-cve` | Dependencies with known vulnerabilities, when the requirements files change. |
| `lint-ratchet` | New lint findings beyond the recorded baseline. |
| `version-sync`, `readme-numbers` | Version numbers or documented counts that disagree with the source. |
| `skill-bash-paths`, `path-references`, `live-data-root`, `gate-integrity`, `test-vacuity` | Ratchets against known mistakes: a new data path spelled by hand in a skill, a new dangling path in prose, a new escape from the data-root setting, a gate without a scope and a test, a new test that passes over an empty set. |
| `user-agent` | A web request with its own User-Agent string instead of the workspace's one. |
| `memory-preservation` | A memory rule lost when it left the memory index. It skips itself when no data repository is present. |
| `documentation-style`, `documentation-style-skills` | Errors against the documentation-style rule in the install page and skill bodies. |
| `test-budget`, `test-budget-raise` | Test code over its ceilings, or a ceiling raised without saying so in the commit message. |
| `fix-note` | A commit whose subject starts `fix:` or `emergency:`, touches `scripts/` or `.claude/hooks/`, adds no test, and has no `Fix-Note: <what broke> \| <what fixed it> \| <why no test>` line. |

To run every commit gate over what you staged, before you commit: `pre-commit run --files $(git diff --cached --name-only)`.

### Push gates

**The pre-push hook.** `.venv/bin/python scripts/install-git-hooks.py` installs `.githooks/pre-push` into the engine and `.githooks/pre-push-data` into the data repository. On every `git push`, the engine hook first runs the push-time content scan of `scripts/push-all.py` (secrets, routing, real-entity content), then the tests this push can reach through `scripts/run-tests.py --pre-push`. Any refusal stops the push. The test step can take minutes; that is normal, not a hang.

**`scripts/push-all.py`** commits and pushes both repositories, the data repository first. It runs only in HELM; in a yard it stops with `this script runs from HELM (the main clone) only, not from a YARD worktree`. Before it commits, it scans everything about to leave. Its refusals start `REFUSING TO PUSH`, for example:

- a secret in the content about to be pushed;
- a file that looks like a credential by name (`.env`, `.session`, `cookies.json`, `.sessions/`);
- the rebuildable `.memory-index/` folder being tracked;
- data-class files or real-entity content in the engine;
- a ratchet finding (the same checks as `check-push-ratchets.py`).

It pushes only to an allowed remote, only from `main`, and only when the engine's pre-push gate is armed. After the push, it checks that the branch is level with the remote and does not trust the exit code alone.

| Exit code | Meaning |
|---|---|
| `0` | Everything that exists was pushed. |
| `3` | At least one repository was skipped for a named reason, such as a branch that is not `main`. The headline says "Partial" when the other one was pushed, and "NOTHING PUSHED" when none was. |
| `1` or `2` | A failure stopped the run: a security refusal, a remote that is not allowed, a missing push token, a misconfigured data root, or a push that did not verify. |

Options: `-m "<message>"`, `--no-commit` (push existing commits only), `--dry-run`.

**`scripts/check-push-ratchets.py`** runs the same engine ratchets as `push-all.py`, on the current checkout, and writes nothing. It works in a yard. Run it before you say a branch is ready, and in HELM on the merged tree before the merge commit. Exit `0` means nothing refused; exit `1` means at least one ratchet refused, and the push would be refused too. `--verbose` prints every scanner's report.

**How these fit SECURITY.md.** `SECURITY.md` names seven layers that keep private data out of the engine. In this guide they appear as: the static bypass guard and the runtime tree-clean check (the `data-root-bypass-guard` commit gate), the leak guard (`leak-guard-paths` and `leak-guard-staged`), the data-path redirect hook, the push-time wall (`push-all.py` and the pre-push hook), and the content guard (`content-guard-workspace`, and again at push). `SECURITY.md` also names a build partition; this guide does not describe it.

## Switches you control

Only the switches below exist in the code. Most walls have none on purpose.

| Switch | Effect | Warning |
|---|---|---|
| `!` at the start of a message | Skips the approval stop for that message. | The instruction file is still written if the message starts a task. |
| `# slow-shell-ok` at the end of a command | Lets one serial suite run or long wait through the slow-shell check. | Use only for a deliberate exception. |
| `WS_RATE_LIMIT_HARD`, `WS_RATE_LIMIT_SOFT` | Daily write cap (default 1000) and its notice (default 200). | Raising the cap also hides a real runaway. |
| `WS_TOOL_BUDGET_HARD`, `WS_TOOL_BUDGET_SOFT` | Tool-call cap per 30 minutes (default 4000) and its per-session notice (default 75). | Same. |
| `WS_FANOUT_BUDGET` | Files read by hand before the delegation note (default 12). | Advisory only. |
| `HEADING_OS_YARD=1` | Marks a Claude session as running in a yard. Yard guards and the yard checks at session start read it. | The yard bootstrap sets it. A yard started without it shows `YARD MARKER MISSING`. |
| `HEADING_OS_STATE_DIR` | Moves the hooks' state files. Must be an absolute path. | A relative value is ignored, with a message. |
| `HINDSIGHT_INJECT_KEY_FILE` | Where the Hindsight key file is. Its presence turns on "Ask Hindsight first". | Without the file, the Hindsight walls do nothing. |
| `.codegraph/` folder in the engine | Its presence turns on "Ask the graph first". | |
| `CLAUDE_HANDOFF_SOFT_THRESHOLD`, `CLAUDE_HANDOFF_HARD_THRESHOLD` | Context-use levels for the `/second-wind` offer (template values 45 and 55). | |
| `HEADING_OS_SELF_TELEGRAM_TARGET` | Pins the one chat that self-notifications may reach. Without it, the per-feature `*_TELEGRAM_TARGET` variables are the allowed set. | With none of them set, notifications send nothing. |
| `calendar.auto_accept`, `calendar.auto_decline` in your Sentinel config | Sentinel answers invites by itself. | Both on by default. See "Human-gated sending". |
| `content-guard: ok <reason>` on a line | Exempts one line from the content guard. | For a real false positive only. |

There is no switch for the release gate, the yard guards, the crashed-wall block, or the push-time secret scan.

## Check that they are armed

Run these from the engine folder after install and after every update. Each exits non-zero when something is missing.

1. Session hooks: `bash scripts/setup-platform.sh --check`. It names each missing hook registration.
2. Commit gate: `uv run python scripts/install-hooks.py --check`. It reads the hook file git would run and follows `core.hooksPath` if set.
3. Git hooks: `uv run python scripts/install-git-hooks.py --check`. It checks the pre-push gate and the pre-commit hook in the engine and in the data repository.
4. Push ratchets: `.venv/bin/python scripts/check-push-ratchets.py`.

These checks confirm that a hook is registered or installed. They do not prove that it ran. To see that the walls do refuse things, look at the denial counts: `.venv/bin/python scripts/denials.py --days 7`.

## Common problems

| What you see | Cause | Fix |
|---|---|---|
| No session alerts, no walls ever refuse anything. | `setup-platform.sh` never ran, so the hooks are not registered. | Run `bash scripts/setup-platform.sh`, then `--check`. |
| A commit with a secret went through. | The commit gate is not installed in this clone. | Run `uv run pre-commit install`, then `uv run python scripts/install-hooks.py --check`. |
| `RELEASE GATE: the operator did not ask for a commit in this turn.` | Your latest typed message did not contain a commit or push word, or contained a negation. Approving the work is not approving the commit. | Type "commit" (or "push") in a new message. |
| `RELEASE GATE: the transcript's two records of the operator's prompt do not yet agree` | A timing state in Claude Code's transcript, common on the first tool call of a turn. | Let one other tool call finish, then retry. If it repeats, send a fresh message. |
| `RELEASE GATE: the newest prompt record is a slash command` | A slash command such as `/compact` came after your last typed message. | Ask for the commit or push again in a new message. |
| `RELEASE GATE: the prompt behind this commit is a machine-to-machine brief` | In a yard, the latest message was a brief from HELM. | Switch to the yard's pane and type the word yourself. |
| `Ask the graph first.` | First code lookup of the session. | Run one `codegraph explore "<names>"` or `codegraph_explore` call. |
| `Ask Hindsight first.` | First memory lookup of the session. | Run one Hindsight query. It unlocks even if the server is down. |
| `BLOCKED: the shell is parked in ...` | An earlier `cd` left the shell in a subfolder. | Run the anchored command from the message. |
| `BLOCKED: this runs the suite in one process.` | A plain `pytest` over the whole suite. | Use `.venv/bin/python scripts/run-tests.py`, or narrow the run to a file. |
| `HIDDEN CHARACTER CONTAMINATION in <file>` | Invisible Unicode was written. | Let Claude re-edit the file; check with `scripts/sanitize-text.py --scan <file>`. |
| `HOOK WALL CRASHED:` | One dispatcher check raised an error. | Read the named check's error, fix it under `.claude/hooks/`. Everything else waits. |
| `YARD write guard` | The yard tried to write into HELM, run git in the data repository, push, or change what runs on the machine. | Do it in HELM, or write it in the yard's closing note. |
| `YARD deletion guard` | The worktree has uncommitted work or unmerged commits. | Merge or commit first, as the message lists. |
| `YARD MARKER MISSING` at session start | The yard's Claude started without `HEADING_OS_YARD=1`. | Close it and start `HEADING_OS_YARD=1 claude --dangerously-skip-permissions` in the yard. |
| `REFUSING TO PUSH` | One of the push-time scans found something. | Read the file list in the message, remove the content, commit, push again. |
| `this script runs from HELM (the main clone) only` | A HELM-only script ran in a yard. | Run it in HELM. |
| The pre-push hook takes minutes. | It runs the tests the push can reach. | Wait. Do not add `--no-verify` and do not wrap it in a short timeout. |
| Meeting invites are accepted or declined with no question. | Sentinel's `auto_accept` and `auto_decline` are on, which is the shipped default. | Set both to `false` in `sentinel_config.yaml` in the `data-config` folder of your data repository and restart Sentinel in HELM. |
