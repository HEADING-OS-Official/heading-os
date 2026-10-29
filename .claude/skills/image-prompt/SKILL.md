---
name: image-prompt
description: "Alias of /image prompt. Typed by the operator only."
disable-model-invocation: true
allowed-tools: "Skill"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-routing:
  category: Content
  label: /image-prompt
  triggers:
    - NEVER auto-trigger. Explicit `/image-prompt` only; runs /image in mode prompt.
  exclusions: []
  router: manual
---

Run the `/image` skill in mode `prompt` with the same arguments.
