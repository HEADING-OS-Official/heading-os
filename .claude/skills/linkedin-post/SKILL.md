---
name: linkedin-post
description: "Alias of /linkedin post. Typed by the operator only."
disable-model-invocation: true
allowed-tools: "Skill"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-routing:
  category: Content
  label: /linkedin-post
  triggers:
    - NEVER auto-trigger. Explicit `/linkedin-post` only; runs /linkedin in mode post.
  exclusions: []
  router: manual
---

Run the `/linkedin` skill in mode `post` with the same arguments.
