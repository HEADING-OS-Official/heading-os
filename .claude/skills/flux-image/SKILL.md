---
name: flux-image
description: "Alias of /image generate. Typed by the operator only."
disable-model-invocation: true
allowed-tools: "Skill"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-routing:
  category: Content
  label: /flux-image
  triggers:
    - NEVER auto-trigger. Explicit `/flux-image` only; runs /image in mode generate.
  exclusions: []
  router: manual
---

Run the `/image` skill in mode `generate` with the same arguments.
