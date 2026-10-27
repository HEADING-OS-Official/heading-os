# Council roster flags - which voices a run calls

Consumed by: `.claude/skills/council/SKILL.md`, Phase 2.

Last Updated: 2026-09-11

Scan the user's invocation text for these flags, then resolve the roster before
you build any command. This file moved out of `SKILL.md` on 2026-09-11, when the
fourth voice pushed that file against its 18432-byte budget.

## The roster

The council voices are Gemini, Grok, Kimi and ChatGPT. The canonical list is
`COUNCIL_PROVIDERS` in `scripts/utils/council_models.py`, and the order there is
the order every transcript section follows.

**Default roster = Kimi (k3.1-pro) only.** The council is therefore Opus 5 (Claude's own
view, formed in-session) plus one deep external reasoner. The CEO set this on
2026-07-25. The other three stay implemented and are one flag away, so a wider
roster is a one-word change, not a rebuild.

## Opt-in flags

Combinable. Each adds a voice to the default roster.

- `--gemini` - also call Gemini.
- `--grok` - also call Grok.
- `--chatgpt` - also call ChatGPT.
- `--all` - call every voice on the roster.

**`--all` means every voice, and it is re-read from the roster on each run.**

## Exclusive flags

Run exactly one. At most one `--*-only` flag is allowed.

- `--gemini-only` - call only Gemini.
- `--grok-only` - call only Grok.
- `--kimi-only` - call only Kimi. Same as the default; accepted for symmetry.
- `--chatgpt-only` - call only ChatGPT.

## Skip flags

Combinable. Each removes a voice from whatever the roster resolved to. A skipped
voice that is not in the roster is a no-op, not an error.

- `--no-gemini`, `--no-grok`, `--no-kimi`, `--no-chatgpt`.

## Reject immediately

Print a one-line error, then stop. Do not proceed to Phase 3.

- More than one `--*-only` flag is set.
- Any `--*-only` is combined with any `--no-*` or any opt-in flag.
- All external voices end up skipped. An example is `--no-kimi` on the default
  roster. Claude's own view is not a council; say so rather than producing a
  one-voice transcript.

## Model overrides

These take **proxy** ids. Check `cliproxy models` for valid values.

- `--gemini-model gemini-2.5-flash` - passed to the Gemini call as `--model gemini-2.5-flash`
- `--grok-model grok-3-mini` - passed to the Grok call as `--model grok-3-mini`
- `--kimi-model <id>` - override the Kimi voice's model. The default is `k3.1-pro` (was `k3` until 2026-10-24).
- `--chatgpt-model <id>` - override the ChatGPT voice's model. The default comes
  from `.venv/bin/python scripts/council-models.py --get chatgpt`.

Other passthrough flags apply to every call: `--temperature`, `--max-tokens`,
`--length-hint`, `--timeout`. All four wrappers accept `--timeout`.
