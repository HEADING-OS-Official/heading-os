---
name: deep-think
description: >
  Decision-analysis document for complex choices, multi-variable decisions and strategy
  under uncertainty. States the real problem, lists assumptions marked tested or untested,
  compares the options against cited evidence, and ends with a recommendation and what
  would change it.

  Use when the user says "think through this", "break this down", "reason through",
  "what are we missing", "analyze this carefully", "help me think about", "deep think",
  or asks for a structured analysis of a decision before acting. Do NOT use for a simple
  question (just answer it) or for a second opinion from other models (use /council).
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "2.0"
argument-hint: "[problem]"
allowed-tools: "Read, Bash(.venv/bin/python:*)"
x-heading-capability:
  what: >
    Writes a decision-analysis document for a complex choice: the real problem, assumptions marked tested or untested, the options with their evidence, and a maritime-framed Course recommendation with confidence, key risk and course correction.
  how: >
    Run /deep-think <problem>. Depth is quick/standard/deep. Strategic analyses save under thinking-notes.
  when: >
    Use before generating a deliverable when the quality of the decision matters (feeds /create-plan, /meeting-prep, /proposal). For a simple question just answer it; for a second opinion from other models use /council.
x-heading-routing:
  category: Strategy
  triggers:
    - think through this
    - break this down
    - reason through
    - what are we missing
    - analyze carefully
  exclusions:
    - Simple question -> just answer it
  compound: 'No'
  router: auto
---
# Deep Think

Decision-analysis document. It turns a complex choice into a short record that anyone can challenge. The record holds the real problem, its assumptions, the options, their evidence and a recommendation. The reader can dispute any line, because every assumption is named and every fact is cited.

The document records the analysis, not a transcript of thoughts. Do the reasoning first, then write down its result. Depth of reasoning comes from the session's effort setting, not from this skill.

---

## Variables

- `problem` (required): the question, decision, or problem to analyse
- `depth` (optional): `quick` (two options, no research section), `standard` (default), `deep` (three or more options, full research, a pre-mortem)
- `context` (optional): additional context, constraints, or files to reference

---

## When to use

Use it when the user asks for it by name or by one of its trigger phrases. It fits these situations:

1. **Multi-variable decisions.** Three or more competing factors with no obvious weighting. Example: "Should we enter [a new market] through PartnerCo or direct? There's a tender deadline, partner margin question, and a competitive threat."
2. **Strategy under uncertainty.** The answer depends on unknowns or contested assumptions. Example: "How should we price the [target-market] deal given competitive pressure from a state-aligned vendor?"
3. **Contradictory signals.** Pipeline shows strong momentum, but CRM health shows RED contacts on key relationships.
4. **High-stakes choices.** Investor positioning, partnership terms, competitive response, market entry timing.
5. **Unclear skill chaining.** A request could go several ways, and the order of skills matters.

When you notice one of these in a request that did not ask for this skill, suggest `/deep-think` in one line. Do not run it unasked.

**Do NOT use for:** simple follow-up emails, translations, straightforward CRM updates, content that involves no strategic decision.

---

## Depth levels

| Level | Options | Sections | When to use |
|-------|---------|----------|-------------|
| **Quick** | 2 | Skip Evidence as its own section; cite inline | Time-sensitive decisions with few options and moderate stakes. |
| **Standard** | 2-3 | All sections | Default. Most strategic decisions, deal positioning, partnership structuring. |
| **Deep** | 3+ | All sections, plus a pre-mortem on the recommended option | Investor positioning, market entry, competitive response, anything where a wrong call costs more than the analysis. |

---

## Sections of the document

Write these sections in this order.

| Section | Content | Required? |
|---------|---------|-----------|
| **Problem** | The decision actually being made. Separate the real question from the presented one, and say what is NOT the question. | Always |
| **Assumptions** | Explicit and implicit assumptions. Mark each TESTED (with its source) or UNTESTED. | Always |
| **Options** | Each option as Option A/B/C, with its upside, downside, and cost. | Always |
| **Evidence** | What workspace context files, DataStore, CRM, or known facts say about each option. Cite the source for each fact. | Standard, Deep |
| **Recommendation** | The option chosen and why it beats the others. Resolve the tensions between options here. | Always |
| **What would change the call** | The specific facts or events that would flip the recommendation, and which untested assumption each one tests. | Always |

**Corrected assumptions.** When evidence overturns an assumption, update it in the Assumptions list. Mark it `REVISED` with one line on what changed. Do not hide the change.

---

## Elicitation methods (optional)

After the Problem section, you may pull 2-5 named analysis or critique methods from the shared catalog. Apply each one to the options and record its result under Options or Evidence. This helps when the default sections are not biting on a hard, multi-angle problem. Skip it when the standard sections already do the work.

- `.venv/bin/python scripts/elicit.py categories`: the cheap map of method families
- `.venv/bin/python scripts/elicit.py list --category <c>`: methods in a family (e.g. risk, framing, core)
- `.venv/bin/python scripts/elicit.py show "<Method Name>"`: full gist + output pattern

Pick methods that match the problem. Pre-mortem Analysis and Inversion Analysis suit a risky launch. First Principles Analysis and Reframe the Question suit a framing that feels wrong. Second-Order Thinking suits cascading effects. Name the methods you chose. Catalog reference: `reference/elicitation-methods.md`.

---

## Output format

```markdown
## Deep Think: [Problem Statement]

**Heading:** [1-sentence framing of what we're navigating]
**Sea State:** [External conditions affecting this decision: market, timing, competitive, relational]
**Depth:** Quick / Standard / Deep

### Problem
[The decision restated precisely. What are we actually deciding? What is NOT the question?]

### Assumptions
- [Assumption 1]: TESTED ([source]) / UNTESTED
- [Assumption 2]: REVISED ([what changed and why])

### Options
**Option A: [Label].** [Upside, downside, cost]
**Option B: [Label].** [Upside, downside, cost]

### Evidence
[Facts bearing on each option, each with its file path or source.]

### Recommendation
[The chosen option and why it beats the alternatives.]

### What would change the call
- [Fact or event] would favour [Option X]; it tests [assumption].

---

**Course:** [One-sentence recommendation]
**Confidence:** HIGH / MEDIUM / LOW
**Key risk:** [The single thing most likely to make this wrong]
**If wrong, adjust by:** [Specific course correction: what to do, not "reassess"]
```

---

## Integration with other skills

Deep-think is an **analysis layer** that feeds downstream skills. Use it BEFORE generating deliverables when decision quality matters.

**Deep-think feeds these skills:**
- `/create-plan`: settle architectural decisions before writing the plan
- `/meeting-prep`: map counterpart motivations and information asymmetry before talking points
- `/proposal`: establish why THIS buyer cares before filling the template
- `/investor-pitch`: find the assumptions investors will attack before building the narrative

**Chaining pattern:**
1. Run deep-think to produce the analysis.
2. Reference the Course recommendation when executing the downstream skill.
3. If the downstream skill surfaces new information, mark the affected assumptions `REVISED`. Do not start over.

---

## Session persistence

**When to save:** Standard and Deep analyses that produce strategic decisions: deal positioning, market entry, investor structuring, partnership terms.

**Where to save:** `YYYY-MM-DD-[topic-slug].md` under `thinking-notes` (`.venv/bin/python scripts/data-path.py path thinking-notes`)

**Format:** Include the full document with a header:
```markdown
# Deep Think: [Problem Statement]
> Date: YYYY-MM-DD
> Depth: Standard / Deep
> Course: [recommendation summary]
> Confidence: HIGH / MEDIUM / LOW
> Outcome: [to be updated when the decision plays out]

[Full document below]
```

**When to skip persistence:** Quick analyses consumed immediately by another skill. Tactical rather than strategic decisions.

---

## Rules

1. **Never skip the Assumptions section.** Assumptions that stay hidden shape everything downstream. This is the most important section.
2. **Never fabricate certainty.** If confidence is LOW, say so. If an assumption is UNTESTED, mark it.
3. **Write conclusions, not a transcript.** Each section states a result and its support. Do not narrate the path you took to reach it.
4. **Maritime vocabulary for framing, not for every section.** Heading, Sea State, Course and Course Correction go in the bookends. The sections use plain analytical language.
5. **Cite workspace sources.** When a fact comes from context files, DataStore, or CRM, include the file path.
6. **Show corrections.** A `REVISED` assumption shows that the evidence changed the analysis. Never hide it.
7. **The Course must be actionable.** Not "consider options" or "evaluate further". Give a specific recommended action with a specific next step.

## Knowledge base

After an analysis with persistence, offer this: "Want me to capture the conclusions? The `/odin log` command records them as an episode in Odin's brain (CEO-only). The `/zk distill` command extracts the strategic insights into the knowledge base."
