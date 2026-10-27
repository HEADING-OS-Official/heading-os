#!/usr/bin/env python3
"""Generate the HEADING OS 2 migration manifest: every file, its family and its leaf.

Nothing is moved. This walks the data repository, applies the ordered rules in
`families.py`, and writes two tables plus a coverage report.

## The two tables, and why they are two

**`families.tsv`** is one row per FILE: where it is, where it belongs, how that
was decided, and what sensitivity it carries. It is the migration's measure --
"we are 60% migrated" becomes a count instead of a feeling -- and it is what a
later pass consumes to actually move anything.

**`family-locations.tsv`** is one row per FAMILY/LEAF: where those records live
right now, where they will live, and which of the two is current. This is the
contract the path SEAM reads. A caller asks for `work/deliverable` and the
resolver answers with today's directory until the day `state` flips to `moved`,
at which point it answers with the new one and no other line of code changes.

Two tables rather than one because they answer different questions and change on
different schedules. The per-file table is regenerated whenever the tree changes;
the per-family table changes once per family, on the day that family moves.

## What this refuses to do

**There is no `other` family and no `misc` leaf.** A file with nowhere to go is
NOT a reason to invent a twelfth bucket; it means the scheme is missing a kind,
and that is a question for the operator. Those files are counted, listed in full,
and reported as questions. The count is a headline number of this pass.

**A `skip` is never silent.** Caches, temporaries and sync bookkeeping are
deliberately not migrated, and every one is counted and listed, because a file
that quietly fails to migrate is indistinguishable from a file that was never
there.

**A row naming a leaf the vocabulary does not define is refused**, not written.
A typo in the rules table would otherwise travel all the way into the tree.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils.workspace import (  # noqa: E402
    get_data_root, get_default_tz)
from scripts.heading_os_2 import families as F  # noqa: E402
from scripts.heading_os_2.resolve import STATE_NOT_MOVED  # noqa: E402
from scripts.utils.excluded_dirs import NAMES as SHARED_EXCLUSIONS  # noqa: E402

# `runtime-junk` is the store every runtime writer now lands in
# (scripts/utils/runtime_dir.py); `_derived` is its predecessor, kept until the
# one-time move has emptied it. The operator's holding pens come from
# `scripts/utils/excluded_dirs.py` and are deliberately not restated here: this
# file was one of five that spelled the same name in its own words (HOS-28),
# and `scripts/datastore-map.py` imports this set rather than writing a sixth.
SKIP_DIRS = {".git", "_derived", "runtime-junk", "node_modules", ".pytest_cache",
             "__pycache__"} | SHARED_EXCLUSIONS

#: The migration lock, relative to the data root. Present from the first
#: rename until `unlock` has re-verified the tree or `rollback` has put it
#: back. Its content names the TRANSACTION it belongs to. Written by the
#: mover (retired 2026-09-28, HOS-166) and by `memory-index.py migration-window
#: open`; read by `memory-index.py`, which must name the SAME file. Lived in
#: `move.py` until HOS-161 moved it here, beside the manifest directory it
#: sits in, so that reader need not import the mover.
MIGRATION_LOCK = "_manifest/MIGRATION-IN-PROGRESS"

_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$")

#: How many bytes of a markdown file to read when the rule needs its frontmatter.
#: Frontmatter sits at the top by definition, so this is a bound and not a guess.
_FM_READ_BYTES = 8000


def _frontmatter(path: Path) -> dict[str, str]:
    """The file's frontmatter as a FLATTENED dict, or empty. Never raises.

    Flattened, not flat, and the difference cost a whole pass. `auto-memory/`
    records carry their kind at `metadata.type`, one level down:

        ---
        name: the-two-hull-layout-is-main
        description: "..."
        metadata:
          node_type: memory
          type: project
        ---

    A reader taking only top-level keys finds no `type` at all. MEASURED
    2026-09-12 on a real corpus: it reported EVERY auto-memory record as
    unresolved, which reads as a corpus problem and is a parser problem. A
    nested key is therefore folded in under its bare name, with the top level
    winning on a collision, because the top level is what a hand-written record
    states about itself.

    A file that cannot be read is not a crash: one bad byte sequence must not
    abort a pass over a whole repository. An unreadable file falls through to
    whatever the path alone can decide, reported as such rather than guessed at.
    """
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:_FM_READ_BYTES]
    except OSError:
        return {}
    m = _FM_RE.match(head)
    if not m:
        return {}
    top: dict[str, str] = {}
    nested: dict[str, str] = {}
    for line in m.group(1).splitlines():
        indented = line[:1] in (" ", "\t")
        km = _KEY_RE.match(line.strip())
        if not km:
            continue
        target = nested if indented else top
        target.setdefault(km.group(1), km.group(2).strip())
    return {**nested, **top}


def _operations_leaf(rel: str) -> str:
    """The `("trace", "report")` or `("trace", "scratch")` leaf for a file under the operations tree.

    `outputs/operations/` is 40% of the whole collection, which makes it the one  # leak-guard: ok (migration vocabulary: the legacy directory this rule classifies)
    directory where a single leaf would be a lie. The discriminator is
    deliberately cheap and deliberately biased toward `scratch`: `trace` is never
    quoted in an answer either way, so a wrong call here costs ranking rather
    than correctness, and paying for a model on 4,700 files to improve a ranking
    signal is the wrong trade.
    """
    lowered = rel.lower()
    return ("report" if any(h in lowered for h in F.OPERATIONS_REPORT_HINTS)
            else "scratch")


def classify(rel: str, abs_path: Path, ext: str, rules=None) -> dict:
    """Decide one file's family, leaf, method and sensitivity.

    Returns a dict always carrying `family`, `leaf`, `method` and `sensitivity`.
    `family` and `leaf` are empty strings when nothing decided them -- that is a
    question for the operator, never a fallback bucket.

    ## The address is asked first, and it is not a rule

    `RULES` is a hand-written list of the addresses this tree had on
    2026-09-12. The move happened, so for a record that has already been
    carried to `<family>/<leaf>/...` that list is a description of where the
    record USED to be, and `str.startswith` against it answers nothing.
    MEASURED 2026-09-19 on the operator's overlay, before this tier existed:
    9,523 of 16,282 files matched no rule in the table at all, and every one of
    them came back `unmatched`. Sixty-five of those had no row in
    `families.tsv` either, so they were invisible to every reader working from
    the manifest.

    So the FIRST question is the file's own address, via `F.address_kind`,
    which is derived from the vocabulary rather than typed out beside it. A
    second hand-written table of the forty-five new prefixes would have worked
    today and drifted from `FAMILIES` the first time a leaf was added; this
    repository's dominant defect is a decision that exists in two copies.

    The method recorded is `rule`, not a fifth value. `rule` is defined in
    `families.py` as "decided by path alone, no file read", which is exactly
    what this is, and `resolve.py` reads the method column to tell an
    unclassified row from an out-of-scheme one -- a value it has not been
    taught would have to be added to `load_local_rules`, to that reader, and to
    every consumer of the column, in exchange for a distinction nobody asks
    for. Where the address settles the family but not the leaf (a family
    README, a directory the scheme does not name), the method is `judgement`:
    the honest statement that a human still has to read the file, and the value
    `resolve.py` already counts as `undecided_by_family`.
    """
    sensitivity = F.sensitivity_for(rel)

    addressed = F.address_kind(rel)
    if addressed is not None:
        family, leaf = addressed
        return {"family": family, "leaf": leaf,
                "method": "rule" if leaf else "judgement",
                "sensitivity": sensitivity}

    for prefix, family, leaf, method in (F.RULES if rules is None else rules):
        if not rel.startswith(prefix):
            continue

        if method == "skip":
            return {"family": "", "leaf": "", "method": "skip",
                    "sensitivity": sensitivity}

        if method == "frontmatter":
            fm = _frontmatter(abs_path)
            pair = F.AUTO_MEMORY_TYPE_MAP.get(fm.get("type", ""))
            if pair:
                return {"family": pair[0], "leaf": pair[1],
                        "method": "frontmatter", "sensitivity": sensitivity}
            # The field is absent or carries a value the map does not define.
            # NOT a guess: it becomes a question, which is the whole point of
            # counting this separately from a rule hit.
            return {"family": "", "leaf": "", "method": "unresolved",
                    "sensitivity": sensitivity}

        # An asset is an asset wherever it sits, but only once no earlier rule
        # has claimed it -- `datastore/brand/` is `asset/brand` by rule, and a  # leak-guard: ok (migration vocabulary: the legacy directory a rule classifies)
        # PNG inside a deliverable folder is still that deliverable's asset.
        if leaf is None and ext in F.ASSET_EXTENSIONS:
            return {"family": "asset", "leaf": F.ASSET_EXTENSIONS[ext],
                    "method": "rule", "sensitivity": sensitivity}

        if family == "trace" and leaf is None:
            return {"family": "trace", "leaf": _operations_leaf(rel),
                    "method": "rule", "sensitivity": sensitivity}

        if family is None or leaf is None:
            return {"family": family or "", "leaf": "",
                    "method": "judgement", "sensitivity": sensitivity}

        return {"family": family, "leaf": leaf, "method": method,
                "sensitivity": sensitivity}

    # No rule matched at all. This is the honest "nowhere to go" case and it is
    # reported as a question, never absorbed into a catch-all.
    return {"family": "", "leaf": "", "method": "unmatched",
            "sensitivity": sensitivity}


def walk(root: Path) -> list[tuple[str, Path, str]]:
    """Every file under `root` as (relative posix path, absolute path, extension).

    Never the runtime store: `SKIP_DIRS` holds its default name, which also
    prunes the migration's own report directories of that name at any depth,
    and the tree's registry name (`runtime_dir.store_dirname`, HOS-5 F1) is
    pruned at its root-relative address, the one place a store sits, nested
    since HOS-241 (two segments inside a subject root).
    """
    from scripts.utils.runtime_dir import store_dirname

    store = store_dirname(Path(root))
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = os.path.relpath(dirpath, root).replace(os.sep, "/")
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".memory-index")
                       and (d if here == "." else f"{here}/{d}") != store]
        for fn in filenames:
            p = Path(dirpath) / fn
            rel = p.relative_to(root).as_posix()
            out.append((rel, p, p.suffix.lower().lstrip(".")))
    return out


def build(root: Path) -> tuple[list[dict], dict]:
    """Classify every file. Returns (rows, summary)."""
    shadowed = F.check_rule_order()
    if shadowed:
        raise SystemExit(
            "refusing to build: these rules can never fire, fix the order in "
            "families.py first:\n  " + "\n  ".join(shadowed))

    # The operator's own deeper rules come FIRST, because they are strictly more
    # specific than the generic directory rules they sit under and first match
    # wins. They live in the overlay, never in this public repository; see
    # `families.LOCAL_RULES_FILENAME` for why.
    local = F.load_local_rules(root / "_manifest" / F.LOCAL_RULES_FILENAME)
    rules = local + F.store_rules(root) + F.RULES

    rows, summary = [], collections.Counter()
    for rel, abs_path, ext in walk(root):
        d = classify(rel, abs_path, ext, rules)
        family, leaf = d["family"], d["leaf"]

        if family and leaf and not F.is_valid(family, leaf):
            raise SystemExit(
                f"refusing to write a row naming an undefined leaf: "
                f"{family}/{leaf} for {rel!r}. Fix families.py.")

        rows.append({
            "path": rel,
            "family": family,
            "leaf": leaf,
            "method": d["method"],
            "sensitivity": d["sensitivity"],
            "target": f"{family}/{leaf}" if family and leaf else "",
        })
        summary[d["method"]] += 1
    return rows, dict(summary)


def locations(rows: list[dict]) -> list[dict]:
    """The per-family table the path seam reads.

    `current_dir` is the directory holding the PLURALITY of a family/leaf's files
    today, which is the honest answer while a family is spread across several of
    today's directories. `state` is `not-moved` for every row until a migration
    pass flips it, and the flip is the entire switchover.

    This builds the table from the TREE, so it carries no state forward: a
    regeneration puts every row back to `not-moved`, discarding `moved` and
    `not-for-migration` alike. That is how it behaved before the fourth state
    existed and it is unchanged here, but the fourth state raises the cost,
    because `moved` can be re-derived by a mover from the disk and a decision
    of the operator's cannot. Re-record the decided rows after any
    regeneration. Until 2026-09-20 `scripts/ho2-set-state.py` was the tool for
    it; it was retired with the rest of the move tooling once the table settled
    at 27 `moved` and 11 `not-for-migration` with nothing in motion, so there is
    now NO tool that writes a `state` cell and a regeneration would have to be
    undone by hand. That is the cost of regenerating, stated rather than left
    to be discovered.
    """
    by_pair: dict[tuple[str, str], collections.Counter] = collections.defaultdict(
        collections.Counter)
    for r in rows:
        if not r["family"] or not r["leaf"]:
            continue
        # The file's own PARENT, not its first two path segments. Taking
        # `top/sub` treats a flat directory's filename as a subdirectory:
        # MEASURED 2026-09-12, `auto-memory/` is flat, so `canon/constraint`
        # came back with a current_dir of `auto-memory/<some-record>.md` and a  # leak-guard: ok (history: MEASURED 2026-09-12)
        # spread of 181 directories that do not exist. A parent is a directory
        # whatever the tree's depth happens to be.
        parent = r["path"].rsplit("/", 1)[0] if "/" in r["path"] else "."
        by_pair[(r["family"], r["leaf"])][parent] += 1

    out = []
    for (family, leaf), dirs in sorted(by_pair.items()):
        out.append({
            "family": family,
            "leaf": leaf,
            "current_dir": _common_root(dirs),
            "target_dir": f"{family}/{leaf}",
            "state": STATE_NOT_MOVED,
            "files": str(sum(dirs.values())),
            "spread_over_dirs": str(len(dirs)),
        })
    return out


def _common_root(dirs: collections.Counter) -> str:
    """The deepest directory containing EVERY one of these paths.

    Not the most populous one. A plurality vote answers "where are most of these
    records" and the seam is asking "where do I look for them", which is a
    different question with a worse failure mode. MEASURED 2026-09-12 on a real
    workspace: for the most scattered family, the plurality returned one deep
    directory out of several hundred, holding a small fraction of that family's
    files. A resolver handed that answer would miss almost everything it was
    asked for, and would look entirely reasonable doing it.

    A common root over a spread family is wide rather than wrong, and `files`
    beside `spread_over_dirs` says exactly how wide. Wide and honest beats narrow
    and confident: the first is visible in the table, the second is not.
    """
    parts = [d.split("/") for d in dirs]
    common = parts[0]
    for p in parts[1:]:
        keep = 0
        # strict=False deliberately: the two paths have different depths, and
        # stopping at the shorter one is the whole point of a common prefix.
        for a, b in zip(common, p, strict=False):
            if a != b:
                break
            keep += 1
        common = common[:keep]
        if not common:
            return "."
    return "/".join(common) or "."


def _emit_tsv(rows: list[dict], fields: list[str], stream=None) -> None:
    """Print a TSV to stdout, or to a stream the CALLER opened.

    `stream` was added 2026-09-19 for `scripts/utils/synthetic_data_root.py`,
    which builds a throwaway data repository and needs the tables written into
    it. The stream is the caller's, so this function decides nothing about
    where anything lands, and the caller that opens a file under the
    operator's live overlay is the one the write guard judges. Passing nothing
    keeps stdout.
    """
    w = csv.DictWriter(stream if stream is not None else sys.stdout,
                       fieldnames=fields, delimiter="\t",
                       quoting=csv.QUOTE_MINIMAL, extrasaction="ignore",
                       lineterminator="\n")
    w.writeheader()
    w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-root", type=Path, default=None,
                    help="data repository to classify (default: this workspace's)")
    ap.add_argument("--emit", default="report",
                    choices=("families", "locations", "questions", "report"),
                    help="what to print to stdout; the caller redirects it")
    args = ap.parse_args()

    root = args.data_root or get_data_root()
    rows, summary = build(root)

    if args.emit == "families":
        _emit_tsv(rows, ["path", "family", "leaf", "method", "sensitivity", "target"])
        return 0

    if args.emit == "locations":
        _emit_tsv(locations(rows),
                  ["family", "leaf", "current_dir", "target_dir", "state",
                   "files", "spread_over_dirs"])
        return 0

    questions = sorted(r["path"] for r in rows
                       if r["method"] in ("unmatched", "unresolved"))
    if args.emit == "questions":
        for q in questions:
            print(q)
        return 0

    by_family = collections.Counter(r["family"] for r in rows if r["family"])
    # The date this run wrote the tables, read from a clock rather than frozen.
    # It was the string literal "2026-09-12" until 2026-09-19, which made
    # `resolve.manifest_generated()` report the age of a regeneration as the age
    # of the FIRST generation: MEASURED 2026-09-19, both TSVs had been rewritten
    # at 01:50 that morning while this field still said 2026-09-12 and
    # `files_total` still said 11411 against the 11401 rows on disk. The defect
    # was written down in `resolve.manifest_generated`'s own docstring on
    # 2026-09-13 and reported to HELM, which is six days of a field that tracked
    # nothing while a reader existed for it.
    #
    # Local date, through the workspace timezone seam, so a run just after
    # midnight in a zone east of UTC is not dated to the previous day by UTC.
    print(json.dumps({
        "generated": datetime.now(get_default_tz()).date().isoformat(),
        "data_root": str(root),
        "files_total": len(rows),
        "by_method": summary,
        "by_family": dict(by_family.most_common()),
        "ceo_only": sum(1 for r in rows if r["sensitivity"] == "ceo-only"),
        "nowhere_to_go": len(questions),
        "family_leaf_pairs_in_use": len(locations(rows)),
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
