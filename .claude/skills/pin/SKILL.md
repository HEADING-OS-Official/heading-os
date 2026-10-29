---
name: pin
description: "Pin an item verbatim for this session, so it survives compaction and /clear: the operator writes \"закрепи: ...\" or types /pin <text>. The text is appended word for word to this session's pins file by scripts/pin.py, and from there it reaches every handoff and the injection after /clear, compact and resume. Do NOT use for \"запомни\" (that creates a Lific issue), for Odin's \"remember\", or for a durable cross-session fact (auto-memory)."
argument-hint: "<text to pin>"
allowed-tools: "Bash(.venv/bin/python scripts/pin.py:*)"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-capability:
  what: >
    Stores one item the operator pinned, verbatim, in this session's pins file,
    so compaction and /clear cannot lose or reword it.
  how: >
    Write "закрепи: <text>" or type /pin <text>. The skill runs scripts/pin.py,
    which writes the text under a lock and atomically. /pin with no text lists
    this session's pins.
  when: >
    Use for a constraint or a fact that must stay word for word until the work
    ends. For a task to do later use Lific ("запомни"); for a fact that outlives
    the work use auto-memory.
x-heading-routing:
  category: Operations
  label: /pin <text>
  triggers:
    - "закрепи: <text>"
    - "/pin <text>"
  exclusions:
    - "запомни -> Lific issue (.claude/rules/lific.md item 2)"
    - "Odin remember -> /odin"
    - "a fact that outlives the work -> auto-memory"
  router: auto
---

# /pin

Pin one item verbatim for this session. The item then reaches two places:

- every handoff this session writes (`/handover`, `/clear-handover`,
  `/second-wind` and the post-compact handoff);
- what the session start injects after `/clear`, compact and resume.

A pin lasts for the work. After `/clear` the next session inherits the pins.
For a task to do later, "запомни" creates a Lific issue. For a fact that
outlives the work, write an auto-memory record.

## Procedure

### Step 1 - Take the text exactly

The text is everything after `закрепи:` or after `/pin`, as the operator wrote
it. Do not reword, shorten, correct or translate it. Line breaks become spaces;
the script does that.

If there is no text, run `.venv/bin/python scripts/pin.py --list` and report its output.
Then stop.

### Step 2 - Write the pin

Pass the text through a quoted heredoc, never as a double-quoted argument. A
double-quoted argument lets the shell run every backtick span in it.

```bash
.venv/bin/python scripts/pin.py <<'PIN'
{the text, exactly as the operator wrote it}
PIN
```

### Step 3 - Report

Reply with the script's first line, which quotes the stored pin. If the script
refused, report its reason in one line and store nothing else. A refusal names
the fix: an item over 500 characters goes in shorter parts, and an item that
looks like a credential stays in `.env`.

## NEVER

- Never reword, shorten, correct or translate a pin
- Never write the pins file yourself; `scripts/pin.py` is the only writer
- Never pin on "запомни": that phrase creates a Lific issue
- Never pin text the operator did not ask to pin
