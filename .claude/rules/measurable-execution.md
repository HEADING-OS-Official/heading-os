# Measurable Execution - Loop / Goal / Metrics Pre-flight

Last Updated: 2026-10-29
Last Verified: 2026-09-04

Always-active rule. Whenever a task is expanded under Phase 1 of `prompt-refinement.md`, agree the metric that defines "done ideally" before the work, and assess whether the task fits the native harnesses `/goal` and `/loop`. This block is the last part of that expansion, so it fires exactly when the expansion fires and inherits its escape valves verbatim (both owned by `prompt-refinement.md`); no separate threshold. Rendered in the operator's working language (Russian: Замер / Метрики / Loop / Goal); the field labels below are canonical English.

## The three checks

- **Metrics (always).** The concrete, checkable signal that defines "done ideally": a test exit code, a count, a file that exists and passes the audit, a named deliverable state.
- **Goal-fit (on signal only).** When the task has a verifiable end condition, propose it as a ready-to-paste `/goal <condition>`. The condition is the metric.
- **Loop-fit (on signal only).** When the task is recurring or needs polling, propose `/loop [interval] <prompt>`.

Surface Goal-fit and Loop-fit ONLY when the task language signals a fit; when neither helps, say so in one phrase rather than printing dead "no" lines.

## The metric is agreed, not asserted

When the operator supplies criteria, Claude adopts them. Otherwise Claude proposes the metric it judges right in the block and asks by what criteria "done" is measured; the operator accepts, sharpens or replaces it at the approval STOP. Subjective bars (voice, "good enough to send") are named as judgment calls or proxies, never dressed as hard metrics.

**The metric is a floor, not the quality bar.** A passing metric or `/goal` never replaces the normal voice and humanisation pass before the work is called done.

## Block format

The final part of the Phase 1 expansion, before the approval STOP. Plain text, real newlines, no markdown tables.

    Measure:
    - Metrics: <how "done ideally" is checked> · /loop and /goal add nothing here

When a fit is signaled, surface only the relevant lines; both are proposals:

    Measure:
    - Goal: /goal <verifiable condition>   (proposed)
    - Loop: /loop 30m <prompt>             (proposed)
    - Metrics: <the criteria that decide the goal is met>

## Boundaries

Advisory: it feeds the Phase 1 expansion, which already carries the approval STOP, so nothing here blocks; an expansion that omits the block is a protocol miss. `/loop` and `/goal` are proposals only: the human launches them, never Claude autonomously (`lethal-trifecta.md`). For non-trivial code the metric seeds the test contract written at step 3 of a Canopus slice, which the operator's approval commit at step 4 carries; run `/create-plan` then `/canopus` then `/implement` there rather than duplicating the planning gate's heavy critique here.

Changes require the operator's explicit approval.
