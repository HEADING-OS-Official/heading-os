# Handover - the handoff text

Consumed by: `.claude/skills/handover/SKILL.md`, Step 2, and
`.claude/skills/clear-handover/SKILL.md`, which follows the same step. Read this
file before you write the handoff text.

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Ask the workspace's data-address resolver for
the real address before the file operation, and use what it answers. An unknown
name refuses rather than resolving to a plausible directory, and a path written
out here goes stale the day the directory moves.

Last Updated: 2026-10-14 (the unanswered «Запомнить?» offers, HOS-244)

---

## The archive file

Pass this text on stdin to `.venv/bin/python scripts/checkpoint-paths.py --write`. It
MUST contain these sections in this order. Keep the prose concise - this is for
resume, not a report.

```markdown
# Handoff - manual checkpoint

Generated: {ISO UTC timestamp}
Trigger: manual-checkpoint
Session: {session id if known}
Trust: Objective, Constraints and Decisions are trusted. Files touched and Current state may be outdated: verify them against the tree before acting.

## Objective

What is the current task aiming to accomplish? One paragraph.

## Acceptance criteria

How will we know the task is done? Bullet list.

## Constraints

What boundaries, deadlines, dependencies, or invariants must hold? Bullet list.

## Decisions

Key choices made so far in this session, with one-line reasoning. Bullet list.
A decision that changed during the session is ONE line in this form, and the old
decision appears nowhere else in the file:

- {was} → {became} → {why}

## Operator messages

Every message the operator sent in this session, oldest first, one line each,
with its status: done, in progress, or open.

1. [done] {the message in a few words, in the operator's own words where they were used}
2. [in progress] {...}
3. [open] {...}

## Files touched / inspected

May be outdated: verify against the tree before acting.

Absolute or workspace-relative paths grouped by role (read / written / planned). Bullet list.

## Current state

May be outdated: verify against the tree before acting.

Where are we right now? What is the last action completed? One paragraph.

## Safe resume

Next action: {the one next step}
Awaiting approval: {what waits for the operator's word, or none}
Already done (do not repeat): {side effects already made: sends, commits, writes outside the tree, or none}

## Commands / tests

Commands that should be re-run on resume (tests, validators, manual checks). Bullet list.

## Open issues

Known problems, blockers, or questions awaiting answers. Bullet list.

## Unanswered «Запомнить?» offers

Every «Запомнить: ...?» offer made in this session that the operator has not
answered, oldest first, one line each, the offer copied word for word:

- «Запомнить: {the memory in one sentence}?» (offered after: {what the answer settled})

## Next steps

The exact next 1-3 actions to take when resuming. Ordered list.

## User note

$ARGUMENTS
```

If `$ARGUMENTS` is empty, omit the "User note" section.

`## Safe resume` is three lines, always, with `none` where a line has nothing.
The next session reads this block first and does nothing listed under
`Awaiting approval` without the operator's word. For `/second-wind` and
`/clear-handover` the Stop hook refuses to send `/clear` or `/compact` without
it (operator decision 2026-10-14).

## What the script adds

Never write these yourself; `--write` adds them, so none can be forgotten or
misspelled (operator decision 2026-09-27):

- the `Master instruction:` and `Compacted before this handoff:` header lines,
  under the title;
- `## Pinned items`, this session's `/pin` items verbatim, before `## Objective`;
- `## Continuation prompt`, with the exact archive path;
- `## Prompts archived in this session`, the prompt archive asked at the moment
  of writing (below);
- the four pointer files: `summary.md` carries the `Objective`, `Current state`
  and `Next steps` sections under a Source / Generated / Trigger header, and
  `prompt.md` the continuation prompt, each with the master line first, for
  this session's pair and for the shared pair `/next` reads.

A section you write under one of those headings is replaced, not duplicated.

## The operator-message registry, and its self-check

The `## Operator messages` section closes a gap the other sections leave open. A
side request ("and also check X") is lost when it never became the objective.
List every operator message, not only the ones that shaped the task: a "go" is a
line too, marked `done`. One line per message, no retelling.

Before you write the archive, do this check:

1. Go through the conversation from the first operator message to the last.
2. Match each message to one line of the registry.
3. Give every line a status. A request that was neither done nor started is
   `open`, and it also goes under `## Open issues`.
4. Write the archive only when every message has its line.

If this session was compacted (`compacted` starts with `yes`), messages from
before the compaction are known only through the summary. List the ones it
kept, and write `Messages before the compaction may be missing.` as the
section's last line.

## The unanswered-offer list, and its self-check

The memory rule has Claude end an answer with one "Remember: ...?"
line when it settles something a later session will need, and has the handoff
carry the offers he left unanswered. An offer lives only in the conversation, so
a handoff that drops it loses it for good.

Before you write the archive, do this check:

1. Go through your own answers in this session and find every "Remember: ...?"
   line.
2. Drop each one he answered: a yes or a correction (the file is written), or a
   no (the offer is dropped for good).
3. List every offer left, word for word. When none is left, write
   `No offer was left unanswered in this session.` as the section's only line,
   so an empty list reads as checked rather than forgotten.

If this session was compacted, offers from before the compaction are known only
through the summary. List the ones it kept, and write
`Offers before the compaction may be missing.` as the section's last line.

## Trust markers

The `Trust:` line in the header and the two `May be outdated` lines tell the
next session what to re-check. Objective, Constraints and Decisions are its
instructions and are trusted. Files touched and Current state describe the tree
at the moment of writing, and the tree moves. Copy the three marker lines as
they stand. Never leave a superseded decision in the file beside the one that
replaced it: write the `was → became → why` line instead.

For `/clear-handover`, write `# Handoff - clear-handover` as the title and
`Trigger: clear-handover`. Everything else is the same.

## The prompt section, and its three outcomes

The script asks `scripts/prompt-archive.py` for THIS session's prompt files and
writes one of three different CLAIMS, the same wording the post-compact hook
uses (`CP.prompts_section`): the paths, oldest first, the last being the
instruction the session was executing; "No prompt was archived for this
session."; or "The prompt archive could not be asked (...)", which is not a
statement that none was archived. The section goes into the archive only; the
pointers carry just the master line, which names the newest of those paths.
