<!-- version: 1.2.0 | last-updated: 2026-10-29 -->
# Zero Hidden Characters Policy

Last Updated: 2026-10-29
Last Verified: 2026-08-20

Write generated text with visible characters, ordinary spaces and newlines only, in ALL outputs: documents, code, messages, posts, proposals, everything. Banned: zero-width spaces (U+200B), zero-width joiners (U+200C/D), soft hyphens (U+00AD), non-breaking spaces (U+00A0), directional marks (U+200E/F), word joiners (U+2060), BOM (U+FEFF), and all other invisible Unicode. Hidden character contamination is a defect on par with fabricating facts.

Full reference: `reference/hidden-characters.md`. Sanitizer: `scripts/sanitize-text.py`.

**Validation on every deliverable.** When presenting any draft copy to the operator (messages, posts, emails, proposals, etc.), run the sanitizer and carry its result: `Word count: X. Hidden characters: <what the scan reported>.` "clean" is one possible value, not the template. If characters were found and removed, say so explicitly.

This literal appears here and nowhere else.

**Both numbers come from the tool, never from an estimate:** copy X from the `--scan` output, which prints the word count beside the character verdict. A guessed figure in a validation line is what `.claude/rules/scope-claims.md` forbids.

This rule is the canonical owner of that confirmation line (its wording, when it is required, what to say when the scan was not clean); any other rule or skill asking for it defers here rather than restating it.
