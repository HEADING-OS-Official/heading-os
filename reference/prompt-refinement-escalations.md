# Prompt refinement — the two explicit escalations, and Phase 3 in full

Consumed by: `.claude/rules/prompt-refinement.md`.

Last Updated: 2026-10-29

The two explicit escalations, and the full reasoning behind the Phase 3 shape
test, which the always-on rule states in short. The three-phase flow in the always-on rule is always active and leaves "when to
clarify" to Claude's judgement. These two commands override that default. Each
is typed by the operator, which loads the skill that implements it, so their
descriptions do not need to be resident — the rule names them in one line and
points here.

## `/devil N` — explicit contrarian critique

The default posture is to validate and proceed. When the operator wants the
opposite — explicit contrarian critique of a recent decision or claim — they
invoke `/devil N` (default N=5, range 1-10).

`/devil` produces N severity-tagged critique points from distinct angles
(correctness, scope, cost, timing, alternatives, second-order effects), exits,
and lets the operator reply with point numbers or move on freely. Honesty floor:
if fewer than N defensible angles exist, the skill stops early rather than
fabricate. See `.claude/skills/devil/SKILL.md`.

## `/burst N` — the same content, N ways

When the operator wants the same content delivered N different ways — to compare
directions, escape a stuck draft, or run the convergence pattern (produce N
variants, pick one, `/burst` again from there) — they invoke `/burst N` (default
N=3, range 2-5).

`/burst` produces N variants of the latest assistant-produced content artifact:
N-1 spread variants attacking distinct axes (opener, tone, structure, lens,
length, voice, metaphor) plus one mandatory "swing-the-other-way" variant
inverting a defining property of the original. See
`.claude/skills/burst/SKILL.md`.

## Interaction with the corporate-docs guardrail

The corporate-docs guardrail (`.claude/rules/corporate-docs.md`) requires
immediate skill announcement when a request matches one of the five locked
doctypes (letter, proposal, partnership-doc, official-doc, xpager). The two rules
reconcile as follows:

- The skill announcement happens **inside** Phase 1, not before it. Open with the
  announcement, then present the expanded prompt.
- Example: `Using /proposal (commercial proposal template, locked typography, GT
  Standard, company letterhead). It looks like you want me to do the following: ...`
- Phase 3's approval gate still applies. Do not start drafting until the operator
  approves the expanded prompt.
- Escape Valve 1 (the `!` prefix) bypasses both rules and lets the skill execute
  directly.

## Phase 3 in full: which prompts are saved

Read this when it is unclear whether a message fixes or changes the shape of a task. The paragraphs below stood in the always-on rule until 2026-10-29, verbatim except that the operator's quoted words moved to the provenance file in the data repository.

A handoff written later asks `prompt-archive.py list` and carries the ordered paths; nothing is registered at approval time, because a prompt approved after the last handoff would otherwise be the one that is lost.

**Which prompts are saved, and the test is the SHAPE OF THE TASK rather than the gate a message passed.** Operator decision, 2026-09-22: a path named out loud survives the compaction that eats the turn it came from. A file is written when a message FIXES the shape of a task, and again when a later message CHANGES that shape. Nothing is written when a message only moves a task whose shape is already on disk.

**The 2026-09-20 decision stands and this is where it still bites.** An archive of every keystroke is a place nobody looks. The line moved from the approval gate to the task, which is a wider set and still a small one: an evening of forty messages carries perhaps six tasks, and the test above writes about six files.

**Where no approval stop happens, the file is written anyway, and still before the first action.** The escape valves, a direct command, a reply that arrives carrying new work, and `/unattended` all bypass the stop by construction, and any of them can fix the shape of a task. The order there is: expand, save, name the absolute path, then act. Nothing else about this phase changes, and the expansion is the master instruction exactly as it is above.

**The verbatim half holds the message that fixed the shape**, which is usually an EARLIER message than the one that released execution. When "go" approves an expansion presented a turn ago, the operator's words in the file are the task he typed, never the word "go".
