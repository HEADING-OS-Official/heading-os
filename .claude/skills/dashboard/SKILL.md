---
name: dashboard
description: "Build the daily CEO morning dashboard - one page over CRM health, pipeline, calendar, email and data freshness. Use for the morning briefing. For the end-of-week review use /weekly-review; for overdue manual actions use /radar."
argument-hint: "(no arguments)"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.1"
context: fork
allowed-tools: "Bash(.venv/bin/python:*), Read"
model: sonnet
x-heading-capability:
  what: >
    Generates the daily CEO Morning Dashboard - a single-page HTML + PDF briefing
    aggregating CRM health, pipeline, calendar, email, strategy, and data
    freshness.
  how: >
    Type /dashboard (no arguments). It runs scripts/generate-dashboard.py --pdf,
    writes to YYYY-MM-DD/ under dashboard, sanitizes the output, and
    reports urgent items, today's meetings, and a pipeline snapshot.
  when: >
    Use for the morning operational briefing. For a full context load at session
    start use /prime; for the logical next action use /next; for the end-of-week
    review use /weekly-review.
x-heading-routing:
  category: Operations
  triggers:
    - dashboard
    - morning dashboard
    - daily brief
  exclusions:
    - Full prime -> /prime
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

# CEO Morning Dashboard

Generate the daily CEO Morning Dashboard -- a single-page operational briefing aggregating CRM health, pipeline, calendar, email, strategy, and data freshness.

## Trigger Phrases
"dashboard", "morning dashboard", "daily brief"

## Execution

### Step 1: Generate the dashboard

Run the generation script:

```bash
.venv/bin/python scripts/generate-dashboard.py --pdf
```

This reads all workspace data sources and produces:
- `YYYY-MM-DD/morning-dashboard.html` under `dashboard`
- `YYYY-MM-DD/morning-dashboard.pdf` under `dashboard`

### Step 2: Validate hidden characters

Bash gets no path redirect. Resolve the report directory BY NAME.

```bash
DASHBOARD_DIR="$(.venv/bin/python scripts/data-path.py path dashboard)"
.venv/bin/python scripts/sanitize-text.py "$DASHBOARD_DIR/YYYY-MM-DD/morning-dashboard.html" --scan
```

### Step 3: Report to the operator

Summarize key findings from the dashboard:
- Number of urgent items (RED contacts, overdue commitments)
- Today's meeting count
- Pipeline snapshot (active deals, won, investors)
- Any stale data files needing refresh

Format: concise 3-5 bullet summary with file paths.

After the hidden-characters confirmation line, append a one-line branding confirmation:
`Branding: corporate (dark cover, GT Standard, orange corner, blue accents).`

The GT Standard fonts or the canonical brand logos can fail to embed, when a
file is missing from `brand-kit`. Surface that explicitly instead of a
silent fall back to Inter.

### Optional: Live Sea State Enrichment

If the operator requests a live sea state update, run a quick WebSearch for:
- the company's product category and sector, in the words `business-info` uses, latest news and developments
- Key regions ([priority regions]) telecom news

Add a brief 2-3 sentence sea state note to the summary.

## Data Sources

| Source | File | What It Provides |
|--------|------|-----------------|
| CRM Health | `scripts/crm-health.py --json` | Contact health scores, commitments |
| Pipeline | `pipeline` | Active deals, investors, partnerships |
| Calendar | `upcoming.md` in `exchange-calendar-cache` | Today's meetings |
| Email | `inbox-latest.md` in `exchange-email-cache` | Latest inbox |
| Strategy | `strategy` | Heading, priorities, phase |
| Metrics | `current-data` | Headcount, product, market data |
| Freshness | `Last verified:` headers of `pipeline`, `current-data`, `strategy` and `people` | Data age tracking |
| Capture Payoff (R10) | `knowledge-base` + `scripts/odin-cadence.py --json` | Signals captured in the last 7 days + episode clusters ripe to promote to an Odin principle. CEO-only: the panel auto-hides on a workspace with no Odin brain. |

## Output Location

`YYYY-MM-DD/morning-dashboard.html` under `dashboard` (+ `.pdf`)
