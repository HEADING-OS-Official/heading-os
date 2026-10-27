---
name: ceo-to-ceo
description: Draft peer-level CEO-to-CEO correspondence in the operator's voice - executive register, relationship-aware, Voss-calibrated. Use for writing to another company's CEO or founder as a peer. Trigger when the user says "CEO letter", "write to [CEO name]", "peer correspondence", or "executive letter". Do NOT use for a non-CEO recipient (use /email-draft) or a formal branded external letter on letterhead (use /corporate-letter). NEVER auto-sends - produces a draft for review.
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.1"
argument-hint: "[name] [subject]"
allowed-tools: "Read, Bash(.venv/bin/python:*)"
x-heading-capability:
  what: >
    Drafts a 150-300 word CEO-to-CEO email to a partner, peer executive, or key
    institutional contact - written peer-to-peer, direct about the objective,
    with a Voss layer and two subject-line options.
  how: >
    Run /ceo-to-ceo [name] [subject]. Loads relationship context from
    the people and pipeline records, then returns the draft inline for review.
  when: >
    Use for peer-level correspondence to another CEO or senior executive. For a
    non-CEO recipient use /email-draft; for a formal branded external letter use
    /corporate-letter.
x-heading-routing:
  category: Communication
  triggers:
    - CEO letter
    - write to [CEO name]
    - peer correspondence
    - executive letter
  exclusions:
    - Non-CEO recipient -> /email-draft
    - formal external letter -> /corporate-letter
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

# CEO-to-CEO Correspondence

Draft CEO-level correspondence to a partner, peer executive, or key institutional contact.

## Variables

to: [CEO/executive name, title, company]

subject_area: [Core subject — e.g., "partnership expansion proposal", "post-MWC follow-up", "strategic alignment meeting"]

objective: [What this communication should achieve]

context: [Background on the relationship and situation]

tone: formal | warm-peer | urgent — default: warm-peer

---

## Instructions

Before drafting, read:
- `voice-guide` — Voice guide, especially Email and CEO-to-CEO notes
- 3-5 varied sent emails from `exchange-email-cache`, loaded into `<examples>` per `reference/voice-examples.md`
- `people` — Load full relationship context for this executive
- `business-info` — company positioning and partner context
- `pipeline` — Check if this relates to an active partnership discussion
- `datastore-index` — If the letter contains specific facts or numbers, validate against source documents

Draft a CEO-to-CEO communication that:
- Opens as a peer - not a vendor writing to a client, not a subordinate writing up
- Gets to the point in the opening paragraph
- Is direct about the objective without being transactional
- Demonstrates that the operator understands the other CEO's business and interests
- Proposes a concrete next step or decision point
- Closes with the captain's tone - decisive, warm, forward-looking

**Voss layer:** Apply per `.claude/rules/voss.md`. Skill-specific hooks: accusation audit on sensitive topics, normative leverage (reference their stated values/public commitments), next steps as calibrated questions. Full framework: `reference/voss-negotiation.md`.

**Length:** 150-300 words. CEOs don't read long emails.

After the draft, provide:
- Subject line options (2)
- One flag: anything that requires extra care given the relationship

## NEVER

- NEVER send the letter. This skill drafts only — outbound send is always human-gated (lethal-trifecta control). The CEO sends or approves explicitly.
- NEVER invent shared history or commitments with the counterpart CEO — if the relationship context is thin, ask.
