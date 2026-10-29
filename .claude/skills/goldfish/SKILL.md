---
name: goldfish
description: >
  Zero-context review of one artifact. Dispatches the read-only `goldfish`
  agent, which starts without this conversation, without the instruction files
  and without memory, and reads only the artifact and a written standard.
  Modes: plan (a plan before approval), reader (would a stranger act on it
  correctly: a yard brief, a handoff, a report, a proposal or tender answer),
  ready (Zero-Decision Rule for a brief or an execution plan), proposal (the
  Heilmeier catechism for an initiative, investment or business decision),
  drift (a document's claims about the present against the code), code (a
  diff, blind to its intent). The author then triages each finding as FIX, CONTEST or
  ESCALATE. Use when the user says "goldfish", "fresh-eyes review", "cold
  read", "would a stranger understand this". Do NOT use
  for a principal review gate with refutation (use /scrutinize), for outside
  model opinions (use /council), or for structure edits to a draft (use
  /editorial-review).
argument-hint: "<plan|reader|ready|proposal|drift|code> <path or 'this'> [--as <audience>] [--context <path>,...]"
allowed-tools: "Read, Write, Glob, Grep, Agent, Bash(.venv/bin/python:*), Bash(git:*)"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-capability:
  what: >
    Sends one artifact to a reviewer that has never seen the conversation that produced it, against a written standard, and returns its findings with the author's FIX, CONTEST or ESCALATE triage beside them.
  how: >
    Run /goldfish <mode> <path>, or name "this" for the draft just written. The report saves under scrutiny-sheets; ESCALATE items come first in chat.
  when: >
    Use before sending a yard brief, before approving a plan, on a proposal or tender answer, on a new initiative (mode proposal), on a handoff, or on a rule whose enforcement claims may have drifted. For the full review gate use /scrutinize; for other model families use /council.
x-heading-routing:
  category: Operations
  label: /goldfish <mode> <path>
  triggers:
    - goldfish review
    - fresh-eyes review
    - cold read this
    - would a stranger understand this
  exclusions:
    - full review gate with refutation -> /scrutinize
    - outside model opinion -> /council
    - restructure a draft -> /editorial-review
  router: auto
---

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Resolve the name before the file operation and
use what it prints:

    .venv/bin/python scripts/data-path.py path <name>

# /goldfish - zero-context review

A reviewer who watched the author work repeats the author's reasoning and
misses the gap between intent and result. The `goldfish` agent starts with
none of it. It has no conversation, no `CLAUDE.md` chain, no memory and no
write tools. The design comes from Google's Elephant-Goldfish Model. This
skill adapts its standards and its triage under Apache-2.0.

## Phase 0 - Resolve the artifact

1. Read the mode from the first argument. Ask for it when it is missing. The
   modes and their standards are in `references/standards.md`.
2. Resolve the artifact to a FILE. The goldfish reads files only.
   - A path: use it as an absolute path.
   - `this`, or a text that exists only in the conversation: write it verbatim
     to `goldfish/<YYYY-MM-DD-HHMM>-<slug>.md` under the directory that
     `.venv/bin/python scripts/data-path.py scratch` prints.
   - Mode `code`: write `git diff $(git merge-base <base> HEAD)` to a file
     there, with the base the caller names; the default base is `main`. That
     diff runs from the branch point to the working tree, so committed, staged
     and unstaged changes are all in it. Then append each untracked file that
     `git ls-files --others --exclude-standard` lists, under a header naming
     it. A `main...HEAD` diff would leave every uncommitted change out, and the
     reviewer could pass a change it never saw.
3. Resolve the standard path: `$(git rev-parse --show-toplevel)/.claude/skills/goldfish/references/standards.md`.
4. Collect the context files: only the ones the user named with `--context`.
   Never add files on your own to "help" the reviewer.

## Phase 1 - Dispatch

Dispatch ONE new agent of type `goldfish` with the Agent tool. Never fork,
never resume an earlier goldfish, never use another agent type. Pass this task
and nothing more:

    Mode: <mode>
    Standard: <absolute standard path>, section "## <mode>"
    Artifact: <absolute artifact path>
    Context files: <absolute paths, or "none">
    Audience: <the --as text, or "none">

**NEVER put any of these in the task:** a summary of the conversation, the
author's reasoning, the intended result, or the findings you expect. Each of
them turns the goldfish back into an elephant.

If the agent type `goldfish` is not available, stop. Say: "The goldfish agent
is not loaded in this session. Start a new session, because a review in this
context is not a goldfish review." The likely cause is a session that started
before `.claude/agents/` existed. Never fall back to a general-purpose agent.

If the reply is `INCOMPLETE: ...`, report that line and stop. That run is not a
review.

## Phase 2 - Triage and report

1. Read `references/triage.md`. Give every finding exactly one category: FIX,
   CONTEST or ESCALATE.
2. Compare the UNDERSTOOD line with what the artifact was meant to say. A
   mismatch is a finding of its own, at the top of the report.
3. Write the report to `YYYY-MM-DD-goldfish-<mode>-<slug>.md` under
   `scrutiny-sheets`. Use four parts: the task you sent, the goldfish reply
   verbatim, the triage table, and the changes made.
4. Apply the FIX items when the artifact is this session's own draft: a brief,
   a plan, a handoff, a report. For any other artifact, list the changes and
   wait for the operator's approval.
5. After BLOCKER or HIGH fixes, run one fresh re-review per `references/triage.md`.
6. Mode `ready` on a yard brief: when the brief is final, run
   `.venv/bin/python scripts/herdr-brief.py --check x "$(cat <brief file>)"`.
   Copy the `Brief-SHA256: <hex>` line it prints into the report, on its own
   line. `herdr-brief.py` refuses a brief whose exact text has no such line
   (HOS-267).

## Phase 3 - Output

In chat, list the ESCALATE items first, each with the question the operator
must answer. Then give one line with the counts per category and the verdict.
Give the report path, the Linux one and the one from `wslpath -w`. Do not paste
the whole report.

## Voice

Write findings as statements. Use hyphens, never double dashes. Follow
`.claude/rules/terminology.md` in prose that reaches the operator.

## NEVER

- Never pass conversation content, author reasoning or expectations to the goldfish.
- Never run the review in this context, and never with a general-purpose agent.
- Never give the goldfish write tools, Bash, or a memory.
- Never mark a finding DECLINE or "fix later". Triage has three categories.
- Never contest a finding without evidence the goldfish could have read.
- Never edit an artifact that is not this session's own draft without approval.
- Never run more than one re-review per artifact.
