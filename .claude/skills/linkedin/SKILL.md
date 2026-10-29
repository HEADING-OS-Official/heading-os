---
name: linkedin
description: "LinkedIn content, in three modes. post: draft one standalone post in the operator's voice (\"linkedin post\", \"draft a post about\", \"write a post\"). series: plan three or more posts with one arc and a publishing order, when the ask names a week, a plan or a post count (\"linkedin series\", \"content series\", \"plan posts for the week\"). archive: file a piece already published from linkedin-drafts into the archive; auto-fires on English or Russian \"I published it on LinkedIn\" phrasing (\"live on linkedin\", \"опубликовал на linkedin\", \"выложил на linkedin\"). Do NOT use for LinkedIn analytics or profile and banner questions (just answer), or for other platforms."
argument-hint: "[post|series|archive] [topic, theme or slug]"
allowed-tools: "Read, Bash(.venv/bin/python:*), AskUserQuestion"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-capability:
  what: >
    Drafts a single LinkedIn post in the operator's voice, plans a multi-post
    series with a draft of its first post, or files a piece already published
    into the typed archive.
  how: >
    Run /linkedin <post|series|archive> followed by the topic, theme or slug.
    Drafting is read-only; a series plan is saved under linkedin-drafts; the
    archive mode shows a dry-run plan and moves nothing before approval.
  when: >
    Use for any LinkedIn post, series or filing. Nothing is ever published from
    here: publishing is the operator's own action.
x-heading-routing:
  category: Content
  label: /linkedin [post|series|archive]
  triggers:
    - linkedin post
    - draft a post about
    - write a post
    - linkedin series
    - content series
    - plan posts for the week
    - 3 posts
    - i published this on linkedin
    - linkedin post is live
    - live on linkedin
    - опубликовал на linkedin
    - выложил на linkedin
    - запостил на linkedin
  exclusions:
    - analytics question -> just answer
    - profile/banner topic -> just answer
  router: auto
---

# LinkedIn

One skill, three modes. Each mode's whole procedure is in its reference file.

## Mode selector

1. If the first argument is `post`, `series` or `archive`, use that mode. Pass the remaining arguments to it.
2. Otherwise infer the mode from the request. One post is `post`. Several posts, a week, a plan or a post count is `series`. A piece already published or live is `archive`.
3. If more than one mode still fits, ask one question that names the modes. Then wait.

## Modes

- `post`: read `references/post.md` and follow it.
- `series`: read `references/series.md` and follow it.
- `archive`: read `references/archive.md` and follow it.

The aliases `/linkedin-post`, `/linkedin-series` and `/linkedin-archive` run the same three modes.
