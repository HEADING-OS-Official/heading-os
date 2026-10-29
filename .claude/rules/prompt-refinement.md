<!-- version: 1.1.0 | last-updated: 2026-10-29 -->
# Prompt Refinement Protocol

Last Updated: 2026-10-29
Last Verified: 2026-05-13

Always-active rule: when Claude expands and confirms a request before acting, and when it acts at once.

## When the flow runs

Operator decision, 2026-09-27.

Run the three phases, approval STOP included, when the request is ambiguous, multi-step, irreversible, or changes the shape of a task. Execute a direct, unambiguous request and answer an informational question at once, with no expansion and no stop. Either way the Phase 3 shape test decides whether a prompt file is written, and where it says yes, the file is written before the first action. This agrees with the clarify rule in `.claude/rules/voice.md`.

## Three-Phase Flow

### Phase 1 - Interpret & Expand

Restate the request as a fully expanded, execution-ready prompt, opening with:

> "It looks like you want me to do the following:"

- Clear objective
- Concrete scope (what's in, what's out)
- Deliverables and file outputs
- Constraints, tone, and quality bar
- Assumptions, flagged explicitly
- Open questions: what is still undecided and would change the result, each answerable at the approval stop; only when there are some. An assumption is a gap you chose to fill; an open question is a gap you hand back.
- Affected systems and people: only when the task touches something live, another repository, or another person
- The measurable-execution block last, per `measurable-execution.md` (metric always; `/loop` and `/goal` proposals only on signal)

### Phase 2 - Clarify (only if needed)

If ambiguity blocks a confident expansion, ask focused questions BEFORE presenting it, only what is necessary. Do not invent detail; flag gaps.

### Phase 3 - Save The Prompt, Then Execute

Where the flow reached an approval stop, STOP. Do not execute. Wait for explicit approval: "approved", "proceed", "go", "execute", or "yes".

Then, and BEFORE the first action, save the prompt:

```bash
python scripts/prompt-archive.py save --slug <short-kebab-name> \
    --original <file with his words verbatim> --expanded <file with the expansion>
```

Then say, in your own words, that the file is now the master instruction, and name its full path. Execute from that file; where it and your memory of the turn disagree, the file wins. Every further prompt the operator gives during the same execution is saved the same way, in sequence. Execute strictly against the approved prompt: if new decisions arise mid-execution, stop and ask rather than expand scope.

**Which prompts are saved: the SHAPE OF THE TASK decides, not the gate a message passed.** Operator decision 2026-09-22. A file is written when a message FIXES the shape of a task, and again when a later message CHANGES it; nothing when a message only moves a task whose shape is already on disk.

Shape is four fields, the same four Phase 1 expands: objective, scope, deliverables, acceptance. Ask once per message:

> Draft the expansion this message deserves. Does it differ, in any of those four fields, from the newest expansion already archived for this session?

Nothing archived yet: write the file. It differs in one of the four: write the next file in sequence. It differs only in how far the work has got: write nothing.

So nothing is written for "yes", "go", "спасибо" or "ага"; a reply that only picks from options you listed; a one-step correction to work just produced; a request to show or report on what exists; or a session switch such as `/unattended on`, which changes how the session runs, not what it does. A file IS written for "commit and push" or "merge that branch and delete the yard" whenever the archived expansion stopped short of them, because they add a deliverable no file carries.

**Where no approval stop happens, the file is still written before the first action:** expand, save, name the absolute path, then act.

**The verbatim half holds the message that fixed the shape**, usually an EARLIER message than the one that released execution, never the word "go".

Full reasoning behind the test: § Phase 3 of `reference/prompt-refinement-escalations.md`. Rationale, file format and location: the script's own docstring.

### Explicit escalations

Two operator-typed commands override the default judgement; each loads the skill defining it, so only their names are resident. What each does to the three phases: `reference/prompt-refinement-escalations.md`.

- `/devil N`: N severity-tagged contrarian critique points (default 5, 1-10).
- `/burst N`: the same content delivered N ways (default 3, range 2-5).

## Escape Valves

Even when a message meets one of the four criteria above, skip the stop and act directly when:

1. The user prefixes the message with `!` (e.g., `!fix this typo`)
2. The message is a direct reply to a question YOU asked
3. The message is a trivial one-step correction to work just produced (a typo, a rename, a single-line tweak)

When unsure whether a request is ambiguous, ask whether two reasonable readings of it would produce different work: if so, expand and stop; if not, act.

**A valve skips the STOP, never the file.** Valves 2 and 3 name messages that change none of the four fields, so Phase 3 writes nothing for them anyway. Valve 1 differs: `!` is how the operator opens a task fast, so a `!` message that fixes the shape of one is expanded in a sentence, saved, and its path named, before the first action.

## Output Discipline

Keep the expanded prompt tight, in plain prose and short lists: no padding, corporate language, hedging, preamble beyond the required opener, or ceremonial formatting.

## Interaction with Corporate-Docs Guardrail

The immediate skill announcement `.claude/rules/corporate-docs.md` requires for a locked doctype happens **inside** Phase 1: announce, then present the expanded prompt. Phase 3's approval gate still applies, and Escape Valve 1 bypasses both rules. Worked example: `reference/prompt-refinement-escalations.md` § Interaction with the corporate-docs guardrail.
