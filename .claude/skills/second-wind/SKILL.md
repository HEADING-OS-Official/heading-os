---
name: second-wind
description: "Save a full handoff of this session and free the context. The Stop hook checks the handoff on disk and then sends /clear through HERDR when nothing runs in the background, or /compact when /loop, cron, /goal, background agents or monitors run, so they keep running. Typed by the operator, picked from the threshold menu, or started by the Stop hook in unattended mode. For an always-clear reset use /clear-handover; for a save without freeing context use /handover."
disable-model-invocation: false
allowed-tools: "Read, Bash(date:*), Bash(.venv/bin/python scripts/checkpoint-paths.py:*)"
argument-hint: "[note]"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-capability:
  what: >
    Saves a full handoff and frees the context without stopping background
    work: /clear when nothing runs in the background, /compact otherwise.
  how: >
    Type /second-wind [note], or pick it from the threshold menu. The skill
    writes the handoff with one command; the Stop hook verifies it and sends
    the command through HERDR.
  when: >
    At the 45% or 55% menu, or whenever the context feels heavy. For an
    always-clear reset use /clear-handover; for a save only use /handover.
x-heading-routing:
  category: Operations
  label: /second-wind [note]
  triggers:
    - NEVER auto-trigger from a message. Explicit `/second-wind [note]`, the threshold menu, or the Stop hook's unattended continuation.
  exclusions:
    - always clear -> /clear-handover
    - save without freeing context -> /handover
  compound: 'No'
  router: manual
---

# /second-wind

Save a full handoff, then free the context. The Stop hook
`.claude/hooks/checkpoint-offer.py` decides HOW, at the moment it runs, never
at the moment the menu was shown:

- nothing in the background: `/clear`, and a new session reads the handoff;
- `/loop`, cron, `/goal`, background agents or monitors running, or a state the
  hook cannot read: `/compact`, and the same session reads the handoff.
  `/compact` keeps them running; `/clear` would stop them.

A skill cannot type a built-in command. So this skill does the writing, and the
hook does the sending after it has checked the handoff on disk. The design and
its measurements: the spec `2026-10-14-second-wind-design/spec.md` under the
registry name `drafts`.

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

### Step 1 - Get this session's paths, in second-wind mode

```bash
.venv/bin/python scripts/checkpoint-paths.py --kind second-wind
```

`--kind second-wind` names the archive `_handoff_second-wind_` and records the
moment this run started. The Stop hook accepts only a handoff written after
that moment. Use every path exactly as printed.

### Step 2 - Write the handoff with one command

Follow Step 2 of `.claude/skills/handover/SKILL.md` exactly, with `--kind
second-wind`, and pass the draft from Step 0 on stdin; do not run the cold read
again. Run Steps 1, 2 and 3 in ONE turn, with no turn end between them. Read `.claude/skills/handover/references/handoff-template.md`
first. Use the title `# Handoff - second-wind` and `Trigger: second-wind`. The
next session may have nothing else, so fill every section, and run the
template's self-check. If `$ARGUMENTS` is not empty, put it under `## User note`.

Two sections are required here and the hook refuses to send anything without
the first:

- `## Safe resume`: the three lines the template gives, `none` where a line has
  nothing. List open tasks from the task list under `Next action` or `## Next
  steps`, because `/clear` loses the task list.
- `## Resume`: two or three lines in the operator's language, shown on screen
  after `/clear` before he types anything. When he writes Russian, open with
  "Продолжаю с того места, где остановились: ...". No paths, no pinned items.

```bash
.venv/bin/python scripts/checkpoint-paths.py --write --kind second-wind <<'HANDOFF'
# Handoff - second-wind
...the sections from the template, Safe resume included...

## Resume

Продолжаю с того места, где остановились: <the objective, in one clause>.
Остановились: <where the work stopped>.
Дальше: <the next step>.
HANDOFF
```

A non-zero exit names what is wrong; fix it and run the command again. A
stderr line saying "the Stop hook will refuse to reset" names what the
`## Safe resume` block lacks; add it and run the command again.

### Step 3 - End the turn

Reply with ONE line: the full path of the archive. Then stop. The Stop hook
runs when this turn ends, checks the files and the background, sends `/clear`
or `/compact`, and says which and why. If it says `NOT sent`, it names what is
missing or what changed, and nothing was lost. If it says "now type /clear" or
"now type /compact", this session is not reachable through HERDR and the
operator types it.

## NEVER

- Never type or request `/clear` or `/compact` yourself
- Never write the archive, a pointer or a header line by hand; `--write` writes them
- Never write into another session's pointer directory
- Never end the turn between Step 1 and the write in Step 2
- Never continue other work after Step 3
