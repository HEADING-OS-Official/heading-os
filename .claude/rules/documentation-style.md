---
paths:
  - "docs/INSTALL.md"
  - ".claude/skills/**/SKILL.md"
---

# Documentation Style — ASD-STE100 Subset

Last Updated: 2026-09-25
Last Verified: 2026-08-31

Path-scoped rule. Governs the pages a reader executes and the instruction bodies skills execute. Those pages are read by people whose first language is not English, on a bad day, while something is broken.

## What this is, honestly

A subset of **Part 1 (Writing Rules)** of ASD-STE100 Simplified Technical English, the controlled-language specification maintained by ASD and its STEMG working group for aerospace maintenance documentation. It is NOT conformance and must not be described as such: **Part 2, the Dictionary, is not adopted.** Its roughly 900 approved words were chosen for aircraft maintenance and hold no `repository`, no `daemon`, no `idempotent`; adopting it would mean declaring most of this engine's vocabulary as Technical Names, which is compliance in name only. Noun-cluster limits and the approved-part-of-speech constraint are out of scope for the same reason. What is adopted is the part that survives the move from aircraft to software.

## The rules

1. **Imperative in procedures.** `Run uv sync`, not `The dependencies should then be installed`.
2. **Active voice.** Passive only where the actor is genuinely unknown or irrelevant.
3. **Twenty words per procedural sentence, twenty-five per descriptive sentence.** Over the limit, split it. **An inline code span counts as ONE word**, never zero and never the words inside it. Until 2026-08-22 the checker deleted spans before counting, which discounted each sentence in proportion to the code it carried — the densest sentences drew the largest pass. One QUICKSTART line read 21 words to the checker and 27 to a person, and reported clean. Adopting the one-word count cost 37 rewrites, 15 on the gated pages and 22 in the skill bodies. Counting the interior instead would score 32 on the pages, and it would penalise naming the exact flag or path, which is pressure in the wrong direction for reference documentation.
4. **One action per numbered step.** Two actions joined by `and then` are two steps.
5. **Keep the articles and the linking verbs.** Telegraphic compression (`Open file, set value, restart`) is banned; it reads as a cable, not an instruction.
6. **One term, one meaning, corpus-wide.** Pick `engine clone` or `engine repo` and use only that one. A synonym introduced for variety is a defect here, not style.
7. **No `-ing` openers** on a sentence or a step, unless the word is part of a technical name.
8. **The warning comes before the step it guards** — never after it, never as the procedure's last line. The one rule with a physical cost when broken.
9. **No `and/or`.** Write `A`, `B`, or both.
10. **No minimising or padding words:** `simply`, `just`, `easily`, `obviously`, `in order to`, `prior to`, `in the event that`. The reader for whom the step is not simple is the reader who needs the step.

## Where it applies

The pages in this file's `paths:` frontmatter, plus the instruction bodies of `.claude/skills/**/SKILL.md`. By convention but deliberately not path-scoped, so the rule is not loaded on every script edit: CLI `--help` text, argparse descriptions, and operator-facing error strings in `scripts/`.

It does NOT apply to explanatory documentation, where the reasoning is the value and flattening it destroys the point: `ARCHITECTURE.md`, `THREAT-MODEL.md`, `SECURITY-MODEL.md`, `CANOPUS.md`, `DESIGN-CHECK.md`, `RELEASE-NOTES.md`, `RULES-REFERENCE.md`, `memory-lifecycle.md`, `engine-data-segregation-contract.md`, README positioning prose, every file in `.claude/rules/` including this one, and commit messages. It NEVER applies to outbound prose of any kind — email, LinkedIn, team messages, corporate documents — or to any non-English text.

**The docs site left the engine on 2026-09-25 (HOS-131).** Its pages now live in the data repository under the registry name `spec-docs`; the rules above still describe how they are written, by convention, and `scripts/ste-check.py` does not read the data repository, so nothing gates them there. The official tree gates `docs/INSTALL.md` here; its user and tool guides in `docs/` are explanatory documentation. A new page added to the engine's `docs/` must be placed in the frontmatter or named in the exclusion sentence above, deliberately: a page that sits in neither list is invisible to every gate on the gated set.

## How this composes with the prose rules

`humanization.md` and `voice.md` govern prose written to be read by a person forming a judgement; this rule governs text written to be executed. The two never both apply to one file: inside this scope this rule wins, everywhere else humanisation wins and this rule is silent. Two rules survive in both places and are never overridden here — `hidden-chars.md` (zero invisible Unicode) and the canonical no-`--` punctuation rule in `voice.md`.

## The checker, and what it cannot see

`python scripts/ste-check.py <file>` audits one file; `--all` audits the gated pages in the frontmatter; `--skills` audits the skill instruction bodies; `--strict` fails on warnings as well as errors; `--json` emits machine output; `--quiet` prints only the files that carry an error.

**`--all` is narrower than this rule's scope, and the two flags exist to say so.** A clean `--all` is not a clean corpus (measured 2026-08-16, when it covered twelve pages of a hundred and eight).

**The sentence splitter treats the closer as a character CLASS** (2026-08-17), because three enumerated shapes before it each made two clean sentences measure as one.

The skill corpus reached **zero** errors on 2026-08-17, and `--skills` is a gate from that day: a `documentation-style-skills` pre-commit hook and a CI step, errors only, exactly like its `--all` sibling.

**There is no vendored exemption:** a vendored skill is edited and re-locked with `python scripts/verify-skills-lock.py --relock` (2026-08-17, `.claude/skills/ast-grep/SKILL.md`), and the lock's `note` tells a re-vendor to re-apply the edit, because an exemption hides one file's debt permanently.

Bring a skill down when you edit it, run `--skills --quiet` to see where the number stands, and do not read a green `--all` as a green corpus.

**Errors gate; warnings stay advisory.** The first measurement, on 2026-08-11, reported 53 errors and 88 warnings across the twelve pages. The errors were 52 over-long sentences and one two-action step, all of them arithmetic on a word count with nothing to be wrong about, and the whole set was rewritten to zero the same day. So `--all --quiet` is now a pre-commit hook (`documentation-style`) and a step in the CI `sovereignty guards` job. The hook's file list used to be held to the checker's own scope by a test, so that a page could not fall out of the gate in silence; since the 2026-09-21 test-suite split nothing live holds it, and a page dropping out of the list is now something only a reader will catch.

The warnings did NOT earn a gate and are not in it. Seventy-nine of the eighty-eight are `passive_voice`, decided by a regex with no part-of-speech tagger behind it, and the rest are the `-ing` opener, the padding vocabulary, and the non-imperative-step heuristic. `--strict` would gate on guesses. Read them, act on the true ones, and leave the checker where it cannot fail a commit over a construction it cannot actually parse.

It verifies rules 3, 4, 7, 8, 9, and 10 mechanically, and flags rules 1 and 2 as heuristics with known false positives — there is no part-of-speech tagger behind the passive check. It **cannot** see rules 5 and 6: article omission and cross-corpus term drift need a parser and a glossary this engine does not have, so those two stay a human reading. That is why they are written down here rather than quietly dropped. Wrap any exempt block in `<!-- ste-skip-start -->` and `<!-- ste-skip-end -->`.
