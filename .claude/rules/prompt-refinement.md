<!-- version: 1.0.0 | last-updated: 2026-04-28 -->
# Prompt Refinement Protocol

Last Updated: 2026-10-12
Last Verified: 2026-05-13

Always-active rule. Governs when Claude expands and confirms a request before acting, and when it acts at once.

## When the flow runs

Operator decision, 2026-09-27, in his words, translated: "stop only when a task is ambiguous, multi-step, irreversible or changes the shape of the task; carry out direct unambiguous requests and informational questions at once, saving the prompt file where the shape test requires it. Keep voice.md"

Run the three phases below, approval STOP included, when the request is ambiguous, multi-step, irreversible, or changes the shape of a task. Execute a direct, unambiguous request and answer an informational question at once, with no expansion and no stop. In both cases the Phase 3 shape test still decides whether a prompt file is written, and where it says yes, the file is written before the first action. This agrees with the clarify rule in `.claude/rules/voice.md`: ask when a gap would change the result, proceed directly when the task is plain.

## Three-Phase Flow

### Phase 1 - Interpret & Expand

Restate the request as a fully expanded, execution-ready prompt. Open with:

> "It looks like you want me to do the following:"

Then present the expanded prompt with:

- Clear objective
- Concrete scope (what's in, what's out)
- Deliverables and file outputs
- Constraints, tone, and quality bar
- Any assumptions being made (flagged explicitly)
- Open questions: what is still undecided and would change the result, each one answerable at the approval stop. Only when there are some; an empty heading is not written. An assumption is a gap you chose to fill; an open question is a gap you are handing back.
- Affected systems and people: only when the task touches something live, another repository, or another person; otherwise omit it
- The measurable-execution block as the final element, per `measurable-execution.md` (metric always; `/loop` and `/goal` proposals only on signal)

### Phase 2 - Clarify (only if needed)

If ambiguity blocks a confident expansion, ask focused questions BEFORE presenting the expanded prompt. Ask only what is necessary. Do not invent detail - flag gaps.

### Phase 3 - Save The Prompt, Then Execute

Where the flow reached an approval stop, STOP. Do not execute. Wait for explicit approval: "approved", "proceed", "go", "execute", or "yes".

Then, and BEFORE the first action, save the prompt:

```bash
python scripts/prompt-archive.py save --slug <short-kebab-name> \
    --original <file with his words verbatim> --expanded <file with the expansion>
```

Then say, in your own words, that the file is now the master instruction, and name its full path. Execute from that file. Where the file and your memory of the turn disagree, the file wins.

Every further prompt the operator gives during the same execution is saved the same way, in sequence. A handoff written later asks `prompt-archive.py list` and carries the ordered paths; nothing is registered at approval time, because a prompt approved after the last handoff would otherwise be the one that is lost.

Execute strictly against the approved prompt. Do not expand scope mid-execution - if new decisions arise, stop and ask.

**Which prompts are saved, and the test is the SHAPE OF THE TASK rather than the gate a message passed.** Operator decision, 2026-09-22, his reason: *"that way I can see how you understood me"* (translated), and a path named out loud survives the compaction that eats the turn it came from. A file is written when a message FIXES the shape of a task, and again when a later message CHANGES that shape. Nothing is written when a message only moves a task whose shape is already on disk.

Shape is four fields, the same four Phase 1 expands: objective, scope, deliverables, acceptance. So one question, asked once per message, which two readers answer the same way:

> Draft the expansion this message deserves. Does it differ, in any of those four fields, from the newest expansion already archived for this session?

Nothing archived yet, so everything differs: write the file. It differs in one of the four: write the next file in sequence. It differs only in how far the work has got: write nothing.

That test refuses "yes", "go", "спасибо" and "ага", which change no field; a reply that only picks from options you yourself listed, because the choice was already inside the expansion you presented; a one-step correction to work just produced; a request to show or report on what already exists, which starts nothing; and a session switch such as `/unattended on`, which changes how the session runs and not what it is doing. It admits "commit and push" and "merge that branch and delete the yard" whenever the archived expansion stopped short of them, because those add a deliverable no file carries.

**The 2026-09-20 decision stands and this is where it still bites.** An archive of every keystroke is a place nobody looks. The line moved from the approval gate to the task, which is a wider set and still a small one: an evening of forty messages carries perhaps six tasks, and the test above writes about six files.

**Where no approval stop happens, the file is written anyway, and still before the first action.** The escape valves, a direct command, a reply that arrives carrying new work, and `/unattended` all bypass the stop by construction, and any of them can fix the shape of a task. The order there is: expand, save, name the absolute path, then act. Nothing else about this phase changes, and the expansion is the master instruction exactly as it is above.

**The verbatim half holds the message that fixed the shape**, which is usually an EARLIER message than the one that released execution. When "go" approves an expansion presented a turn ago, the operator's words in the file are the task he typed, never the word "go".

Rationale, the file format and the location: the script's own docstring.

### Explicit escalations

Two commands override the default judgement above. Each is typed by the
operator, which loads the skill that defines it, so only their names are
resident. What each does to the three phases:
`reference/prompt-refinement-escalations.md`.

- `/devil N`: N severity-tagged contrarian critique points (default 5, 1-10).
- `/burst N`: the same content delivered N ways (default 3, range 2-5).

## Escape Valves

Even when a message meets one of the four criteria above, skip the stop and act directly when:

1. The user prefixes the message with `!` (e.g., `!fix this typo`)
2. The message is a direct reply to a question YOU asked
3. The message is a trivial one-step correction to work just produced (a typo, a rename, a single-line tweak)

When it is unclear whether a request is ambiguous, ask whether two reasonable readings of it would produce different work. If they would, expand and stop; if they would not, act.

**A valve skips the STOP, never the file.** Valves 2 and 3 name messages that change none of the four fields, so the test in Phase 3 already writes nothing for them and the valve costs nothing extra. Valve 1 is different: `!` is how the operator opens a task fast, so a `!` message that fixes the shape of one is expanded in a sentence, saved, and its path named, before the first action.

## Output Discipline

- Keep the expanded prompt tight. Brevity over verbosity.
- Do not pad with corporate language, hedging, or preamble beyond the required opener.
- Use plain prose and short lists. No ceremonial formatting.

## Interaction with Corporate-Docs Guardrail

When a request matches one of the five locked doctypes,
`.claude/rules/corporate-docs.md` requires an immediate skill announcement. It
happens **inside** Phase 1, not before it: announce, then present the expanded
prompt. Phase 3's approval gate still applies, and Escape Valve 1 bypasses both
rules. Worked example:
`reference/prompt-refinement-escalations.md` § Interaction with the corporate-docs
guardrail.
