# LinkedIn Post

Consumed by: `.claude/skills/linkedin/SKILL.md`, mode `post` (the alias `/linkedin-post` runs the same mode).

Last Updated: 2026-10-30

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Ask the workspace's data-address resolver for
the real address before the file operation, and use what it answers. An unknown
name refuses rather than resolving to a plausible directory, and a path written
out here goes stale the day the directory moves.

    .venv/bin/python scripts/data-path.py path <name>

Draft a LinkedIn post in the operator's authentic voice.

## Variables

topic: [What is this post about?]

angle: [What's the specific angle, insight, or counterintuitive point?]

context: [Any specific details, data points, events, or recent observations to include]

length: short (~150 words) | medium (~300 words) | long (~500 words) — default: medium

---

## Instructions

Before drafting, read:
- `voice-guide` — Complete voice guide (especially the LinkedIn section)
- 3-5 varied published posts from `linkedin-archive`, loaded into `<examples>` per `reference/voice-examples.md`
- `business-info` — company positioning and product context
- `strategy` — the category narrative and the current heading
- `datastore-index` — If the post contains specific facts or numbers, validate against source documents

Draft a LinkedIn post that:
- Opens with a single, powerful sentence that works as a standalone hook
- Builds a narrative arc: observation → tension → insight → what it means
- Uses maritime metaphors only when they arise naturally (never forced)
- Connects to the company's own category narrative, as `business-info` and `strategy` state it, when relevant
- Ends with a thought-provoking statement — not a call to action, not a question asking for comments
- Reads like a human wrote it, not a marketing team
- Has paragraph breaks every 1-3 sentences (LinkedIn is read on mobile)

After the draft, provide:
- 2 alternative opening lines to consider
- 3-5 hashtags from the hashtag list in `voice-guide`, plus the company and product hashtags in `business-info`. With neither present, pick topic hashtags and say so
- Recommended posting time (Tuesday-Thursday, 7-9am in the operator's time zone, `HEADING_OS_TZ`, typically performs best)
- One line on what to cut if shortening

## NEVER
- "I'm excited to share..."
- "Thrilled to announce..."
- "In today's rapidly evolving landscape..."
- Competitor names
- Military references
- Emojis in the body text
- Bullet-point lists as the main content structure
