# LinkedIn Content Series

Consumed by: `.claude/skills/linkedin/SKILL.md`, mode `series` (the alias `/linkedin-series` runs the same mode).

Last Updated: 2026-10-30

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Ask the workspace's data-address resolver for
the real address before the file operation, and use what it answers. An unknown
name refuses rather than resolving to a plausible directory, and a path written
out here goes stale the day the directory moves.

The resolver, `scripts/data-path.py`, ships in the `heading-core` plugin bundle (HOS-48).

Plan a multi-post LinkedIn content series in the operator's voice around a theme or strategic moment.

## Variables

theme: [Core theme or strategic narrative — e.g., "speed vs. compliance", "the category we're creating", "what the trade show taught us"]

posts: [Number of posts — default: 4]

timeframe: [When to publish — e.g., "leading up to MWC", "during launch week", "over 4 weeks"]

goal: [What this series should accomplish — e.g., "establish category leadership", "build investor intrigue", "team culture signal"]

---

## Instructions

Before planning, read:
- `voice-guide` — Voice guide including LinkedIn section
- 3-5 varied published posts from `linkedin-archive`, loaded into `<examples>` per `reference/voice-examples.md`
- `strategy` — Strategic priorities to align content with
- `current-data` — Current milestones and proof points to reference

Produce a content series plan with:

**Series Overview:**
- Theme and why it matters now
- Strategic goal this series serves
- Audience (who we're talking to)

**For each post:**
- Post number and publish date
- Title / working concept
- Opening line (draft)
- Core angle and narrative arc (2-3 sentences)
- Key proof point or story to anchor it
- Hashtags
- How it connects to the next post in the series

**Series Arc:**
- Post 1: Hook / provocation (sets up the tension)
- Posts 2-N: Build evidence, story, proof
- Final post: Resolution / call to the future

After the plan, produce a ready-to-publish draft of Post 1.

**Save the plan** with the Post 1 draft as `[theme-slug]/series-plan.md` under `linkedin-drafts`, beside the memlog. Resolve the directory with `.venv/bin/python scripts/data-path.py path linkedin-drafts`, and give the operator the full path of the saved file. When the plan already exists, update it in place and keep the posts already marked as published. Nothing is posted: publishing is the operator's own action, and `/linkedin archive` files a post after it goes live.

---

## Session Memory (memlog)

A multi-post series is planned across turns. Keep an append-only working memory so the plan survives a context compaction and a later session can resume it.

- **On start:** if `[theme-slug]/.memlog.md` under `linkedin-drafts` is absent, `.venv/bin/python scripts/memlog.py init --workspace "$(.venv/bin/python scripts/data-path.py path linkedin-drafts)/[theme-slug]" --field topic="[theme]" --field mode=series`. If it already exists, do NOT re-run `init` — read it to resume, then `append`/`set`.
- **As you go:** record each settled angle, hook, or proof point — `.venv/bin/python scripts/memlog.py append --workspace "$(.venv/bin/python scripts/data-path.py path linkedin-drafts)/[theme-slug]" --text "post 2 anchors on the customer case study" --type decision`.
- **On wrap-up:** `.venv/bin/python scripts/memlog.py set --workspace "$(.venv/bin/python scripts/data-path.py path linkedin-drafts)/[theme-slug]" --key status --value complete`.

The `.memlog.md` file is gitignored; the series plan, `series-plan.md`, is the deliverable derived from it.
