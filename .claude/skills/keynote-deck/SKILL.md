---
name: keynote-deck
description: "Alias of /pitch keynote. Typed by the operator only."
disable-model-invocation: true
allowed-tools: "Skill"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-routing:
  category: Strategy
  label: /keynote-deck
  triggers:
    - NEVER auto-trigger. Explicit `/keynote-deck` only; runs /pitch in mode keynote.
  exclusions: []
  router: manual
---

Run the `/pitch` skill in mode `keynote` with the same arguments.
