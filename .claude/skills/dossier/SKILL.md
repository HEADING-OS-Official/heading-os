---
name: dossier
description: >
  Assemble everything about one Lific issue in chat: the prompts, handoffs and yard briefs whose
  Task header names it, its [dossier] journal and comments in Lific, the commits in both
  repositories that mention it, and older files that mention it only as text. Read-only; prints
  paths, never full texts. Use when the user says "dossier HOS-N", "досье HOS-N", "show the dossier
  of HOS-N", "что по задаче HOS-N", or when a session or yard receives an issue id to work on and
  needs its history first. Do NOT use for the tracker board or close requests (that is /prime's
  Lific panel), for creating or moving issues (Lific itself, per .claude/rules/lific.md), or for a
  free-text memory search with no issue id (use /recall).
argument-hint: "HOS-N"
allowed-tools: "Read, Bash(.venv/bin/python:*)"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-capability:
  what: >
    The task dossier of one Lific issue: current state, what awaits approval, the chronology of
    prompts, handoffs, briefs and close records, the commits, and possibly related older files.
  how: >
    Run /dossier HOS-N. It runs scripts/lific-board.py dossier HOS-N, then opens the newest handoff
    and the newest prompt it lists. Writes nothing anywhere.
  when: >
    Use before resuming or reviewing work on a named issue. For the whole board use /prime; for a
    memory search without an issue id use /recall.
x-heading-routing:
  category: Operations
  triggers:
    - dossier HOS-
    - досье HOS-
    - show the dossier of
    - что по задаче HOS-
  exclusions:
    - board or close requests -> /prime
    - free-text search with no issue id -> /recall
  compound: 'No'
  router: auto
---
# Dossier (one Lific issue, every trace of it)

Everything about one issue, assembled from the files that are the original and the Lific journal that indexes them. Read-only: it writes nothing, in Lific or anywhere else.

## Phase 0: the issue id

Take the id from the message (`HOS-9001` shape). With no id in the message, ask for it once; never guess one.

## Phase 1: assemble

    .venv/bin/python scripts/lific-board.py dossier HOS-9001

Add `--json` when another step consumes the result. The output starts with one header line (id, title, status), then `Now:` and `Awaiting approval:`. The chronology follows, one event per line with its data-root-relative path. Last come the count of full texts, the commits and the possibly related files.

The first line can read `Lific did not answer; journal missing`. Then tell the operator so in one sentence. The dossier rests on files and git only, and a gap in the chronology is not a gap in the work.

## Phase 2: read what matters

Open the newest handoff and the newest prompt the chronology lists, resolving each path against the data root the chronology heading names. Open older entries only when the question needs them. "Possibly related" files mention the id without a `Task:` header: treat them as leads, not as the task's record.

## Phase 3: answer

Lead with where the task stands and what waits for the operator, then the chronology in brief, with paths. Quote nothing in full that the operator did not ask for.

## Voice

Plain language, no em-dashes, the operator's language. Product names spelled as `.claude/rules/terminology.md` gives them.

## NEVER

- Never write to Lific, the files or git from this skill. A correction to a wrong task link is a `relink` or `unlink` record through `scripts/dossier-journal.py`, by a separate decision.
- Never paste the full text of a prompt, handoff or brief into Lific.
- Never present a "possibly related" file as part of the task without opening it.
