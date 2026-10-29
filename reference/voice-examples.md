# Voice Examples: Loading the Operator's Own Writing

How a drafting skill loads 3-5 real pieces of the operator's writing as few-shot examples at run time.

Last Updated: 2026-09-27
Consumed by: `/linkedin` (modes `post` and `series`), `/email-draft`, `/email-respond`, `/follow-up`, `/ceo-to-ceo`, `/tribe-message`, `/tribe-monday`, `/pitch` (mode `update`)

## Why

The voice guide describes the voice. Examples show it, and they steer tone and structure more reliably than a description does. Anthropic's prompting guidance asks for 3-5 examples, varied enough that the model does not copy one of them, each wrapped in `<example>` tags inside `<examples>`.

The examples stay in the data repository. This file names where they are; it never holds one.

## Sources

| Register | Registry name | What it holds |
|---|---|---|
| LinkedIn | `linkedin-archive` | Published posts, comments and older posts. `INDEX.md` lists them; each piece is a file or a folder under `posts/`, `comments/` or `old-archive/`. |
| Email | `exchange-email-cache` | `sent-latest.md`: the most recent sent emails as one markdown file, one `## ` section per email. It is a sync cache, not an archive. Each sync overwrites it, and it can be stale or absent. |

Resolve a source with `python scripts/data-path.py read <name>`, because both may legitimately be absent. No archive exists for team messages, investor updates or CEO-to-CEO letters. For those registers, use the email source.

## Procedure

1. Resolve the source for the register you are drafting in.
2. Pick 3-5 pieces that differ from each other in topic, length, audience and opening. Prefer recent pieces.
3. From an email, keep only the text the operator wrote. Drop quoted history, signatures and forwarded text.
4. Put the pieces before the drafting instructions, in this shape:

```
<examples>
<example>
[first piece]
</example>
<example>
[second piece]
</example>
</examples>
```

5. Use the examples for voice only: rhythm, openings, stance, length and closings. Never copy their facts, names, figures or sentences into the draft.
6. If the source is absent or holds fewer than three usable pieces, use what exists and fall back to `voice-guide`. Say so in the notes that go with the draft.

## What this does not do

It does not copy any piece into the engine or into a skill file. It does not rank pieces by quality; the operator's published and sent writing is the sample. It does not replace the banned-phrase lists in each skill, which remain the precise form of the negative guidance.
