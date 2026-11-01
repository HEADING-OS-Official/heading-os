# /scrutinize — target and flag catalog

Consumed by: `.claude/skills/scrutinize/SKILL.md` (loaded by Phase 0 step 1,
which loads every file in this directory).

Last Updated: 2026-10-31

A catalog of targets and flags that a reader consults, not a step the skill executes.

## Target

`target` (optional) — one of:

| Value | Meaning |
|---|---|
| `execution` | git status plus this session's commits |
| `file:<path>` | one file; a plan is reviewed this way (a plan file, see `references/target-detection.md`) |
| `dir:<path>` | a directory, with standard exclusions |
| `workspace` | the whole workspace; Phase 2 dispatches area specialists |
| `trajectory:<run_id>` | a recorded implementation trajectory |

If omitted, auto-detect per `references/target-detection.md`. The `plan` target is retired: a typed `/scrutinize plan` prints `The plan target is retired. Run /scrutinize file:<path of the plan>.` and stops.

## Flags

| Flag | Effect |
|---|---|
| `--relentless` | Auto-apply and loop with adaptive termination per `references/relentless-adaptive.md`. Pre-approves every proposed fix, applies it, re-runs Phase 1 on the same target, and loops until terminated. Not compatible with a plan file. |
| `--no-refute` | Skip Phase 2.5 (refutation and debate). Findings emit straight to the approval block with scorer-emitted confidence only. Recorded in the saved report. |

No flag changes what the approval block shows: below-threshold and AMBIGUOUS findings always go to the `Not proposed` section after it (`references/approval-block.md`), and the external code voice always runs on code targets (`references/code-review-voice.md`).
