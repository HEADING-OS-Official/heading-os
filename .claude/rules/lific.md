<!-- version: 1.0.0 | last-updated: 2026-09-18 -->
# Lific: the issue tracker, and who writes to it

Last Updated: 2026-10-24
Last Verified: 2026-09-18

Always-active rule. Lific holds the projects and issues this workspace tracks across sessions: one SQLite file in the private data overlay, a CLI, an MCP server registered in every session, and a browser view of the same data. Operator decision, 2026-09-18: **projects and issues only.** Plans, brainstorming and design records stay where they already live, so no Lific plan, page or folder is created here.

Resident because both moments it governs are invisible to a path. The first is a judgement made in prose, when the operator says "don't forget this" and a task has to be captured. The second is an `mcp__lific__*` call or a `lific` command, where no file is opened either. A path-scoped rule would load only when somebody edits the Lific documentation, which is the one moment none of this is needed.

**The measurements behind every line below, the data model, modules and labels, and where the CLI, MCP and the web disagree: `reference/lific-integration-detail.md`.** Install, checksum, the upgrade to v2.10.0 and the defects measured against v2.9.0: `LIFIC.md`, in the data repository under registry name `spec-docs`. This machine's instance, its session wiring and the security measurements item 8 rests on: `lific-integration-detail.setup.md` in the data repository under registry name `reference-docs` (`python scripts/data-path.py path reference-docs`).

## The five statuses mean something here that they do not mean upstream

Lific's own documentation describes `todo` and `active` with the identical words, "An open workflow status". This workspace assigns them:

| Status | Meaning here | Who moves it |
|---|---|---|
| `backlog` | Captured, not yet triaged by the operator. The default on creation. | Claude, on creation |
| `todo` | The operator approved it. Ready to hand to a yard. | The operator |
| `active` | Allocated. A yard or HELM has it. | HELM, when it dispatches the brief |
| `done` | Merged into `main`, or closed by hand where no commit exists. | The merge, or the operator |
| `cancelled` | Dropped. | The operator |

## Provenance, because two kinds of statement live here

A sentence in quotation marks with a date is the operator's own wording, reproduced verbatim. Everything else is a decision recorded on his approval, and where HELM chose the form rather than he, the line says so and names the approval it stands on. The two are never blended into one voice. He can reverse any decision below, and each is written so that a reversal changes one line rather than the rule.

## The conduct

**1. Never run `lific connect`, in any form, including `--dry-run`.** Measured 2026-09-18: it rewrites the whole of `~/.claude.json`, 75 top-level keys holding every project's settings on this machine, and by default registers an HTTP transport carrying a live API key in plaintext. This session already reaches Lific over stdio through a launcher that keeps the token in one 0600 file. The `--stdio` flag would write a smaller stanza and the prohibition still stands: the blast radius is the file it edits, not the transport it picks.

**2. The operator creates projects and issues. Claude creates an issue when the operator says so in passing.** "Don't forget to do this", "we will do this after such-and-such", and anything of that shape: capture it as an issue, on top of whatever is done with it inside the workspace. Operator instruction, 2026-09-18. Three parts, and none is optional. Search first (`lific search "<the gist>" -p <project>`), because the same thought arrives twice across sessions and the duplicate is invisible to whoever wrote the first one. Put it in the project it belongs to if one is known, and in the operator's default project if none is. Leave it at `backlog`, which is already the default: **never create an issue at `todo`.** Deciding a task is real is the operator's, and `backlog` is his inbox.

**3. "After such-and-such a task" is a `blocks` link, and HELM is the one who records it.** The operator's answer on whether to use links at all, 2026-09-18: "yes, we create blocks" (translated). Without them `workable` means nothing and a dependency survives only as prose in a description that nobody rereads.

That HELM records them rather than a yard is **a decision HELM proposed and the operator approved with his "go" of 2026-09-18, not his own phrasing.** The reasoning: a link between two issues is a shared index the rest of the work reads, and not a record of one task's own progress. So a yard states the dependency in words in its closing comment, and HELM creates the link. He may reverse this and move `link_issues` back to the yard, which changes this paragraph and item 4 and nothing else.

Only that phrasing earns a link. One caveat from Lific's documentation, because it silently costs time: a blocker is unresolved until its status is `done`, and **a `cancelled` blocker still blocks.** Close a dead blocker as `done` or unlink it.

**4. A YARD writes two things and nothing else: `add_comment` and `create_issue`.** Both only add a row and touch nobody else's work, which is why they are safe from a worktree. Never a status, never `link_issues`, never `bulk_update`, never `delete`, never an edit to another task's issue, and never a project, module, label, folder, membership or instance setting. Those belong to HELM, along with `dump`, `restore`, `import`, `bind` and `doctor`. Anything under `lific service`, `lific start` or `lific init` is the daemon rule's and is forbidden from a yard whatever it is called.

**5. Two comments per issue are mandatory; the rest are optional.** On taking it: the yard's name and its branch, because this workspace does not use Lific's assignment (Lific 2.11.1 added `--assign`, MEASURED 2026-10-26 from `lific issue update --help`; it names a person or "human", not a yard or branch) and `active` otherwise says that somebody is working without saying who. On finishing: what was done, **what was NOT done**, and one of two closing lines. Either `branch <name> ready for merge`, or, when the work produces no commit at all, this as the comment's whole first line:

    MIRIM, THIS ISSUE MUST BE MOVED TO DONE

Nothing else on that line. `scripts/lific-board.py` matches the entire first line, never a substring, so the sentence above is prose here and a command only there. `/prime` surfaces every issue carrying it, and the operator closes it.

A yard's closing comment also carries, verbatim, the `Unattended:` line that `checkpoint-paths.py --done` printed, so the comment says whether the yard ever continued on its own (operator decision 2026-10-14, in his words, translated: "make the yard say, somewhere in its summary, that there was a moment when it worked with Unattended switched on").

Before a yard writes `branch <name> ready for merge`, it runs `/security-review` over the branch, passing the commit message file as its argument so the message text is reviewed too, beside `pre-commit run --files` over the entire staged set and `scripts/check-push-ratchets.py`, which takes no argument because it reads only the tree. The engine copy of the command (`.claude/commands/security-review.md`) diffs from the merge-base of `HEAD` with local `main` to the working tree, so a staged, uncommitted branch is reviewed whole. The closing comment carries its verdict: the diff base it printed, the count of findings, and each high or medium finding with its `file:line`. Every such finding is either fixed in the yard before the comment is written or listed under what was NOT done. (operator approval 2026-10-19 of the proposal to build `/security-review` into the yard cycle; this wording is proposed by the yard that did it)

The closing comment keeps its first line exactly as above; the yard's `close` record for the task journal is a SEPARATE comment, item 10.

**6. `done` arrives with the merge.** HELM's merge commit carries `closes HOS-7`, one per issue when a branch carried several, and HELM then feeds that one merge commit's message to the hook, straight after each merge: `git log -1 --format=%B <merge-sha> | lific git-hook`. Never a `--range`: the hook remembers nothing, so a range over every unpushed commit replays each older `closes` and re-closes an issue the operator reopened by hand. Measured 2026-09-28 on a throwaway database: an issue closed by a message, reopened, then given the same message again went back to `done`; given only a later message, it stayed open. Measured 2026-09-18: several `closes` in one message all fire, on separate lines or inline with commas, and a bare mention such as "see HOS-7" correctly does nothing. Never set `done` from a yard, and never on the strength of a plan: an issue is done when the thing it names is true.

**7. Comments go through MCP, never through the CLI.** Measured 2026-09-18: MCP records the author as the agent bot and the door as `mcp`. `lific comment add` defaults its author to "first admin user", which is the operator, and records the door as `system via cli`. A comment written from a terminal therefore arrives signed with his name.

**8. The tracker is classified private and it is not isolated.** Three measured properties decide what may be written: the REST API on loopback answers reads **and writes** with no credential at all; a deleted issue keeps its full text for thirty days; and the audit trail survives even a project deletion, with nothing that removes it. No credential or secret ever goes into an issue or a comment. For everything else, write what the work IS and point at the overlay record holding the substance. An issue cannot be unwritten.

**9. A module is a theme, never a batch for one yard.** An issue can carry one module or none, so the slot is exclusive and spending it on a transient allocation costs the ability to group by subject. The batch a yard received is already recorded where it belongs: the branch name, and one merge commit closing each issue. Labels are not used here at all, because supplying labels on an update replaces the complete set.

**10. The `[dossier]` journal.** Scripts in HELM and in yards post a `[dossier]` comment, as the bot, through `lific comment add --user` with the bot username from `LIFIC_BOT_USER` (the one exception to item 7: MEASURED 2026-10-24 on a copy of the database, `--user` records the bot as author, never the operator), on exactly four events: a prompt saved, a handoff saved, a brief sent, a task closed. A record is a head line, at most three summary lines and a data-root path; never a full text (item 8). A closing comment keeps the done marker as its whole first line; the `close` record is a separate comment posted with `scripts/dossier-journal.py post --event close`. A wrong or missing link is corrected by a later `relink` or `unlink` record, never by editing a file or a record. The format and the outbox: `reference/lific-integration-detail.md`. (The operator approved the task-dossier spec and plan on 2026-10-24, HOS-256; this wording is proposed by the yard that built it and approved by him the same day, in his words, translated: "'Item 10' is approved".)

Changes to this rule need the operator's explicit approval.
