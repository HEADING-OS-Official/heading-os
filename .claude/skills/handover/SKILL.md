---
name: handover
description: "Сохранить manual handoff текущей сессии в `handoff-archive` без выполнения /compact (раньше называлась /checkpoint). Используй когда хочешь зафиксировать состояние работы и иметь возможность вернуться позже с чистым контекстом; чтобы сохранить и сразу очистить контекст, есть /clear-handover. Запускается явной командой /handover ИЛИ вызовом снаружи через инструмент Skill. NEVER auto-trigger: обычная фраза скилл не запускает, потому что router стоит в manual и триггеров нет."
allowed-tools: "Read, Bash(date:*), Bash(.venv/bin/python scripts/checkpoint-paths.py:*)"
argument-hint: "[заметка] | unattended on|off|status"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "3.0"
x-heading-capability:
  what: >
    Writes a manual session handoff (objective, current state, files touched,
    next steps, continuation prompt) to `handoff-archive` so
    the session can be resumed later with clean context. Formerly /checkpoint.
  how: >
    Two ways in. Type /handover [optional note], or call it from outside
    through the Skill tool. An ordinary message never fires it: router mode
    manual, no trigger phrase. One command, `checkpoint-paths.py --write`, takes
    the handoff text on stdin and writes the archive file plus the .latest/
    pointer files the SessionStart inject hook reads. The skill itself never
    runs /compact or /clear; freeing the context is /second-wind's job.
  when: >
    Use before switching tasks, before a risky action, or when the
    threshold menu appears. To save and free the context in one step use
    /second-wind, or /clear-handover to always clear. For
    reflective end-of-session capture use /calibrate; for cross-session memory
    consolidation use /dream.
x-heading-routing:
  category: Operations
  label: /handover [note]
  triggers:
    - NEVER auto-trigger. Explicit `/handover [optional note]` only (formerly `/checkpoint`, which is now the built-in alias of `/rewind`). Saves manual session handoff to `handoff-archive`, scoped to this session, without running /compact. Also carries one session switch. `unattended on|off|status` continues at a pause after a countdown instead of halting, and at the threshold menu runs /second-wind by itself; it is on by default in a yard and off in HELM. Nothing lowers the mode except the operator - the assistant's done marker, three continued turns with no tool call, and the continuation ceiling stop a stretch and leave the switch up, and the status line shows its state on every render.
  exclusions:
    - Auto-resume after /compact handled by checkpoint-save.py (PostCompact)
    - reflective end-of-session -> /calibrate
    - cross-session memory consolidation -> /dream
    - save and free the context in one step -> /second-wind
    - save and always clear -> /clear-handover
  compound: 'No'
  router: manual
---


# /handover

Save a manual session handoff without running `/compact` or clearing context.

"/checkpoint" in an older note means this skill. To save and then clear the context, use
`/clear-handover`, which follows this same procedure.

## What this does

- Hands the handoff text to ONE command, `.venv/bin/python scripts/checkpoint-paths.py
  --write`. It writes the combined handoff file to `handoff-archive`, a
  logical name the workspace resolver places.
- The same command writes all FOUR pointer files under `.latest/` in the
  runtime address `runtime:handoff/state`. This session's pair
  `.latest/{session-slug}/{summary.md,prompt.md}` is what the SessionStart
  inject hook reads. This checkout's pair
  `.latest/.checkout/{checkout}/{summary.md,prompt.md}` is what `/prime` and
  `/next` read, so HELM and each YARD see only their own handoff.
- Does NOT run `/compact`
- Does NOT clear the session
- Does NOT continue implementation after writing - wait for the user to direct next action

## When to use

- Soft / hard checkpoint offer fired (`Stop` hook surfaced the context threshold)
- About to switch to an unrelated task, want resume-ready snapshot
- Mid-implementation, want to save state before risky action
- Long session approaching natural pause point

## Procedure

### Step 0 - Handle a switch argument first

Split `$ARGUMENTS` on whitespace. If the FIRST token is exactly `unattended`,
this is a switch. It is not a handoff. Anything else is a note, including a word
that merely begins with it. Match the whole token, never a prefix.

Run one of these, then stop. Do not write any file.

```bash
.venv/bin/python scripts/checkpoint-paths.py --unattended on
.venv/bin/python scripts/checkpoint-paths.py --unattended off
.venv/bin/python scripts/checkpoint-paths.py --unattended status
```

Report the command output in one line, then stop. For `unattended on` the
operator is about to leave, so a question here defeats the switch.

The switch applies to this session only. It overrides the environment default
and the yard default in both directions. It needs no cleanup. The state file carries a
session key, and the pruner removes it with the session.

### Step 1 - Get this session's paths

```bash
.venv/bin/python scripts/checkpoint-paths.py
```

It emits `key=value` lines, among them `session_id`, `archive`,
`master_instruction` and `compacted`. A `compacted` value that starts with
`yes` means a compaction came before this handoff. Details from before it may
be missing from your context, so say so in `Current state`.

If the script is unavailable, stop and say so: the write in Step 2 needs it too.

### Step 2 - Write the handoff with one command

Read `references/handoff-template.md` first. It holds the section list, in
order, and the self-check: every operator message of this session has one line
under `## Operator messages`, with its status. Then pass the whole text on stdin
through a quoted heredoc, so no backtick in it is executed.

```bash
.venv/bin/python scripts/checkpoint-paths.py --write <<'HANDOFF'
# Handoff - manual checkpoint
...the sections from the template...
HANDOFF
```

The script adds what the model must never write:

- the `Master instruction:` and `Compacted before this handoff:` header lines;
- this session's pinned items (`/pin`) verbatim as `## Pinned items`;
- the continuation prompt with the exact archive path;
- the list of this session's archived prompts.

It then writes both pointer pairs from the archive, the master line first in
each. A non-zero exit names what is wrong: fix it and run the command again.

#### Cold read before the write

The next session reads the handoff with no memory of this one. Test the
handoff that way before the write. `/clear-handover` and `/second-wind`
inherit this step.

1. Write the handoff text to `goldfish/<stamp>-handoff-draft.md` under the
   directory that `.venv/bin/python scripts/data-path.py scratch` prints.
2. Dispatch the `goldfish` agent in mode `reader`, per Phase 1 of
   `.claude/skills/goldfish/SKILL.md`. Use this audience: "a new Claude session
   that resumes this work with no memory of this conversation".
3. Triage the findings per `.claude/skills/goldfish/references/triage.md`.
   Apply every FIX to the draft. Put every ESCALATE item under
   `## Open issues`.
4. Add one line at the end of `## Current state`:
   `Cold read: <n> findings, <f> fixed, <e> escalated.`
5. Write the draft: `.venv/bin/python scripts/checkpoint-paths.py --write [--kind K] < <draft file>`.

`/second-wind` and `/clear-handover` run items 1 to 4 as their Step 0, before
the start stamp. The reason: the agent replies only after a turn ends. Their
Stop hook checks the archive at the first turn end after the stamp.

A missing cold read never blocks the write. If the agent type is not loaded,
or the reply is `INCOMPLETE`, write `Cold read: not run (<reason>)` and
continue to step 5. Run no re-review here.

### Step 3 - Respond to the user

Reply with:

1. Full path of the archive file written (the script's `wrote` line)
2. Confirmation that the script updated this session's pointer pair and the shared pair
3. One-line current state summary
4. Recommendation: "Run `/second-wind` if you want to free context now. Otherwise nothing else happens - the checkpoint stays on disk for resume."

Do NOT continue implementation, do NOT call `/compact`, do NOT clear the session. Wait for the user's next instruction.

## Freeing the context: the menu and /second-wind

The Stop hook shows one menu at the first threshold (45%) and a last call at the
second (55%), each once. The menu offers `/second-wind` and "Continue as is".
`/second-wind` writes a handoff like this skill does. The hook then sends
`/clear` when nothing runs in the background. It sends `/compact` when `/loop`,
cron, `/goal`, background agents or monitors run. Claude Code compacts by itself near
60% (`CLAUDE_CODE_AUTO_COMPACT_WINDOW` 635000). Spec:
`2026-10-14-second-wind-design/spec.md` under the registry name `drafts`.

Read `mode-mechanics.md` in the data repository at
`$(.venv/bin/python scripts/data-path.py path skill-design-references skill=checkpoint)/mode-mechanics.md`.
The directory keeps the skill's former name.
A public clone does not have it, and this file is then the whole account.

## Unattended mode

Unattended mode decides what happens when the session pauses and nobody answers.
It is OFF by default in HELM and ON by default in a yard, whose agent starts
with `HEADING_OS_YARD=1` (operator decision 2026-10-14). The switch and its
record survive `/clear`.

```bash
.venv/bin/python scripts/checkpoint-paths.py --unattended on
```

The slash command `/unattended on|off|status` is a shorter route to the same
switch. It runs the command above and nothing else.

At every pause, at any context level, the Stop hook then waits instead of asking. The wait
shows a countdown in the HERDR label, so a still terminal is visibly a wait and
not a hang. Type anything inside it, and the turn goes back to you within one
poll. Stay silent for the whole wait, and the hook tells the assistant to carry
on.

Use it for work that runs past the time you are at the keyboard: overnight, or
across a weekend.

At the threshold menu the same wait applies. The menu stays on screen, and the
countdown runs in the HERDR label. Stay silent, and the assistant runs
`/second-wind` by itself; the hook then sends `/clear` or `/compact`. After
`/clear` the new session continues from the handoff's `## Safe resume` block.
Outside HERDR nothing can be sent, so the work continues and Claude Code's own
compaction frees the context near 60%.

The switch belongs to the operator. The assistant never lowers it.

The status line names its state on every render: `⏵ unattended` or
`⏸  manual`. Read it there. Turn the mode off yourself when you want the window
back:

```bash
.venv/bin/python scripts/checkpoint-paths.py --unattended off
```

**Nothing lowers the switch except you.** The bounds below stop a stretch. They
hand the turn back and leave the mode on, so your next instruction resumes it.

### How a night ends

The assistant declares the plan finished. This is the primary signal, and it is
explicit. Run this at the end of the work:

```bash
.venv/bin/python scripts/checkpoint-paths.py --done "plan X: 7 of 7 items"
```

The Stop hook reads session state. It never reads prose. So an assistant that
writes "the work is finished" and stops has told the mechanism nothing, and the
next pause continues it again. The continuation message names this command at
every pause for that reason.

`--done` also prints the session's `Unattended:` line, for example
`Unattended: continued on its own 12 time(s), ...; last stop: ...`. Code writes
it from session state, and every handoff header carries it too. Copy it
verbatim into the final report and, in a yard, into the closing Lific comment
(operator decision 2026-10-14).

Your next instruction clears the marker on its own. You do not run a command to
resume.

A stuck run ends on its own. Three continued turns in a row with no tool call
stop the stretch, because a turn that only writes text has moved nothing.

The ceiling is the backstop, for the case where the marker never arrives. It
stops a stretch after 100 continuations. Measure one night before you move that
number with `CLAUDE_HANDOFF_UNATTENDED_MAX`.

**A stopped stretch is silent by design.** The hook records why it stopped, and
when, in the session state. The ceiling stop and the no-progress stop also send one
Telegram notice, when you configured a target; the done marker sends none. Read that state with
`--unattended status`.

The plain continuation stays quiet whenever something else already drives the
Stop event. Three signals claim it. A scheduled `/loop` wakeup claims it.
In-flight background work claims it. A ralph-loop that names this session
claims it. The threshold menu still shows, and names what runs.

`/second-wind` reads `/goal` from this session's transcript: a typed `/goal`
command with a condition, not cleared since. An active goal makes it send
`/compact`, because `/clear` removes the goal.

Environment defaults, for the whole workspace rather than one session:

```json
"env": {
  "CLAUDE_HANDOFF_UNATTENDED": "1",
  "CLAUDE_HANDOFF_UNATTENDED_WAIT": "10",
  "CLAUDE_HANDOFF_UNATTENDED_POLL": "2",
  "CLAUDE_HANDOFF_UNATTENDED_MAX": "100"
}
```

**A wait outside 1 to 60 seconds is ignored, and you get 60.** This is not a
clamp. `env_int` returns the DEFAULT on any invalid value, so `WAIT=600` gives
60. `WAIT=0` also gives 60, not the nothing you asked for. Measured
2026-08-20. Raise the hook's own registered timeout first if you need a longer
grace period. Read `mode-mechanics.md` for the cost of a long wait.

**The wait can also be SHORTER than you set.** The hook bounds the grace period
against its own registered timeout, so a Stop that already spent time on other
work grants less. The continuation prints the number it actually gave you.

## NEVER

- Never invoke `/compact` automatically as part of this skill
- Never clear context as part of this skill
- Never proceed with the next task unless the user explicitly says so after seeing the checkpoint report
- Never write the archive or a pointer file by hand - `--write` writes all five, and a hand-written pointer can disagree with its archive
- Never write into another session's pointer directory
