# Implement — wave execution mechanics & version history

The full wave-execution procedure and the per-version emission-discipline changelog for the `/implement` skill. Read the wave section on demand — it matters only when a plan has `### Wave` headers (most runs are sequential and never load this file); the version history is reference-only.

Consumed by: `.claude/skills/implement/SKILL.md`

Last Updated: 2026-10-16

## Wave execution mode

Execute waves in order. For each wave:

1. **Parse all steps** (`####` headings) within the wave.

2. **Emit `wave_start` event** (skip if `--no-trajectory`): `.venv/bin/python scripts/implement-trajectory-log.py --event --run-id {run_id} --type wave_start --wave N --step-count <count> --parallel|--no-parallel`. **`wave_start` MUST be emitted before the wave's first `step_start`** — never after a member step has already opened. A step whose `step_start`/`step_end` fall outside its wave's brackets cannot be reconstructed from the bracketed record (the L1 emission defect).

3. **If the wave is marked `(parallel)` AND has 2+ steps:**
   - Announce: "Executing Wave N: {count} parallel tasks"
   - Dispatch each step as an independent agent using `superpowers:dispatching-parallel-agents`
   - **Each child sub-agent brief MUST include**: (a) the `run_id`, (b) the path `scripts/implement-trajectory-log.py`, (c) instructions to emit its own `step_start` / `step_end` / `deviation` events via the typed flags (never `--data-json`), (d) a reminder that the JSONL is shared state and the helper handles atomic-append concurrency.
   - Each agent receives: the step's full text (not a file reference), relevant context files, and instructions to leave its work uncommitted (never `git commit`), then report the files it changed; the commit happens once, on the operator's word
   - Wait for ALL agents in the wave to complete
   - Collect results (successes and failures)
   - If ANY step failed: report ALL results (successes and failures), STOP, and ask the user how to proceed. Do not advance to the next wave.

4. **If the wave is NOT marked `(parallel)` OR has only 1 step:**
   - Execute the steps sequentially using the standard rules above (including the per-step `step_start` / `step_end` emission).
   - **Sequential ordering invariant:** emit each step's `step_end` *before* the next step's `step_start`. Never open step N+1 while step N's bracket is still open, and never let a later step's `step_end` land after `wave_end`. If the work genuinely reorders (a step finishes out of declared sequence), emit a `deviation` event recording the swap — do not let the markers interleave silently (the M1 emission defect).

5. **After a wave completes successfully:**
   - Verify all files mentioned in the wave's steps exist
   - **Emit `wave_end` event** (skip if `--no-trajectory`): `.venv/bin/python scripts/implement-trajectory-log.py --event --run-id {run_id} --type wave_end --wave N --successes <count> --failures <count>`. Emit `wave_end` only *after* every member step's `step_end` has been emitted. `successes` MUST equal the number of `step_end` events with `status` ok/deviation that fall inside this wave's `wave_start`/`wave_end` brackets — not the plan's declared membership count. The two must reconcile from the bracketed record alone.
   - Report: "Wave N complete: {count} tasks done"
   - Proceed to the next wave

6. **If an entire wave is deferred — not executed at all this run** (deferred to a focused session, blocked, or descoped): do NOT emit `wave_start`/`wave_end` for it (no steps ran, so there would be no bracketed `step_end`s to reconcile against). Instead emit exactly ONE **wave-scoped `deviation` event keyed to the wave's first step number**, so a whole-wave skip is a first-class structured record rather than only a `run_end` summary string (the L1 trajectory-audit defect, 2026-06-16). Skip if `--no-trajectory`. Emit: `.venv/bin/python scripts/implement-trajectory-log.py --event --run-id {run_id} --type deviation --step <first step number of the deferred wave> --scope wave --wave N --reason "<why deferred>" --what-changed "wave N (steps X-Y) deferred, not executed this run"`. The `run_end` summary should still mention the deferral, but the wave-scoped deviation event is the authoritative structured record. (A per-step deviation per skipped step is also acceptable when only part of a wave is deferred; the `scope: "wave"` form is for the whole-wave case.)

## Version history

The authoritative statement of the emission invariants is the **Trajectory emission contract** in `SKILL.md`. Milestones: v1.3 per-phase emission; v1.4 pairing and literal-path discipline; v1.5 whole-wave deferral; v1.6 typed flags, no temp file; v1.7 emit-time sequencing guard (exit `5`) and the files reconciliation; v1.8 (2026-08-09) `--verify` checks orphan `wave_end`, out-of-bracket `step_end` and a `wave_start` missing its shape, and covers the engine tree only. The full per-version text is in git history (`git log -p -- .claude/skills/implement/references/implement-details.md`).
