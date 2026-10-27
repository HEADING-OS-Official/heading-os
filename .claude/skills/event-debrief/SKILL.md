---
name: event-debrief
description: Produce a post-event recap after a conference, summit, or VIP engagement - contacts met, conversations, commitments, leads to log, and follow-up actions. Use after attending an event to capture outcomes before they decay. Trigger when the user says "event debrief", "post-event recap", or "debrief [event]".
argument-hint: "[event name]"
allowed-tools: "Read, Write, Bash(.venv/bin/python:*)"
model: sonnet
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.1"
x-heading-capability:
  what: >
    Turns raw event notes and business cards into a structured 10-section
    debrief - leads table, meetings held, competitive intel, market signals,
    what worked, action items with owners and deadlines, and pipeline updates.
  how: >
    Run /event-debrief [event name] and paste the messy notes. Produces the
    debrief, then offers to log CRM interactions for each contact met.
  when: >
    Use after a conference, summit, or VIP launch to capture leads and lessons.
    To then send follow-ups to the people met, use /follow-up for each one.
x-heading-routing:
  category: Operations
  triggers:
    - event debrief
    - post-event recap
    - debrief [event]
  exclusions:
    - N/A
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

# Event Debrief

Post-event recap with leads, follow-ups, lessons learned, and action items.

## Variables

event: [Event name — MWC, regional expos, launch events, partner summit, etc.]

dates: [Event dates]

notes: [Paste raw notes, business cards collected, conversations had, impressions — as messy as needed]

outcome_assessment: strong | moderate | below-expectations — optional

---

## Instructions

Before processing, read:
- `pipeline` — Current pipeline to map new leads against
- `people` — Check if any contacts from the event are already known
- `strategy` — Strategic priorities to assess event ROI against

Process the raw notes and produce a structured debrief.

---

## Event Debrief Structure

### 1. Executive Summary
- Event name, dates, location
- The company's presence (booth, meetings, presentations)
- Overall assessment in one sentence
- Top 3 outcomes

### 2. Leads Generated

| Contact | Company | Role | Interest Level | Next Step | Owner | Deadline |
|---------|---------|------|---------------|-----------|-------|----------|
| | | | Hot / Warm / Cold | | | |

### 3. Meetings Held
For each meaningful meeting:
- Who: name, title, company
- What discussed: 2-3 sentences
- Their interest: what specifically resonated
- Commitment: what they said they'd do next
- Our follow-up: what we need to do, by when
- Pipeline impact: new opportunity / existing deal advancement / relationship building

### 4. Competitive Intelligence Gathered
- What competitors were present
- What they were saying/showing
- Any intel on their deals, positioning, or customer conversations
- Market sentiment observations

### 5. Market Signals
- Themes that kept coming up in conversations
- What buyers are actually asking for (vs. what we assumed)
- Regulatory or geopolitical signals
- Technology trends observed

### 6. What Worked
- Messages that resonated
- Materials or demos that generated interest
- Approach or positioning that landed

### 7. What to Improve
- What didn't land
- Gaps in our materials or pitch
- Logistics or preparation improvements for next time

### 8. Action Items

| Action | Owner | Priority | Deadline |
|--------|-------|----------|----------|
| | | High/Medium/Low | |

### 9. Pipeline Updates
Recommend updates to `pipeline` based on event outcomes.

### 10. People Updates
Recommend additions to `people` for new key contacts.

---

**Output:** Complete event debrief. Organized, actionable, with clear ownership and deadlines. Ready to distribute to relevant team members.

## CRM Auto-Log

After completing the event debrief:
1. For each contact in the "Meetings Held" section:
   a. If CRM file exists: log the interaction (date, type: Event, summary of discussion and commitments) and update `last_touch`
   b. If no CRM file: offer to create one for contacts marked Hot or Warm with `/crm add`
2. Add any new commitments from the debrief to the contact's Active Commitments section
3. Flag any contacts where the event interaction reveals a change in relationship stage

## Knowledge Base

After the debrief is complete, offer this: "Want me to capture the key takeaways? The `/odin log` command records them as an episode in Odin's brain (CEO-only). The `/zk distill` command adds the durable takeaways to the knowledge base."
