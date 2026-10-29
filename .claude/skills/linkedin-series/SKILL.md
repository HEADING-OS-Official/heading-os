---
name: linkedin-series
description: "Alias of /linkedin series. Typed by the operator only."
disable-model-invocation: true
allowed-tools: "Skill"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-routing:
  category: Content
  label: /linkedin-series
  triggers:
    - NEVER auto-trigger. Explicit `/linkedin-series` only; runs /linkedin in mode series.
  exclusions: []
  router: manual
---

Run the `/linkedin` skill in mode `series` with the same arguments.
