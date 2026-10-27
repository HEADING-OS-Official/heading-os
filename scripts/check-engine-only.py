#!/usr/bin/env python3
"""Refuse every tracked engine file that is not the engine.

Usage:
    python scripts/check-engine-only.py              # the gate over git's index (pre-commit)
    python scripts/check-engine-only.py --carried    # the index plus untracked-not-ignored (push)
    python scripts/check-engine-only.py --json       # every file with its verdict
    python scripts/check-engine-only.py --outside    # the out-of-allowlist paths, one per line

Exit codes: 0 no finding; 1 refused; 2 nothing to judge (an empty index or an
allowlist covering nothing), because a gate that read nothing established
nothing.

THE RULE (HOS-131). Operator decision 2026-09-25: the engine repository holds
only the engine, and anything else in it is a leak. The definition he accepted
the same day: the engine is code and what that code cannot run or install
without. So this is an ALLOWLIST, the reverse of every other wall here: those
look for a known kind of private content and pass whatever they do not
recognise, while this passes only what it recognises and refuses the rest,
invented demo content included.

WHAT IS READ. git's INDEX, through `git_index_paths`, so at pre-commit the
staged tree is judged, and at push the committed one. The allowlist is
`config/engine-allowlist.yaml`: classes of path patterns, plus per-file entries
where a pattern cannot decide, each with its reason. Untracked files are not
this gate's subject at the commit. `--carried`, the push ratchet's argv, adds
the untracked files `.gitignore` does not hide, because `scripts/push-all.py`
runs `git add -A` after its ratchets when it commits, and a file created
between the two would otherwise be carried without being judged.

EIGHT REFUSALS.
  not-engine      a tracked file no class and no entry covers, and not in the
                  baseline.
  stale-baseline  a baseline entry whose file is gone or is now covered.
  baseline-rise   a baseline entry absent from the baseline at HEAD or at
                  origin/main, whichever carries the file. The baseline was the
                  migration list frozen on the day the gate went in and has
                  drained to empty; a path added to it is a leak bought a pass,
                  so the rise guard stays while the seed and shrink writers are
                  retired (audit 2026-10-15).
  never           an allowlist class or entry covering a path in NEVER below.
                  Content classes are refused here, in code, so allowing one
                  takes a change a reviewer reads rather than one config line.
  stale-allow     a pattern or per-file entry matching no tracked file, or a
                  per-file entry already covered by a pattern. A rule that
                  covers nothing reads exactly like one that holds.
  document-content
                  a covered Python file that builds a DOCX or PPTX and either
                  joins one named document onto a data address or carries more
                  than PROSE_WORDS_MAX words of literal prose (HOS-159). The
                  class `engine-code` covers `scripts/**/*.py` whole, so a
                  document written out as its own generator passed as engine.
  departed        a tracked path in the allowlist's `departed` list: a file that
                  left for the data repository and may not come back.
  business-figure a covered Markdown file under a skill, rule, agent or
                  reference directory, or a covered Python file outside
                  `tests/`, that states a business figure of the class in
                  _BUSINESS_FIGURES: a money amount with a magnitude, a pricing
                  multiplier, a CAGR, an interview, hire, patent, headcount or
                  dealer count, an annual support rate (HOS-164, Python since
                  HOS-165). The class `engine-code` covers the skill tree and
                  `scripts/**/*.py` whole, so a company's prices, hiring record,
                  market sizing and headcount passed as engine.

NOT CLAIMED. It judges the PATH, and reads BYTES only for the two shapes above: a
`*.example.*` template can still carry invented values, and a skill, rule, agent,
reference or Python file can still carry a business fact the patterns do not
spell (a figure in words, a named customer, a round-free price with no magnitude
such as a negotiation example, a bare count or percentage with no unit beside
it), and nothing here reads those. The figure patterns read Markdown in the
directories above and Python outside `tests/`, and nothing else: MEASURED
2026-09-28 over every tracked file, they also match a negotiation eval's JSON,
and every test fixture that plants a figure for a gate to refuse. The Python
lines that state a FORMAT rather than a figure (a parser's accepted inputs, a
slide layout's sample value, a measured speed-up) are exempt one by one in
_FIGURE_FORMAT_EXAMPLES, by path AND matched text. The bytes are read from the
working tree; at pre-commit the framework stashes unstaged edits first, so the
tree it reads is the staged one; at push (`--carried`) it reads the working
tree as it stands, which can differ from the commits being pushed. A document
builder that takes its document from its caller passes whatever its caller
hands it, which is the point, and so does one that hard-codes a few hundred
words of a single document around that: MEASURED 2026-09-28, a proposal
converter taking `--name` whose cover was one tender's passed both jaws, and
only the departed list holds it.

WIRED at pre-commit (`engine-only` in `.pre-commit-config.yaml`) and as the
sixth push ratchet (`scripts/utils/push_ratchets.py`), both on the master
instruction of 2026-09-25, which asked for commit and push. That differs from
the five ratchets before it, which stay off pre-commit by the operator's
decisions of 2026-09-22 and 2026-09-25 because an address inside the private
overlay is neither failure A nor B; a non-engine file in the public engine is
failure A by his own definition, so this one runs at the commit.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils import engine_allowlist  # noqa: E402
from scripts.utils.engine_guard import repo_carried_paths  # noqa: E402
from scripts.utils.repo_files import IndexUnreadable, git_index_paths  # noqa: E402

#: The checkout this file sits in, so a yard judges the yard's tree.
ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_REL = engine_allowlist.ALLOWLIST_REL
BASELINE_REL = "config/engine-only-baseline.json"

#: Content classes no allowlist entry may cover, and the one matcher: both live
#: in `scripts/utils/engine_allowlist.py`, because the routing resolver asks
#: the same question (HOS-232) and a second copy is how the two would drift.
NEVER, NEVER_EXCEPT = engine_allowlist.NEVER, engine_allowlist.NEVER_EXCEPT
matches, is_never = engine_allowlist.matches, engine_allowlist.is_never

#: A document builder imports python-docx or python-pptx, lazily or not, or
#: the engine's shared binder for them.
_BUILDER_MODULES = frozenset({"docx", "pptx", "scripts.utils.docx_helpers", "utils.docx_helpers"})
#: A file name a document builder writes or reads as its one document.
_DOCUMENT_NAME = re.compile(r"\.(?:docx|dotx|pptx|xlsx|pdf|md)\Z", re.I)
#: The resolvers that turn a logical name into a data-repository address.
_DATA_RESOLVERS = frozenset({"data_path", "data_write_dir"})
_WORD = re.compile(r"[A-Za-z][A-Za-z']+")
#: MEASURED 2026-09-28 over every tracked builder: 789 words at most in a
#: legitimate one, 3090 at least in a leak that carried its text inline.
PROSE_WORDS_MAX = 2000

#: Where a business figure is refused: the prose the model reads as
#: instruction, and the code that runs (HOS-165). `tests/` is left out on
#: purpose: a gate's test plants invented figures to watch it refuse them.
_FIGURE_SCOPE: tuple[str, ...] = (
    ".claude/skills/**/*.md", ".claude/rules/**/*.md", ".claude/agents/**/*.md", "reference/**/*.md",
    "**/*.py",
)
_FIGURE_UNSCOPED: tuple[str, ...] = ("tests/**",)
#: Python lines that state a format, not a figure, each exempt by the exact
#: text the pattern matched in that one file, so the same file stating any
#: other figure is still refused. MEASURED 2026-09-28: these were every hit over
#: the engine's Python outside tests/ once HOS-165's figures left, 8 matches on 6
#: lines in 5 files, besides this file's own quotation of them below.
_FIGURE_FORMAT_EXAMPLES: dict[str, frozenset[str]] = {
    # A slide layout's sample values, in the comment that says how short a value may be.
    ".claude/skills/pptx-generator/cookbook/giant-focus-slide.py": frozenset({"$1B"}),
    ".claude/skills/pptx-generator/cookbook/stats-slide.py": frozenset({"$2.1M"}),
    # The inputs a pipeline value parser accepts, in its docstring.
    "scripts/generate-dashboard.py": frozenset({"$1.5M"}),
    "scripts/pipeline-summary.py": frozenset({"$500K", "$1.2M"}),
    # A measured serial-to-parallel speed-up in a timing comment, not a price.
    "scripts/turn-check.py": frozenset({"(1.37x)"}),
}
# This file quotes every exempt text above, and nothing more.
_FIGURE_FORMAT_EXAMPLES["scripts/check-engine-only.py"] = frozenset().union(*_FIGURE_FORMAT_EXAMPLES.values())
#: The business-figure class HOS-164 moved out of the skills. MEASURED
#: 2026-09-28: no Markdown file in _FIGURE_SCOPE matched once the figures had
#: left, and 21 lines in 10 skill files matched on the tree before.
_BUSINESS_FIGURES: tuple[tuple[str, re.Pattern], ...] = (
    ("a money amount with a magnitude", re.compile(
        r"(?:[$€£]|\b(?:USD|EUR|AED|GBP) ?)\d[\d,]*(?:\.\d+)? ?(?:[KMB]|bn|mn|million|billion)\b")),
    ("a pricing multiplier", re.compile(r"\(\d\.\d{1,2}x\)")),
    ("a growth rate", re.compile(r"\d+(?:\.\d+)?% ?CAGR\b")),
    ("an interview count", re.compile(r"\b\d[\d,]*\+? interviews\b")),
    ("a hire count", re.compile(r"~?\b\d+\+? hires\b")),
    ("a patent count", re.compile(r"\b\d+ patents? (?:filed|granted|pending|issued)\b|patent portfolio: ?\d", re.I)),
    ("a headcount", re.compile(r"\b\d+\+? (?:Tribe members|employees|staff|experienced professionals)\b")),
    ("an annual support rate", re.compile(r"\b\d+% annual(?:ly)?\b")),
    ("a dealer count", re.compile(r"\b\d+\+? dealers\b")),
)


def _resolver_call(node: ast.AST, bound: frozenset[str] = frozenset()) -> bool:
    """Whether `node` is a data address: a resolver call, a name assigned one,
    or either with path segments joined on."""
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        node = node.left
    if isinstance(node, ast.Name):
        return node.id in bound
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name in _DATA_RESOLVERS


def _is_builder(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        for name in names:
            if name in _BUILDER_MODULES or name.split(".")[0] in {"docx", "pptx"}:
                return True
    return False


def document_content(text: str) -> str | None:
    """Why this Python source is a document rather than engine, or None."""
    if "docx" not in text and "pptx" not in text:
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return f"names docx or pptx and does not parse ({exc.msg}), so its content cannot be judged"
    if not _is_builder(tree):
        return None
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)):
            docstrings.add(id(body[0].value))
    bound = frozenset(
        target.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
        and _resolver_call(node.value) for target in node.targets if isinstance(target, ast.Name))
    words = 0
    for node in ast.walk(tree):
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)
                and _DOCUMENT_NAME.search(node.right.value) and _resolver_call(node.left, bound)):
            return (f"line {node.lineno} builds the one document {node.right.value!r} "
                    f"at a data address; a document's generator lives with the document")
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            found = _WORD.findall(node.value)
            if len(found) >= 3:
                words += len(found)
    if words > PROSE_WORDS_MAX:
        return (f"builds a document and carries {words} words of literal prose "
                f"(over {PROSE_WORDS_MAX}); the text is the document")
    return None


def business_figure(text: str, formats: frozenset[str] = frozenset()) -> str | None:
    """Where this text states a business figure, or None. A match whose text is
    in `formats` is a declared format example, never a figure."""
    for lineno, line in enumerate(text.splitlines(), 1):
        for what, pattern in _BUSINESS_FIGURES:
            for found in pattern.finditer(line):
                if found.group(0) not in formats:
                    return (f"line {lineno} states {what} ({found.group(0)!r}); the figure "
                            f"lives in the data repository and the code reads it by logical name")
    return None


def load_allowlist(root: Path) -> tuple[list[dict], list[dict], list[dict]]:
    """The allowlist's classes, per-file entries and departed list, from one read."""
    data = yaml.safe_load((root / ALLOWLIST_REL).read_text(encoding="utf-8")) or {}
    return (list(data.get("classes") or []), list(data.get("files") or []),
            list(data.get("departed") or []))


def load_baseline(text: str | None) -> set[str] | None:
    if text is None:
        return None
    return set(json.loads(text).get("files") or [])


def _git_show(root: Path, rev: str, rel: str) -> str | None:
    proc = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
        ["git", "show", f"{rev}:{rel}"], cwd=str(root),
        capture_output=True, text=True, check=False,
    )
    return proc.stdout if proc.returncode == 0 else None


def judge(root: Path, paths: list[str], classes: list[dict], entries: list[dict],
          baseline: set[str], references: dict[str, set[str]],
          departed: list[dict] | None = None) -> dict:
    """Every verdict for one tree. Pure apart from its arguments and the bytes
    of the covered Python and in-scope Markdown files under `root`."""
    findings: list[tuple[str, str, str]] = []
    covered: dict[str, str] = {}
    pattern_hits: dict[tuple[str, str], int] = {}
    for cls in classes:
        name = cls.get("class", "?")
        if not str(cls.get("reason", "")).strip():
            findings.append(("stale-allow", f"class {name}", "carries no reason"))
        for pat in cls.get("patterns") or []:
            hits = [p for p in paths if matches(pat, p)]
            pattern_hits[(name, pat)] = len(hits)
            if not hits:
                findings.append(("stale-allow", pat, f"class {name} pattern matches no tracked file"))
            for p in hits:
                covered.setdefault(p, name)
    index = set(paths)
    for entry in entries:
        rel, name = entry.get("path", ""), entry.get("class", "?")
        if not str(entry.get("reason", "")).strip():
            findings.append(("stale-allow", rel, "per-file entry carries no reason"))
        if rel not in index:
            findings.append(("stale-allow", rel, "per-file entry names no tracked file"))
        elif rel in covered:
            findings.append(("stale-allow", rel, f"per-file entry already covered by class {covered[rel]}"))
        else:
            covered[rel] = name
    for rel, name in sorted(covered.items()):
        if is_never(rel):
            findings.append(("never", rel, f"class {name} covers a content path the checker's NEVER list refuses"))
    for rel in sorted(covered):
        if rel.endswith(".py") and not is_never(rel):
            try:
                text = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                findings.append(("document-content", rel, f"unreadable, so its content cannot be judged: {exc}"))
                continue
            why = document_content(text)
            if why is not None:
                findings.append(("document-content", rel, why))
    for rel in sorted(covered):
        if (not is_never(rel) and any(matches(p, rel) for p in _FIGURE_SCOPE)
                and not any(matches(p, rel) for p in _FIGURE_UNSCOPED)):
            try:
                text = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                findings.append(("business-figure", rel, f"unreadable, so its content cannot be judged: {exc}"))
                continue
            why = business_figure(text, _FIGURE_FORMAT_EXAMPLES.get(rel, frozenset()))
            if why is not None:
                findings.append(("business-figure", rel, why))
    for entry in departed or []:
        rel = entry.get("path", "")
        if not str(entry.get("reason", "")).strip():
            findings.append(("stale-allow", rel, "departed entry carries no reason"))
        if rel in index:
            findings.append(("departed", rel, f"left the engine and may not come back: {entry.get('reason')}"))
    outside = sorted(p for p in paths if p not in covered or is_never(p))
    for rel in outside:
        if rel not in baseline:
            findings.append(("not-engine", rel, "no class or entry covers it and it is not in the baseline"))
    for rel in sorted(baseline - set(outside)):
        why = "file is gone" if rel not in index else f"now covered by {covered.get(rel)}"
        findings.append(("stale-baseline", rel, f"{why}; remove it from {BASELINE_REL}"))
    for ref, ref_set in references.items():
        for rel in sorted(baseline - ref_set):
            findings.append(("baseline-rise", rel, f"not in the baseline at {ref}"))
    return {"paths": len(paths), "covered": covered, "outside": outside,
            "findings": findings, "pattern_hits": pattern_hits}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--carried", action="store_true",
                    help="judge the index plus untracked-not-ignored files, as a push would carry")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--outside", action="store_true")
    args = ap.parse_args()

    try:
        paths = repo_carried_paths(ROOT) if args.carried else git_index_paths(ROOT)
    except (IndexUnreadable, subprocess.CalledProcessError) as exc:
        print(f"engine-only: git's index could not be read, nothing judged: {exc}", file=sys.stderr)
        return 2
    if not paths:
        print("engine-only: the index is empty, nothing judged.", file=sys.stderr)
        return 2
    classes, entries, departed = load_allowlist(ROOT)
    baseline_path = ROOT / BASELINE_REL
    current_text = baseline_path.read_text(encoding="utf-8") if baseline_path.exists() else None
    if current_text is None:
        print(f"engine-only: {BASELINE_REL} is missing; with no baseline every "
              f"out-of-allowlist file refuses.", file=sys.stderr)
    baseline = load_baseline(current_text) or set()

    references: dict[str, set[str]] = {}
    for rev in ("HEAD", "origin/main"):
        ref = load_baseline(_git_show(ROOT, rev, BASELINE_REL))
        if ref is not None:
            references[rev] = ref

    verdict = judge(ROOT, paths, classes, entries, baseline, references, departed)
    if not verdict["covered"]:
        print("engine-only: the allowlist covers no tracked file, nothing judged.", file=sys.stderr)
        return 2

    if args.outside:
        print("\n".join(verdict["outside"]))
        return 0
    if args.json:
        print(json.dumps({"paths": verdict["paths"], "covered": len(verdict["covered"]),
                          "outside": verdict["outside"], "baseline": len(baseline),
                          "findings": [dict(zip(("kind", "path", "why"), f, strict=True)) for f in verdict["findings"]]},
                         indent=2))
        return 1 if verdict["findings"] else 0

    for kind, rel, why in verdict["findings"]:
        print(f"REFUSED {kind}: {rel}: {why}", file=sys.stderr)
    judged = "index plus untracked-not-ignored" if args.carried else "git index"
    summary = (f"engine-only: {verdict['paths']} files judged ({judged}), {len(verdict['covered'])} "
               f"covered by the allowlist, {len(verdict['outside'])} outside it, "
               f"baseline {len(baseline)}, {len(verdict['findings'])} findings")
    if verdict["findings"]:
        print(summary, file=sys.stderr)
        print("A non-engine file leaves for the data repository; it is never added "
              "to the baseline. An engine file gets a class or a per-file entry with "
              f"its reason in {ALLOWLIST_REL}.", file=sys.stderr)
        return 1
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
