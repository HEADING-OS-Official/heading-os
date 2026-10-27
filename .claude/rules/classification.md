---
paths:
  - "reference/**"
  - "config/routing-map.yaml"
---
<!-- version: 5.0.0 | last-updated: 2026-10-12 -->

# Record Classification Policy

Last Verified: 2026-09-25

Path-scoped since 2026-09-04. The obligation below fires on a FILE PATH, "I am
creating something under `reference/`", which is the one signal the harness can
match mechanically, so this rule loads when a tool call names the one
ask-directory or the routing map it tells you to edit, and costs nothing on the
sessions that never touch them.

Two limits, stated rather than left to be found. The match is on a tool's
`file_path` argument, so a file created through `Bash` (a heredoc, a `cp`) does
not load this rule. And a path outside the globs above gets no prompt to
classify, and if no rule matches, it takes its tree's default: `engine` under
the ENGINE root or `private` (`ceo-only`) under the DATA root. Neither is a
hole in the engine/data seam: `scripts/leak-guard.py`, `scripts/utils/engine_guard.py` and the
push-time content scan in `scripts/push-all.py` (also run by the pre-push hook) refuse private content at the
push, whatever this rule did or did not do at the keyboard.

Every workspace record resolves to one of two **routing destinations** (HEADING OS
engine/data separation):

- **engine**: code, public (`.heading-os`).
- **private**: CEO data, never shared (`.heading-os-data`).

A third, **corporate**, was retired with the executive model (HOS-130/132,
2026-09-25): a map that declares it is read as `private` with a stderr warning
and fails `scripts/classification-health.py`. Never offer it for a file.

Privacy by repository since HOS-232 (2026-10-12): every path in the data
repository is `private`, and no rule is read there. An engine path is `engine`
only when `config/engine-allowlist.yaml` covers it AND the engine's
`config/routing-map.yaml` routes it `engine`; any other engine path, and any
path outside both trees, unresolvable or ambiguous, is REFUSED (`RoutingRefused`),
never quietly `private`. Resolved by `get_routing_destination()` in
`scripts/utils/workspace.py`; health check `scripts/classification-health.py`.
A *broken* list answers `engine` for nothing.

The full resolution order and its worked example are in
`engine-data-segregation-contract.md` § Record classification, in the data
repository under registry name `spec-docs`.

## When Creating New Files

**Always ask "engine or private?"** when creating a new file under `reference/`
(an `engine` procedure file vs `private` CEO content); the answer is not obvious
from the path.

A new file in the data repository needs no question: context documents,
knowledge, and the DataStore (the registry group `datastore-homes`) all resolve
`private`, like everything else there.

**Never ask** — these always resolve `private` (CEO data):
- the registry names `deliverables` and `drafts` — CEO deliverables
- the registry name `crm-contacts` — personal CRM data
- the registry names `plans` and `threads-business` —
  session/operational state

**Never ask** — these always resolve `engine` (shared code, public):
- `.claude/rules/`, `.claude/skills/`, `.claude/hooks/` — workspace logic
- `scripts/` — utility scripts (CEO-personal scripts get an explicit `private` rule)
- `docs/INSTALL.md`: the install guide, beside the user and tool guides the official build lays into `docs/`
- `config/` — except per-instance/identity configs (`private`)

## The record-level key: `sensitivity:`

Added 2026-09-12 by operator directive. Routing decides WHERE a record lives.
`sensitivity:` decides whether a record may be attached to anything sent to a
third party, and it is the only record-level classification this workspace reads.

    sensitivity: open | internal | ceo-only

Write it in the YAML frontmatter of the record itself. Case and surrounding
whitespace do not matter, so `CEO-Only` is the same value as `ceo-only`.

Why it exists: the air gap that used to define the ceo-only boundary was removed
on 2026-09-12, and no publish path read any record-level field, so the marking
moved to where it can be enforced.

`scripts/utils/sensitivity.py` is the single reader. It is consulted by every
path that copies a record toward a third party: the file-attachment paths
`send-email.py`, `gmail-draft.py` and the Telegram skill's `send-file`.

Three things it does, and one it deliberately does not:

- `ceo-only` refuses. So does an unrecognised value, a null, a list, or
  frontmatter that will not parse: anything this workspace cannot prove
  publishable is refused, and the refusal names the file and the reason.
- `classification: ceo-only` is honoured as a legacy alias for the refusal, and
  for nothing else. Any other `classification:` value is ignored here.
- **An ABSENT key permits**, and the path decision above stands. The
  record-level layer only ever SUBTRACTS from what routing allows. MEASURED
  2026-09-12 against the live overlay: 600 files then resolved `corporate` and
  282 of them were fonts, PDFs, spreadsheets and images that can never carry
  frontmatter, so refusing on absence would have been a permanent outage
  rather than a control.

What that means when you attach a record: marking is opt-in, and an unmarked
record goes out. If a record must never be attached at all, mark it
`sensitivity: ceo-only`.

Operator directive, 2026-09-13, and it overrides any narrower wording elsewhere:
**ceo-only means private means NOBODY.** There is no internal exception. A
record marked `ceo-only` is not "held back from the executives and fine for the
team", and it is not "internal, so it can circulate inside the company". The three
values are not a gradient of audiences with the operator's circle at the
permissive end: `ceo-only` is a full stop.

## After Classification

A new data file needs no rule: everything in the data repository is
`private`. Never write a data address into the engine's
`config/routing-map.yaml`: the engine map is public and the address would
disclose it. A new ENGINE file must be covered by `config/engine-allowlist.yaml`
and routed `engine` by the engine map, or the resolver refuses it and the
commit gate with it. Then run `scripts/classification-health.py` to confirm.
