<!-- version: 1.1.0 | last-updated: 2026-08-20 -->
# Zero Hidden Characters Policy

Last Verified: 2026-08-20

Write generated text with visible characters, ordinary spaces and newlines only; never include invisible Unicode characters. This applies to ALL outputs: documents, code, messages, posts, proposals, everything.

Banned characters include: zero-width spaces (U+200B), zero-width joiners (U+200C/D), soft hyphens (U+00AD), non-breaking spaces (U+00A0), directional marks (U+200E/F), word joiners (U+2060), BOM (U+FEFF), and all other invisible Unicode.

Treat hidden character contamination as a defect on par with fabricating facts.

Full reference: `reference/hidden-characters.md`
Sanitizer: `scripts/sanitize-text.py`

**Validation on every deliverable.** When presenting any draft copy to the operator (messages, posts, emails, proposals, etc.), run the sanitizer and carry its result: `Word count: X. Hidden characters: <what the scan reported>.` "clean" is one possible value, not the template. If characters were found and removed, say so explicitly.

This literal appears here and nowhere else (since 2026-08-23), because a copy with the clean outcome written in nudges toward stating an outcome instead of reading one; nothing but a reader catches a restatement.

**Both numbers come from the tool, never from an estimate.** `--scan` prints the word count beside the character verdict, so copy X from its output. A guessed figure inside a validation line is what `.claude/rules/scope-claims.md` forbids.

This rule is the canonical owner of that confirmation line: its wording, when it is required, and what to say when the scan was not clean are defined here, and any other rule or skill that asks for it defers to this file rather than restating it.
