---
name: council
description: |
  Second-opinion advisor. Default roster is Kimi k3.1-pro alongside Claude's own view; Gemini, Grok and ChatGPT are opt-in via --gemini / --grok / --chatgpt / --all.
  Two modes: independent (the models see the problem only, reason fresh) and critique (they stress-test a draft).
  Distinct from /deep-think (Claude reasoning structured, alone) and /odin (Claude + the curated knowledge brain).
  Trigger when the user says: "council", "/council", "second opinion on", "consult the council",
  "what would Gemini/Grok/Kimi/ChatGPT say about", "stress-test this with Gemini/Grok/Kimi/ChatGPT", "council vote".
argument-hint: "[question] | --critique [draft]"
allowed-tools: "Read, Bash(.venv/bin/python:*), Write"
context: fork
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.6"
x-heading-capability:
  what: >
    Independent second opinions presented side-by-side with Claude's own view — no synthesized final answer, the CEO decides. Default roster since 2026-07-25 is Kimi only (k3.1-pro since 2026-10-24); Gemini, Grok and ChatGPT are one flag away.
  how: >
    Run /council <question> for independent mode, or /council --critique <draft> to stress-test a draft. Transcript saved under council-transcripts unless --no-log. Add voices with --gemini / --grok / --chatgpt / --all; run exactly one with --gemini-only / --grok-only / --kimi-only / --chatgpt-only; drop one with --no-gemini / --no-grok / --no-kimi / --no-chatgpt.
  when: >
    Use for a hard or high-stakes call where an independent view is worth the latency; add --all when cross-model disagreement is itself the signal. For Claude reasoning alone use /deep-think; for Claude plus the curated knowledge brain use /odin.
x-heading-routing:
  category: Strategy
  triggers:
    - second opinion
    - consult the council
    - what would Gemini say
    - what would Grok say
    - what would Kimi say
    - what would ChatGPT say
    - stress-test with Gemini
    - stress-test with Grok
    - stress-test with Kimi
    - stress-test with ChatGPT
    - gemini council
    - kimi council
    - council vote
    - second opinion on
  exclusions:
    - Reasoning alone -> /deep-think
    - Claude + curated knowledge brain -> /odin
  compound: 'No'
  router: auto
---

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Resolve the name before the file operation and
use what it prints:

    .venv/bin/python scripts/data-path.py path <name>

Use `read <name>` for a source that may legitimately be absent, and
`read --required <name>` for one whose absence is a defect. An unknown name
refuses; it never resolves to a plausible directory, and a path written out
here goes stale the day the directory moves.

# Council - Independent Second Opinions (Kimi k3.1-pro by default; Gemini, Grok and ChatGPT opt-in)

Independent second opinions dispatched in parallel. Use when:
- The user wants fresh views on a hard call (independent mode)
- The user wants a draft stress-tested before it ships (critique mode)

This skill is distinct from `/deep-think` (Claude reasoning harder, alone) and `/odin` (Claude + the curated knowledge brain). The unique value is a model with a different training pedigree, different RLHF, and different failure modes reaching the question independently. Agreement is stronger evidence, and disagreement is itself information. `--all` calls every voice on the roster when breadth matters more than latency.

---

## Phase 0 - Determine mode

Read the user's request.

CRITIQUE mode if any of:
- The user passed `--critique`
- The user said "stress-test this", "critique this draft", "find flaws in", "what's wrong with this", "review this draft"
- The user pasted a draft (proposal, message, claim) and asked for review

INDEPENDENT mode otherwise (default).

---

## Phase 1 - Gather inputs

For INDEPENDENT mode, prepare:
- `question`: the user's question, cleaned and concrete
- `context`: facts the user has shared in this conversation that bear on the question. Important: include the FACTS (numbers, names, dates, constraints), NOT Claude's reasoning or proposed answer. The whole point of independent mode is to give every outside voice a clean slate.

For CRITIQUE mode, prepare:
- `draft`: the exact draft text to critique. If the user passed `--critique 'text'` inline, the draft is the quoted text immediately following the flag. If the user pasted a draft in the message body without the flag, use that body verbatim.
- `context`: why the draft was produced (audience, goal, constraints). In critique mode, Claude's reasoning IS appropriate to include. The outside voices are being asked to stress-test it.

**Optional** — to give Claude's view a distinct lens from the outside models, pull 2-5 methods from `reference/elicitation-methods.md` (`.venv/bin/python scripts/elicit.py list --category collaboration`; categories `collaboration`/`research`/`framing`, e.g. Steelmanning, Reframe the Question). Skip when the question is already well-framed.

---

## Phase 2 - Call the model scripts

### Determine which models to call

**Read `references/roster-flags.md` first.** It carries the four voices, the
default roster, every opt-in, exclusive, skip and model-override flag, and the
combinations you must reject. Resolve the roster there before you build a command.

### Build the commands

Use `Bash` with single-quoted args (escape any single quotes in the inputs as `'\''`). Build a command for each SELECTED model:

For independent mode:
```bash
.venv/bin/python scripts/gemini-consult.py  --mode independent --question '...' --context '...'
.venv/bin/python scripts/grok-consult.py    --mode independent --question '...' --context '...'
.venv/bin/python scripts/chatgpt-consult.py --mode independent --question '...' --context '...' --reasoning-effort high
.venv/bin/python scripts/kimi-consult.py    --mode independent --question '...' --context '...' --model k3.1-pro --reasoning-effort high --max-tokens 12000
```

For critique mode:
```bash
.venv/bin/python scripts/gemini-consult.py  --mode critique --draft '...' --context '...'
.venv/bin/python scripts/grok-consult.py    --mode critique --draft '...' --context '...'
.venv/bin/python scripts/chatgpt-consult.py --mode critique --draft '...' --context '...' --reasoning-effort high
.venv/bin/python scripts/kimi-consult.py    --mode critique --draft '...' --context '...' --model k3.1-pro --reasoning-effort high --max-tokens 12000
```

**The ChatGPT voice takes its pin from the config, not from a literal.** Omit
`--model` and it resolves `chatgpt` through `config/council-models.json`. The
proxy validates `--reasoning-effort` for this voice and accepts `low`, `medium`,
`high`, `xhigh` and `max`. That set was measured on 2026-09-11 against the live
proxy; an unknown level comes back as a 400.

**Kimi voice runs the deep model (k3.1-pro, since 2026-10-24), and ONLY that model.** Dispatch it with the explicit
`--model k3.1-pro` shown above, NOT `.venv/bin/python scripts/council-models.py --get kimi`. That pin
stays `kimi-for-coding` and is reserved for other callers' fast path. The deep model always
thinks and ignores `--temperature`; give it `--max-tokens` head-room (12000, as shown). Before
dispatch, check `cliproxy models` for `k3.1-pro`.

**If k3.1-pro is absent, SKIP the Kimi voice.** Do not silently fall back to `kimi-for-coding`,
the 2.6/2.7 line. A shallower model answering in the deep model's name is a quality
substitution nobody consented to. On the default roster it would also be the council's only
external voice. Say plainly `kimi voice: k3.1-pro unavailable, skipped`. That empties the default
roster, so stop rather than presenting Claude's own view as a council.

Model overrides and passthrough flags are in `references/roster-flags.md`.

**A cut-off answer is an ERROR, not a short answer** (since 2026-08-23). On
"the answer is cut off mid-word", raise `--max-tokens` and `--timeout` together and re-dispatch that
voice. Never report the failure as the model's opinion.

**`--length-hint` is the closing "Aim for 200-400 words."** Leave it alone here;
that cap is what a council consult wants. Pass `""` only for an enumerating task
("list every X"), where a word cap truncates the list.

### Dispatch IN PARALLEL

Fire all SELECTED model Bash calls in a SINGLE assistant message (parallel dispatch). Do NOT call them sequentially — that multiplies latency.

When only one model is being called (`--*-only`), fire just that one call.

### Capture results

For each script call:
- If exit code 0: capture stdout as that model's verbatim response. Mark model as SUCCEEDED.
- If exit code non-zero: capture stderr as the error message. Mark model as FAILED.

If ALL called models FAILED: print `Error: all council models failed.` followed by each model's error, then stop. Do NOT write a transcript.

If at least one model SUCCEEDED, proceed to Phase 3.

---

## Phase 3 - Formulate Claude's view

Read the verbatim responses captured in Phase 2, from whichever models succeeded. That may be any subset of the roster. Then write Claude's own view on the question or draft. Reach a real position independently of what any outside model said — don't just react to them.

Claude's view should be 3-5 bullets covering: position, key reasons, main risk Claude sees. (Tightened from 3-7 in Phase 1 — several views in one output need shorter bullets to stay readable.)

---

## Phase 4 - Present the side-by-side

Render exactly the sections below to the user. No more, no less. No synthesised final answer.

```
## Gemini's view
[3-5 bullets distilling Gemini's response. Preserve Gemini's actual conclusions and arguments — don't soften or rewrite them. If Gemini hedged, say so.]

## Grok's view
[3-5 bullets distilling Grok's response. Same rule — preserve Grok's actual conclusions and arguments.]

## Kimi's view
[3-5 bullets distilling Kimi's response. Same rule — preserve Kimi's actual conclusions and arguments.]

## ChatGPT's view
[3-5 bullets distilling ChatGPT's response. Same rule — preserve ChatGPT's actual conclusions and arguments.]

## Claude's view
[3-5 bullets — Claude's own position, reached independently of any outside model.]

## Where we agree / disagree
[1 paragraph — make convergence and divergence explicit across all views present. If they agree, say that. If they disagree, name where. Don't fabricate disagreement.]

## Open questions for you
[1-3 things the user actually needs to weigh — not generic platitudes.]
```

### Conditional sections

Omit any model's section if that model was not called (`--*-only` / `--no-*`) or failed. When only one outside model ran, the output has two sections (that model + Claude). Replace a failed model's section with `## Failed: {Model}` and put the error message inside (one paragraph, plain text, no bullets). All-failed is caught in Phase 2 and never reaches Phase 4.

### Alignment check (mandatory, applies to whichever verbatims were captured)

Before writing the side-by-side:

1. For EVERY voice that was requested AND succeeded, re-read its verbatim response from Phase 2 stdout.
2. Verify every bullet under that voice's section is traceable to a specific sentence in its verbatim text.
3. If a verbatim is ambiguous, truncated, or hedged, say so explicitly in the bullets. Never infer a position.
4. Do NOT cross-feed: never use one model's verbatim to interpret another model's bullets.

The check is per voice, so a voice added to the roster is covered without a new
step here.

---

## Phase 5 - Persist the transcript (default)

### Detect `--no-log`

Before writing anything, scan the user's original invocation text for any of:

- The literal flag `--no-log`
- Natural-language equivalents: `no transcript`, `don't log`, `do not save`, `skip the log`, `skip transcript`, `без записи` (Russian)

If any match, skip the entire Write step below and announce in the chat output: `Transcript skipped.` Then end normally.

### Write the transcript

Otherwise, write to:

```text
$(.venv/bin/python scripts/data-path.py path council-transcripts)/{YYYY-MM-DD}_council_{HHMMSS}_{slug}.md
```

The transcript history sits in two homes, so a search for a past transcript
reads both. The bulk sits in `council-transcripts` beside `_aggregate.md`; the
audit-flavoured transcripts (release audits, intent audits, meta-reviews) sit in
`council-reports`. New transcripts go to `council-transcripts`.

The `{HHMMSS}` segment (current time, 24-hour, no separators) prevents collisions when two consultations on the same date produce the same slug. Use the local clock at the moment of writing.

Slug rules:

- Lowercase, kebab-case
- First 5 meaningful words of the question (or draft summary)
- Strip articles (a, an, the) and common stop words (is, are, was, what, how, why, do, does, should, would, could, will)
- Max 40 characters total
- Fallback to `untitled` if no meaningful words remain

Use the `Write` tool. The exact transcript content (YAML frontmatter + body sections) and the post-write absolute-path announcement are specified in `references/transcript-format.md`.

---

## Phase 6 - Capture CEO verdict (Track C of LLM-fit logging)

Skip this phase entirely if `--no-log` was set (no transcript = nothing to record against).

After the transcript path is reported, ask the CEO **one short question** as the final line of the chat output:

> Which answer landed best - `claude`, `gemini`, `grok`, `kimi`, `chatgpt`, `mix`, `reject`, or `skip`? (one word + optional sentence on why)

That is all. Do NOT re-summarise, push for a decision, or explain the choice values — the CEO knows them. Keep the question to one line so the CEO can reply in 5 seconds.

When the CEO replies, parse their next message:

- First token (case-insensitive) is the choice. Accept `claude` / `gemini` / `grok` / `kimi` / `chatgpt` / `mix` / `reject` / `skip` / Russian equivalents (`пропустить`, `мix`, etc — normalise to the English choice).
- Everything after the first token is the optional `notes` string. Trim whitespace.

If choice is **`skip`** (or any non-recognised first token without an explicit `claude/gemini/grok/kimi/chatgpt/mix/reject` keyword anywhere in the reply): do NOT record. Print one line: `Verdict skipped (left pending).` Do not nag.

The authoritative set is `VERDICT_CHOICES` in `scripts/utils/council_models.py`.
It is the roster plus `claude`, `mix` and `reject`, so a new voice becomes a
recordable choice with no edit here. Run
`.venv/bin/python scripts/council-record-verdict.py --help` to see what this build accepts.

Otherwise, run:

```bash
.venv/bin/python scripts/council-record-verdict.py \
  --id {transcript filename stem, NO .md extension} \
  --choice {claude|gemini|grok|kimi|chatgpt|mix|reject} \
  --notes "{notes string, or omit the flag if empty}"
```

The script prints `recorded: ...` and then a tally naming every choice it accepts. Echo only the tally line back to the CEO so they see the running count, plus one final line:

`Recorded. Aggregate refreshed.`

Then run `.venv/bin/python scripts/council-aggregate.py` (no flags) to rebuild `_aggregate.md` under `council-transcripts` from the updated JSONL. This is the ONLY supported way verdicts enter the system - the CEO never opens the aggregate or the JSONL.

The CEO may not have replied when you would otherwise close out, because they went silent or moved to another task. In that case, do not record anything. The verdict stays pending until they answer or another /council run prompts them again. Pending verdicts are reflected in the aggregate as `_(pending CEO verdict)_`.

---

## NEVER

- Synthesise a single "final answer" combining the outside voices and Claude. The user decides.
- Show any outside model Claude's reasoning in INDEPENDENT mode. (In critique mode it's fine.)
- Cross-feed one model's response to another. Each model reasons independently.
- Re-run a failed model silently. If a model fails, render `## Failed: {Model}` with the error and continue.
- Forget the workspace system block — every `scripts/*-consult.py` wrapper injects it automatically; if you ever bypass a script, inject it yourself.
- Run without `CLIPROXY_API_KEY` set in `.env` — the proxy transport is required for every outside voice.
- Modify any `scripts/*-consult.py` wrapper from inside this skill — those are code changes, not skill behaviour.
- Dispatch the model scripts sequentially when multiple are requested. Always parallel (single assistant message, multiple Bash tool calls).
- Ask the CEO to open `_aggregate.md` or `_verdicts.jsonl` directly. The CEO never edits those files; Phase 6 + `scripts/council-record-verdict.py` are the only writing path.

---

## Voice rules (apply to Claude's view and to the side-by-side prose)

- `.claude/rules/voice.md` - workspace voice
- `.claude/rules/humanization.md` - five fundamentals on Claude's own prose
- `.claude/rules/terminology.md` - workspace vocabulary and core principles
- `.claude/rules/hidden-chars.md` - zero invisible Unicode in the transcript

Validation before declaring done: run `.venv/bin/python scripts/sanitize-text.py {transcript-path} --scan`
and report both numbers it prints, per `.claude/rules/hidden-chars.md`.
