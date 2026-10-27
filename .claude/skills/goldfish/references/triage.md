# Goldfish triage

How the author of an artifact answers a reviewer's findings: FIX, CONTEST or ESCALATE.

Consumed by: `.claude/skills/goldfish/SKILL.md` (Phase 2); also named by `scripts/review-branch.py` for a yard answering pre-merge findings, and by `/canopus` step 6.
Last Updated: 2026-10-27

Adapted, with changes, from the `egm-resolve` skill of Google's Elephant-Goldfish Model (github.com/google/elephant-goldfish-model), licensed Apache-2.0. EGM's fourth category, DECLINE, is not adopted: in this workspace review findings are fixed, never filed (`.claude/rules/development-standards.md`, Restraint carve-out).

The author is the session that wrote the artifact, or the yard that wrote the branch. It knows things the reviewer does not, and that is both its advantage and its bias. Every finding gets exactly one category.

## The three categories

- **FIX.** The finding is right. Write the exact change: where, and the replacement text or code. Then make it.
- **CONTEST.** The finding is wrong, and you can prove it with evidence the reviewer COULD have read: a file and line, a symbol, a sentence in the artifact itself. Quote the evidence.
- **ESCALATE.** You believe the finding is wrong, but the proof lies outside everything the reviewer could read: a decision the operator took in conversation, a convention nobody wrote down. Say so. Do not fix it and do not contest it. The operator decides.

## The rules

1. **A contest must be checkable.** "This was intentional" is not evidence. Without a file, a line or a quote, the category is ESCALATE or FIX.
2. **Your wish to contest is not evidence.** You wrote the artifact. Reread the finding as if someone else had.
3. **Never contest a reader gap.** If a fresh reader could not follow the artifact, the artifact is missing something. Fix it.
4. **Never add text only to satisfy the reviewer.** Add context when the real reader needs it. When only a zero-context agent would, that is ESCALATE.
5. **Never change a severity.** If you think a BLOCKER is a LOW, contest it and give the reason.
6. **An ESCALATE often means a decision lives only in conversation.** Name that in the triage. The fix may be to write the decision down where it belongs.

## What the operator sees

ESCALATE items first, because they need the operator's knowledge and not only his approval. Then FIX items with the change made or proposed. Then CONTEST items with their evidence. The raw findings are kept verbatim beside the triage, so a reader can check every category against what the reviewer actually said.

## Re-review

After BLOCKER or HIGH fixes, dispatch ONE fresh goldfish on the changed artifact. Never resume the first one: it has read the old version. One re-review only. Findings left after it are listed for the operator as open, by number.
