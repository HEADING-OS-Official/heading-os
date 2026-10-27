---
name: clear-handover
description: "Clear by HEADING OS: save a full handoff of this session, then start clean. The model writes the handoff exactly as /handover does; the Stop hook checks it on disk and only then sends /clear through HERDR. The next session receives the handoff and the master prompt. If /loop, cron, /goal, a background agent or a monitor is seen running, it sends nothing and says so, because /clear would stop them; /second-wind keeps them running. Typed by the operator only (disable-model-invocation). For a save without clearing use /handover."
disable-model-invocation: true
allowed-tools: "Read, Bash(date:*), Bash(.venv/bin/python scripts/checkpoint-paths.py:*)"
argument-hint: "[note]"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.3"
x-heading-capability:
  what: >
    Saves a full handoff of this session and then clears the context. The
    next session starts with the handoff and the master prompt injected once.
  how: >
    Type /clear-handover [optional note]. The skill hands the handoff text to
    one command, which writes the archive and its pointers. When the turn ends, the Stop hook verifies the files on disk and
    sends /clear through HERDR. Without HERDR it says "handover verified, now
    type /clear".
  when: >
    Use when the task has changed or the old conversation is no longer needed.
    To save without clearing use /handover; to free context without stopping
    background work use /second-wind.
x-heading-routing:
  category: Operations
  label: /clear-handover [note]
  triggers:
    - NEVER auto-trigger. Explicit `/clear-handover [optional note]` only. Saves a full handoff, then the Stop hook verifies it on disk and sends `/clear` through HERDR; the next session receives the handoff and the master prompt once.
  exclusions:
    - save without clearing -> /handover
    - free context and keep background work running -> /second-wind
  compound: 'No'
  router: manual
---


# /clear-handover

Save a full handoff of this session, then clear the context. "Clear by HEADING OS".

The name cannot be `/clear`: that is a built-in command, and a built-in keeps
its name. A skill also cannot type `/clear` itself, because built-in commands
run only from the terminal's input. So this skill does the writing, and the Stop
hook `.claude/hooks/checkpoint-offer.py` does the clearing, after it has checked
the handoff on disk.

## Procedure

### Step 0 - Cold read, before the start stamp

Run the cold read now: items 1 to 4 of Step 2 of
`.claude/skills/handover/SKILL.md`. Write the draft and dispatch the `goldfish`
agent. Then triage its reply and apply every FIX to the draft. The agent runs
in the background, and its reply arrives only after a turn ends. So this step
may take a turn of its own.

Do it before Step 1, never after it. Step 1 records the start, and the Stop hook
checks the archive at the first turn end after that start, once. MEASURED
2026-10-27: a run dispatched the agent after Step 1 and ended its turn to wait.
The hook found no archive, 94 s before the write, and consumed the request.

### Step 1 - Get this session's paths, in clear mode

```bash
.venv/bin/python scripts/checkpoint-paths.py --kind clear
```

`--kind clear` names the archive `_handoff_clear_` and records the moment this
run started. The Stop hook accepts only a handoff written after that moment.
Use every path exactly as printed.

### Step 2 - Write the handoff with one command

Follow Step 2 of `.claude/skills/handover/SKILL.md` exactly, with `--kind
clear`, and pass the draft from Step 0 on stdin; do not run the cold read
again. Run Steps 1, 2 and 3 in ONE turn, with no turn end between them. Read `.claude/skills/handover/references/handoff-template.md` first. Use
the title `# Handoff - clear-handover` and `Trigger: clear-handover`. The next
session has nothing else, so fill every section, and run the template's
self-check: every operator message has its line under `## Operator messages`.
If `$ARGUMENTS` is not empty, put it under `## User note`.

Add one more section, `## Resume`, with two or three lines. Write them in the
language the operator writes to you. The SessionStart hook shows them ON SCREEN
the moment `/clear` finishes, before he types anything. No model turn runs then,
so these lines are all he sees until he writes. Open with the words you would
use to continue. When he writes Russian, that is "Продолжаю с того места, где
остановились: ...". Then say where the work stopped, then the next step. Do not
write paths: the hook adds the handoff path itself. The hook shows only the
first three non-blank lines. Never put a pinned item here; pins travel verbatim
in their own section.

```bash
.venv/bin/python scripts/checkpoint-paths.py --write --kind clear <<'HANDOFF'
# Handoff - clear-handover
...the sections from the template...

## Resume

Продолжаю с того места, где остановились: <the objective, in one clause>.
Остановились: <where the work stopped>.
Дальше: <the next step>.
HANDOFF
```

If `compacted` starts with `yes`, say in `Current state` that this session was
compacted before this handoff. The script adds the master line, the
continuation prompt and the prompt list. It copies this session's pinned items
(`/pin`), which the next session receives too, and writes all four pointers. A
non-zero exit names what is wrong. Fix it, then run the command again. It
prints `Resume block: N line(s)`. A note on stderr instead means the
`## Resume` section is missing. The screen after `/clear` then shows the first
line of Objective, Current state and Next steps. Add the section and run the
command again.

### Step 3 - End the turn

Reply with ONE line: the full path of the archive. Then stop. Do not start any
other work: the Stop hook runs when this turn ends, checks the files, and sends
`/clear`.

The hook reports the outcome itself. If it says `/clear NOT sent`, nothing was
lost. It names what is missing on disk, or the `/loop`, `/goal`, agent or
monitor that `/clear` would stop. In the second case type `/clear` to proceed
anyway, or `/second-wind` to keep the work running. The handoff needs a
`## Safe resume` block; the template has it. If it says "handover verified,
now type /clear", this session is not reachable through HERDR, and the operator
types `/clear` by hand. The next session receives the handoff either way.

## NEVER

- Never type or request `/clear` or `/compact` yourself
- Never write the archive, a pointer or the `Master instruction:` line yourself; `--write` writes them
- Never write into another session's pointer directory
- Never end the turn between Step 1 and the write in Step 2
- Never continue other work after Step 3
