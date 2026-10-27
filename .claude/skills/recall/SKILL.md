---
name: recall
description: "Semantic recall across the whole workspace - Odin brain, threads, CRM, context, plans, outputs, knowledge and auto-memory - answering only from retrieved sources with file citations, or saying \"not in memory\". Use for \"what do we know about X\", \"where did we decide Y\", \"have we touched Z before\". Do NOT use for counting or aggregating across many files, where the answer sits in no single chunk - that is /census. Do NOT use for Odin-brain-only advice - that is /odin."
argument-hint: "<what to recall> [--collection content|code|all] [--layer NAME] [--personal]"
allowed-tools: "Read, Bash(.venv/bin/python:*)"
metadata:
  author: Misha Hanin
  email: misha.hanin@headingos.com
  version: "1.0"
x-heading-capability:
  what: >
    Workspace-wide semantic recall - finds past decisions, briefs, contacts, and
    notes by MEANING (cross-lingual RU/EN) across the whole workspace, and answers
    only from retrieved sources with path citations, or says "not in memory".
  how: >
    Run /recall <query>. It refreshes the index (incremental), queries
    scripts/memory-index.py with --json, reads the top cited files, and composes a
    cited answer. --collection code searches skills/rules; default is content.
  when: >
    Use to surface something the workspace already knows. For Odin-brain advice use
    /odin recall; for external intel use /osint; to capture a new note use /zk; for
    exact-string search use Grep.
x-heading-routing:
  category: Strategy
  triggers:
    - recall
    - what do we know about
    - where did we decide
    - search my memory for
    - have we touched [X] before
    - find what we said about
    - surface past notes on [X]
  exclusions:
    - Odin-brain-only advice / episode dedup -> /odin recall (brain-scoped)
    - external/world intel on a company or person -> /osint
    - capture a NEW note -> /zk
    - exact-string file search -> Grep.
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

# Recall (workspace-wide semantic memory)

Surface what the workspace already knows, by meaning. The engine
(`scripts/memory-index.py`) does hybrid dense+lexical retrieval entirely on the
local machine (`bge-m3`, zero API cost) across every meaningful layer. The
embedder is the program the inference-host config declares: Lemonade Server or Ollama.
This skill turns its ranked hits into a **cited answer** — or relays its
**honest "gap"** when the answer is not in memory. It never guesses past the
sources, and it never sends anything.

## Phase 0: Hindsight first, then the local index

0. **Ask Hindsight first.** It is the main memory (operator decision 2026-10-05,
   HOS-203). Use the `recall` tool of the `hindsight-main` MCP server, or this
   command in a terminal:

   ```bash
   .venv/bin/python scripts/hindsight-snapshot.py lookup "<the user's question>"
   ```

   **Hindsight can fail to answer.** The MCP call errors, the connection is
   refused, the call times out, or the server refuses the key. Then answer from
   the on-disk snapshot of what lives only in Hindsight:

   ```bash
   .venv/bin/python scripts/hindsight-snapshot.py query "<the user's question>" --json
   ```

   Its JSON carries the snapshot date. Write "from snapshot of <date>" every time
   you use one of its hits, because anything newer than that date is not in it.
   `lookup` makes the same switch by itself and prints the same date. A hit is
   still a pointer, so open the file or document it names. Then continue with the
   local index below, which covers the files.

1. **Refresh the index first** (mirrors `/odin recall`): run

   ```bash
   .venv/bin/python scripts/memory-index.py build
   ```

   This is incremental — it embeds only changed files and updates the gitignored
   `runtime:memory-index/cache`. It is NOT a workspace write. **If the embedder is down**, the
   build prints an embedding error. Do not fail. Note "index not refreshed
   (embedder down), recalling from the existing index" in one line, then query
   whatever is already indexed.

2. **Query with JSON output:**

   ```bash
   .venv/bin/python scripts/memory-index.py query "<the user's question, RU or EN>" --json
   ```

   - Default collection is `content` (what we know / decided). To search the
     machinery (skills, rules), pass `--collection code`. To search everything,
     `--collection all`. To pin one layer, `--layer NAME`
     (e.g. `odin`, `thread`, `crm`, `outputs`, `context`, `skill`).
   - Pass the user's phrasing as the query text; the engine is cross-lingual, so
     a Russian question recalls English notes and vice-versa.

## Phase 1 — Answer from sources, or admit the gap

Parse the JSON. It is one object:

- **Gap** (`{"hits": [], "gap": true, "best": <float>, "threshold": <float>}`):
  there is no match above the salience threshold. **Say so plainly** — e.g.
  "Not in memory: nothing above the recall threshold for that (closest match
  scored {best} vs {threshold})." Do NOT pad and do NOT speculate. Do NOT answer
  from your own prior knowledge. Optionally name the nearest layer or suggest a
  rephrase or `--collection all`. Stop here.

- **Near-miss** (`"gap": false` but `"near_miss": true` and `"confident": false`,
  each hit carrying `"below_threshold": true`): **not the same as Hits below.**
  Nothing cleared the salience threshold; the engine surfaced the nearest
  material anyway because it sat within the near-miss margin, not because it
  answers the question. Say so plainly — "No confident match in memory for
  that; nearest by similarity: <titles>, relevance not established." List the
  `title`/`path` pairs as **possible leads only**. Do NOT read the files, and do
  NOT compose an answer from them. Read them only when the user explicitly asks
  to go deeper ("check that anyway", "open it", "read it"). Stop here.

- **Hits** (`{"hits": [ {path,title,layer,ntype,classification,collection,score,channels}, ... ], "gap": false}`,
  no `near_miss` key — confident matches):
  1. **Read the top cited files** (`Read` each `path`, highest `score` first —
     usually the top 3–5 are enough). Read the actual files; the JSON carries
     only titles and scores, not full content.
  2. **Compose a concise answer grounded ONLY in what those files say.** Every
     claim traces to a source. Cite inline as `` `path` `` after the claim it
     supports. If two sources disagree, surface the disagreement rather than
     silently picking one.
  3. **If the read files do not actually contain the answer** (a near-miss
     retrieval), say that honestly. For example: "the closest sources touch
     the topic but don't answer it directly". Then name what they do cover.
     Never invent the missing fact.
  4. **Reinforce cited auto-memory hits.** This covers auto-memory facts only.
     Every other layer is left untouched: `odin`, `thread`, `crm`, `context`,
     `reference`, `plans`, `outputs`, `knowledge`, `datastore-extract`,
     `doc-companion`, `chronicle`, `skill` and `rule`. For each cited hit whose `layer` is
     `memory`, once
     per unique file path, run:

     ```bash
     .venv/bin/python scripts/memory-touch.py <path>
     ```

     This bumps `access_count`/`last_accessed` in the file's frontmatter only —
     it never touches content. Run this regardless of whether Phase 0's index
     build was a no-op (it edits the source file directly, not the index).

## Phase 1.5 — Chronicle (historical class, always below the brain)

After the primary answer, run a SEPARATE secondary pass over the Conversation
Chronicle — a dated record of past sessions ("on date X we discussed Y"). It is
NOT a belief and NOT current fact; it only tells you a conversation happened.

```bash
.venv/bin/python scripts/memory-index.py query "<the user's question>" --collection chronicle --json
```

- Append chronicle hits **below** the brain/content answer, never mixed into it.
  The separate collection is what makes this ordering structural: a brain hit
  always precedes a chronicle hit on the same topic. If the primary pass already
  answered, chronicle is supplementary "we talked about this on <date>" context.
- Render each hit **tagged with its date and class**, e.g.
  `[chronicle 2026-05-19] session about the Globex pricing debate — `path``.
  Never restate a chronicle summary as a current decision; if it matters now,
  say "we discussed this on <date>; confirm whether it still holds."
- The chronicle's `personal` entries (the `chronicle-personal` layer) appear
  here on purpose. Tag a personal hit `[Личное <date>]` and rank it like any
  other dated chronicle entry.
- Skip this pass entirely when the user pinned `--collection code`/`--layer`, or
  when the chronicle query returns a gap (say nothing rather than pad).

## Phase 1.6 — Personal chronicle (a scope choice, no longer a wall)

Personal chronicle is an ordinary indexed layer (the air-gap deny ended
2026-09-12), so Phase 1.5 already returns personal entries.

`--personal` therefore selects a SCOPE rather than opening a door. Run it when
the user asks for personal material explicitly: `recall --personal ...`, "search
my personal history", "when did I discuss the villa". It narrows the answer to
personal entries alone, rather than personal beside business. It also works when
no index exists yet.

```bash
.venv/bin/python scripts/chronicle.py personal-recall "<the user's question>"
```

That command is unchanged: it reads every `*.md` in the `personal` branch of
`chronicle` ON THE FLY, scores locally (bge-m3, lexical fallback), and
persists NOTHING. Present the
dated hits tagged `[Личное <date>]`, clearly historical, never as a current
fact. If it returns no match, say so plainly.

**What is NOT claimed.** Nothing mechanical now keeps personal chronicle out of
a recall that happens to precede an outbound draft. Judgement does, and it is
yours: do not carry personal-life material into a draft the user did not ask to
have it in. Personal THREADS are a separate question with a separate control,
still live, see the NEVER section below.

## Phase 2 — Source list

End with a one-line-per-source list of what you cited. Give each as a clickable
`path`, with its `layer` and `classification` tag, so the CEO can open the
originals. Example:

```
Sources:
- <path as returned by the index>  (odin, ceo-only)
- <path as returned by the index>  (thread, ceo-only)
```

## Voice

- Match `voice-guide` and the always-on humanisation rule. Plain,
  committed, specific. No "I cannot find information regarding..." filler —
  either answer with citations or state the gap in one clean line.
- Hyphens, not double dashes. Company terms per terminology.

## NEVER

- **Never fabricate beyond the returned sources.** No answer from your own
  training knowledge when the engine reports a gap — relay the gap.
- **Never send anything.** This is a read/recall skill; it drafts no outbound
  message and calls no send transport.
- **Never read the personal thread branch.** `check_protect_personal_threads` in
  `.claude/hooks/_dispatch.py` refuses `threads/personal/` and <!-- leak-guard: ok (the guard's own match pattern, not an address) -->
  `ledger/episode/threads/archive/<any segment>/personal/`. It covers the Read, Bash and Glob <!-- leak-guard: ok (the guard's own match pattern, not an address) -->
  spellings alike, and no index layer reaches those paths. The thread layer is
  `*.md` under the `business-threads` group (`threads-business` and every
  account's `account-threads`). Do not work around it. **This is not the air gap.**
  That boundary ended on 2026-09-12. The `personal` branches of `chronicle`,
  `context-reference` and `datastore-documents`
  are indexed on purpose, and reading them is ordinary work.
- **Never present a hit's snippet as the answer without reading the file.** The
  index stores a 500-char embed snippet, not the full note.
- **Never claim freshness you don't have.** If the embedder was down and the index
  wasn't refreshed, say so.
- **Never treat a chronicle hit as current fact or a decision.** It is a dated
  historical record of a past conversation. Surface it tagged with its date,
  below the brain, and flag that it may be stale.
- **The reinforcement touch step never modifies content.** `scripts/memory-touch.py`
  only bumps `access_count`/`last_accessed` in frontmatter, scoped to `memory`-layer
  hits; it refuses any path outside the auto-memory directory.
