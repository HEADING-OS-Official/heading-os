---
name: thread
description: "Open, log, close, find threads in the operational threads registry. Triggers on 'open a thread', 'log to the [name] thread', 'close the [name] thread', 'thread list', 'thread find'. NOT for: single emails (use /email-draft), notes (use /zk), CRM logs (use /crm)."
argument-hint: "<command> [args]"
allowed-tools: "Read, Write, Edit, Bash(.venv/bin/python:*), Glob, Grep"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-capability:
  what: >
    Maintains the operational threads registry - the running state of business
    and personal situations across sessions, with decisions, follow-ups, and
    linked artifacts. Distinct from CRM (people), pipeline (deals), ZK (ideas).
  how: >
    Run /thread <command> [args] - open, log, close, hold, reopen, list, find,
    show. Drives scripts/thread.py writing under threads; always asks before
    opening or logging.
  when: >
    Use for a multi-step situation that follows through across sessions. For a
    single email use /email-draft; for a note use /zk; for a contact log use
    /crm.
x-heading-routing:
  category: Operations
  triggers:
    - open a thread
    - log to thread
    - close thread
    - hold thread
    - reopen
    - thread list
    - thread find
    - what threads are active
  exclusions:
    - Single email -> /email-draft
    - knowledge note -> /zk
    - CRM log -> /crm
  compound: 'No'
  router: auto
---

# /thread - Operational Threads Registry

Manages running state of business and personal life situations across sessions. Distinct from CRM (people), pipeline (deals), ZK (ideas), outputs (artifacts).

**Spec:** `superpowers/specs/2026-04-29-threads-registry-design.md` under `spec-docs` in the data repository (resolve with `.venv/bin/python scripts/data-path.py path spec-docs`)

## When to use

| Trigger | Action |
|---|---|
| Multi-step external situation that has follow-through across sessions | `open` |
| Recurring project (a book, a product partnership, a recurring program) | `open` |
| Personal life thread (medical, family, travel) | `open` with type `personal` |
| Outbound communication on an existing thread | `log` with `--artifact` |
| Decision made on an active thread | `log --decision "..."` |
| Pending action surfaced | `log --follow-up "..."` |
| Follow-up completed | `log --done <index>` |
| Thread done | `close --reason "<why>"` |
| Thread paused for weeks | `hold --reason "<why>"` |
| Thread reactivates | `reopen` |
| Operator says "do not remind me about this until <date>" | `quiet --until <date>` |
| Operator says "freeze this, I will raise it myself" | `quiet --indefinite` |
| Quiet period is over | `quiet --clear` |
| Survey active threads | `list` |
| Lookup by keyword | `find <query>` |
| Read full thread | `show <id>` |
| /prime archive scan | `archive-scan --apply` |

## When NOT to use

- Code work, debugging, technical fixes - explicitly out of scope.
- Single emails to new contacts - use `/email-draft`.
- Knowledge notes / insights - use `/zk`.
- CRM contact logs - use `/crm`.

## Approval gate (always)

I never open or log silently. Before invoking the CLI, I ask:

> "This looks like a thread, or part of the [Quillon] thread. Open it, or log to it?"

The user approves, modifies, or skips. After several months of trusted use, this gate may be relaxed - deferred to v2.

## CLI

```bash
.venv/bin/python scripts/thread.py open <business|personal> "<title>" [--account <slug> [--new-account]]
.venv/bin/python scripts/thread.py log <thread-id> "<event>" [--artifact PATH ...] [--decision TEXT ...] [--follow-up TEXT ...] [--done INDEX]
.venv/bin/python scripts/thread.py close <thread-id> --reason "<why it is retired>"
.venv/bin/python scripts/thread.py hold <thread-id> --reason "<why it is retired>"
.venv/bin/python scripts/thread.py reopen <thread-id>
.venv/bin/python scripts/thread.py list [--type business|personal] [--status active|on-hold|closed]
.venv/bin/python scripts/thread.py find "<query>"
.venv/bin/python scripts/thread.py show <thread-id>
.venv/bin/python scripts/thread.py quiet <thread-id> [--until YYYY-MM-DD | --indefinite | --clear]
.venv/bin/python scripts/thread.py archive-scan [--apply]
```

## Where a new thread goes

- A thread about one account: `open business "<title>" --account <slug>`, which
  writes it under that account's `account-threads`. The slugs are
  `.venv/bin/python scripts/data-path.py accounts`; a slug that is not there refuses
  unless `--new-account` says the account is new.
- Any other business thread: `open business "<title>"`, under `threads-business`.
- A personal thread: `open personal "<title>"`. It never goes under an account,
  and `--account` with `personal` refuses.

`list`, `find`, `show`, `log` and `archive-scan` read every place a thread sits:
the registry group `business-threads` (`threads-business` and every account's
threads) and the personal threads.

## Quiet periods

A quiet thread is one the operator has asked me not to raise. I never surface it
proactively. It stays out of session-opener rollups, `/next`, `/dashboard`,
`/weekly-review`, ad-hoc "what is open" answers, and any nudge. I answer about it
only when the operator raises it themselves.

Two forms. `--until <date>` is a dated pause and it expires on its own.
`--indefinite` has no end date. It lifts only when the operator raises the
subject. Both forms write the state into the thread's frontmatter.

`list` marks a quiet thread. A dated pause prints `[quiet until <date>]`. An
indefinite freeze prints `[quiet indefinitely]`. Read that suffix before you
raise a thread with the operator.

## Where the record lives

The thread file is the record. There is no second copy.

No copy lives in `MEMORY.md`: a live status and date in an always-loaded index drift, which `.claude/rules/memory-discipline.md` forbids.

Run `list` to see the live set. Run `show <id>` to read one thread.

The `session-start.py` hook prints an active-threads panel at every session
start. It computes the panel from the thread files, so it cannot go stale. It
shows the threads touched in the last 14 days, up to 12 rows, and it names every
thread it left out. A quiet thread never appears in it.

`archive-scan` reports a dated quiet period once it expires. It never proposes
on-hold for a thread that is still quiet.

## Personal-thread rule

I never reference content from the personal layer of `threads` in any output destined outside the workspace - emails, LinkedIn posts, proposals, team messages, anything. Personal-thread context informs my work for the operator only.

The personal-thread PreToolUse guard is `check_protect_personal_threads` in
`.claude/hooks/_dispatch.py`. The guard plus `.gitignore` plus classification + path-filter belts make four independent
enforcement layers; behavioural compliance from me is the fifth.

The guard's match patterns are `PERSONAL_DIR_RE` and `PERSONAL_ARCHIVE_RE` in
`scripts/utils/personal_paths.py`, plus the home it resolves by the registry name
`threads-personal`.

## Auto-trigger heuristic

### Propose OPENING when:
- A second-or-later round of an external email exchange likely to span sessions.
- An external situation gets named with no current home (registrar, vendor dispute, legal, medical, family).
- A recurring-project decision is being made.
- The user says "track this" or "log this" about an external situation. "Remember this" is a memory (an `auto-memory` file), and "don't forget to do this" is a Lific issue (`.claude/rules/lific.md`), not a thread.

### Propose LOGGING when:
- Outbound communication to a counterparty already in an active thread's `counterparties:`.
- A decision is made matching an active thread's title or tags.
- An artifact is saved to a path linked by an active thread.

### Skip:
- Code work, debugging, technical fixes.
- One-shot tasks.
