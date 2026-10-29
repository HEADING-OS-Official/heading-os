---
name: investor-update
description: "Alias of /pitch update. Typed by the operator only."
disable-model-invocation: true
allowed-tools: "Skill"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-routing:
  category: Strategy
  label: /investor-update
  triggers:
    - NEVER auto-trigger. Explicit `/investor-update` only; runs /pitch in mode update.
  exclusions: []
  router: manual
---

Run the `/pitch` skill in mode `update` with the same arguments.
