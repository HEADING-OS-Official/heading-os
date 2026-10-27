# Prompt refinement — the two explicit escalations

Consumed by: `.claude/rules/prompt-refinement.md`.

Last Updated: 2026-09-04

The three-phase flow in the always-on rule is always active and leaves "when to
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
