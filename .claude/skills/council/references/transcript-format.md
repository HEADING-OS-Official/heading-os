# Council — Transcript Format

Consumed by: `.claude/skills/council/SKILL.md` (Phase 5 — Persist the transcript).
Last Updated: 2026-09-11

The exact transcript file content written to
`{YYYY-MM-DD}_council_{HHMMSS}_{slug}.md` under `council-transcripts`. Kept out of the
SKILL body so the phase logic stays under the inline budget. The SKILL body holds the
path, `{HHMMSS}` collision rule, slug rules, and the `--no-log` detection.

## Transcript content

**The section order is fixed and the headings are parsed.**
`scripts/council-aggregate.py` finds each voice's answer with a regex on its
`## <Voice>'s full response` heading, so a renamed or reordered heading drops
that voice out of the comparative log in silence. The voice order matches
`COUNCIL_PROVIDERS` in `scripts/utils/council_models.py`. ChatGPT was appended on
2026-09-11 rather than interleaved, so every earlier transcript still reads in
the order it was written.

```markdown
---
timestamp: {ISO 8601 with seconds}
mode: {independent|critique}
models_requested: [gemini, grok, kimi, chatgpt]   # or subset based on flags
models_succeeded: [gemini, grok, kimi, chatgpt]   # or subset based on runtime
gemini_model: {gemini model name}    # only if Gemini was requested (include even on failure — model name is still known from the command)
grok_model: {grok model name}        # only if Grok was requested (include even on failure — model name is still known from the command)
kimi_model: {kimi model name}        # only if Kimi was requested (include even on failure — model name is still known from the command)
chatgpt_model: {chatgpt model name}  # only if ChatGPT was requested (include even on failure — model name is still known from the command)
---

# Council Consultation - {question excerpt, max 80 chars}

## Question / Draft

{question text or draft text}

## Context (sent to all models)

{context, or "_(none provided)_"}

## Gemini's full response (verbatim)

{Gemini's stdout, unmodified — OR "_(not requested)_" — OR "**FAILED:** {error message}"}

## Grok's full response (verbatim)

{Grok's stdout, unmodified — OR "_(not requested)_" — OR "**FAILED:** {error message}"}

## Kimi's full response (verbatim)

{Kimi's stdout, unmodified — OR "_(not requested)_" — OR "**FAILED:** {error message}"}

## ChatGPT's full response (verbatim)

{ChatGPT's stdout, unmodified — OR "_(not requested)_" — OR "**FAILED:** {error message}"}

## Claude's view

{the bullets from Phase 3}

## Side-by-side (as presented to user)

{the rendered Phase 4 output — same content shown to the user}

## Decision (filled by the operator)

_(empty - fill manually after deciding)_
```

After writing, show the full absolute path of the transcript at the end of the chat
output, in the form `Transcript: <council-transcripts>/<filename>`.
Resolve `<council-transcripts>` at runtime from the registry name of the same name
(`.venv/bin/python scripts/data-path.py path council-transcripts`, or `data_path("council-transcripts")`)
— never embed a drive letter or platform-specific literal. Example:
`Transcript: <council-transcripts>/2026-05-09_council_143201_tier1-vs-tier2.md`.
