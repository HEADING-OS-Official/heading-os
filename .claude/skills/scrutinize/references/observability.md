# Observability Layer - /scrutinize Telemetry

**Consumed by:** `.claude/skills/scrutinize/SKILL.md` (Phase 0, all subsequent phases)
**Last Updated:** 2026-10-16

Langfuse Cloud telemetry contract for /scrutinize. Closes R9 from the 2026-05-27 meta-review. Uses the existing `scripts/utils/observability.py` `@observe` decorator and is fail-closed on sensitivity: telemetry flows only when `SENSITIVE_MODE` is deliberately cleared, so an unset or garbage value keeps tracing off. See `SECURITY-MODEL.md` in the data repository (registry name `spec-docs`).

## What gets traced

What is traced today, and only this: every subcommand of `scripts/scrutinize-dispatch.py` runs under `@observe`, one span each, named `scrutinize-judge`, `scrutinize-role-scan`, `scrutinize-currency`, `scrutinize-reproduce` and `scrutinize-promote` (wired 2026-10-16, engine audit F-2.B6). The markdown phases themselves emit no span, because no Python runs per phase. The tags and the per-phase metadata below are the contract for the saved Phase 5 report, which carries them as structured fields, and for any phase helper that is later wrapped in `@observe`. Tags:

| Tag | Value | Source |
|---|---|---|
| `skill` | `scrutinize` | static |
| `target_type` | `plan` / `execution` / `file` / `dir` / `workspace` | resolved in Phase 0 |
| `target_path` | path or `<conversation-plan>` for plan target | resolved in Phase 0 |
| `relentless` | bool | resolved in Phase 0 (`--relentless` flag present) |
| `iteration` | int (only during --relentless loop) | Phase 4 closure per iteration |
| `judge_family_rotation` | `split` / `fixed-claude` | resolved per `bias-mitigation.md`, over the three-family roster; the tag name predates the split and is kept so older traces stay comparable |
| `kimi_model` | model id | `get_model("kimi_reasoning")` at call time, tagged when the k3 side ran; the reasoning pin, never `kimi-for-coding` |
| `chatgpt_model` | model id | `get_model("chatgpt_reasoning")` at call time, tagged when the ChatGPT side ran; the judge pin, which can move without moving the `chatgpt` council pin |
| `claude_model` | model id | the running session's model, whatever it is - never a pin this skill carries |

## Per-phase span metadata

Each phase emits a span with structured metadata. The metadata is the same data persisted to the saved report (Phase 5) - the observability layer is a parallel sink, not a different data shape.

### Phase 0 span

- `references_loaded`: count of reference files read
- `rules_loaded`: count of rule files read
- `target_resolution_path`: which detection rule fired (priority 1-4 per `target-detection.md`)
- `confirmation_required`: bool

### Phase 1 / 2 span

- `subchecks_run`: list of subcheck IDs (1-14) actually exercised
- `subchecks_na`: list of subcheck IDs logged as `N/A (out of scope)`
- `findings_count_by_severity`: `{BLOCKER: int, HIGH: int, MEDIUM: int, LOW: int, NIT: int}`
- `findings_count_by_confidence_band`: `{0-24: int, 25-49: int, 50-74: int, 75-100: int}`
- `artifact_evaluator_called`: bool (deterministic layer toggle)
- `artifact_evaluator_findings`: count of findings from `artifact-evaluator.py`

### Phase 2.5 span (when refutation runs)

- `refutation_layer`: `2.5a` / `2.5b` / `both` / `skipped`
- `refuted_count`: int (findings dropped by refutation)
- `refute_partial_count`: int (findings downgraded by refutation)
- `survived_count`: int (findings that reached Phase 3)
- `judge_families_used`: list (e.g. `['claude', 'k3', 'chatgpt']`)
- `debate_runs`: count of Phase 2.5b debates dispatched
- `position_swap_count`: int (number of Meta-Judge calls where Skeptic appeared first)
- `skip_reason`: string (only when skipped: `plan-target` / `no-refute-flag` / `no-findings` / `family-unavailable` / `sensitive-mode`)

### Phase 3 span

- `findings_above_threshold`: count surfaced to CEO
- `findings_below_threshold`: count hidden by default (require `--include-low-confidence`)
- `grade`: `PASS` / `PASS-WITH-NOTES` / `NEEDS-REWORK` / `BLOCKED`

### Phase 4 span (per approval action)

- `approval_command`: `approve all` / `approve <ids>` / `reject all` / `revise` / `skip` / `flag-as-fp`
- `applied_count`: int
- `deferred_count`: int
- `flagged_as_fp_count`: int
- `post_apply_check_pass_count`: int
- `post_apply_check_fail_count`: int

### Phase 4.5 span (when promotion runs)

- `eligible_findings`: count
- `auto_scaffold_offered`: bool (R5)
- `auto_scaffold_accepted`: `scaffold-and-promote-all` / `scaffold-and-promote-subset` / `scaffold-only` / `skip` / `not-offered`
- `promoted_count`: int
- `skipped_count`: int

### Phase 5 span

- `report_path`: absolute path to saved report
- `report_word_count`: int
- `hidden_chars_clean`: bool (must be true)

## Cost telemetry

The fields below are the cost contract. The `scrutinize-judge` span records each external judge call's wall clock; token counts reach Langfuse only if the proxy transport reports them, which was not verified when the spans were wired. The report aggregates:

- `input_tokens_total`
- `output_tokens_total`
- `cache_creation_input_tokens` (if prompt caching applies)
- `cache_read_input_tokens`
- `wall_clock_seconds_per_phase`
- `wall_clock_seconds_total`

These are visible on the Langfuse dashboard and feed the cost-per-finding metric used to triage R6 debate vs R1 refutation cost-effectiveness over time.

## Trace ID in saved report

Every Phase 5 saved report includes a footer section:

```text
## Observability

Langfuse trace: {trace_id}
View at: https://cloud.langfuse.com/trace/{trace_id}
(blank when LANGFUSE_ENABLED=false or SENSITIVE_MODE is not cleared)
```

When observability is disabled (SENSITIVE_MODE not cleared, LANGFUSE_ENABLED=false, or langfuse package missing), the trace_id field is `_disabled_` and the URL is omitted.

## Wiring pattern

The skill does NOT call Langfuse directly. The pattern follows the workspace standard:

1. The external judge sides (Phase 2.5 refutation, Phase 2.5b debate) go through `scripts/scrutinize-dispatch.py --judge`, which calls the shared proxy transport inside its `scrutinize-judge` span. The role scan, currency check, reproduction and promotion subcommands each run in their own span the same way.
2. A phase helper written in Python later takes `@observe(name="scrutinize-phase-N")` from `scripts/utils/observability.py`; none exists today.
3. The skill itself (markdown-driven) cannot emit Langfuse spans directly. What no span covers is reported as structured fields in the saved Phase 5 report.

## Sensitive and disabled modes

When `SENSITIVE_MODE` is not explicitly cleared OR `LANGFUSE_ENABLED=false`:

- All `@observe` decorators degrade to no-op (already handled by `scripts/utils/observability.py`)
- The saved report still includes the "Observability" footer, with `trace_id: _disabled_` and a one-line reason

This keeps a sensitive session off third-party telemetry while keeping the
artefact format consistent.

## Surfacing the run history

**The data is always written, and it has no reader today.** Every pass appends to `*.md` under `scrutiny-sheets` and to `runs.jsonl`; nothing aggregates them, so read `runs.jsonl` directly.

The split the old design drew is worth keeping when anyone does write one: the data is written unconditionally, and the presentation layer is the part that is optional.
