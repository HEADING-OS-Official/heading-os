<!-- version: 1.0.0 | last-updated: 2026-09-18 -->
# Lific integration: the measured detail

Detail file for `.claude/rules/lific.md`. The rule carries the conduct that has to fire without anyone going to look something up. Everything here is fetched when it is needed and costs nothing the rest of the time.

**Every claim on this page came from running a command or opening a file on this machine on 2026-09-18, and the command or the path is written beside it.** Nothing here comes from general knowledge about how issue trackers behave. Where something could not be established, it says so in its own sentence rather than being smoothed over. Instance-specific values (absolute paths, usernames, the repository's own identity) are written as placeholders: this file ships in the public engine, and the real ones resolve at run time.

Install, the release-binary checksum and the defects found during evaluation are a separate page, `LIFIC.md`, in the data repository at registry name `spec-docs` (`python scripts/data-path.py path spec-docs`), and are not repeated here.

---

Sections 1, 2, 11, 12 and 13, which recorded this machine's instance, how its sessions reach it, its measured security posture, its data-repository exclusion and what its evaluation did not establish, now live in the data repository at registry name `reference-docs`, file `lific-integration-detail.setup.md` (`python scripts/data-path.py path reference-docs`).

## 3. The four doors, and what the audit log records

`get_activity` on a live project and on a throwaway probe project returned all four:

| Door | Actor recorded | Reached by |
|---|---|---|
| `web` | the human's display name | the browser on loopback |
| `mcp` | the bot, marked `(agent)` | MCP tools, identity from `LIFIC_TOKEN` |
| `cli` | `system` | any `lific` subcommand on the default `sql` backend |
| `api` | `system` | an HTTP request to the REST API |

**Two of the four record no identity at all.** So the log answers "through which door" reliably and "by whom" only for the web and for MCP. That, plus `lific comment add --user` defaulting to "first admin user", is why the rule sends comments through MCP.

## 4. The data model, and the part of it this workspace uses

From `lific <command> --help` and the MCP tool schemas.

- **Project.** Name plus an identifier of at most five characters, which becomes the issue prefix. Optional emoji as `lucide:<Name>` or a literal character. Has a lead user.
- **Issue.** Title, markdown description, status, priority, at most one module, any number of labels, optional `start_date` and `target_date`, and a stable identifier. **Assignment arrived in Lific 2.11.1**: `lific issue create` and `lific issue update` take `--assign <WHO>` ("human" for any person, or usernames) and `update` takes `--unassign` (MEASURED 2026-10-26 from their `--help`). Before that version there was no assignee anywhere. This workspace does not use it: who holds an issue stays a convention, the yard's taking comment (rule item 5), because the field names people, not yards or branches.
- **Relation.** `blocks`, `relates_to` or `duplicate` between two issues, and they may cross projects. `blocks` is what makes `workable=true` mean anything.
- **Comment.** Markdown on an issue or a page. `lific comment list --json` returns `author`, `author_display_name`, `content`, `created_at`, `id`, `issue_id`, `page_id`, `seq`, `updated_at`, `user_id` (measured against a throwaway issue).
- **Module, label, folder, page, plan, attachment.** Described below or in Lific's own documentation.

Operator decision, 2026-09-18: **this workspace uses projects, issues, statuses, comments and `blocks` relations, and nothing else.** Plans, pages, folders and attachments are not created here, because the workspace already has better homes for what they hold. Priority is used, because it is how HELM picks what to hand out next.

## 5. Modules and labels

Lific's own page, `site/content/docs/concepts/modules-and-labels.mdx` (fetched 2026-09-18), verbatim on the two points that decide how they can be used:

> "A module groups issues within one project. **An issue can have one module or no module.** Deleting a module removes that module assignment from its issues."

> "Supplying labels during an issue or page update replaces the complete label set. Supplying an empty list clears all labels. Omitting labels leaves the current set unchanged."

The schema agrees: `issues.module_id` is a single column, while labels hang off a join table `issue_labels`. So a module is an **exclusive** grouping and a label is a many-to-many one.

Two consequences the rule states and this page explains.

**A module is a theme, not a batch for one yard.** The exclusive slot is worth spending on something stable, like an area of the project, because that is what `lific issue list -m <module>` and `get_board --group_by module` are for when a project holds forty issues. A yard allocation is transient: next week the yard is gone and the module remains, occupying the only slot.

**Module status is decorative unless someone maintains it.** The statuses are `backlog`, `planned`, `active`, `paused`, `done`, `cancelled`, default `active`, and Lific's documentation gives each of them the same description, "A module status". Nothing derives a module's status from its issues. Left alone it will simply be wrong, so this workspace does not set it.

**Labels are not used.** The replace-on-update semantics above mean any tool that updates an issue without re-supplying labels is one keystroke from clearing them, and nothing in the workspace needs a second grouping axis today.

## 6. Where the three surfaces disagree

One tool, three ways in, and they are not the same tool.

### In the CLI and not in MCP

`user`, `key`, `member`, `instance`, `import`, `bind`, `agents-md`, `git-hook`, `dump`, `restore`, `doctor`, `service`, `start`, `project-archive`, `login`, `logout`, `completion`. Administration and lifecycle are a terminal job; the agent cannot do any of it.

### In MCP and not in the CLI

Plans (`create_plan`, `get_plan`, `edit_plan_step`, `update_plan_step`), relations (`link_issues`, `unlink_issues`), the audit log (`get_activity`), the board (`get_board`), bulk edits (`bulk_update`), attachments, comment editing and deletion, and deletion of an issue, page, plan or project. `lific --help` lists thirty-six commands and none of them is `plan`; the CLI deletes modules, labels and folders and nothing else.

Two smaller gaps in the same direction. MCP `create_issue` accepts `start_date` and `target_date`, and the CLI's `issue create` has no such flags. MCP `list_issues` filters on `blocked`, `created_since`, `created_until`, `updated_since`, `updated_until` and pages with `offset`, `order_by`, `order`, where `lific issue list` offers only status, priority, module, label, `--workable` and `--limit`.

**One of those gaps matters to this workspace and one does not.** Relations are MCP-only, and the rule has HELM create them, so that is fine. The rest are not used.

### The upstream text that gets this wrong

`lific agents-md` writes a block into a repository's `AGENTS.md` claiming the CLI "**also mirrors every data command**, so it works when MCP is not configured at all". That is false by the two lists above, and Lific's own CLI documentation page documents no plan command either. This workspace does not run `lific agents-md`, so the block is not written here.

### The bot filter, which is the one that costs a person time

`GET /api/users` returns only the human, although the database holds the bot as well, and `?include_bots=true` changes nothing. `lific instance info` reports the same undercount in its `users` block. The web interface reads that endpoint, so **a bot cannot be seen or added in the browser.** `lific user list` is the only surface that reports bots at all, and `lific member add` is the only way to grant one access.

### Deletion is not one thing

An issue deleted in the web interface is soft-deleted: `issues.deleted_at` is set and the row keeps its full title and description for `retention.trash_days`, which is thirty days here. An MCP `delete` of a project is hard: after deleting a probe project, `projects`, `issues`, `plans` and `plan_steps` held no row belonging to it. The audit log is the exception, and fourteen rows for that project remained with no surface that removes them.

## 7. Closing an issue: the merge, and the issues no merge will close

`lific git-hook` closes the issues a commit message says it closes. Measured 2026-09-18 with `--dry-run` against identifiers that do not exist, so nothing was written:

- Several references in one message all fire, both on separate lines and inline as `Closes X, closes Y`.
- A bare mention (`see HOS-998`) fires nothing, as documented.
- An identifier with no issue behind it reports `skipped: not found` rather than failing the run.
- `--range` works from a git worktree: `lific git-hook --range origin/main..HEAD --dry-run` ran clean.

So HELM's merge commit carries one `closes <ID>` per issue the branch finished, and HELM then feeds that merge commit's message alone to the hook: `git log -1 --format=%B <merge-sha> | lific git-hook`. Not the range form, although it works. Measured 2026-09-28 on a throwaway database: the hook closes whatever a message names unless it is already closed, and keeps no record of messages it has seen, so an issue closed, reopened by hand and then given the same message again returns to `done`. A range over every unpushed commit replays every older `closes` on each run, which is how issues reopened by hand came back closed. That transition records as `system via cli`, with no name, which is acceptable for a mechanical close.

**Issues that produce no commit are the whole reason `scripts/lific-board.py` exists.** A decision, a piece of research, a document written into the data overlay: no merge, so no `closes`, so nothing would ever move them out of `active`. The yard's closing comment opens with this as its entire first line instead, and `/prime` surfaces it:

    MIRIM, THIS ISSUE MUST BE MOVED TO DONE

The sentence lives once in code, as `DONE_REQUEST_MARKER` in `scripts/lific-board.py`. Nothing checks that the two agree, so whoever renames the marker updates this page in the same change.

Two measurements decided how that marker is matched.

**Comment bodies ARE searchable**, correcting an earlier reading of the FTS schema. `search_index` is an FTS5 table whose own definition comments `entity_type -- 'issue' or 'page'`, which suggested comments were invisible to search. They are not: searching a comment's text returned it with `result_type: "comment"`.

**But search matches tokens, not the phrase.** Searching `DONE MOVED MIRIM` returned the same comment as the full marker, and so did an unrelated fragment of its second paragraph. A token match cannot decide whether a comment is a command or a discussion of one. So the board script never searches: it lists `active` issues, which is by definition a small set, reads their comments, and requires the marker to be the **entire first non-empty line**, compared case-insensitively. A comment that merely contains the phrase, including this page, does nothing.

## 8. The boundary against what this workspace already has

The standing instruction is to check what exists before inventing, and to extend the nearest thing rather than put a sibling beside it. Five stores here hold something task-shaped, and each one's scope is quoted from its own file.

| Store | What it holds | Where its own words draw the line |
|---|---|---|
| `scripts/thread.py`, `/thread` | A running situation, business or personal, with events, decisions and follow-ups. | "Code work, debugging, technical fixes: explicitly out of scope." |
| Action Queue, `scripts/action-queue.py` | A drafted outbound action awaiting the operator's go. Two types only, `email_send` and `note`. | Approval is the send. It holds nothing that is not on its way out of the machine. |
| `scripts/ops-radar.py`, `/radar` | Eight named signals, hard-coded. | "It is a DETECTOR, not an executor." Nothing deposits an arbitrary item into it. |
| `/create-plan` | One document per change, written into the private overlay with the date and a slug in its filename: objective, scope, critical files, steps, success criteria, risks. | "You are creating a PLAN, not implementing changes." |
| `audit-rotation-ledger` (data repository registry name) | A finding against one artifact, keyed to that artifact's content hash. | A finding recorded against bytes that have since changed stops counting. |

**The line that separates all five from Lific: text against state.** Each of those is a document whose prose is edited to keep it true. A Lific issue is a unit of work whose text is written once and whose STATUS then changes, addressable by a stable identifier from any session.

The test is one question. *Would finishing this work mean rewriting the text, or only moving a status?* Rewriting means a plan document, a thread or a knowledge note. Only a status means Lific.

Two consequences worth writing down. A Lific issue never becomes the brief: a brief runs to thousands of words and is edited, so it goes out through `scripts/herdr-brief.py` and the issue records that it was sent. And nothing whose subject is a person goes into Lific; that is a thread.

## 9. HELM, a YARD, and why the answer differs from the data overlay

`CLAUDE.md` puts "a shared index other work reads" on HELM's side, and Lific is one. The conclusion does not follow, and the reason is worth being precise about.

**The measurement first.** `lific bind` run from a worktree computes the repository identity from the origin remote and the root commit, "never from the path" per its own help. HELM and every YARD of this repository produce the same two aliases and resolve to the same project. Lific cannot tell them apart. No binding exists at present: `repo_bindings` and `repo_identities` are both empty.

**The reason behind the overlay prohibition does not transfer.** That rule exists because the data overlay is one working tree with one index, so a commit taken from a task sweeps other tasks' unfinished work into one unreviewed commit. Lific has no index and no commit. Every write is one scoped row with an audit entry naming its door, and the failure mode the overlay rule prevents has no counterpart.

**What does have a counterpart is the sweep.** An action reaching across other tasks' records is the same mistake whatever the store. Hence the rule's split: a yard may `add_comment` and `create_issue`, because both only add; everything that changes or removes an existing record is HELM's.

`link_issues` only adds a row too, and it is still HELM's. The line is not "does this add?" but "whose record does this describe?". A comment and a new issue describe the yard's own work. A `blocks` relation is a statement about the shape of the whole backlog, which every other piece of work reads through `workable=true`. That split was proposed by HELM and approved by the operator with his "go" of 2026-09-18; it is a decision, not his wording, and reversing it changes one paragraph of the rule.

`lific doctor` is HELM's for a second reason: it probes the running server, and `--repair` applies database migrations.

## 10. Console-first

Every verb this workspace uses has a CLI subcommand: listing and filtering issues, creating and updating them, comments, search, export, membership, backup. `--json` turns itself on when stdout is not a terminal, so scripting needs no flag. `scripts/lific-board.py` drives the CLI and nothing else, so the `/prime` panel works with the browser closed.

The one surface with no CLI is plans, and this workspace does not use plans. Operator decision, 2026-09-18. That decision closes the only console-first gap Lific had here rather than working around it.

## 11. The `[dossier]` journal (HOS-256)

Rule item 10 owns when a record is written; this section owns its shape. The code is `scripts/utils/dossier.py`; the CLI is `scripts/dossier-journal.py`; the reader is `scripts/lific-board.py dossier HOS-N`.

One record is one comment on each issue in the session's task list:

    [dossier] 2026-01-01 09:00 UTC · handoff · manual · session 0000aaaa
    Next action: run the example step.
    Awaiting approval: nothing.
    Already done (do not repeat): the example draft.
    → <handoff-archive>/2026-01-01-090000_handoff_manual_0000aaaa.md

- The first line starts with `[dossier]`, then the UTC minute, the event (`prompt`, `handoff`, `brief`, `close`, `relink`, `unlink`), an optional kind, and the first 8 characters of the session id. The reader keys on the first line only.
- At most three summary lines, each cut to 160 characters, in the session's language. A summary carrying a secret-shaped string (`scripts/utils/secret_patterns.py`) is dropped whole and the record goes out with the head and the path only, because Lific's audit log cannot be scrubbed.
- The last line is `→ ` and the file's path relative to the data repository root.
- The task list is `dossier_tasks` in the session's checkpoint state, filled by `.claude/hooks/dossier-bind.py` from the ids an operator's message names, from the `Task:` line of a yard brief (a prompt whose first line is the `X-HEADING-BRIEF` marker) (its other ids bind nothing), and from an `update_issue` to `active` that the response confirms; a confirmed `done` or `cancelled` drops it. `create_issue` binds nothing (HELM, 2026-10-24). Across /clear `checkpoint-inject.py` carries it, with any candidates of its own, as candidates under `dossier_carried`, which no writer reads (HOS-259): the first prompt `prompt-archive.py` saves afterwards confirms the candidates its verbatim half names and drops the rest, writing `Carried <ID> dropped: not named in this prompt; relink with <command>` under its `Task:` line, which /prime repeats once from `dossier-outbox.dropped.jsonl` beside the outbox. A message naming a candidate does not confirm it; a confirmed `update_issue` to `active` and a yard brief's `Task:` line do. The mechanical handoff built at /clear posts to the previous session's confirmed tasks only. Empty means `Task: none` in the file and no record. The carry makes no Lific call: the list goes across as it is, and a closed issue is filtered when its record is flushed.
- The transport is the CLI's default `sql` backend: no running server, no network, no credential. `HEADING_OS_LIFIC_DB` points every call, reads included, at another database file; a verification run uses a copy that way and never the live one.
- Every record goes to the outbox, registry name `dossier-outbox` (beside the database named by `HEADING_OS_LIFIC_DB` when that is set), one JSON line with an event id per record, and nothing else happens in the writer: no Lific call runs inside a hook's critical path (HELM, 2026-10-24). In HELM the writer then starts one detached `dossier-journal.py flush` with the checkout's `.venv` interpreter (none: nothing is started), never waited for, whose lines go to `dossier-outbox.jsonl.flush.log` beside it, rotated to `.1` past 1 MB by the flush that holds the lock, at its start, never by a writer (a rename, so a running flusher loses no line; a rename that fails is tried again by the next flush); on Windows it is detached with `DETACHED_PROCESS`. A yard starts nothing that outlives its command (M029.1): its records wait for HELM's next record or `/prime`, and `dossier-journal.py flush` run by hand in a yard is fine. Whatever way a pass ends, the flusher merges spills before it releases the lock, and after a pass that did not end on `down` it passes again over what they brought, three passes per flush at most; an id is sent or struck at most once per flush. A line in the outbox that is not a record (torn by a crash, garbage, not UTF-8, or `attempts` / `soft` that is not a non-negative integer) moves, byte for byte, to `dossier-outbox.corrupt.jsonl` beside it, one line each, never deleted, and `/prime` names it; a record appended after a torn line gets its own line. The lock is a file created with `O_CREAT | O_EXCL`, the same on every platform; a writer waits 0.2 s for it and otherwise writes its record whole to a spill file of its own, which the next flush merges; a flush takes it without waiting and skips everything when it is held (the log rotation and the project-key refresh included); the lock holds a token and its holder removes it only while it still holds that token; a lock older than 120 s is a dead holder's, renamed aside before it is removed so two contenders cannot both remove it, and a running flush keeps its own fresh. One record id per distinct record: two relinks for one file stay two. Per entry the flush reads the issue's status first. A done or cancelled issue receives only `close`, `relink` and `unlink`, except a record written before the issue closed (each entry carries its `at`): that one is posted. The close time is the newest move into done or cancelled in Lific's own `status_transitions` (written by the database's trigger `status_transitions_capture` on every status change, whatever the path), and a record of that same second counts as before it; read read-only by the flusher (never a hook) from the database `HEADING_OS_LIFIC_DB` or `~/.config/lific/lific.toml` names; MEASURED 2026-10-24, both `issue update -s done` and `git-hook` write that row. Where it cannot be read, the bound is `updated_at`, which a comment moves too, so it errs toward posting. Either bound is taken only from a read that saw the issue closed, once per flush. A later one moves to `dossier-outbox.rejected.jsonl` with the reason `issue done` or `issue cancelled` and a line. Then the newest 50 comments, so a send that timed out but landed is not repeated. A status read or a send that fails is classified by one rule. `down` is a timeout, no bot user, or a failure whose stderr, anywhere and in any case, names the database or its transport (database is locked, database table is locked, database is busy, DatabaseBusy, readonly or read-only database, disk I/O, unable to open database, database or disk is full): the pass stops. Lific's NotFound for the issue is `missing`. Everything else is refused, a malformed id (`BadRequest`), permission and membership refusals included, and a refused record never stops the pass. A status or comment read Lific does not answer stops the pass with the entry kept and no strike. `dossier-journal.py post` refuses an id that is not `KEY-N` before queueing anything (exit 2). Strikes are gathered during the pass and applied at its end, and only when the pass did not end on `down` and sent at least one record (a send found already landed does not count); otherwise they are dropped. Lific saying the issue does not exist is a strike, and three move the record to the rejected file with the reason `missing`; a refusal is a soft strike, and five move it there with the reason `refused: <Lific's first line>`. `/prime` names every new rejection once, by reason (refused, missing, closed). A flush with three or more sends, every one refused, adds one to `dossier-outbox.jsonl.systemic.json`; only sends count, a refused status read does not; a flush that sends anything resets it; from two, `/prime` says "Dossier: Lific refused every record in the last N flushes: <first line>". Nothing is ever set aside for that, and the count is cleared when a flush leaves the outbox empty. A failed entry keeps `fail_kind`, `last_error` and `stuck_flushes`, the flushes in a row it failed unstruck with that kind (refused, or its issue not found, `last_error` "issue not found"; a new kind starts the count again); from three, `/prime` says "Dossier: N record(s) stuck: <issue> <last error>" (three named at most), and never sets one aside for it. A spill `.tmp` older than 10 minutes, left by a writer that died, is merged when it holds one complete record and moved to the corrupt file otherwise; `outbox_count` counts it until then. `/prime` runs the flush beside its board read, with `--deadline` set to its timeout minus `LIFIC_TIMEOUT` minus 5 s: no Lific call is started after it, so it overruns by one call at most, and the lock is released as usual. An entry already in the rejected file (by id) or a line already in the corrupt file (by its bytes) is not appended again, so a killed flush cannot count one twice. Before every send and every rewrite the flush checks the lock still holds its token, and stops if not. Records are never deleted; the rejected file takes each id once. `LIFIC_BOT_USER` is read from the environment and, when absent there, from the checkout's `.env`; still unset means nothing is posted, said once per pass, because the CLI's default author is the operator. The project keys the prompt hook matches ids against come from `dossier-outbox.jsonl.projects.json`, which the flush holding the lock refreshes, outbox or not, at most once per 10 minutes counted from its last attempt (`.stamp` beside it, renewed on success, failure and an empty answer alike; a failure keeps the old cache); without it the hook uses `HOS`. No hook calls Lific.

### Known limits

Accepted on the operator's bar of 2026-10-24 (HOS-256 round 9): a finding is fixed in code only when it can happen in normal use (one machine, WSL), loses, duplicates or silently sets aside a record, or breaks /clear, /compact or unattended. These meet none of the three and are not fixed:

- `lific-board.py dossier` accepts an id with non-ASCII digits (Arabic-Indic and the like); Lific answers it as an unknown issue.
- A handoff whose header time is near year 1 with an offset (`0001-01-01T00:00:00+01:00`) stops `lific-board.py dossier` with an OverflowError.
- `/prime` imports the dossier module at its top, so a dossier module that fails to import fails every `/prime` check, not only the Lific panel.
- The comment pager in `lific-board.py` relies on Lific honouring `--offset` and returning comment objects; a Lific that returned full pages of something else would loop until `/prime` kills it.
