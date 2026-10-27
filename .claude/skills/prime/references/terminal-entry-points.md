# Prime - Terminal Entry Points

Consumed by: `.claude/skills/prime/SKILL.md` section 5.1. Open it when the operator asks how to reach one of the dashboard's functions from a terminal, and answer with the command from the table. Do not print the whole table unprompted, and do not run any row as part of `/prime`.

Last Updated: 2026-10-16
Last Verified: 2026-09-22

## Why this file exists

The always-on `console-first` rule says every capability must work from a terminal, from a CLI, and from chat. A browser is a convenience and never a dependency. An audit on 2026-09-20 measured eleven dashboard functions against that rule and found the terminal path for most of them was real but unwritten. Unwritten is close enough to absent, because nobody finds it at the moment of need.

Each row below was run against a live workspace and produced real output. The third column states what the command leaves out. A partial answer presented as a whole one is the failure this table exists to prevent.

Re-drive any row before you rely on it: one row was found broken on 2026-09-22 under a sentence saying every row had been run (HOS-48).

## The table

| Function | Command | What it leaves out |
|---|---|---|
| Deals and stages | `.venv/bin/python scripts/pipeline-summary.py` | no overdue count by due date |
| Active threads | `.venv/bin/python scripts/thread.py list` | nothing; `/prime` already renders this |
| Drafts waiting to send | `.venv/bin/python scripts/action-queue.py list` | nothing; `approve` sends in the same command |
| Apply pending notify cards | `.venv/bin/python scripts/action-queue.py sweep [--dry-run]` | gated sends; they still wait for `approve` |
| Second opinion on a queued draft | `.venv/bin/python scripts/draft-critique.py <id>` (or `--body-file F`) | advisory only; it never approves, dismisses or sends |
| Contacts needing attention | `.venv/bin/python scripts/crm-health.py` | sees 19 of the 55 team members |
| Full team roster | `.venv/bin/python scripts/crm-health.py --roster` | no commitments, and no health colour |
| Skill catalogue | `.venv/bin/python scripts/skill-metadata-check.py --json` | no capability block; `--summary` prints counts |
| Knowledge base | `.venv/bin/python scripts/knowledge-health.py` | run `odin-brain-health.py` for the Odin half |
| Search across records | `.venv/bin/python scripts/memory-index.py query "<question>"` | no email subjects, no calendar, no skill catalogue |
| Today's agenda | read `<today>.md` under `.venv/bin/python scripts/data-path.py path exchange-calendar-cache` | does not mark which event is next |
| Published LinkedIn posts | `ls "$(.venv/bin/python scripts/data-path.py path linkedin-archive)"/posts/` | no preview text, no image list, and read the note below before trusting a zero |
| Log an email to a contact | `/crm log` | the dashboard's own log button stays lit |
| Mark an email read | `.venv/bin/python scripts/email-intelligence.py --mark-read <conv_id>` | no local dismiss record |

Fifteen rows (the sweep and draft-critique rows added 2026-10-16), and two of them resolve a directory through a helper rather than a literal path. Those two stay correct after a directory moves. A literal does not, and this workspace moved its whole data tree once already.

## The three that have no path

Say so plainly when asked. Never offer a substitute as though it were one.

**Defer an email to a later date.** Nothing reaches it. The nearest thing is to mark the message read now and unread on the day, which is a reminder in the operator's head rather than a deferred state.

**Mark a deal touched.** The dashboard writes a touch log. From a terminal you can only reach the EFFECT, by editing the stage date or by prefixing the next action with a pause word. The first records a date that is not true. Offer the second, never the first.

**The combined operational screen.** It left with the bridge daemon (HOS-7, 2026-09-21), and no single command reproduces it. The rows above reach some of its parts one at a time; the five journals of daily activity have no terminal reader.

## What a reader of this file should check first

Two commands read a store whose address moved. The store is the record, and the reader is what has to agree with it. When a row returns an empty answer that looks wrong, compare the path the reader composes against the file on disk before you conclude the store is empty. A reader pointed at an absent file reports zero, and zero reads exactly like "nothing is waiting".

**The LinkedIn archive row asks the registry for the whole address by the name `linkedin-archive`**, the same name its writer `scripts/linkedin-archive.py` asks for, so the row cannot disagree with the writer after a move.

**Four rows lost their original subject on 2026-09-21, when HOS-7 removed the bridge daemon**, and this file is the reason they were written down in advance. The dashboard pages for the operational screen, the day agenda, critical marks and the approvals queue are gone; the records they read are not. The three the operator decided against replacing are named in HOS-15's trail rather than restated here, because a decision belongs with its reasoning and not in a lookup table.
