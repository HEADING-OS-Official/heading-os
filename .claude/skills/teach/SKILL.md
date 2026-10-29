---
name: teach
description: "Teach the operator a new skill or concept over several sessions, as a stateful course: a mission, trusted resources, short interactive HTML lessons, reference sheets, a glossary and learning records that set the next lesson. Every course file lives in the data repository under the registry name `teach-courses`, never in the engine and never in the current directory. EXPLICIT INVOCATION ONLY: /teach <topic or course>. Do NOT use for teaching Odin's brain (use /odin learn or /odin teach), for capturing a note (use /zk), or for a one-off researched answer (use /deep-research-advance or /osint)."
disable-model-invocation: true
argument-hint: "<what you want to learn, or an existing course name>"
allowed-tools: "Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, Bash(.venv/bin/python scripts/teach.py:*), Bash(.venv/bin/python scripts/data-path.py:*), Bash(.venv/bin/python scripts/sanitize-text.py:*), Bash(.venv/bin/python scripts/humanization-check.py:*), Bash(hindsight memory recall:*)"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
  adapted-from: "mattpocock/skills, skills/productivity/teach (MIT License, Copyright (c) 2026 Matt Pocock)"
x-heading-capability:
  what: >
    Runs a multi-session course that teaches the operator one topic, grounded in
    a written mission and in high-trust sources, through short HTML lessons with
    immediate feedback, reference sheets and learning records.
  how: >
    Type /teach <topic> to start a course or /teach <course> to continue one.
    scripts/teach.py is the only door to disk: it resolves the course under the
    registry name teach-courses and refuses with no data repository or a path
    inside the engine. Every lesson path is given as a \\wsl.localhost path.
  when: >
    Use when the operator wants to learn something himself, over time. To make
    Odin learn use /odin; to store a note use /zk; for a one-off researched
    answer use /deep-research-advance or /osint.
x-heading-routing:
  category: Operations
  label: /teach <topic>
  triggers:
    - 'NEVER auto-trigger. Explicit `/teach <topic>` only.'
  exclusions:
    - All natural language
  router: manual
---

# /teach

Adapted from Matt Pocock's `teach` skill (`mattpocock/skills`, MIT License, Copyright (c) 2026 Matt Pocock; the permission notice applies to the parts reproduced here). The pedagogy is his. What changed: where the course lives, how paths are given, and the workspace rules every lesson obeys.

The operator has asked to be taught something. This is a stateful request: he intends to learn the topic over several sessions.

## The one rule about disk

**Every file of a course is written under the directory `scripts/teach.py` prints, and nowhere else.** Never the current directory, never the engine checkout, never a path you built yourself. The upstream skill writes into the directory it was run in; here that would put private learning records into the public engine. `teach.py` refuses (exit 2) when there is no data repository or when the answer would land inside the engine. A refusal ends the session's writing: report its message and stop. Never work around it.

<!-- ste-skip-start -->

    .venv/bin/python scripts/teach.py list                       # every course
    .venv/bin/python scripts/teach.py init <course>              # create; never overwrites
    .venv/bin/python scripts/teach.py where <course>             # the course directory
    .venv/bin/python scripts/teach.py next <course> lessons      # next number (also reference, learning-records)
    .venv/bin/python scripts/teach.py check <path>               # exit 0 only under teach-courses

<!-- ste-skip-end -->

Each prints the Linux path and then the Windows `\\wsl.localhost\...` path. Before writing ANY course file, run `check <path>` on its full path and write only on exit 0. `next` gives a pattern, not a final path, so its result is checked too.

## The course

`<course>` is a kebab-case name, one per topic (`dpi-protocol-basics`, `arabic-a1`). One mission per course: two unrelated topics are two courses. Inside the course directory:

- `MISSION.md`: why the operator is learning this. Grounds all teaching. Format: [references/MISSION-FORMAT.md](references/MISSION-FORMAT.md).
- `RESOURCES.md`: trusted sources for knowledge, and communities for wisdom. Format: [references/RESOURCES-FORMAT.md](references/RESOURCES-FORMAT.md).
- `GLOSSARY.md`: the canonical terms of the course. Format: [references/GLOSSARY-FORMAT.md](references/GLOSSARY-FORMAT.md).
- `learning-records/0001-<dash-case-name>.md`: what he has demonstrably learned, already knew, or had corrected. Used to find the zone of proximal development. Format: [references/LEARNING-RECORD-FORMAT.md](references/LEARNING-RECORD-FORMAT.md).
- `lessons/0001-<dash-case-name>.html`: the primary unit of teaching.
- `reference/*.html`: compressed learnings: cheat sheets, algorithms, syntax, glossaries. Built to be printed and revisited.
- `assets/*`: reusable components shared across lessons.
- `NOTES.md`: his preferences on how he is taught, and your working notes.

The four `references/*-FORMAT.md` links resolve from this skill's folder; every other path resolves from the course directory.

## Phase 0: load

1. Resolve the course with `teach.py list`. For an existing course, run `where`. For a new topic, propose a kebab-case name and run `init` after the operator agrees. A refusal stops here.
2. Ask Hindsight what the workspace already holds on the topic (`hindsight memory recall main "<topic>"`) and show the check as the workspace requires. Facts found there can seed the mission or prior knowledge. Each fact is a pointer: open its source before you rely on it.
3. Read `MISSION.md`, `NOTES.md`, `GLOSSARY.md`, `RESOURCES.md`, every learning record and the list in `assets/`, as far as they exist.

## Phase 1: mission first

If `MISSION.md` is missing or vague, the first job is to ask why he wants to learn this, then write it. A mission ties every lesson to a real outcome; without one, lessons drift abstract and there is no way to judge what comes next. Missions change as he learns: update `MISSION.md` only after he confirms, and write a learning record capturing the change.

## Phase 2: knowledge, then skills

To learn deeply he needs three things:

- **Knowledge**, from high-quality, high-trust resources.
- **Skills**, from interactive lessons you devise on that knowledge.
- **Wisdom**, from contact with other learners and practitioners.

Until `RESOURCES.md` is well populated, finding resources is the focus. Never trust your parametric knowledge: search (WebSearch with the domain filtering of the registry name `search-domains`, `blocked_domains` always), read the source, and cite it. Nothing is invented; when no trustworthy source answers a point, say so and list it under `## Gaps`.

Some topics are mostly knowledge (theory), some mostly skill (a physical practice).

**Fluency versus storage strength.** Fluency is in-the-moment retrieval and gives an illusion of mastery; storage strength is long-term retention and is the goal. Design for desirable difficulty: retrieval practice, spacing across sessions, and interleaving of related topics (for skills practice only).

**Zone of proximal development.** Each lesson should challenge him enough, and no more. If he names what to learn, teach that. If not, read the learning records, check the mission, and teach the most relevant thing that fits.

**Knowledge in a lesson** is only what the skill needs. Teach it first, then practise. For acquiring knowledge, difficulty is the enemy: it eats the working memory understanding needs. Litter lessons with citations to the resources.

**Skills** are about durability. Difficulty is the tool here: effortful retrieval builds storage strength. Use interactive lessons (quizzes, light in-browser tasks) or guided real-world steps. Each rests on a feedback loop, as tight and as automatic as possible. In a quiz, give every answer the same number of words, and the same number of characters where possible. Vary which position holds the correct answer. Give no clue through formatting or order.

## Phase 3: the lesson

A lesson is one self-contained HTML file at the path `teach.py next <course> lessons` gives. It is:

- **short**, completable quickly, inside a small working memory, giving one tangible win to build on;
- **tied to the mission** and in his zone of proximal development;
- **beautiful**: clean, readable typography and layout (think Tufte), because he returns to it;
- **linked** by HTML anchors to other lessons and reference documents;
- **sourced**: it recommends the single best primary source found, and cites every claim;
- **open**: it reminds him to bring follow-up questions back to you, his teacher.

**Assets.** Reuse is the default. Before authoring, read `assets/` and build from what is there. A shared stylesheet is the first component every course earns; every lesson links it. Anything a second lesson could reuse (a quiz widget, a simulator, a diagram helper) is written as a component in `assets/` and linked, never inlined. Relative links only, so a course survives being moved.

**Reference documents** are made alongside lessons: the compressed essence, designed for quick lookup, revisited far more than lessons are. Glossaries are essential; once one exists, every lesson keeps to its terms.

**Delivering it.** Do not open the file with a command. Give its path as `teach.py` printed it, the `\\wsl.localhost\...` form, which he opens in his browser.

## Phase 4: record

After the session, write what changed:

- a learning record when he demonstrated real understanding, disclosed prior knowledge, had a misconception corrected, or the mission shifted (number from `teach.py next <course> learning-records`);
- a glossary term only once he uses it correctly;
- his stated preferences in `NOTES.md`;
- resources added, annotated, or pruned in `RESOURCES.md`.

The course directory is in the data repository, so its files are mirrored into Hindsight by the file sync with nothing further to do.

## Wisdom and communities

When a question needs wisdom, try to answer it. Then point him to a **community** where he can test the skill for real: a moderated forum, a subreddit, a class, a local group. Find high-reputation ones and list them in `RESOURCES.md`. If he declines communities, record it and stop proposing them.

## Voice rules

- Lessons and course files are in the language of his message unless `NOTES.md` says otherwise; Russian follows the workspace terminology dictionary.
- No em-dash and no `--` as punctuation in prose you author; quotes from sources stay verbatim.
- Before handing over a lesson or a reference sheet, run `.venv/bin/python scripts/sanitize-text.py <file> --scan` and `.venv/bin/python scripts/humanization-check.py <file>`, fix what they find, and give the confirmation line the workspace requires.
- Address the operator by the name `NOTES.md` gives.

## NEVER

- Never write a course file outside the directory `teach.py` printed, and never retry a refused write some other way.
- Never write into the engine checkout, the current directory, or `/tmp`.
- Never open a lesson with `explorer.exe`, `cmd.exe`, `wslview` or any Windows-side tool.
- Never sign him up to a community, post, or contact anyone on his behalf; communities are a list.
- Never put a credential, or a document he has not cleared, into a course file.
- Never present unsourced claims as facts in a lesson.
- Never change the mission without his confirmation.
- Never delete a learning record; mark it `Status: superseded by LR-NNNN`.
