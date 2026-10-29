---
name: investor-pitch
description: "Alias of /pitch investor. Typed by the operator only."
disable-model-invocation: true
allowed-tools: "Skill"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-routing:
  category: Strategy
  label: /investor-pitch
  triggers:
    - NEVER auto-trigger. Explicit `/investor-pitch` only; runs /pitch in mode investor.
  exclusions: []
  router: manual
---

Run the `/pitch` skill in mode `investor` with the same arguments.
