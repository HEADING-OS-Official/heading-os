---
name: weekly-review
description: >
  CEO weekly review: sea state, heading, pipeline movement, team state,
  content cadence, priorities, and continuity tracking. Use at the end of each
  week (Friday) or start of the next (Sunday/Monday). Saves the completed
  review under weekly-reviews for longitudinal tracking.
argument-hint: "[week date]"
allowed-tools: "Read, Write, Edit, Bash(.venv/bin/python:*), Glob, mcp__hindsight-main__reflect"
model: sonnet
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "2.2"
x-heading-capability:
  what: >
    Produces the CEO weekly review - sea state, heading, pipeline movement, team
    state, content cadence, top-3 priorities, and course corrections - with
    week-over-week continuity against the prior review.
  how: >
    Type /weekly-review [week date]. It reads the context and pipeline files,
    analyzes movement and content cadence, and saves the 2-3 page review to
    YYYY-MM-DD-weekly-review.md under weekly-reviews.
  when: >
    Use at the end of the week (Friday) or start of the next. For the daily
    briefing use /dashboard.
x-heading-routing:
  category: Operations
  triggers:
    - weekly review
    - end of week review
    - friday review
  exclusions:
    - Daily briefing -> /dashboard
  compound: 'No'
  router: auto
---

**Data addresses.** Resources in the data repository are named here by their
logical name, never by a path. Resolve the name before the file operation and
use what it prints:

    .venv/bin/python scripts/data-path.py path <name>

Use `read <name>` for a source that may legitimately be absent, and
`read --required <name>` for one whose absence is a defect. An unknown name
refuses; it never resolves to a plausible directory, and a path written out
here goes stale the day the directory moves.

# Weekly Review

CEO weekly review: sea state, heading, pipeline movement, team state,
content cadence, priorities, course corrections, and week-over-week continuity.

## Variables

week_of: [Date of the week being reviewed, e.g. 2026-03-22]
notes: [Anything specific on your mind going into this review - optional]

---

## Phase 0: Previous Week Continuity

Before building this week's review, check for a prior review.

1. Scan `weekly-reviews` for the most recent `*-weekly-review.md` file.
2. If found, read it and extract:
   - **Top 3 Priorities** from the previous week.
   - **Course Corrections** that were prescribed.
3. For each priority, assess: accomplished, partially accomplished, or not addressed.
   Use evidence from `current-data`, `pipeline`, CRM activity,
   and any other observable signals.
4. For each course correction, assess: executed, in progress, or not started.
5. Summarize the continuity status as a short block. Put it at the top of the
   new review (see output template below).

If no previous review exists, note "First review - no prior baseline" and proceed.

---

## Phase 1: Context Loading

Read these files to build the review:

- `current-data`: Current metrics, milestones, active workstreams
- `pipeline`: Active deals and investor conversations
- `strategy`: Strategic priorities and 3-year arc
- `people`: Key contacts and team state
- `operational-state-model`: Operational states and vocabulary
- `state-check-guide.md` under `company-reference` in the data repository: State Check framework; absent on a public clone, where you say so and continue without it

Also scan:
- `linkedin-archive`: LinkedIn posts published this week (filed there once live, dated within the review week)
- `linkedin-drafts`: drafts still in flight: its TOP-LEVEL files only, never a subdirectory, because the C3 move nests `linkedin-archive` inside it
- `newsletters`: Newsletter editions published this week
- The five context homes below. Check "Last verified" or "Last updated" dates in every file.

---

## Phase 2: Analysis & Synthesis

### Pipeline Movement Analysis

Read `pipeline` and compare against the previous review's pipeline snapshot
(if available) or against observable signals:

- **Deals that advanced stages**: any deal that moved forward (e.g., Discovery -> Demo, Demo -> NDA)
- **Deals that stalled**: any deal with no recorded movement in >14 days
- **New deals added**: deals not present in the prior review
- **Deals won or lost**: any closed outcomes
- **Weighted pipeline value**: sum of Est. Value columns where available, weighted by stage:
  - Discovery: 10%, Demo: 20%, NDA/Workshop: 30%, Preparing/Proposal: 50%, Closed-Won: 100%
  - Use "TBD" aggregate if most values are unknown

### Content Cadence Analysis

- Count LinkedIn posts in `linkedin-archive` dated within this review week
  (published); report the top-level files of `linkedin-drafts` as drafts in
  flight, never counting its subdirectories. Target: 3 posts/week.
- Check `newsletters` for editions dated within this review week.
  Target: 1 newsletter/week.
- Flag if behind cadence with specific gap.

### Context Freshness Scan

The context files fan out over five homes. Read all five, never one root:
`context-position`, `context-reference`, `context-entity`,
`context-matter`, `context-measurement`.

For each file in those five:
- Extract the "Last verified" or "Last updated" date.
- Flag any file where that date is >14 days old.
- Compile a list for the Context Refresh Prompt section.

---

## Phase 3: Review Output

Produce the following review document. Keep total output to 2-3 pages - actionable,
not exhaustive.

```markdown
# CEO WEEKLY REVIEW: Week of [date]

---

## Previous Week Continuity

[If prior review exists:]

**Priorities from [prior week date]:**
1. [Priority 1] - [Accomplished / Partially accomplished / Not addressed]. [Brief evidence.]
2. [Priority 2] - [Status]. [Brief evidence.]
3. [Priority 3] - [Status]. [Brief evidence.]

**Course Corrections prescribed:**
- [Correction 1] - [Executed / In progress / Not started]
- [Correction 2] - [Status]

Continuity grade: [Strong / Mixed / Weak]

[If no prior review: "First review - no prior baseline."]

---

## Sea State

External conditions this week: [market, geopolitical, competitive dynamics --
what is the environment doing?]

---

## Heading Confirmation

Are we on heading? [Yes / Slight drift / Significant drift]
Key evidence: [2-3 observable signals]

---

## Pipeline Movement

**Advances:**
- [Deal] moved from [Stage A] to [Stage B]

**Stalled (>14 days no movement):**
- [Deal] - last activity [date/description]

**New this week:**
- [Deal] - [Region] - [Stage]

**Won/Lost:**
- [Deal] - [outcome]

**Weighted pipeline value:** [calculated value or "Insufficient data - most values TBD"]

**1-2 pipeline actions this week:**
- [Action 1]
- [Action 2]

---

## Team State

- Overall operational state: [assessment]
- Any signals of drift to address
- Any wins to acknowledge

---

## Content Pulse

| Metric | Target | Actual | Status |
|--------|--------|--------|--------|
| LinkedIn posts | 3/week | [N] | [On track / Behind] |
| Intelligence Briefing newsletter | 1/week | [N] | [On track / Behind] |

[If behind cadence, note specific gap and suggested remedy.]

---

## This Week's Top 3 Priorities

1. [Priority 1 - outcome + owner]
2. [Priority 2 - outcome + owner]
3. [Priority 3 - outcome + owner]

---

## Course Corrections

[Any adjustments to make to heading or operational states]

---

## Suggested Skills This Week

[Which /skills from the skill army are most relevant this week, and why]

---

## Context Refresh Prompt

[If any context file has a "Last verified" date >14 days old:]

These context files need refreshing:
- `[full path to the file]`: last verified [date] ([N] days ago)
- ...

Want me to draft updates for these files?

[If all files are current: "All context files verified within the last 14 days."]

---

## Knowledge Capture

- Any decisions this week that warrant a `/deep-think` session?
- Any insights worth capturing? (`/odin log` for CEO; `/zk add` to the knowledge base)
```

---

## Phase 3.5: Odin Reflection Cadence (CEO workspace only)

Existence guard FIRST: run the Bash test `test -d "$(.venv/bin/python scripts/data-path.py path odin-brain)" && test -f scripts/odin-cadence.py`. An unknown name or a missing registry resolves to nothing here, and the guard then fails closed. If it exits non-zero, silently skip this ENTIRE phase - say nothing about it. On a workspace with no brain the guard fails, so this phase is a provable no-op. This is a Bash test the model runs, NOT a Python code guard. The graceful-degradation precedent is `/odin` SKILL.md Rules 9 and 13: degrade quietly, never fail the operation.

If the guard passes:

1. Run `.venv/bin/python scripts/odin-cadence.py --json`. Treat any non-zero exit, empty output, or unparseable JSON as "no clusters - skip silently".
2. Parse `reflect_clusters` from the JSON. If `reflect_clusters >= 1`, surface to the CEO: "N episode cluster(s) ready to mature - run `/odin reflect`?" Do NOT re-check `.last-reflect` here. The detector already reads it, and counts only clusters with an episode logged after the last confirmed pass. The JSON carries `last_reflect` if you want to show the date.
3. ONLY on an explicit CEO go-ahead, invoke `/odin reflect` via the Skill tool. Reflect's own per-graduation CEO gate then applies. Nothing enters the brain without per-candidate approval, and reflect advances `.last-reflect` on a confirmed pass.

NEVER auto-invoke `/odin reflect` without an explicit CEO go-ahead. NEVER run this phase on a workspace that lacks the brain.

---

## Phase 3.6: Proposed Conduct Rules From Hindsight

Operator decision, 2026-10-05: "Add to /weekly-review a question to Hindsight about new rules. The answer is saved as a file and stays a proposal until you approve it." (translated) This phase asks one question and writes one file. It writes nothing into any rule file.

1. Call the `reflect` tool of the `hindsight-main` MCP server once, over bank `main`. Use `budget: "mid"`, `tags: ["source:chat"]`, `include_based_on: true`, `max_tokens: 8000`, and a `response_schema` with a `rules` array. Each item has `rule`, `why`, `stated_by_operator`, `sources` and `dates`. Name the first and last day of the review week in the query. Ask only for standing rules whose evidence falls in that week. Use MCP, not the `hindsight` CLI. On 2026-10-05 the CLI dropped this call at 118 seconds with a false "Cannot connect". The MCP call ran to the end in two to four minutes.
2. Drop every item whose `sources` are empty, or whose dates all fall outside the week. Never add a source or a rule that Hindsight did not return.
3. Read every file in `.claude/rules/`. Mark each remaining item `new`, `covered by <id or file>`, or `contradicts <id or file>`.
4. Save the result beside the review, in the directory `weekly-reviews` resolves to, as `YYYY-MM-DD-rule-proposals.md`. Write the header line `PROPOSAL - not a rule until the operator approves it`. Give one block per item with the rule text, the verdict, and the sources as `conversation:<id>` or file paths. Quote rule texts only, never private conversation content.
5. Add one line to the review's Knowledge Capture section: the file path and the count per verdict.

If Hindsight does not answer, or the call fails, write `Rules not checked this week: <reason>` in the review and continue. Never write proposals from memory.

Rules that the operator states directly are still recorded at once, outside this phase.

---

## Phase 4: Save Review

1. Confirm that `weekly-reviews` exists (`.venv/bin/python scripts/data-path.py path weekly-reviews`).
   Do not create a directory elsewhere when it does not: report the miss and stop.
2. Save the completed review to:
   `YYYY-MM-DD-weekly-review.md` under `weekly-reviews`
   where `YYYY-MM-DD` is the `week_of` date.
3. Confirm save path to the operator.

---

## Voice & Style

- Use maritime vocabulary: Sea State, Heading, Course Corrections, Drift.
- Hyphens, not em dashes.
- Product names styled exactly as the company writes them.
- The company's own terms per `.claude/rules/terminology.md`.
- Be direct. No filler. Every line should be actionable or diagnostic.
- Numbers are precise (Voss: $347,850 not $350,000).

## NEVER

- Fabricate pipeline data. If a deal's status is unclear, say so.
- Skip the continuity check. Week-over-week tracking is the backbone.
- Produce more than 3 pages. Compress ruthlessly.
- Replace the company's own terms with generic ones.
- Use em dashes or double dashes anywhere. Single hyphens only.
