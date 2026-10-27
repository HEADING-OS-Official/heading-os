---
paths:
  - "scripts/**"
  - "config/tool-risk.json"
---

# Tiered Risk Gate (R3)

Last Updated: 2026-10-27
Last Verified: 2026-10-27

Path-scoped rule. Loads when work touches the Action Queue executor, the risk ledger, or `scripts/action-queue.py sweep`, the hand-run command that routes cards (no daemon or timer has routed them since HOS-7, 2026-09-21). Sibling to `trace-id.md`. Governs how the Action Queue decides friction per action, and the one invariant that must never be weakened.

## The model

Every Action Queue card carries an `action_type`. The reversibility ledger `config/tool-risk.json` maps each `action_type` to one of three tiers, resolved by `scripts/utils/tool_risk.tier_for()`:

| Tier | Meaning | Flow |
|---|---|---|
| `autonomous` | No-op / read-only / display-only (`note`, `alert`) | Surfaced read-only; no CEO click; never auto-sends anything. |
| `notify` | Reversible state edit (`pipeline_update`, `crm_write`, `knowledge_write`, `task_create`) | `scripts/action-queue.py sweep`, run by hand, marks a notify card `applied` only when its type is in `_AUTO_APPLY_TYPES` (today `pipeline_update` alone) and reports every other notify card as not handled. Sweep changes no pipeline, CRM or note itself: the producing skill made the edit. A `prev_value` on the card is a producer-skill convention that no code sets or checks; when present, a revert is a manual edit back to it. |
| `gated` | Irreversible outbound send (`email_send`, any `telegram_send`) | Hard review gate: the executor sends only after an explicit CEO approve click. **Unchanged. This is the lethal-trifecta control.** |

The tier is stamped on the card at append time (`append_cards`) and `scripts/action-queue.py sweep` routes by it when it runs: autonomous display types stay surfaced for the CEO to read and dismiss, notify cards in `_AUTO_APPLY_TYPES` are marked applied and the rest are reported, gated sends wait for `approve`.

> **`telegram_send` is reserved-and-gated but not yet wired.** It is pre-registered in `send_capable` (floors to `gated`), but no transport is attached. `scripts/action-queue-execute.py` now carries an explicit refusal branch for it: an approved telegram card returns `send_failed` with `"telegram executor not implemented (501)"` and `classification: "permanent"` in the executor's result, so it fails loudly rather than skipping silently. The approve path in `scripts/action-queue.py` records that failure on the card with no classification, so the stored card does not say `permanent`. Approved telegram cards therefore do NOT deliver. Do not read the presence of that branch as the transport having landed; it is the placeholder that refuses in the safe direction (a gated send that cannot send).

## The invariant (non-negotiable)

**The ledger is data; the send-gate is code.** Any `action_type` listed in the ledger's `send_capable` set resolves to `gated` no matter what its `tiers` entry says. A `tool-risk.json` edited to mark `email_send` autonomous still resolves `gated`. Unknown or missing types also resolve `gated` (safe default, matching the workspace "missing metadata → friction-maximal" convention).

This makes the lethal-trifecta control impossible to defeat by editing a config file. The assertion that a tampered ledger cannot auto-send is no longer made by any live test, the 2026-09-21 test-suite split having taken the two that made it; the invariant itself is in the code, and it is the code that has to be read to confirm it. The ledger can *raise* friction freely; it can never *lower* a send below `gated`.

When adding a new `action_type`:
- If it can send anything outbound, add it to `send_capable` (it floors at `gated`).
- Otherwise add a `tiers` entry; if you forget, it defaults to `gated` until classified.
- Never remove an entry from `send_capable`. Changes to the invariant require CEO approval.

## Classification

Determine it with `get_routing_destination()` in `scripts/utils/workspace.py`
(`.claude/rules/classification.md`), never from a list written here. This rule,
`scripts/utils/tool_risk.py` and `config/tool-risk.json` are engine files.

## Change control

Changes to the `send_capable` invariant or this rule require the operator's explicit approval.
