---
name: pitch
description: "Deck and update content built from the user's own company records, in three modes. investor: a pitch deck for a fundraise with new investors (\"investor pitch\", \"pitch deck\", \"fundraising deck\", \"investor presentation\"). update: a progress update to existing investors (\"investor update\", \"board update\", \"shareholder update\", \"quarterly update\"). keynote: a speaking deck for a conference, expo, summit, VIP or government meeting (\"keynote\", \"event presentation\", \"conference slides\", \"speaking deck\"). Do NOT use to render slides as PPTX (use /pptx-generator), for a markdown runbook deck (use /marp), or to prepare for a meeting with a named person (use /meeting-prep)."
argument-hint: "[investor|update|keynote] [investor type, period or event and topic]"
allowed-tools: "Read, Bash(.venv/bin/python:*)"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-capability:
  what: >
    Writes the content of a deck or an investor update from the company's own
    records: an investor pitch for a fundraise, a progress update for people
    already invested, or a keynote script for an event, each with its structure
    and speaker notes or metrics, and no invented figure.
  how: >
    Run /pitch <investor|update|keynote> followed by the investor type, the
    period, or the event and topic. The mode decides which procedure in
    references/ runs. The result is text for the operator to review; nothing is
    rendered or sent.
  when: >
    Use before an investor meeting, for a quarterly or milestone update, or for a
    speaking slot. To render slides as PPTX use /pptx-generator; for a markdown
    deck use /marp; for a brief on a named counterpart use /meeting-prep.
x-heading-routing:
  category: Strategy
  label: /pitch [investor|update|keynote]
  triggers:
    - investor pitch
    - pitch deck
    - fundraising deck
    - investor update
    - board update
    - quarterly update
    - keynote
    - event presentation
    - conference slides
    - speaking deck
  exclusions:
    - N/A
  router: auto
---

# Pitch

One skill, three modes. Each mode's whole procedure is in its reference file.

## Mode selector

1. If the first argument is `investor`, `update` or `keynote`, use that mode. Pass the remaining arguments to it.
2. Otherwise infer the mode from the request. Raising money or meeting new investors is `investor`. A progress report to people already invested is `update`. A talk at an event, or to a government or VIP audience, is `keynote`.
3. If more than one mode still fits, ask one question that names the modes. Then wait.

## Modes

- `investor`: read `references/investor.md` and follow it.
- `update`: read `references/update.md` and follow it.
- `keynote`: read `references/keynote.md` and follow it.

The aliases `/investor-pitch`, `/investor-update` and `/keynote-deck` run the same three modes.
