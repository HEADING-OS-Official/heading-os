#!/usr/bin/env python3
"""Score a candidate `MEMORY.md` on the only job a resident index has.

Usage:
    python scripts/memory-index-probe.py budget   --index <path>
    python scripts/memory-index-probe.py verify   --index <candidate> --against <baseline>
    python scripts/memory-index-probe.py prompts  --index <path> [--situations <json|jsonl>] [--batch 16]
    python scripts/memory-index-probe.py score    --index <path> --verdicts <json> [--situations <json|jsonl>]

WHAT THIS MEASURES, and why it is not the graph-retrieval probe wearing a new hat.

The graph-retrieval probe of the memory pilot (removed 2026-10-04, HOS-198) asked whether SEARCH returns a record when the query does not
name it. Measured 2026-09-10: 0.567 for conduct rules, 0.588 for lookup facts.
But `MEMORY.md` is not searched. It is RESIDENT: the whole file is already in
context, every turn, with no query and no decision from anyone. So retrieval
recall says nothing whatever about whether the index works, and optimising the
file against a retrieval score would be optimising against the wrong reader.

The resident index has exactly one job: when a working moment arrives, the
reader must RECOGNISE that one of these lines applies and open it. That is what
this scores. A judge is given the candidate index and one situation, with no
hint that any record is relevant, and asked which pointer applies. The metric is
whether it names the right one.

WHY THE JUDGE IS A CLAUDE SUBAGENT AND NOT A PROXY MODEL. The production reader
of this file is Claude, in a Claude Code session. A cheaper judge would measure a
different reader, and the whole finding would then rest on the assumption that
the two behave alike -- an assumption nothing here establishes. So this script
does NOT call a model. It renders prompts, the caller dispatches them to
subagents, and it scores what comes back. That keeps the judge honest at the cost
of making the loop caller-driven, which is the right trade for a measurement
whose conclusion is meant to survive being quoted.

THE FLOOR THIS MEASUREMENT CANNOT CROSS, stated here rather than discovered
later. A judge asked "which line applies" has already been told that looking is
worth doing. In a real turn nobody asks. So every number here is an UPPER BOUND
on resident recognition, and the honest use of it is COMPARATIVE: variant B beat
variant A by this much, under a prompt that flattered both equally.

WHAT COUNTS AS A POINTER, since 2026-10-16. On 2026-09-20 the conduct rules
left `MEMORY.md` for `CLAUDE.private.md`, and the `](record.md)` links went
with them, so the tool had no subject. A pointer is now either form: a
`[label](record.md)` link, or a bracketed rule id (`[M12]`, `[M12.3]`, `[K4]`)
opening a list item, which is how the policy file names each rule. The ground
truth is a file passed with `--situations` (a JSON array or JSONL of
`{"sid", "text", "target"}`, the target a record filename or a rule id), kept
in the data repository because it names private rules; without the flag the
built-in packs of `utils.memory_probe_situations` are used. `prompts` and
`score` still REFUSE an index that names no pointer of either form, because a
rate over nothing reads as a bad index.

`budget` and `verify` still work on any index, because neither asks what the
index POINTS AT: one measures the file against the harness's two ceilings, the
other compares a candidate to a baseline. `verify` carries its own floor for
the one claim of its that IS about pointers.
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.memory_probe_situations import situations  # noqa: E402
from scripts.utils.memory_expiry import _LINK_RE, index_links  # noqa: E402

# The harness truncates the resident index from the END past either budget, and
# reports the truncation to the vendor rather than to the reader. So a variant
# that scores well while over budget scores well on a file nobody will ever see
# in full.
LINE_BUDGET = 200
U16_BUDGET = 25000

PROMPT = """You are reading a working assistant's resident memory index. It is \
loaded into every session automatically; nobody searched for it.

Below is the index, then {n} moments of work. For EACH moment, decide whether \
any single line of the index carries a record that the assistant should open, \
or a rule it should apply, before acting, and name that record's filename or \
that rule's bracketed id.

Judge each moment independently. Answer `null` when no line applies -- a wrong \
name costs more than an honest null, because acting on the wrong record is worse \
than acting on none.

=== INDEX BEGINS ===
{index}
=== INDEX ENDS ===

=== MOMENTS ===
{moments}

Return ONLY a JSON array, one object per moment, in the order given:
[{{"sid": "<sid>", "record": "<filename.md, rule id, or null>", "line": "<the index line \
you read it off, truncated to 80 chars, or null>"}}]
"""


def budget(index: Path) -> dict:
    text = index.read_text(encoding="utf-8")
    u16 = len(text.encode("utf-16-le")) // 2
    lines = len(text.split("\n"))
    return {
        "index": str(index),
        "lines": lines,
        "utf16_units": u16,
        "line_pct": round(lines / LINE_BUDGET, 3),
        "u16_pct": round(u16 / U16_BUDGET, 3),
        "within_budget": lines <= LINE_BUDGET and u16 <= U16_BUDGET,
    }


def pointers(index: Path) -> list[tuple[str, str]]:
    """Every `[label](record.md)` in file order.

    Delegates to `memory_expiry.index_links`, which is the tree's one link
    grammar. This function used to carry its own `\\[([^\\]]*)\\]` and could not
    cross the `]` inside a label like `args[0] on Path.open`: MEASURED
    2026-09-10 on the live index, it returned 339 pointers where the shared
    parser returns 340. `verify` compares pointer LISTS, so a candidate that
    dropped exactly that record compared equal to its baseline.
    """
    return [(label, target)
            for label, target in index_links(index.read_text(encoding="utf-8"))
            if target.endswith(".md")]


# A rule id opening a list item: `- [M13.1] ...`, `* [K6] ...`.
RULE_ID_RE = re.compile(r"^[ \t]*[-*][ \t]+\[(?P<id>[MK]\d+(?:\.\d+)?)\]", re.M)
_BARE_RULE_ID = re.compile(r"^\[?(?P<id>[MK]\d+(?:\.\d+)?)\]?$")


def rule_ids(index: Path) -> list[str]:
    """Every rule id that opens a list item, in file order."""
    return [m.group("id") for m in RULE_ID_RE.finditer(index.read_text(encoding="utf-8"))]


@dataclass(frozen=True)
class FileSituation:
    sid: str
    text: str
    target: str


def load_situations(path: Path | None) -> list:
    """The ground truth: `path` (JSON array or JSONL), else the built-in packs."""
    if path is None:
        return situations("all")
    raw = path.read_text(encoding="utf-8").strip()
    rows = json.loads(raw) if raw.startswith("[") else [
        json.loads(line) for line in raw.splitlines() if line.strip()]
    out = []
    for i, row in enumerate(rows, 1):
        if not all(isinstance(row.get(k), str) and row.get(k) for k in ("sid", "text", "target")):
            raise ValueError(f"{path}: row {i} needs non-empty sid, text and target")
        target = row["target"].strip()
        rid = _BARE_RULE_ID.match(target)
        out.append(FileSituation(row["sid"], row["text"],
                                 rid.group("id") if rid else target))
    return out


def require_pointers(index: Path, what: str) -> None:
    """Refuse an index that names no record, rather than scoring 0.000 over it.

    The floor this repository already uses for this exact shape, and the pattern
    is deliberately copied rather than reinvented: `check-memory-preservation.py`
    carries a measured floor on every corpus it walks, because the validator it
    replaced passed over an emptied one. Same class as the refusals in `prompts`
    and `score` below for an empty situation corpus - a rate over nothing is not
    a rate of zero, it is no rate.

    The direction matters. Over-reporting a defect that was never observed is
    the failure here: 0.000 reads as "this index is terrible" when the truth is
    "this index was never the thing being measured".
    """
    if not pointers(index) and not rule_ids(index):
        raise ValueError(
            f"{index} carries no `[label](record.md)` pointer and no list item "
            f"opening with a rule id such as `[M1]`, so it names nothing for a "
            f"judge to recognise and {what} would be a rate over an empty set. "
            f"Refusing rather than returning 0.000, which reads as a bad index "
            f"instead of an absent subject.")


def _spine(text: str) -> str:
    """The index with every link collapsed to a single marker.

    What is left is the PROSE BETWEEN the pointers: the section headings, the
    subject prefixes, the trailing explanations, the dates, the statuses. That
    text is what `verify` could not see, and it is where the meaning of a
    pointer often lives -- `tide-tables-live-in-the-almanac.md` is annotated "the
    almanac is kept in the chart room", and `sextant-trial-until-2031-01-01.md` is
    annotated KEPT. Drop the annotation and the filename asserts the opposite of
    the fact.
    """
    return _LINK_RE.sub("\x00", text)


def verify(candidate: Path, baseline: Path) -> dict:
    """A variant may change LABELS. It may not change anything else.

    Without this, the loop compares two files that differ in two ways at once and
    attributes the whole difference to the one it meant to change. A variant that
    quietly dropped six pointers would score lower and be read as "the new layout
    is worse", when six records had simply stopped being in the index at all.

    Order is checked too, not just membership: the reader scans top to bottom and
    the file is truncated from the END, so moving a pointer is a real change.

    AND THE PROSE BETWEEN THE POINTERS IS CHECKED, which until 2026-09-10 it was
    not. This function compared two lists of `(label, target)` and nothing else,
    so everything a line says AROUND its links was invisible to it. MEASURED that
    day: a candidate built by dropping all 71 subject prefixes and all 77
    trailing explanations from the live index -- 3,407 UTF-16 units of the
    highest-signal prose in the file -- was reported `same_records: true` and
    passed. The loss was found by decomposing the budget by hand, which is not a
    check and does not run twice.

    `spine_preserved` is that check, and it is deliberately strict: a variant
    that means to reflow the prose fails it and must say so, rather than having
    the reflow scored as a layout result.

    `records_checked` IS THE FLOOR UNDER `same_records`, added 2026-09-20.
    `same_records` is `not lost and not added and len(want) == len(got)`, and
    over two indexes with no pointers at all that is `True` for the same reason
    an empty loop asserts nothing: both sides parse to zero and compare equal.
    MEASURED 2026-09-20, the operator's live index holds 0 pointers, so ANY
    candidate whatsoever passed the record-preservation half of this check
    vacuously. `spine_preserved` still had teeth over the same pair (with no
    links, `_spine` is the identity and the whole text is compared), which is
    why this was invisible: the exit code kept refusing real changes while one
    of the three claims behind it had quietly stopped meaning anything.
    `records_checked` is False when the BASELINE names no record, it is
    reported beside the others, and the exit code requires it - an unearned
    claim is not allowed to contribute a pass.
    """
    base_links = pointers(baseline)
    cand_links = pointers(candidate)
    want = [name for _, name in base_links]
    got = [name for _, name in cand_links]
    lost = sorted(set(want) - set(got))
    added = sorted(set(got) - set(want))
    base_spine = _spine(baseline.read_text(encoding="utf-8"))
    cand_spine = _spine(candidate.read_text(encoding="utf-8"))
    return {
        "candidate": str(candidate),
        "baseline": str(baseline),
        "baseline_pointers": len(want),
        "candidate_pointers": len(got),
        "lost": lost,
        "added": added,
        "order_preserved": want == got,
        "labels_changed": sum(
            1 for (a, _), (b, _) in zip(base_links, cand_links, strict=False)
            if a != b),
        "same_records": not lost and not added and len(want) == len(got),
        # Reported BESIDE the vacuous value rather than instead of it, so the
        # floor is visibly the thing that refuses a `same_records: true` nobody
        # earned. Quietly flipping `same_records` to False would look like the
        # candidate had lost a record.
        "records_checked": bool(want),
        "spine_preserved": base_spine == cand_spine,
        "spine_units_lost": (len(base_spine.encode("utf-16-le"))
                             - len(cand_spine.encode("utf-16-le"))) // 2,
    }


def prompts(index: Path, batch: int, situations_file: Path | None = None) -> list[dict]:
    require_pointers(index, "rendering judge prompts against it")
    text = index.read_text(encoding="utf-8")
    cases = load_situations(situations_file)
    if not cases:
        raise ValueError(
            "the situation corpus is empty, so there are no moments to render; "
            "refusing rather than writing zero prompt batches and exiting 0")
    out = []
    for start in range(0, len(cases), batch):
        chunk = cases[start:start + batch]
        moments = "\n".join(f"- sid={c.sid}: {c.text}" for c in chunk)
        out.append({
            "batch": len(out) + 1,
            "sids": [c.sid for c in chunk],
            "prompt": PROMPT.format(n=len(chunk), index=text, moments=moments),
        })
    return out


# `<family>/<leaf>/<YYYYMMDDHHMMSS>-<slug>/record.md`, the HEADING OS 2 form
# (`docs/heading-os-2/002-repository-layout.md`). The identity is the SLUG; the
# id is a machine key and the family lives in the path.
HOS2_ID = re.compile(r"^\d{14}-")


def record_identity(answer: str | None) -> str | None:
    """Reduce a judge's answer to the record it names, whatever scheme it used.

    Without this the HEADING OS 2 variant scores 0.000 and the number is false:
    the judge would be naming the right record, in the new scheme's own spelling,
    against a ground truth written in the old one. A rename is not a miss, and a
    comparison that cannot tell them apart measures the notation rather than the
    index.
    """
    if not answer:
        return None
    answer = answer.strip().strip("`")
    if not answer:
        return None
    rid = _BARE_RULE_ID.match(answer)
    if rid:
        return rid.group("id")
    parts = [p for p in answer.split("/") if p]
    if parts and parts[-1] == "record.md":
        if len(parts) < 2:
            return None
        return HOS2_ID.sub("", parts[-2]) + ".md"
    if not parts:
        return None
    # A rebuilt index drops the extension too: the family is the section header
    # and `record.md` is the same filename for every record, so neither is worth
    # a character. Restore it so one ground truth serves every scheme.
    tail = HOS2_ID.sub("", parts[-1])
    return tail if tail.endswith(".md") else tail + ".md"


def score(index: Path, verdicts: list[dict], situations_file: Path | None = None) -> dict:
    """Recognition, plus the two failure modes that a single rate would merge.

    A miss (`null` on a moment that has a record) and a MISFIRE (a confident wrong
    name) are not the same defect and must not average into one number: the first
    means the index did not carry the moment, the second means it carried it
    toward the wrong record, which is worse than silence.
    """
    require_pointers(index, "scoring recognition against it")
    cases = {c.sid: c for c in load_situations(situations_file)}
    if not cases:
        # A floor under the corpus, not dead code. `situations("all")` now
        # refuses when a pack could not be READ, which is what the rehearsal
        # tree hit; what still reaches here is every pack having been read and
        # found genuinely EMPTY, which `packs()` drops by design. Either way a
        # rate over no moments is not a score of zero, it is no score, and
        # until 2026-09-16 this line was `round(hit / n, 3)` with nothing above
        # it: n was 0 and the measurement ended in ZeroDivisionError.
        raise ValueError(
            "the situation corpus holds no moments, so there is nothing to "
            "score; a recognition rate over an empty corpus is not 0.0")
    by_sid = {v["sid"]: v for v in verdicts}
    missing = sorted(set(cases) - set(by_sid))
    if missing:
        raise ValueError(f"no verdict for {len(missing)} moment(s): {missing[:5]}")

    hit = silent = misfire = 0
    packs: Counter[str] = Counter()
    pack_hit: Counter[str] = Counter()
    rows = []
    for sid, case in cases.items():
        named = record_identity(by_sid[sid].get("record"))
        pack = sid.split("-")[0]
        packs[pack] += 1
        if named == case.target:
            hit += 1
            pack_hit[pack] += 1
            outcome = "hit"
        elif named is None:
            silent += 1
            outcome = "silent"
        else:
            misfire += 1
            outcome = "misfire"
        rows.append({"sid": sid, "situation": case.text, "want": case.target,
                     "got": named, "outcome": outcome,
                     "line": by_sid[sid].get("line")})

    n = len(cases)
    return {
        "index": str(index),
        "budget": budget(index),
        "asked": n,
        "recognition": round(hit / n, 3),
        "hit": hit,
        "silent": silent,
        "misfire": misfire,
        "by_pack": {p: {"asked": packs[p], "hit": pack_hit[p],
                        "recognition": round(pack_hit[p] / packs[p], 3)}
                    for p in sorted(packs)},
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("budget", "prompts", "score", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--index", required=True, type=Path)
        if name == "verify":
            p.add_argument("--against", required=True, type=Path,
                           help="the baseline index whose pointer set must be kept")
        if name == "prompts":
            p.add_argument("--batch", type=int, default=16)
        if name in ("prompts", "score"):
            p.add_argument("--situations", type=Path, default=None,
                           help="ground truth, JSON array or JSONL of {sid, text, "
                                "target}; default: the built-in packs")
        if name == "score":
            p.add_argument("--verdicts", required=True, type=Path,
                           help="JSON array of {sid, record, line} from the judges")
    args = parser.parse_args()

    if not args.index.exists():
        print(f"no such index: {args.index}", file=sys.stderr)
        return 2

    if args.command == "verify":
        if not args.against.exists():
            print(f"no such baseline: {args.against}", file=sys.stderr)
            return 2
        result = verify(args.index, args.against)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if (result["records_checked"] and result["same_records"]
                     and result["order_preserved"]
                     and result["spine_preserved"]) else 1
    if args.command == "budget":
        print(json.dumps(budget(args.index), ensure_ascii=False, indent=2))
    elif args.command == "prompts":
        for item in prompts(args.index, args.batch, args.situations):
            print(json.dumps({"batch": item["batch"], "sids": item["sids"],
                              "chars": len(item["prompt"])}, ensure_ascii=False))
            out = args.index.parent / f"prompt-batch-{item['batch']}.txt"
            out.write_text(item["prompt"], encoding="utf-8")
            print(f"  written: {out}", file=sys.stderr)
    elif args.command == "score":
        verdicts = json.loads(args.verdicts.read_text(encoding="utf-8"))
        result = score(args.index, verdicts, args.situations)
        out = args.index.parent / f"score-{args.index.stem}.json"
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        summary = {k: v for k, v in result.items() if k != "rows"}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"\nwritten: {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
