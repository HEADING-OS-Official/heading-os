---
name: linkedin-archive
description: "Alias of /linkedin archive. Typed by the operator only."
disable-model-invocation: true
allowed-tools: "Skill"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-routing:
  category: Content
  label: /linkedin-archive
  triggers:
    - NEVER auto-trigger. Explicit `/linkedin-archive` only; runs /linkedin in mode archive.
  exclusions: []
  router: manual
---

Run the `/linkedin` skill in mode `archive` with the same arguments.
