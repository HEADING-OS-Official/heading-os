<!-- version: 1.1.0 | last-updated: 2026-10-29 -->
# Lific: the issue tracker, and who writes to it

Last Updated: 2026-10-29
Last Verified: 2026-09-18

Always-active rule. Lific holds the projects and issues this workspace tracks across sessions: one SQLite file in the private data overlay, a CLI, an MCP server registered in every session, and a browser view of the same data. Operator decision, 2026-09-18: **projects and issues only.** Plans, brainstorming and design records stay where they live; no Lific plan, page or folder is created here.

**The measurements behind every line below, the data model, modules and labels, and where CLI, MCP and web disagree: `reference/lific-integration-detail.md`.** Install, checksum, the v2.10.0 upgrade and defects measured against v2.9.0: `LIFIC.md` under registry name `spec-docs`. This machine's instance, session wiring and the security measurements item 8 rests on: `lific-integration-detail.setup.md` under registry name `reference-docs` (`python scripts/data-path.py path reference-docs`).

## The five statuses mean something here that they do not mean upstream

Upstream describes `todo` and `active` with identical words ("An open workflow status"). Here:

| Status | Meaning here | Who moves it |
|---|---|---|
| `backlog` | Captured, not yet triaged by the operator. The default on creation. | Claude, on creation |
| `todo` | The operator approved it. Ready to hand to a yard. | The operator |
| `active` | Allocated. A yard or HELM has it. | HELM, when it dispatches the brief |
| `done` | Merged into `main`, or closed by hand where no commit exists. | The merge, or the operator |
| `cancelled` | Dropped. | The operator |

## The conduct

**1. Never run `lific connect`, in any form, including `--dry-run`.** It rewrites the whole of `~/.claude.json` and by default registers an HTTP transport carrying a live API key in plaintext (measured 2026-09-18). `--stdio` writes a smaller stanza and the prohibition still stands: the blast radius is the file it edits, not the transport.

**2. The operator creates projects and issues; Claude creates an issue when the operator says so in passing** ("don't forget to do this", "we will do this after such-and-such", anything of that shape), on top of whatever is done with it in the workspace. Operator instruction, 2026-09-18. All three parts are required: search first (`lific search "<the gist>" -p <project>`), because the same thought arrives twice across sessions; file it in its project if known, else in the operator's default project; leave it at `backlog`, the default: **never create an issue at `todo`.** Deciding a task is real is the operator's; `backlog` is his inbox.

**3. "After such-and-such a task" is a `blocks` link, and HELM records it.** Operator decision 2026-09-18; HELM rather than a yard was proposed by HELM and approved by the operator 2026-09-18: a yard states the dependency in words in its closing comment, and HELM creates the link. Only that phrasing earns a link. A blocker is unresolved until its status is `done`, and **a `cancelled` blocker still blocks:** close a dead blocker as `done` or unlink it.

**4. A YARD writes only `add_comment` and `create_issue`**, which add a row and touch nobody else's work. Never a status, `link_issues`, `bulk_update`, `delete`, an edit to another task's issue, or a project, module, label, folder, membership or instance setting: those are HELM's, with `dump`, `restore`, `import`, `bind` and `doctor`. Anything under `lific service`, `lific start` or `lific init` falls under the daemon rule and is forbidden from a yard whatever it is called.

**5. Two comments per issue are mandatory.** On taking it: the yard's name and branch. On finishing: what was done, what was NOT done, the `Unattended:` line, the `/security-review` verdict, and one closing line: `branch <name> ready for merge`, or, when the work produces no commit, this as the comment's whole first line:

    MIRIM, THIS ISSUE MUST BE MOVED TO DONE

Nothing else on that line. Read § 7 of `reference/lific-integration-detail.md` before writing a closing comment.

**6. `done` arrives with the merge.** HELM's merge commit carries `closes HOS-N`, and HELM pipes that one merge commit's message to `lific git-hook`, never a `--range`. Never set `done` from a yard or on the strength of a plan: an issue is done when the thing it names is true. Read § 7 of `reference/lific-integration-detail.md` before running the hook.

**7. Comments go through MCP, never the CLI:** MCP records the author as the agent bot, while `lific comment add` defaults its author to the operator and signs a terminal comment with his name (measured 2026-09-18).

**8. The tracker is classified private and is not isolated.** Measured: the loopback REST API answers reads **and writes** with no credential; a deleted issue keeps its full text for thirty days; the audit trail survives even a project deletion and nothing removes it. No credential or secret ever goes into an issue or comment; write what the work IS and point at the overlay record holding the substance. An issue cannot be unwritten.

**9. A module is a theme, never a batch for one yard.** An issue carries one module or none, so a transient allocation costs grouping by subject; the batch is already recorded in the branch name and the merge commit closing each issue. Labels are not used, because supplying labels on an update replaces the complete set.

**10. The `[dossier]` journal.** Four events (prompt saved, handoff saved, brief sent, task closed), posted as the bot through `lific comment add --user` with `LIFIC_BOT_USER`, the one exception to item 7; never a full text. Format and outbox: § 11 of `reference/lific-integration-detail.md`. Operator approval 2026-10-24, HOS-256.

Changes to this rule need the operator's explicit approval.
