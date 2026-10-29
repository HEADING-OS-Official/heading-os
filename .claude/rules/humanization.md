<!-- audit-skip-start -->
<!-- version: 4.1.0 | last-updated: 2026-10-29 -->
<!-- audit-skip-end -->
# Humanisation Rule - Write All Prose As Human

> Always-active rule for every natural-language prose output here: posts, emails, proposals, team messages, notes, plans, even casual chat replies. Not for code, config, JSON, machine logs, structured data tables, or quoted/cited third-party text.
>
> Last Updated: 2026-10-29
> Last Verified: 2026-09-04
>
> **The rule in full (Step 0 table, each fundamental, the two-pass order, the carve-outs): `reference/humanization-rule-detail.md`; read it before any outbound voice pass.** Empirical basis and failure modes: `humanization-empirical-basis.md`, registry name `reference-docs`. Banned vocabulary: `reference/humanization-banned-vocabulary.md`. Mechanical audit: `scripts/humanization-check.py`.

Override the LLM default register on every output: five fundamentals plus enforcement, for prose a reader experiences as alive.

## Step 0 - test before rewriting (mandatory, and the one that gets skipped)

Before applying any fundamental to EXISTING prose, **test the baseline** with `scripts/humanization-check.py` or an external detector. Never rewrite blind.

- **Under 15% AI / "human written": the text is a BINARY BLOB. Preserve it byte-for-byte and ship verbatim.** No rewriting, punctuation normalisation, typo fix, whitespace cleanup or curly-to-straight quote conversion: any modification regresses the score. Its em-dashes, en-dashes, curly apostrophes and curly quotes are the human signal, NOT covered by the dash rules of `.claude/rules/voice.md`. After saving such prose to a file, grep it back for U+2019, U+201C, U+201D, U+2014, U+2013, U+2026, because the Write tool can silently straighten them.
- **15-40%: diagnose before intervening.** Polished-and-dramatic → strip the polish. Rough-and-lived → do NOT re-rhythm. Detail file, § Step 0a.
- **Over 40%: content-first rewrite.** Specificity and stance FIRST; structural changes only if that alone did not move the score. Re-test after each change and revert anything that worsens it.

From-scratch prose has no baseline: apply the fundamentals, then test before delivery and revert any change that worsens the score.

## The five fundamentals, strongest first

1. **Specificity density: the dominant signal.** Every paragraph carries at least one named, dated or numbered specific, roughly one per 30-50 words: "The 1997 Camry", not "an older sedan". Ask for a fact you lack; a fabricated specific is worse than a missing one.
2. **Committed stance.** Take a position, refuse to balance every claim, close asymmetrically.
3. **Burstiness on purpose, FROM-SCRATCH ONLY.** Never re-rhythm existing prose; fragmenting and smoothing both worsen the score.
4. **Kill the AI vocabulary and phrase fingerprints**, never by deletion alone. **The vacuum trap:** scrubbing without replacing produces vapid prose; replace with specific, committed content. "Not only X but also Y" and "it's not X, it's Y" are tells ONLY when Y is vacuous; Title Case For Headings is always banned.
5. **Geometry over vocabulary.** Once per paragraph, take the second-most-natural continuation.

## Fundamental 6: two-pass voice editing (enforcement)

**Mandatory on outbound prose:** content draft first, then a SEPARATE voice pass; never deliver in one pass. The six steps, referenced elsewhere by number (the drafting skill runs 1-4 on its content draft, then 5-6):

1. Specificity: every paragraph carries a named/dated/numbered specific.
2. Commitment: does it take a position, or balance every claim?
3. Vocabulary fingerprint scan.
4. Read aloud mentally: fix stumbles in FROM-SCRATCH content only.
5. `python scripts/humanization-check.py <file>`: findings are hints, not orders.
6. External detector spot-check (optional; on borderline, fix content first).

Over 3,000 words, chunk it and re-apply per chunk.

**Every prose deliverable carries the confirmation line** owned by `.claude/rules/hidden-chars.md`, plus the humanisation audit result from `scripts/humanization-check.py`. Fix findings before delivering.

Rule and reference files legitimately discuss the items they ban: wrap those sections in `<!-- audit-skip-start -->` / `<!-- audit-skip-end -->`.

## Carve-outs

Quote third-party sources verbatim even where they violate this rule. On a user-supplied draft, ask before changing voice. Never chase a detector score as the success metric.
