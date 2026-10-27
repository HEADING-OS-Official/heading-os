# Calibrate - Final state report template

Consumed by: `.claude/skills/calibrate/SKILL.md` Phase 5 (Step 5.4).
Last Updated: 2026-10-16

Static template for the final state report. The orchestration around it
(private vs queue routing, sanitisation, rollback policy) stays in SKILL.md.
/calibrate makes no git commit and stages nothing: memory files and the
review queue live in the data repository, whose git belongs to HELM, and
`.claude/settings.local.json` is gitignored.

## Final state report (Step 5.4 template)

Print:

```
Applied {M} memory patches: {paths}

Written to the review queue ({C} proposals): {date}_engine-review.md under calibrate-reviews
  - {S} settings.local: {properties} (applied by HELM)
  - {K} skills: {paths} (applied as an approved yard change)
  - {R} rules: {paths} (applied as an approved yard change)
  - {E} other engine-routed: {paths}

Light mode: {true|false}
{Hidden-character cleanup note if any}

Nothing committed. HELM commits the data repository on the operator's approval.

Rollback:
  - Memory files: edit manually in the canonical auto-memory store
  - Review queue: the proposals are inert until someone applies one
```
