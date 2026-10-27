---
paths:
  - "scripts/datastore-*.py"
data-scope:
  - "group:datastore-homes"
  - "deliverable-content"
  - "deliverable-deliverables"
---

<!-- version: 2.2.0 | last-updated: 2026-10-13 -->
# DataStore: source of truth

Last Verified: 2026-09-02

The DataStore holds the authoritative original documents: contracts, decks,
datasheets, spreadsheets, research. It is not one directory in the private data
repository. It is a set of homes, the registry group `datastore-homes`, one
name per home (`datastore-brand`, `datastore-runbook` and the rest); `python
scripts/data-path.py group datastore-homes` prints where each one is today. The
top-level directory of that name is gone, so never write to it or send a reader
to it. The index is the registry name `datastore-index` and the map is
`datastore-map` (`python scripts/data-path.py path <name>`).

**How this rule reaches you.** The DataStore and its deliverables live in the
data repository, OUTSIDE this project, and Claude Code matches `paths:` globs
against the project's own files only (MEASURED 2026-09-28: reading a DataStore
file by its absolute path from an engine session loaded nothing). So the data
surface is the `data-scope:` list above, in registry names, and
`.claude/hooks/data-path-redirect.py` names this rule once per session when a
Read, Write or Edit lands under one of them (2026-10-16, engine audit F-7.C1).
The `paths:` glob covers the engine's own DataStore tools.

When you state a fact about the company, whether it is a number, a price, a partner
detail, a market figure or a product capability,
**check it against the DataStore before you write it down.**

## Everything in the DataStore is private

Operator directive, 2026-09-02. Nothing in any of those subtrees may reach a
public surface. That covers file CONTENTS and file NAMES alike, and it covers
this repository, which is public.

Three consequences, and none of them is optional:

- **Never quote a datastore filename in engine code, prose, a comment, a
  docstring, a test fixture or an example.** Invent one of the same shape.
- **Engine code that loads a brand asset resolves it through the private
  manifest**, so the filename lives in the data overlay and never in a public
  file.
- There is no routing exception. `corporate`, which meant shared down to
  executives through private repositories, was retired on 2026-09-25
  (HOS-132); a datastore path is `private` and never publishable.

Nothing enforces this mechanically since the 2026-09-21 test-suite split, and the
rule alone has already failed once (2026-09-02, a public docstring quoting a
private document's title). Read any new public string against this rule yourself.

## Where the structure is written down

**Not here.** The inventory is generated into the private data overlay, at the
registry name `datastore-map`, by `python scripts/datastore-map.py`. It carries
the real directory names, per-subtree file counts, the routing destination of
each subtree, and how much of each is reachable by search.

The hand-written copy this file carried until 2026-09-02 drifted and, in a
public file, would have published private names; so the inventory is generated
and only the policy stays here.

Read the generated map when you need the shape of the tree. Regenerate it when
it looks stale; `python scripts/datastore-map.py --check` exits 1 when it is.

## Validation workflow

1. Find the source document. `python scripts/datastore-log.py summary` says what
   appeared or changed recently; the generated map says what is there in total.
2. Read the document, or its `-extract.md` companion when the original is a
   binary.
3. Cross-check your claim against it.
4. **The DataStore wins.** If it contradicts anything in `*/*/context/` or
   `reference/`, the DataStore is right and the other file is stale.

### Binary documents are invisible until extracted

A PDF, spreadsheet, deck or Word file cannot be read by search. It becomes
reachable only when `python scripts/datastore-extract.py` writes its
`-extract.md` companion beside it. MEASURED 2026-09-02: 983 files in the tree,
593 of them binary, and only 11 of those 593 had a companion. So a document you
cannot find may well be there and simply unreadable. Check the map's Opaque
column before concluding something is absent.

Since HOS-149 (2026-09-26) the script covers HTML too and every document
directory of the data repository, not only the DataStore, and the hourly
memory-index refresh runs it before each build, so a new document is findable
within the hour. Archives are never written into.

## When to validate

Any external-facing content that carries a specific fact, number or claim:
LinkedIn posts, proposals, investor materials, partner communications, keynote
decks.

## Brand assets

Templates, fonts and logos live under the registry name `brand-kit`. Engine code never
spells their filenames; it resolves them through the private manifest, for the
reason at the top of this file. The typography, palette and letterhead rules
themselves are policy and live in the corporate style guide, the registry name
`corporate-style-guide`.

`examples/` under the registry name `brand-kit` holds real production documents and is refreshed
periodically. Read it before creating new corporate material, so the output
matches current practice rather than a remembered version of it.

## Competitive intelligence

Competitor product documents are filed by vendor. When positioning your product
against a competitor, preparing a proposal, or answering a customer question
about differentiation, read the actual documents. Ground the claim in what the
competitor's own material says, never in an assumption about it.

Those documents are among the most sensitive in the tree. Their filenames name
the vendor and often the end customer, so the rule at the top of this file
applies to them with no exception whatsoever.
